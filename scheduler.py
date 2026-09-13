"""Step 7：持久化定时任务与后台调度器。

``TaskStore`` 保存一次性、固定间隔和 Cron 任务；``Scheduler`` 在后台计算
下一次触发时间、原子 claim 到期任务，并把带 scheduler 元数据的请求交给共享
AgentRuntime。任务结果先写执行历史，再按配置投递到 Web、QQ、飞书等渠道；
重启、时区、结束时间、最大次数和失败退避均由持久化状态恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from runtime import AgentRuntime
from session_store import SessionStore, utc_now
from state_database import StateDatabase
from gateway_security import redact_sensitive


def parse_datetime(value: str) -> datetime:
    """解析带时区时间；无时区值按本地配置解释后统一转 UTC。"""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无法解析时间：{value}") from exc
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区，例如 2026-07-06T09:00:00+08:00。")
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))

# These are persisted as part of the Task contract before the more advanced
# execution/delivery backends are enabled.  Keeping the vocabulary typed now
# lets Web, CLI and channel callers evolve without inventing incompatible
# task fields later.  ``main/session`` preserve today's behavior.
EXECUTION_CONTEXTS = frozenset({"main", "current", "isolated"})
DELIVERY_MODES = frozenset({"session", "channel", "webhook", "none"})
DELIVERY_CHANNELS = frozenset({"feishu", "qqbot", "weixin"})


def validate_task_modes(execution_context: str, delivery_mode: str) -> tuple[str, str]:
    context = str(execution_context or "main").strip().lower()
    delivery = str(delivery_mode or "session").strip().lower()
    if context not in EXECUTION_CONTEXTS:
        raise ValueError("executionContext 必须是 main、current 或 isolated。")
    if delivery not in DELIVERY_MODES:
        raise ValueError("deliveryMode 必须是 session、channel、webhook 或 none。")
    return context, delivery


def validate_delivery_channel(delivery_channel: str | None, delivery_mode: str) -> str | None:
    """Validate an optional explicit channel target for channel delivery.

    A missing target keeps the backwards-compatible "latest route" behavior.
    An explicit target is intentionally restricted to ``channel`` mode so a
    task cannot silently carry a routing hint that another delivery backend
    ignores.
    """
    channel = str(delivery_channel or "").strip().lower() or None
    if channel is not None and channel not in DELIVERY_CHANNELS:
        raise ValueError("deliveryChannel 必须是 feishu、qqbot 或 weixin。")
    if channel is not None and delivery_mode != "channel":
        raise ValueError("deliveryChannel 只能与 channel 投递模式一起使用。")
    return channel


def normalize_delivery_channels(
    delivery_channel: str | None,
    delivery_channels: list[str] | tuple[str, ...] | None,
    delivery_mode: str,
) -> tuple[str | None, list[str]]:
    """Normalize legacy single-channel and new broadcast task fields."""
    single = validate_delivery_channel(delivery_channel, delivery_mode)
    if delivery_channels is None:
        return single, ([single] if single else [])
    if not isinstance(delivery_channels, (list, tuple)):
        raise ValueError("deliveryChannels 必须是渠道名称数组。")
    channels: list[str] = []
    for item in delivery_channels:
        if not str(item or "").strip():
            raise ValueError("deliveryChannels 不能包含空渠道。")
        channel = validate_delivery_channel(str(item), delivery_mode)
        if channel and channel not in channels:
            channels.append(channel)
    if single and single not in channels:
        if channels:
            raise ValueError("deliveryChannel 与 deliveryChannels 不一致。")
        channels.append(single)
    if len(channels) > len(DELIVERY_CHANNELS):
        raise ValueError("deliveryChannels 包含重复或过多渠道。")
    return (channels[0] if len(channels) == 1 else None), channels


class WebhookDelivery:
    """Small dependency-free JSON webhook sender for Scheduler results."""

    def __init__(self, url: str, timeout: float = 10.0):
        value = str(url or "").strip()
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("Scheduler webhook URL 必须使用 http:// 或 https://")
        self.url = value
        self.timeout = max(1.0, float(timeout))

    def __call__(self, session_id: str, event) -> bool:
        import json as _json

        payload = _json.dumps(
            {
                "sessionId": session_id,
                "eventType": event.event_type,
                "text": event.text,
                "data": event.data,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return 200 <= int(response.status) < 300
        except (HTTPError, URLError, TimeoutError, OSError):
            return False


def _cron_values(token: str, lower: int, upper: int, field_name: str) -> set[int]:
    values: set[int] = set()
    for piece in token.split(","):
        piece = piece.strip()
        if not piece:
            raise ValueError(f"Cron {field_name} 不能为空")
        if "/" in piece:
            base, step_text = piece.split("/", 1)
            try:
                step = int(step_text)
            except ValueError as exc:
                raise ValueError(f"Cron {field_name} 步长无效") from exc
            if step < 1:
                raise ValueError(f"Cron {field_name} 步长必须大于等于 1")
        else:
            base, step = piece, 1
        if base in {"", "*"}:
            start, end = lower, upper
        elif "-" in base:
            left, right = base.split("-", 1)
            try:
                start, end = int(left), int(right)
            except ValueError as exc:
                raise ValueError(f"Cron {field_name} 范围无效") from exc
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise ValueError(f"Cron {field_name} 数值无效") from exc
        if field_name == "星期":
            if start == 7:
                start = 0
            if end == 7:
                end = 0
            if start > end and piece not in {"7"}:
                raise ValueError(f"Cron {field_name} 范围无效")
        if start < lower or start > upper or end < lower or end > upper or start > end:
            raise ValueError(f"Cron {field_name} 超出范围 {lower}-{upper}")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expression: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """解析受限的五字段 Cron 表达式。"""

    parts = str(expression or "").split()
    if len(parts) != 5:
        raise ValueError("Cron 表达式必须包含 5 段：分 时 日 月 星期")
    parsed = tuple(
        _cron_values(part, lower, upper, name)
        for part, (lower, upper), name in zip(
            parts, _CRON_RANGES, ("分钟", "小时", "日期", "月份", "星期")
        )
    )
    return parsed  # type: ignore[return-value]


def cron_next(expression: str, timezone_name: str, after: datetime) -> datetime:
    """计算指定时区中严格晚于 ``after`` 的下一次 Cron 时间。"""

    fields = parse_cron(expression)
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{timezone_name}") from exc
    local = after.astimezone(local_zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    dom, dow = fields[2], fields[4]
    dom_restricted = len(dom) != 31
    dow_restricted = len(dow) != 7
    # Minute-by-minute search is bounded to two years, enough for sparse
    # expressions such as "0 0 29 2 *" while keeping malformed rules safe.
    for _ in range(2 * 366 * 24 * 60):
        if local.minute not in fields[0] or local.hour not in fields[1] or local.month not in fields[3]:
            local += timedelta(minutes=1)
            continue
        day_dom = local.day in dom
        day_dow = local.weekday() in {(item - 1) % 7 for item in dow}
        day_ok = (day_dom or day_dow) if dom_restricted and dow_restricted else day_dom and day_dow
        if day_ok:
            return local.astimezone(timezone.utc)
        local += timedelta(minutes=1)
    raise ValueError("Cron 表达式在两年内没有可执行时间")


@dataclass
class ScheduledTask:
    """一个可持久化任务及其调度、执行和投递状态。"""

    task_id: str
    content: str
    task_type: str
    session_id: str
    next_run_at: str | None
    status: str = "pending"
    interval_seconds: int | None = None
    cron_expression: str | None = None
    timezone_name: str = "Asia/Shanghai"
    execution_context: str = "main"
    delivery_mode: str = "session"
    delivery_channel: str | None = None
    delivery_channels: list[str] = field(default_factory=list)
    # Owner Session remains the task's audit/delivery target.  An isolated
    # task additionally gets a dedicated execution Session, created lazily at
    # first run and reused by later periodic runs.
    execution_session_id: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    max_runs: int | None = None
    run_count: int = 0
    enabled: bool = True
    pending_approval_ids: list[str] = field(default_factory=list)
    waiting_started_at: str | None = None
    active_run_mode: str | None = None
    history: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict:
        return {
            "taskId": self.task_id,
            "content": self.content,
            "taskType": self.task_type,
            "sessionId": self.session_id,
            "nextRunAt": self.next_run_at,
            "status": self.status,
            "intervalSeconds": self.interval_seconds,
            "cronExpression": self.cron_expression,
            "timezone": self.timezone_name,
            "executionContext": self.execution_context,
            "deliveryMode": self.delivery_mode,
            "deliveryChannel": self.delivery_channel,
            "deliveryChannels": self.delivery_channels or None,
            "executionSessionId": self.execution_session_id,
            "startsAt": self.starts_at,
            "endsAt": self.ends_at,
            "maxRuns": self.max_runs,
            "runCount": self.run_count,
            "enabled": self.enabled,
            "pendingApprovalIds": self.pending_approval_ids,
            "waitingStartedAt": self.waiting_started_at,
            "activeRunMode": self.active_run_mode,
            "history": self.history,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict, source: Path) -> "ScheduledTask":
        required = {
            "taskId", "content", "taskType", "sessionId", "nextRunAt",
            "status", "history", "createdAt", "updatedAt",
        }
        missing = required - data.keys()
        if missing:
            raise ValueError(f"Task 数据缺少字段 {sorted(missing)}：{source}")
        execution_context, delivery_mode = validate_task_modes(
            data.get("executionContext", "main"), data.get("deliveryMode", "session")
        )
        delivery_channel, delivery_channels = normalize_delivery_channels(
            data.get("deliveryChannel"), data.get("deliveryChannels"), delivery_mode
        )
        return cls(
            task_id=str(data["taskId"]),
            content=str(data["content"]),
            task_type=str(data["taskType"]),
            session_id=str(data["sessionId"]),
            next_run_at=data["nextRunAt"],
            status=str(data["status"]),
            interval_seconds=data.get("intervalSeconds"),
            cron_expression=data.get("cronExpression"),
            timezone_name=str(data.get("timezone") or "Asia/Shanghai"),
            execution_context=execution_context,
            delivery_mode=delivery_mode,
            delivery_channel=delivery_channel,
            delivery_channels=delivery_channels,
            execution_session_id=data.get("executionSessionId"),
            starts_at=data.get("startsAt"),
            ends_at=data.get("endsAt"),
            max_runs=data.get("maxRuns"),
            run_count=int(data.get("runCount", len(data.get("history") or []))),
            enabled=bool(data.get("enabled", data.get("status") not in {"cancelled", "completed", "expired", "paused"})),
            pending_approval_ids=[str(item) for item in (data.get("pendingApprovalIds") or [])],
            waiting_started_at=data.get("waitingStartedAt"),
            active_run_mode=data.get("activeRunMode"),
            history=data["history"],
            created_at=str(data["createdAt"]),
            updated_at=str(data["updatedAt"]),
        )


class TaskStore:
    """线程安全的任务仓库，负责 claim、历史与状态迁移。"""

    def __init__(self, data_dir: str | Path, session_store: SessionStore):
        self.path = Path(data_dir) / "tasks.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_store = session_store
        self._lock = RLock()
        self.database = StateDatabase(self.path.parent)
        self.database.ensure_imported("tasks", self.path, [])
        self.recover_running_tasks()

    def _read_unlocked(self) -> list[dict]:
        data = self.database.read("tasks", [])
        if not isinstance(data, list):
            raise ValueError(f"Task 文件顶层必须是列表：{self.path}")
        return data

    def _write_unlocked(self, data: list[dict]) -> None:
        try:
            self.database.write("tasks", data)
        except OSError as exc:
            raise OSError(f"保存 Task 数据失败：{self.database.path}（{exc}）") from exc

    def list(self, session_id: str | None = None) -> list[ScheduledTask]:
        with self._lock:
            tasks = [ScheduledTask.from_dict(item, self.path) for item in self._read_unlocked()]
        if session_id is not None:
            tasks = [item for item in tasks if item.session_id == session_id]
        return sorted(tasks, key=lambda item: item.created_at, reverse=True)

    def get(self, task_id: str) -> ScheduledTask:
        task = next((item for item in self.list() if item.task_id == task_id), None)
        if task is None:
            raise KeyError(f"Task 不存在：{task_id}")
        return task

    def find_waiting_by_approval(self, approval_id: str) -> ScheduledTask | None:
        """Return the task paused for an approval, if this is a Scheduler approval."""
        for task in self.list():
            if approval_id in task.pending_approval_ids and task.status == "waiting_approval":
                return task
        return None

    def _replace(self, task: ScheduledTask) -> None:
        with self._lock:
            tasks = self._read_unlocked()
            for index, item in enumerate(tasks):
                if item.get("taskId") == task.task_id:
                    tasks[index] = task.to_dict()
                    self._write_unlocked(tasks)
                    return
            raise KeyError(f"Task 不存在：{task.task_id}")

    def create_once(
        self, content: str, session_id: str, run_at: str, *,
        execution_context: str = "main", delivery_mode: str = "session",
        delivery_channel: str | None = None,
        delivery_channels: list[str] | None = None,
    ) -> ScheduledTask:
        self.session_store.get(session_id)
        text = content.strip()
        if not text:
            raise ValueError("任务内容不能为空。")
        run_time = parse_datetime(run_at)
        if run_time <= datetime.now(timezone.utc):
            raise ValueError("一次性任务的触发时间必须在未来。")
        execution_context, delivery_mode = validate_task_modes(execution_context, delivery_mode)
        delivery_channel, delivery_channels = normalize_delivery_channels(
            delivery_channel, delivery_channels, delivery_mode
        )
        task = ScheduledTask(
            f"task_{uuid.uuid4().hex[:10]}",
            text,
            "once",
            session_id,
            iso_utc(run_time),
            starts_at=iso_utc(run_time),
            execution_context=execution_context,
            delivery_mode=delivery_mode,
            delivery_channel=delivery_channel,
            delivery_channels=delivery_channels,
        )
        with self._lock:
            tasks = self._read_unlocked()
            tasks.append(task.to_dict())
            self._write_unlocked(tasks)
        return task

    def create_interval(
        self,
        content: str,
        session_id: str,
        interval_seconds: int,
        start_at: str | None = None,
        end_at: str | None = None,
        max_runs: int | None = None,
        *,
        execution_context: str = "main",
        delivery_mode: str = "session",
        delivery_channel: str | None = None,
        delivery_channels: list[str] | None = None,
    ) -> ScheduledTask:
        self.session_store.get(session_id)
        text = content.strip()
        if not text:
            raise ValueError("任务内容不能为空。")
        if not isinstance(interval_seconds, int) or isinstance(interval_seconds, bool) or interval_seconds < 1:
            raise ValueError("intervalSeconds 必须是大于等于 1 的整数。")
        if max_runs is not None and (
            not isinstance(max_runs, int) or isinstance(max_runs, bool) or max_runs < 1
        ):
            raise ValueError("maxRuns 必须是大于等于 1 的整数。")
        now = datetime.now(timezone.utc)
        first_run = parse_datetime(start_at) if start_at else now + timedelta(seconds=interval_seconds)
        if first_run <= now:
            raise ValueError("周期任务的首次触发时间必须在未来。")
        end_time = parse_datetime(end_at) if end_at else None
        if end_time is not None and end_time <= first_run:
            raise ValueError("周期任务的结束时间必须晚于首次触发时间。")
        execution_context, delivery_mode = validate_task_modes(execution_context, delivery_mode)
        delivery_channel, delivery_channels = normalize_delivery_channels(
            delivery_channel, delivery_channels, delivery_mode
        )
        task = ScheduledTask(
            f"task_{uuid.uuid4().hex[:10]}",
            text,
            "interval",
            session_id,
            iso_utc(first_run),
            interval_seconds=interval_seconds,
            starts_at=iso_utc(first_run),
            ends_at=iso_utc(end_time) if end_time else None,
            max_runs=max_runs,
            execution_context=execution_context,
            delivery_mode=delivery_mode,
            delivery_channel=delivery_channel,
            delivery_channels=delivery_channels,
        )
        with self._lock:
            tasks = self._read_unlocked()
            tasks.append(task.to_dict())
            self._write_unlocked(tasks)
        return task

    def create_cron(
        self,
        content: str,
        session_id: str,
        expression: str,
        start_at: str | None = None,
        end_at: str | None = None,
        max_runs: int | None = None,
        timezone_name: str = "Asia/Shanghai",
        *,
        execution_context: str = "main",
        delivery_mode: str = "session",
        delivery_channel: str | None = None,
        delivery_channels: list[str] | None = None,
    ) -> ScheduledTask:
        self.session_store.get(session_id)
        text = content.strip()
        if not text:
            raise ValueError("任务内容不能为空")
        parse_cron(expression)
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知时区：{timezone_name}") from exc
        if max_runs is not None and (
            not isinstance(max_runs, int) or isinstance(max_runs, bool) or max_runs < 1
        ):
            raise ValueError("maxRuns 必须是大于等于 1 的整数")
        now = datetime.now(timezone.utc)
        start_time = parse_datetime(start_at) if start_at else now
        first_run = cron_next(expression, timezone_name, start_time - timedelta(minutes=1))
        if first_run <= start_time:
            first_run = cron_next(expression, timezone_name, start_time)
        end_time = parse_datetime(end_at) if end_at else None
        if end_time is not None and end_time <= first_run:
            raise ValueError("Cron 任务的结束时间必须晚于首次触发时间")
        execution_context, delivery_mode = validate_task_modes(execution_context, delivery_mode)
        delivery_channel, delivery_channels = normalize_delivery_channels(
            delivery_channel, delivery_channels, delivery_mode
        )
        task = ScheduledTask(
            f"task_{uuid.uuid4().hex[:10]}",
            text,
            "cron",
            session_id,
            iso_utc(first_run),
            starts_at=iso_utc(start_time),
            ends_at=iso_utc(end_time) if end_time else None,
            max_runs=max_runs,
            cron_expression=" ".join(str(expression).split()),
            timezone_name=timezone_name,
            execution_context=execution_context,
            delivery_mode=delivery_mode,
            delivery_channel=delivery_channel,
            delivery_channels=delivery_channels,
        )
        with self._lock:
            tasks = self._read_unlocked()
            tasks.append(task.to_dict())
            self._write_unlocked(tasks)
        return task

    def cancel(self, task_id: str) -> ScheduledTask:
        task = self.get(task_id)
        if task.status in {"completed", "cancelled", "expired"}:
            raise ValueError(f"Task 已处于终态：{task.status}")
        task.status = "cancelled"
        task.enabled = False
        task.next_run_at = None
        task.updated_at = utc_now()
        self._replace(task)
        return task

    def mark_waiting_approval(
        self, task_id: str, approval_ids: list[str], started_at: str | None = None
    ) -> ScheduledTask:
        task = self.get(task_id)
        if task.status in {"cancelled", "completed", "expired"}:
            return task
        task.status = "waiting_approval"
        task.enabled = False
        task.pending_approval_ids = [str(item) for item in approval_ids]
        task.waiting_started_at = started_at or task.waiting_started_at or utc_now()
        task.updated_at = utc_now()
        self._replace(task)
        return task

    def resume_after_approval(self, task_id: str) -> ScheduledTask:
        task = self.get(task_id)
        if task.status != "waiting_approval":
            return task
        task.status = "running"
        task.enabled = True
        task.pending_approval_ids = []
        task.updated_at = utc_now()
        self._replace(task)
        return task

    def pause(self, task_id: str) -> ScheduledTask:
        task = self.get(task_id)
        if task.status == "waiting_approval":
            raise ValueError("等待审批的 Task 不能暂停，请先处理审批")
        if task.status in {"completed", "cancelled", "expired"}:
            raise ValueError(f"Task 已处于终态：{task.status}")
        task.status = "paused"
        task.enabled = False
        task.updated_at = utc_now()
        self._replace(task)
        return task

    def resume(self, task_id: str) -> ScheduledTask:
        task = self.get(task_id)
        if task.status != "paused":
            raise ValueError("只有已暂停的 Task 才能恢复。")
        now = datetime.now(timezone.utc)
        if task.ends_at and parse_datetime(task.ends_at) <= now:
            task.status = "expired"
            task.enabled = False
            task.next_run_at = None
        elif task.max_runs is not None and task.run_count >= task.max_runs:
            task.status = "completed"
            task.enabled = False
            task.next_run_at = None
        else:
            task.enabled = True
            task.status = "pending"
            if task.task_type == "interval":
                next_time = parse_datetime(task.next_run_at) if task.next_run_at else now
                if next_time <= now:
                    next_time = now + timedelta(seconds=task.interval_seconds or 1)
                task.next_run_at = iso_utc(next_time)
            elif task.task_type == "cron":
                task.next_run_at = iso_utc(
                    cron_next(task.cron_expression or "* * * * *", task.timezone_name, now)
                )
        task.updated_at = utc_now()
        self._replace(task)
        return task

    def due(self, now: datetime | None = None) -> list[ScheduledTask]:
        current = now or datetime.now(timezone.utc)
        return [
            task for task in self.list()
            if task.status in {"pending", "failed"}
            and task.enabled
            and task.next_run_at is not None
            and parse_datetime(task.next_run_at) <= current
        ]

    def mark_running(self, task_id: str) -> ScheduledTask:
        with self._lock:
            task = self.get(task_id)
            # ``run_due_once`` may be called concurrently by a poll tick and
            # a recovery/manual trigger.  A task that another worker has
            # already claimed must not be claimed a second time before the
            # first Agent turn advances its cursor and history.
            if task.status not in {"pending", "failed"} or not task.enabled:
                return task
            task.status = "running"
            task.active_run_mode = "scheduled"
            task.updated_at = utc_now()
            self._replace(task)
            return task

    def mark_manual_running(self, task_id: str) -> ScheduledTask:
        """Reserve a task for an explicit, out-of-schedule run.

        Only pending/failed tasks are eligible.  The separate run mode lets
        ``finish_run`` restore an interval task's original next-run cursor
        instead of accidentally advancing the recurring schedule.
        """
        with self._lock:
            task = self.get(task_id)
            if task.status not in {"pending", "failed"} or not task.enabled:
                raise ValueError("只有待执行或上次失败的任务可以立即执行")
            task.status = "running"
            task.active_run_mode = "manual"
            task.updated_at = utc_now()
            self._replace(task)
            return task

    def finish_run(self, task_id: str, history_entry: dict, success: bool) -> ScheduledTask:
        task = self.get(task_id)
        manual = task.active_run_mode == "manual"
        task.history.append(history_entry)
        history_entry.setdefault("manual", manual)
        if not manual or task.task_type == "once":
            task.run_count += 1
        task.pending_approval_ids = []
        task.waiting_started_at = None
        task.active_run_mode = None
        if manual and task.task_type != "once":
            # A manual test run must not consume a scheduled occurrence or
            # move the recurring task's next_run_at.  It remains runnable if
            # the explicit run succeeded, or retryable if it failed.
            task.status = "pending" if success else "failed"
            task.enabled = True
            task.updated_at = utc_now()
            self._replace(task)
            return task
        if task.status == "cancelled":
            task.next_run_at = None
        elif task.status == "paused" or not task.enabled:
            task.status = "paused"
            task.enabled = False
        elif task.task_type == "once":
            task.status = "completed" if success else "failed"
            # A failed one-shot remains manually retryable; it has no due
            # cursor, so this does not create an automatic retry loop.
            task.enabled = not success
            task.next_run_at = None
        elif task.task_type == "cron":
            now = datetime.now(timezone.utc)
            if task.max_runs is not None and task.run_count >= task.max_runs:
                task.status = "completed"
                task.enabled = False
                task.next_run_at = None
            else:
                next_time = cron_next(
                    task.cron_expression or "* * * * *",
                    task.timezone_name,
                    parse_datetime(task.next_run_at or now.isoformat()),
                )
                if task.ends_at and next_time > parse_datetime(task.ends_at):
                    task.status = "expired"
                    task.enabled = False
                    task.next_run_at = None
                else:
                    task.status = "pending" if success else "failed"
                    task.next_run_at = iso_utc(next_time)
        else:
            now = datetime.now(timezone.utc)
            if task.max_runs is not None and task.run_count >= task.max_runs:
                task.status = "completed"
                task.enabled = False
                task.next_run_at = None
                task.updated_at = utc_now()
                self._replace(task)
                return task
            next_time = parse_datetime(task.next_run_at) + timedelta(seconds=task.interval_seconds)
            while next_time <= now:
                next_time += timedelta(seconds=task.interval_seconds)
            if task.ends_at and next_time > parse_datetime(task.ends_at):
                task.status = "expired"
                task.enabled = False
                task.next_run_at = None
            else:
                task.status = "pending" if success else "failed"
                task.next_run_at = iso_utc(next_time)
        task.updated_at = utc_now()
        self._replace(task)
        return task

    def recover_running_tasks(self) -> None:
        with self._lock:
            raw_tasks = self._read_unlocked()
            changed = False
            for item in raw_tasks:
                if item.get("status") == "running":
                    item["status"] = "pending"
                    item["enabled"] = True
                    item["activeRunMode"] = None
                    item["updatedAt"] = utc_now()
                    changed = True
            if changed:
                self._write_unlocked(raw_tasks)


class Scheduler:
    """轮询到期任务并复用 AgentRuntime 执行的后台服务。"""

    def __init__(
        self,
        task_store: TaskStore,
        runtime: AgentRuntime,
        session_lock: Callable[[str], Lock] | None = None,
        poll_seconds: float = 1.0,
        notifier=None,
        webhook_sender: Callable[[str, object], bool] | None = None,
    ):
        self.task_store = task_store
        self.runtime = runtime
        self.session_lock = session_lock or (lambda _: Lock())
        self.poll_seconds = poll_seconds
        self.notifier = notifier
        self.webhook_sender = webhook_sender
        self._stop = Event()
        self._thread: Thread | None = None
        # Runtime invokes this hook after an approval decision so a waiting
        # scheduled task can be finalized without re-running its prompt.
        if hasattr(self.runtime, "approval_result_callback"):
            self.runtime.approval_result_callback = self.on_approval_result

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._loop, name="sjtuclaw-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self.poll_seconds * 2))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_due_once()
            except Exception:
                # A damaged task must not terminate the long-running Scheduler.
                pass
            self._stop.wait(self.poll_seconds)

    def run_due_once(self, now: datetime | None = None) -> int:
        tasks = self.task_store.due(now)
        for task in tasks:
            self._execute(task)
        return len(tasks)

    def run_task_now(self, task_id: str) -> ScheduledTask:
        """Start one explicit run without waiting for ``next_run_at``.

        The model work happens on a daemon thread so the Gateway request (and
        an Agent tool call) returns immediately.  Task state/history are the
        source of truth and the normal approval callback continues the same
        run when approval is granted.
        """
        running = self.task_store.mark_manual_running(task_id)
        worker = Thread(
            target=self._execute,
            args=(running,),
            kwargs={"run_mode": "manual", "already_marked": True},
            name=f"sjtuclaw-scheduler-manual-{task_id}",
            daemon=True,
        )
        worker.start()
        return running

    def _resolve_execution_session(self, task: ScheduledTask) -> str:
        """Resolve the session used by the Agent turn.

        ``main`` keeps today's owner-session behavior.  ``current`` follows
        the active UI session at trigger time without changing it.  ``isolated``
        lazily creates one dedicated Session and reuses it for subsequent runs,
        while the task's owner Session remains the audit/delivery target.
        """
        if task.execution_context == "current":
            return self.runtime.store.current_id
        if task.execution_context != "isolated":
            return task.session_id
        if task.execution_session_id:
            try:
                self.runtime.store.get(task.execution_session_id)
                return task.execution_session_id
            except (KeyError, ValueError):
                # A manually removed execution Session is recreated safely.
                pass
        isolated = self.runtime.store.create(
            f"定时任务 · {task.task_id}", make_current=False
        )
        task.execution_session_id = isolated.session_id
        task.updated_at = utc_now()
        self.task_store._replace(task)
        return isolated.session_id

    def _execute(
        self,
        task: ScheduledTask,
        run_mode: str = "scheduled",
        already_marked: bool = False,
    ) -> None:
        running = task if already_marked else self.task_store.mark_running(task.task_id)
        if running.status != "running":
            return
        started_at = utc_now()
        execution_session_id = task.session_id
        try:
            if task.delivery_mode == "webhook" and self.webhook_sender is None:
                raise RuntimeError(
                    "当前 Scheduler 尚未配置 webhook 投递后端；"
                    "请改用 session、channel 或 none。"
                )
            execution_session_id = self._resolve_execution_session(task)
            with self.session_lock(execution_session_id):
                session = self.runtime.store.get(execution_session_id)
                session.activity.append({
                    "eventId": f"activity_{uuid.uuid4().hex[:12]}",
                    "timestamp": started_at,
                    "type": "scheduler_triggered",
                    "turnId": None,
                    "data": {
                        "taskId": task.task_id,
                        "content": task.content[:500],
                        "executionContext": task.execution_context,
                        "executionSessionId": execution_session_id,
                    },
                })
                self.runtime.store.save(session)
                kwargs = {"make_current": False}
                run_parameters = inspect.signature(self.runtime.run).parameters
                if "source" in run_parameters:
                    kwargs["source"] = "scheduler"
                if (
                    "scheduler_context" in run_parameters
                    or any(
                        item.kind is inspect.Parameter.VAR_KEYWORD
                        for item in run_parameters.values()
                    )
                ):
                    # Keep Scheduler metadata out of the persisted user text.
                    # ContextBuilder turns this into a transient system section
                    # for every internal Agent Loop iteration.
                    kwargs["scheduler_context"] = {
                        "taskId": task.task_id,
                        "taskType": task.task_type,
                        "runMode": run_mode,
                        "scheduledFor": task.next_run_at,
                        "triggeredAt": started_at,
                        "runCount": task.run_count + 1,
                    }
                result = self.runtime.run(task.content, execution_session_id, **kwargs)
            if result.pending_approvals:
                approval_ids = [
                    item["approvalId"] for item in result.pending_approvals
                    if item.get("approvalId")
                ]
                self.task_store.mark_waiting_approval(
                    task.task_id, approval_ids, started_at=started_at
                )
                self._notify(
                    task.session_id,
                    "approval_required",
                    "定时任务等待审批：" + ", ".join(approval_ids),
                    {
                        "taskId": task.task_id,
                        "approvals": result.pending_approvals,
                        "deliveryId": f"scheduler:{task.task_id}:approval:{','.join(sorted(approval_ids))}",
                    },
                )
                # Approval resolution resumes the same Agent Loop through the
                # Runtime callback; do not count this as a failed run.
                return
            entry = {
                "startedAt": started_at,
                "finishedAt": utc_now(),
                "success": True,
                "assistantReply": result.reply,
                "error": None,
            }
            run_index = len(task.history) + 1
            self._notify(
                task.session_id, "final", result.reply or "定时任务已完成。",
                {
                    "taskId": task.task_id,
                    "success": True,
                    "deliveryId": f"scheduler:{task.task_id}:run:{run_index}",
                },
            )
            self.task_store.finish_run(task.task_id, entry, True)
        except Exception as exc:
            try:
                with self.session_lock(execution_session_id):
                    session = self.runtime.store.get(execution_session_id)
                    session.messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"[scheduler_task_failed taskId={task.task_id}]\n"
                                f"任务：{task.content}\n错误：{exc}"
                            ),
                            "metadata": {"source": "scheduler"},
                        }
                    )
                    session.updated_at = utc_now()
                    self.runtime.store.save(session)
            except Exception:
                pass
            entry = {
                "startedAt": started_at,
                "finishedAt": utc_now(),
                "success": False,
                "assistantReply": None,
                "error": str(exc),
            }
            run_index = len(task.history) + 1
            self._notify(
                task.session_id, "error", f"定时任务执行失败：{redact_sensitive(exc)}",
                {
                    "taskId": task.task_id,
                    "success": False,
                    "deliveryId": f"scheduler:{task.task_id}:run:{run_index}",
                },
            )
            self.task_store.finish_run(task.task_id, entry, False)

    def on_approval_result(self, approval_id: str, result) -> None:
        """Join Runtime approval continuation back to the Scheduler task."""
        task = self.task_store.find_waiting_by_approval(approval_id)
        if task is None:
            return
        approval_store = getattr(self.runtime, "approval_store", None)
        if approval_store is not None:
            unresolved = []
            for item in task.pending_approval_ids:
                try:
                    if approval_store.get(item).status == "pending":
                        unresolved.append(item)
                except KeyError:
                    continue
            if unresolved:
                return
        new_approvals = list(getattr(result, "pending_approvals", None) or [])
        new_ids = [item["approvalId"] for item in new_approvals if item.get("approvalId")]
        if new_ids:
            self.task_store.mark_waiting_approval(task.task_id, new_ids)
            self._notify(
                task.session_id,
                "approval_required",
                "定时任务继续执行前需要审批：" + ", ".join(new_ids),
                {
                    "taskId": task.task_id,
                    "approvals": new_approvals,
                    "deliveryId": f"scheduler:{task.task_id}:approval:{','.join(sorted(new_ids))}",
                },
            )
            return
        try:
            running = self.task_store.resume_after_approval(task.task_id)
            entry = {
                "startedAt": running.waiting_started_at or utc_now(),
                "finishedAt": utc_now(),
                "success": True,
                "assistantReply": getattr(result, "reply", None),
                "error": None,
                "approvalId": approval_id,
            }
            run_index = len(task.history) + 1
            self._notify(
                task.session_id,
                "final",
                getattr(result, "reply", None) or "定时任务已完成。",
                {
                    "taskId": task.task_id,
                    "success": True,
                    "deliveryId": f"scheduler:{task.task_id}:run:{run_index}",
                },
            )
            self.task_store.finish_run(task.task_id, entry, True)
        except Exception as exc:
            self._notify(
                task.session_id,
                "error",
                f"定时任务审批后收尾失败：{exc}",
                {"taskId": task.task_id, "success": False},
            )

    def _notify(self, session_id: str, event_type: str, text: str, data: dict) -> bool:
        task_id = data.get("taskId") if isinstance(data, dict) else None
        if task_id:
            try:
                task = self.task_store.get(str(task_id))
                if task.delivery_mode == "none":
                    return False
                data = dict(data)
                data.setdefault("deliveryMode", task.delivery_mode)
                if task.delivery_channel:
                    data.setdefault("deliveryChannel", task.delivery_channel)
                if len(task.delivery_channels) > 1:
                    data.setdefault("deliveryChannels", list(task.delivery_channels))
                data.setdefault(
                    "notification",
                    {
                        "kind": "scheduler",
                        "status": {
                            "approval_required": "approval_required",
                            "error": "failed",
                            "final": "completed",
                        }.get(event_type, "completed"),
                        "taskId": task.task_id,
                    },
                )
            except KeyError:
                pass
        from channels.base import OutboundEvent
        event = OutboundEvent(event_type, text, data)
        if task_id:
            try:
                task = self.task_store.get(str(task_id))
                if task.delivery_mode == "webhook":
                    if self.webhook_sender is None:
                        return False
                    return bool(self.webhook_sender(session_id, event))
            except KeyError:
                pass
        if self.notifier is None:
            return False
        try:
            return bool(self.notifier.notify(session_id, event))
        except Exception:
            # Delivery failure must not corrupt Scheduler state/history.
            return False
