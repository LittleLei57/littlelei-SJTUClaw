"""Step 7：供 Agent 自然语言创建和管理定时任务的 Tools。

这些 handler 与 Gateway Scheduler 共用同一个 ``TaskStore``，因此 Agent 创建的
任务会立即出现在网页面板并由现有后台线程执行。查询是只读操作；创建、暂停、
恢复、取消和立即运行都进入 Approval。
"""

from __future__ import annotations

from typing import Callable

from scheduler import TaskStore
from tools import Tool, ToolExecutionContext, ToolRegistry


class SchedulerTools:
    """把 TaskStore 操作适配为带 Session 上下文的 Tool handler。"""

    def __init__(self, task_store: TaskStore, runner: Callable[[str], object] | None = None):
        self.task_store = task_store
        # Gateway 注入正在运行的 Scheduler；没有 Gateway 时保持向后兼容。
        self.runner = runner

    def create(
        self,
        context: ToolExecutionContext,
        content: str,
        task_type: str = "once",
        run_at: str | None = None,
        interval_seconds: int | None = None,
        cron_expression: str | None = None,
        timezone: str = "Asia/Shanghai",
        start_at: str | None = None,
        end_at: str | None = None,
        max_runs: int | None = None,
        execution_context: str = "main",
        delivery_mode: str = "session",
        delivery_channel: str | None = None,
        delivery_channels: list[str] | None = None,
    ) -> dict:
        if task_type == "once":
            if not run_at:
                raise ValueError("once 任务需要提供带时区的 run_at")
            task = self.task_store.create_once(
                content, context.session_id, run_at,
                execution_context=execution_context, delivery_mode=delivery_mode,
                delivery_channel=delivery_channel,
                delivery_channels=delivery_channels,
            )
        elif task_type == "interval":
            if interval_seconds is None:
                raise ValueError("interval 任务需要提供 interval_seconds")
            task = self.task_store.create_interval(
                content,
                context.session_id,
                interval_seconds,
                start_at=start_at,
                end_at=end_at,
                max_runs=max_runs,
                execution_context=execution_context,
                delivery_mode=delivery_mode,
                delivery_channel=delivery_channel,
                delivery_channels=delivery_channels,
            )
        elif task_type == "cron":
            if not cron_expression:
                raise ValueError("cron 任务需要提供 cron_expression")
            task = self.task_store.create_cron(
                content,
                context.session_id,
                cron_expression,
                start_at=start_at,
                end_at=end_at,
                max_runs=max_runs,
                timezone_name=timezone,
                execution_context=execution_context,
                delivery_mode=delivery_mode,
                delivery_channel=delivery_channel,
                delivery_channels=delivery_channels,
            )
        else:
            raise ValueError("task_type 只能是 once 或 interval")
        return {"success": True, "task": task.to_dict()}

    def list_tasks(self, context: ToolExecutionContext) -> dict:
        return {
            "success": True,
            "tasks": [item.to_dict() for item in self.task_store.list(context.session_id)],
        }

    def get(self, context: ToolExecutionContext, task_id: str) -> dict:
        task = self.task_store.get(task_id)
        if task.session_id != context.session_id:
            raise PermissionError("只能查看当前 Session 的定时任务")
        return {"success": True, "task": task.to_dict()}

    def pause(self, context: ToolExecutionContext, task_id: str) -> dict:
        task = self.task_store.get(task_id)
        if task.session_id != context.session_id:
            raise PermissionError("只能暂停当前 Session 的定时任务")
        return {"success": True, "task": self.task_store.pause(task_id).to_dict()}

    def resume(self, context: ToolExecutionContext, task_id: str) -> dict:
        task = self.task_store.get(task_id)
        if task.session_id != context.session_id:
            raise PermissionError("只能恢复当前 Session 的定时任务")
        return {"success": True, "task": self.task_store.resume(task_id).to_dict()}

    def cancel(self, context: ToolExecutionContext, task_id: str) -> dict:
        task = self.task_store.get(task_id)
        if task.session_id != context.session_id:
            raise PermissionError("只能取消当前 Session 的定时任务")
        return {"success": True, "task": self.task_store.cancel(task_id).to_dict()}

    def run_now(self, context: ToolExecutionContext, task_id: str) -> dict:
        task = self.task_store.get(task_id)
        if task.session_id != context.session_id:
            raise PermissionError("只能立即执行当前 Session 的定时任务")
        if self.runner is None:
            raise RuntimeError("当前运行时尚未启动 Scheduler")
        running = self.runner(task_id)
        return {"success": True, "queued": True, "task": running.to_dict()}


def register_scheduler_tools(registry: ToolRegistry, service: SchedulerTools) -> None:
    """向共享 Registry 注册 Scheduler 查询和变更 Tools。"""

    string = {"type": "string"}
    integer = {"type": "integer"}
    no_extra = False
    registry.register(Tool(
        "schedule_create",
        "创建当前 Session 的一次性或周期性定时任务。一次性任务使用带时区的 run_at；周期任务可设置 start_at、end_at 和 max_runs。",
        {
            "type": "object",
            "properties": {
                "content": string,
                "task_type": {"type": "string"},
                "run_at": string,
                "interval_seconds": integer,
                "cron_expression": string,
                "timezone": string,
                "start_at": string,
                "end_at": string,
                "max_runs": integer,
                "execution_context": {"type": "string", "enum": ["main", "current", "isolated"]},
                "delivery_mode": {"type": "string", "enum": ["session", "channel", "webhook", "none"]},
                "delivery_channel": {"type": "string", "enum": ["feishu", "qqbot", "weixin"]},
                # SJTU's Grammar compiler does not implement JSON Schema's
                # ``uniqueItems`` keyword.  normalize_delivery_channels()
                # still removes duplicates locally before persistence.
                "delivery_channels": {"type": "array", "items": {"type": "string", "enum": ["feishu", "qqbot", "weixin"]}, "maxItems": 3},
            },
            "required": ["content", "task_type"],
            "additionalProperties": no_extra,
        },
        service.create,
        "approval_required",
        True,
        parallel_safe=False,
        side_effect=True,
    ))
    registry.register(Tool(
        "schedule_list",
        "列出当前 Session 的定时任务及其状态、下次执行时间和执行历史。",
        {"type": "object", "properties": {}, "additionalProperties": no_extra},
        service.list_tasks,
        "read_only",
        True,
    ))
    registry.register(Tool(
        "schedule_get",
        "查看当前 Session 中某个定时任务的详情。",
        {"type": "object", "properties": {"task_id": string}, "required": ["task_id"], "additionalProperties": no_extra},
        service.get,
        "read_only",
        True,
    ))
    for name, description, handler in (
        ("schedule_pause", "暂停当前 Session 的定时任务。", service.pause),
        ("schedule_resume", "恢复当前 Session 中已暂停的定时任务。", service.resume),
        ("schedule_cancel", "取消当前 Session 的定时任务。取消后不可恢复。", service.cancel),
        ("schedule_run", "立即执行当前 Session 的定时任务；周期任务不会改变下一次计划时间。", service.run_now),
    ):
        registry.register(Tool(
            name,
            description,
            {"type": "object", "properties": {"task_id": string}, "required": ["task_id"], "additionalProperties": no_extra},
            handler,
            "approval_required",
            True,
            parallel_safe=False,
            side_effect=True,
        ))
