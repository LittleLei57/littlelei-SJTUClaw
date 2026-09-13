"""Step 6：HTTP Gateway、Web UI 与外部入口。

Gateway 把 Session、聊天/流式 Turn、附件、Approval、Scheduler、Memory 和 Skill
暴露为本地 FastAPI 接口，并托管 ``web/`` 静态页面。请求最终都进入同一个
``AgentRuntime``；SSE 只负责广播持久化事件，因此刷新页面不会中断后台 Turn。
附件在进入 Runtime 前完成大小、类型和 Session 隔离校验。
"""

from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import asyncio
import json
import logging
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock
from threading import Thread
from typing import Any, Callable, Iterator, Literal
import uuid

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from attachment_store import AttachmentStore
from attachment_preview import inline_media_type, preview_attachment, render_pdf_preview_pages
from approval_store import ApprovalStore
from bootstrap import BASE_DIR, build_runtime
from compaction import CompactionError
from download_store import DownloadStore
from runtime import AgentCancelled, AgentRuntime
from scheduler import Scheduler, TaskStore, WebhookDelivery
from workspace import WorkspaceManager
from native_dialog import pick_directory
from turn_store import TurnStore
from channels.dedup import EventDeduplicator
from channels.base import OutboundEvent
from channels.feishu import FeishuAdapter
from channels.feishu_ws import FeishuLongConnection
from channels.qq import QQBotAdapter, QQBotConnection
from channels.weixin import WeixinAdapter, WeixinConnection, WeixinCredentialStore, WeixinLoginManager
from channels.service import ChannelNotifier, ChannelService
from channels.session_map import ChannelSessionMap
from conversation_view import is_internal_message, visible_message_stats
from daily_quotes import DAILY_QUOTES, quote_for_day
from gateway_security import (
    add_security_headers, is_loopback_client, redact_sensitive,
    remote_access_enabled, same_origin,
)
from config import (
    SJTU_API_MODELS,
    SJTU_MODEL_DETAILS,
    normalize_sjtu_model,
    scheduler_webhook_url,
)


GATEWAY_VERSION = "1.0.0"
API_PROTOCOL_VERSION = 2
logger = logging.getLogger(__name__)


class CreateSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=100)
    workspace: str | None = Field(default=None, min_length=1, max_length=2000)


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    sessionId: str | None = None
    turnId: str | None = Field(default=None, max_length=100)
    skillName: str | None = Field(default=None, min_length=1, max_length=100)


class ReplayChatRequest(BaseModel):
    sessionId: str
    messageIndex: int = Field(ge=0)
    message: str | None = Field(default=None, min_length=1, max_length=100_000)
    turnId: str | None = Field(default=None, max_length=100)


class WeixinLoginPollRequest(BaseModel):
    verifyCode: str | None = Field(default=None, max_length=20)


class CreateTaskRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    sessionId: str
    taskType: Literal["once", "interval", "cron"]
    runAt: str | None = None
    startAt: str | None = None
    endAt: str | None = None
    maxRuns: int | None = Field(default=None, ge=1)
    intervalSeconds: int | None = Field(default=None, ge=1)
    cronExpression: str | None = Field(default=None, max_length=100)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    executionContext: Literal["main", "current", "isolated"] = "main"
    deliveryMode: Literal["session", "channel", "webhook", "none"] = "session"
    deliveryChannel: Literal["feishu", "qqbot", "weixin"] | None = None
    deliveryChannels: list[Literal["feishu", "qqbot", "weixin"]] | None = Field(default=None, max_length=3)


class WorkspaceRequest(BaseModel):
    path: str = Field(min_length=1, max_length=2000)


class DraftWorkspacePickRequest(BaseModel):
    initialPath: str | None = Field(default=None, max_length=2000)


class SelectModelRequest(BaseModel):
    model: str = Field(min_length=1, max_length=100)


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    reason: str | None = Field(default=None, max_length=2000)
    turnId: str | None = Field(default=None, max_length=100)


def _append_approval_retry_hint(reply: str | None, tool_events: list[dict]) -> str | None:
    """Keep a failed approval actionable without re-running it implicitly."""
    failed_ids = [
        event.get("approvalId")
        for event in (tool_events or [])
        if isinstance(event, dict)
        and event.get("approvalId")
        and isinstance(event.get("result"), dict)
        and event["result"].get("success") is False
    ]
    if not failed_ids:
        return reply
    hint = (
        "工具执行失败；原审批已结束，未重复执行。"
        f"如需再次申请审批，请发送：/retry {failed_ids[-1]}"
    )
    return f"{reply}\n\n{hint}" if reply else hint


class CancelTurnRequest(BaseModel):
    """Cancellation mode for an active Agent Turn.

    Both modes use the same cooperative cancellation token. ``immediate``
    additionally tells the web client to detach from the turn immediately;
    the provider stream watcher then attempts to close an in-flight HTTP
    response instead of waiting for its full network timeout.
    """

    mode: Literal["graceful", "immediate"] = "graceful"


class MemoryCandidateDecisionRequest(BaseModel):
    accepted: bool


class MemoryCandidateUpdateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    type: str | None = Field(default=None, max_length=30)


class SkillRunRequest(BaseModel):
    sessionId: str
    task: str = Field(min_length=1, max_length=100_000)


def create_app(
    runtime: AgentRuntime | None = None,
    attachment_store: AttachmentStore | None = None,
    web_dir: str | Path | None = None,
    scheduler: Scheduler | None = None,
    directory_picker=None,
    start_background_services: bool = True,
) -> FastAPI:
    """装配可测试的 FastAPI 应用及其共享服务。"""

    if runtime is None:
        runtime, _ = build_runtime()
    attachments = attachment_store or AttachmentStore(runtime.store)
    workspace_manager = getattr(runtime, "workspace_manager", WorkspaceManager(runtime.store))
    download_store = getattr(runtime, "download_store", DownloadStore(runtime.store.data_dir))
    approval_store = runtime.approval_store or ApprovalStore(runtime.store.data_dir)
    runtime.approval_store = approval_store
    memory_candidate_store = getattr(runtime, "memory_candidate_store", None)
    skill_registry = getattr(runtime, "skill_registry", None)
    turn_store = TurnStore(runtime.store.data_dir)
    locks: defaultdict[str, Lock] = defaultdict(Lock)
    active_turns: dict[str, dict] = {}
    active_turns_lock = Lock()
    shutdown_requested = Event()
    if scheduler:
        task_store = scheduler.task_store
    else:
        task_store = getattr(runtime, "task_store", None) or TaskStore(
            runtime.store.data_dir, runtime.store
        )
    scheduler_service = scheduler or Scheduler(
        task_store, runtime, session_lock=lambda session_id: locks[session_id]
    )
    webhook_url = scheduler_webhook_url()
    if webhook_url:
        scheduler_service.webhook_sender = WebhookDelivery(webhook_url)
    scheduler_tools = getattr(runtime, "scheduler_tools", None)
    if scheduler_tools is not None:
        scheduler_tools.runner = scheduler_service.run_task_now
    # Approval resolution belongs to Runtime, while Scheduler owns the task
    # lifecycle.  Keep the bridge optional so embedded runtimes continue to
    # work without Scheduler.
    runtime.approval_result_callback = scheduler_service.on_approval_result
    choose_directory = directory_picker or pick_directory
    channel_service = ChannelService(
        runtime,
        ChannelSessionMap(runtime.store.data_dir, runtime.store),
        EventDeduplicator(runtime.store.data_dir),
        attachments,
    )
    feishu_adapter = FeishuAdapter()
    feishu_connection = FeishuLongConnection(feishu_adapter, channel_service)
    qq_adapter = QQBotAdapter()
    qq_connection = QQBotConnection(qq_adapter, channel_service)
    weixin_store = WeixinCredentialStore(runtime.store.data_dir)
    weixin_adapter = WeixinAdapter(weixin_store)
    weixin_connection = WeixinConnection(weixin_adapter, channel_service)
    weixin_login = WeixinLoginManager(weixin_store)

    def record_delivery_activity(session_id: str, data: dict) -> None:
        if not session_id:
            return
        try:
            with locks[session_id]:
                session = runtime.store.get(session_id)
                AgentRuntime._record_activity(session, "outbound_delivery", None, data)
                session.updated_at = utc_now()
                runtime.store.save(session)
        except Exception:
            # Delivery diagnostics are best effort and must never block the
            # retry worker or alter the Scheduler result.
            return

    scheduler_service.notifier = ChannelNotifier(
        channel_service.session_map,
        {"feishu": feishu_adapter, "qqbot": qq_adapter, "weixin": weixin_adapter},
        data_dir=runtime.store.data_dir,
        activity_recorder=record_delivery_activity,
    )

    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def durable_turn_snapshot(record: dict) -> dict:
        """Translate one persisted Turn row into the public reconnect shape."""
        status = str(record.get("status") or "running")
        return {
            "turnId": record.get("turn_id"),
            "runId": record.get("run_id"),
            "sessionId": record.get("session_id"),
            "approvalId": record.get("active_approval_id"),
            "kind": record.get("kind") or "chat",
            "phase": record.get("phase") or status,
            "status": status,
            "message": record.get("message") or "正在处理",
            "startedAt": record.get("started_at"),
            "updatedAt": record.get("updated_at"),
            "lastSeq": int(record.get("last_seq") or 0),
            "canCancel": status not in {"completed", "cancelled", "error", "failed", "interrupted"},
            "cancelMode": None,
            "cancelRequestedAt": record.get("cancel_requested_at"),
            "durableOnly": True,
        }

    def active_turn_snapshot(session_id: str | None = None) -> list[dict]:
        # SQLite is the source of truth. The in-memory map only enriches live
        # workers with cancellation handles and fresher UI telemetry.
        turns_by_id = {
            str(record["turn_id"]): durable_turn_snapshot(record)
            for record in turn_store.list_active(session_id)
        }
        with active_turns_lock:
            for turn_id, item in active_turns.items():
                if session_id is not None and item["sessionId"] != session_id:
                    continue
                turns_by_id[turn_id] = {
                    "turnId": turn_id,
                    "runId": item.get("runId"),
                    "sessionId": item["sessionId"],
                    "approvalId": item.get("approvalId"),
                    "kind": item.get("kind", "chat"),
                    "phase": item.get("phase", "running"),
                    "status": "cancelling" if item.get("phase") == "cancelling" else "running",
                    "message": item.get("message", "正在处理"),
                    "startedAt": item.get("startedAt"),
                    "updatedAt": item.get("updatedAt"),
                    "lastSeq": item.get("nextSeq", 1) - 1,
                    "canCancel": not bool(item.get("cancellation").is_set()),
                    "cancelMode": item.get("cancelMode"),
                    "cancelRequestedAt": item.get("cancelRequestedAt"),
                    "durableOnly": False,
                }
        return sorted(turns_by_id.values(), key=lambda item: item.get("startedAt") or "")

    def active_turn_for_session(session_id: str) -> dict | None:
        turns = active_turn_snapshot(session_id)
        return turns[-1] if turns else None

    def active_turn_for_approval(approval_id: str) -> dict | None:
        with active_turns_lock:
            matches = [
                (turn_id, item)
                for turn_id, item in active_turns.items()
                if item.get("approvalId") == approval_id
            ]
            if not matches:
                return None
            turn_id, item = max(matches, key=lambda pair: pair[1].get("startedAt") or "")
            return {
                "turnId": turn_id,
                "sessionId": item["sessionId"],
                "approvalId": approval_id,
                "phase": item.get("phase", "running"),
                "message": item.get("message", "正在处理审批"),
                "startedAt": item.get("startedAt"),
                "updatedAt": item.get("updatedAt"),
                "lastSeq": item.get("nextSeq", 1) - 1,
                "canCancel": not bool(item.get("cancellation").is_set()),
            }
    def register_active_turn(
        turn_id: str, session_id: str, cancellation: Event, kind: str,
        *, approval_id: str | None = None,
    ) -> str:
        """Start or resume one durable logical Turn and return its run fence."""
        with active_turns_lock:
            if turn_id in active_turns:
                raise HTTPException(status_code=409, detail="turnId \u6b63\u5728\u8fd0\u884c")
            timestamp = now_iso()
            run_id = f"run_{uuid.uuid4().hex[:12]}"
            record = turn_store.get(turn_id)
            if record is None:
                record = turn_store.start(turn_id, session_id, kind, started_at=timestamp, run_id=run_id)
            elif (approval_id and record.get("session_id") == session_id
                  and record.get("status") == "awaiting_approval"
                  and (not record.get("active_approval_id")
                       or record.get("active_approval_id") == approval_id)):
                record = turn_store.transition(
                    turn_id, "running", phase="approval_resume",
                    message="\u6b63\u5728\u6062\u590d\u5ba1\u6279\u540e\u7684\u6267\u884c", active_approval_id=None,
                    run_id=run_id, expected_run_id=record.get("run_id"),
                    expected_statuses={"awaiting_approval"}, increment_resume=True,
                )
                if record.get("status") != "running":
                    raise HTTPException(status_code=409, detail="\u672a\u80fd\u6062\u590d\u6302\u8d77\u7684 Turn")
            else:
                raise HTTPException(status_code=409, detail="Turn \u65e0\u6cd5\u542f\u52a8\uff1a{}\uff08\u72b6\u6001\uff1a{}\uff09".format(turn_id, record.get("status")))
            active_turns[turn_id] = {
                "runId": run_id, "sessionId": session_id, "cancellation": cancellation,
                "kind": kind, "approvalId": approval_id,
                "phase": "approval_resume" if approval_id else "starting",
                "message": "\u6b63\u5728\u6062\u590d\u5ba1\u6279\u540e\u7684\u6267\u884c" if approval_id else "\u6b63\u5728\u542f\u52a8",
                "startedAt": record.get("started_at") or timestamp, "updatedAt": timestamp,
                "events": [], "subscribers": [],
                "nextSeq": max(1, int(record.get("last_seq", 0) or 0) + 1),
                "step": 0, "cancelMode": None, "cancelRequestedAt": None,
                "trace": {"currentModel": None, "last": None, "tools": {}, "approvals": {}},
            }
            return run_id

    def update_active_turn(
        turn_id: str,
        phase: str | None = None,
        message: str | None = None,
        *,
        expected_run_id: str | None = None,
    ) -> None:
        with active_turns_lock:
            item = active_turns.get(turn_id)
            if not item or (expected_run_id and item.get("runId") != expected_run_id):
                return
            if phase:
                item["phase"] = phase
            if message:
                item["message"] = message
            item["updatedAt"] = now_iso()
            persisted_phase = item.get("phase")
            persisted_message = item.get("message")
            # ``phase`` is detailed telemetry; the durable state machine only
            # has one non-terminal running state (plus cancelling). This
            # prevents a restart during ``model_call``/``tool_result`` from
            # looking like an already-finished turn.
            # Once the final assistant event has been published, the answer
            # is durable even if post-turn bookkeeping (for example summary
            # compaction) is still running.  Never downgrade that terminal
            # checkpoint back to ``running``; otherwise a Gateway restart in
            # this small window would falsely report an unfinished Turn.
            if item.get("terminalStatus"):
                persisted_status = None
            else:
                persisted_status = "cancelling" if persisted_phase == "cancelling" else "running"
        try:
            turn_store.update(
                turn_id,
                phase=persisted_phase,
                message=persisted_message,
                status=persisted_status,
                expected_run_id=expected_run_id,
            )
        except Exception:
            logger.exception("无法持久化 Turn 状态：%s", turn_id)

    def trace_metadata(
        item: dict,
        turn_id: str,
        seq: int,
        event_name: str,
        payload: dict,
        event_step: int,
    ) -> tuple[int | None, str, str]:
        """Assign deterministic parent/trace identities to Turn events."""
        trace = item.setdefault(
            "trace", {"currentModel": None, "last": None, "tools": {}, "approvals": {}}
        )
        current_model = trace.get("currentModel")
        last = trace.get("last")
        parent: int | None = None
        trace_type = event_name
        trace_id = f"{turn_id}:{event_name}:{seq}"
        call_id = payload.get("callId")
        if call_id is not None:
            call_id = str(call_id)

        if event_name == "status" and payload.get("phase") == "model_call":
            parent = last if isinstance(last, int) and last < seq else None
            trace_type = "model_call"
            trace_id = f"{turn_id}:model:{event_step or seq}"
            trace["currentModel"] = seq
        elif event_name in {"tool_call", "tool_calls"}:
            parent = current_model or last
            trace_type = "tool"
            trace_id = call_id or f"{turn_id}:tool:{seq}"
            if event_name == "tool_calls":
                for call in payload.get("calls") or []:
                    nested_id = None
                    if isinstance(call, dict):
                        nested_id = call.get("callId") or call.get("id")
                    if nested_id is not None:
                        trace["tools"][str(nested_id)] = seq
            elif call_id:
                trace["tools"][call_id] = seq
        elif event_name == "tool_result":
            parent = trace["tools"].get(call_id) if call_id else None
            parent = parent or current_model or last
            trace_type = "tool_result"
            trace_id = call_id or f"{turn_id}:tool-result:{seq}"
        elif event_name in {"approval_required", "approval_result"}:
            approval = payload.get("approval")
            approval_id = payload.get("approvalId")
            if isinstance(approval, dict):
                approval_id = approval.get("approvalId") or approval_id
            approval_id = str(approval_id) if approval_id is not None else None
            parent = trace["tools"].get(call_id) if call_id else None
            if event_name == "approval_result" and approval_id:
                parent = trace["approvals"].get(approval_id) or parent
            parent = parent or current_model or last
            trace_type = "approval"
            trace_id = approval_id or call_id or f"{turn_id}:approval:{seq}"
            if approval_id:
                trace["approvals"][approval_id] = seq
        elif event_name.startswith("compaction"):
            parent = current_model or last
            trace_type = "compaction"
            trace_id = f"{turn_id}:compaction:{seq}"
        else:
            parent = current_model or last
            if current_model:
                trace_id = f"{turn_id}:model:{event_step or current_model}"

        if isinstance(parent, int) and parent >= seq:
            parent = None
        trace["last"] = seq
        return parent, trace_id, trace_type

    def finish_active_turn(turn_id: str, expected_run_id: str | None = None) -> None:
        """Release one in-memory run and durably settle or suspend its Turn."""
        with active_turns_lock:
            current = active_turns.get(turn_id)
            if current is None or (expected_run_id and current.get("runId") != expected_run_id):
                return
            item = active_turns.pop(turn_id)
        try:
            approval_id = item.get("suspendedApprovalId")
            if approval_id:
                turn_store.transition(
                    turn_id, "awaiting_approval", phase="awaiting_approval",
                    message="\u7b49\u5f85\u7528\u6237\u5ba1\u6279", active_approval_id=approval_id,
                    expected_statuses={"running"},
                    expected_run_id=item.get("runId"),
                )
            else:
                status = item.get("terminalStatus") or "error"
                turn_store.finish(
                    turn_id, status, error=item.get("terminalError"),
                    expected_run_id=item.get("runId"),
                )
        except Exception:
            logger.exception("\u65e0\u6cd5\u5b8c\u6210 Turn \u72b6\u6001\u843d\u76d8\uff1a%s", turn_id)
        for subscriber in list(item.get("subscribers", [])):
            subscriber.put(("_finished", {"turnId": turn_id}))

    def publish_turn_event(
        turn_id: str,
        event_name: str,
        payload: dict,
        *,
        expected_run_id: str | None = None,
    ) -> dict | None:
        """Publish only for the currently owning run; late workers are fenced out."""
        with active_turns_lock:
            item = active_turns.get(turn_id)
            if not item or (expected_run_id and item.get("runId") != expected_run_id):
                return None
            seq = item["nextSeq"]
            item["nextSeq"] += 1
            if event_name == "status" and payload.get("phase") == "model_call":
                item["step"] = item.get("step", 0) + 1
            event_step = payload.get("step", item.get("step", 0))
            try:
                event_step = max(0, int(event_step))
            except (TypeError, ValueError):
                event_step = item.get("step", 0)
            parent_seq, trace_id, trace_type = trace_metadata(
                item, turn_id, seq, event_name, payload, event_step
            )
            event_timestamp = now_iso()
            agent_event = {
                "runId": item.get("runId"),
                "turnId": turn_id,
                "seq": seq,
                "step": event_step,
                "type": event_name,
                "payload": dict(payload),
                "timestamp": event_timestamp,
                "parentSeq": parent_seq,
                "traceId": trace_id,
                "traceType": trace_type,
            }
            # Keep the existing top-level fields for the current Web/CLI
            # consumers, while exposing one canonical envelope for new
            # clients and durable replay.
            enriched = {
                **payload,
                "eventSeq": seq,
                "runId": item.get("runId"),
                "turnId": turn_id,
                "step": event_step,
                "eventTimestamp": event_timestamp,
                "parentSeq": parent_seq,
                "traceId": trace_id,
                "traceType": trace_type,
                "agentEvent": agent_event,
            }
            item["events"].append((seq, event_name, enriched))
            item["events"] = item["events"][-500:]
            if event_name == "done":
                outcome = payload.get("status", "completed")
                if outcome == "approval_required":
                    pending = payload.get("pendingApprovals") or []
                    first = pending[0] if pending and isinstance(pending[0], dict) else {}
                    item["suspendedApprovalId"] = first.get("approvalId")
                else:
                    item["terminalStatus"] = outcome if outcome in {"completed", "cancelled", "error", "failed"} else "completed"
            elif event_name == "cancelled":
                item["terminalStatus"] = "cancelled"
            elif event_name == "error":
                item["terminalStatus"] = "error"
                item["terminalError"] = payload.get("message")
            subscribers = list(item.get("subscribers", []))
        # Token deltas are transport data, not durable lifecycle facts.
        # Persisting every token makes SQLite the hot path and delays Stop.
        # Live clients still receive every delta from the in-memory queue;
        # ``assistant_final`` durably stores the complete visible answer.
        if event_name != "assistant_delta":
            try:
                turn_store.append_event(
                    turn_id, seq, event_name, enriched,
                    parent_seq=parent_seq, trace_id=trace_id, trace_type=trace_type,
                    expected_run_id=expected_run_id,
                )
            except Exception:
                logger.exception("无法持久化 Turn 事件：%s #%s", turn_id, seq)
        for subscriber in subscribers:
            subscriber.put((event_name, enriched))
        return enriched

    def subscribe_turn(
        turn_id: str, after: int = 0
    ) -> tuple[Queue, list[tuple[int, str, dict]], bool]:
        """Replay durable events and subscribe to a live worker when present.

        Refresh must never depend on the worker still being registered in this
        Python process. Persisted events are read first; the queue is attached
        only when the matching Turn is still live.
        """
        queue: Queue = Queue()
        record = turn_store.get(turn_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Turn 不存在")
        cached_by_seq: dict[int, tuple[int, str, dict]] = {
            int(event["seq"]): (
                int(event["seq"]), str(event["eventName"]), dict(event["payload"])
            )
            for event in turn_store.events_after(turn_id, after)
        }
        live = False
        with active_turns_lock:
            item = active_turns.get(turn_id)
            if item:
                live = True
                for event in item.get("events", []):
                    if event[0] > after:
                        cached_by_seq[event[0]] = event
                item.setdefault("subscribers", []).append(queue)
        cached = [cached_by_seq[seq] for seq in sorted(cached_by_seq)]
        return queue, cached, live

    def unsubscribe_turn(turn_id: str, queue: Queue) -> None:
        with active_turns_lock:
            item = active_turns.get(turn_id)
            if not item:
                return
            subscribers = item.get("subscribers", [])
            if queue in subscribers:
                subscribers.remove(queue)

    def stream_turn_operation(
        turn_id: str,
        run_id: str,
        cancellation: Event,
        thread_name: str,
        operation: Callable[[Callable[[dict], None]], Any],
        done_payload: Callable[[Any], dict],
        error_prefix: str,
    ) -> Iterator[str]:
        """Run one logical Turn through the single durable SSE lifecycle.

        Chat, replay and approval-resume used to carry three subtly different
        worker loops. Keeping the event mapping, terminal commit and exception
        handling here prevents one route from forgetting cancellation,
        compaction telemetry or run fencing.
        """
        queue: Queue = Queue()
        finished = object()
        phase_messages = {
            "tool_call": "正在调用工具",
            "tool_result": "正在处理工具结果",
            "approval_required": "等待用户审批",
            "compaction_started": "正在整理上下文",
            "compaction": "上下文整理完成",
            "compaction_failed": "上下文整理失败",
        }

        def emit(event: dict) -> None:
            event_name = str(event.get("type") or "status")
            if event_name == "status":
                update_active_turn(
                    turn_id, event.get("phase"), event.get("message"),
                    expected_run_id=run_id,
                )
            elif event_name in phase_messages:
                phase = "compaction" if event_name.startswith("compaction") else event_name
                update_active_turn(
                    turn_id, phase, phase_messages[event_name],
                    expected_run_id=run_id,
                )
            enriched = publish_turn_event(
                turn_id, event_name, event, expected_run_id=run_id
            )
            if enriched is not None:
                queue.put((event_name, enriched))

        def worker() -> None:
            try:
                started = publish_turn_event(
                    turn_id, "turn_started", {"turnId": turn_id},
                    expected_run_id=run_id,
                )
                if started is not None:
                    queue.put(("turn_started", started))
                result = operation(emit)
                payload = {**done_payload(result), "turnId": turn_id}
                done = publish_turn_event(
                    turn_id, "done", payload, expected_run_id=run_id
                )
                # Commit the terminal state before exposing ``done``. A page
                # refresh in this window must never resurrect a finished run.
                finish_active_turn(turn_id, run_id)
                if done is not None:
                    queue.put(("done", done))
            except AgentCancelled as exc:
                cancelled = publish_turn_event(
                    turn_id, "cancelled",
                    {"turnId": turn_id, "status": "cancelled", "message": str(exc)},
                    expected_run_id=run_id,
                )
                if cancelled is not None:
                    queue.put(("cancelled", cancelled))
            except Exception as exc:
                error = publish_turn_event(
                    turn_id, "error", {"message": f"{error_prefix}?{exc}"},
                    expected_run_id=run_id,
                )
                if error is not None:
                    queue.put(("error", error))
            finally:
                finish_active_turn(turn_id, run_id)
                queue.put(("_finished", finished))

        Thread(target=worker, name=thread_name, daemon=True).start()
        while True:
            try:
                event_name, payload = queue.get(timeout=15)
            except Empty:
                yield ": keep-alive\n\n"
                continue
            if payload is finished:
                break
            yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    @asynccontextmanager
    async def lifespan(_):
        shutdown_requested.clear()
        # A process restart cannot safely resume an in-flight tool call. Mark
        # the durable checkpoint as interrupted so clients never see a stale
        # "running" turn after reconnecting.
        interrupted = turn_store.mark_interrupted()
        uncertain_approvals = approval_store.recover_executing()
        if interrupted:
            logger.warning("Gateway \u91cd\u542f\u540e\u5c06 %s \u4e2a\u8fd0\u884c\u4e2d Turn \u6807\u8bb0\u4e3a interrupted", interrupted)
        if uncertain_approvals:
            logger.warning("Gateway \u91cd\u542f\u540e\u5c06 %s \u4e2a\u6267\u884c\u4e2d\u5ba1\u6279\u6807\u8bb0\u4e3a outcome-unknown", uncertain_approvals)
        if start_background_services:
            scheduler_service.start()
            scheduler_service.notifier.start()
            feishu_connection.start()
            qq_connection.start()
            weixin_connection.start()
        try:
            yield
        finally:
            if start_background_services:
                scheduler_service.stop()
                scheduler_service.notifier.stop()
                qq_connection.stop()
                weixin_connection.stop()
            shell_manager = getattr(runtime, "shell_manager", None)
            if shell_manager:
                shell_manager.close_all()

    app = FastAPI(title="SJTUClaw Gateway", version=GATEWAY_VERSION, lifespan=lifespan)
    app.state.turn_store = turn_store
    app.state.shutdown_requested = shutdown_requested

    @app.get("/api/daily-quote")
    def daily_quote():
        """Expose the same welcome text used by the CLI for the Web UI."""
        return {"quote": quote_for_day(), "quotes": list(DAILY_QUOTES)}

    @app.middleware("http")
    async def secure_local_gateway(request: Request, call_next):
        client_host = request.client.host if request.client else ""
        webhook = request.url.path == "/api/channels/feishu/events"
        if not webhook and not remote_access_enabled() and not is_loopback_client(client_host):
            response = JSONResponse(status_code=403, content={"error": "Gateway 默认仅允许本机访问。"})
            add_security_headers(response)
            return response
        origin = request.headers.get("origin")
        if (
            origin
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not same_origin(origin, request.url.scheme, request.headers.get("host", ""))
        ):
            response = JSONResponse(status_code=403, content={"error": "拒绝跨站写操作。"})
            add_security_headers(response)
            return response
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        add_security_headers(response)
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception):
        logger.exception("Gateway 请求失败：%s %s", request.method, request.url.path)
        response = JSONResponse(
            status_code=500,
            content={"error": "Gateway 内部错误，请查看服务端日志。"},
        )
        add_security_headers(response)
        return response

    @app.get("/api/health")
    def health():
        capability_reader = getattr(runtime.model, "capability_profile", None)
        model_capabilities = (
            capability_reader() if callable(capability_reader) else None
        )
        return {
            "status": "ok", "service": "SJTUClaw Gateway",
            "gatewayVersion": GATEWAY_VERSION,
            "apiProtocolVersion": API_PROTOCOL_VERSION,
            "modelCapabilities": model_capabilities,
            "feishu": {
                "mode": feishu_connection.mode,
                "configured": feishu_connection.configured,
                "running": feishu_connection.running,
                "error": redact_sensitive(feishu_connection.last_error),
            },
            "qqbot": {
                "configured": qq_connection.configured, "running": qq_connection.running,
                "connected": qq_connection.connected,
                "error": redact_sensitive(qq_connection.last_error),
            },
            "weixin": {
                "configured": weixin_connection.configured, "running": weixin_connection.running,
                "connected": weixin_connection.connected,
                "error": redact_sensitive(weixin_connection.last_error),
            },
        }

    @app.get("/api/model/capabilities")
    def model_capabilities():
        reader = getattr(runtime.model, "capability_profile", None)
        if not callable(reader):
            return {"available": False, "capabilities": None}
        return {"available": True, "capabilities": reader()}

    def model_selection_payload() -> dict:
        reader = getattr(runtime.model, "capability_profile", None)
        return {
            "currentModel": getattr(runtime.model, "model", None),
            "models": [
                {
                    "id": model_id,
                    "label": label,
                    **SJTU_MODEL_DETAILS.get(model_id, {}),
                }
                for model_id, label in SJTU_API_MODELS.items()
            ],
            "capabilities": reader() if callable(reader) else None,
        }

    @app.get("/api/model")
    def get_model_selection():
        return model_selection_payload()

    @app.put("/api/model")
    def set_model_selection(payload: SelectModelRequest):
        if active_turn_snapshot():
            raise HTTPException(
                status_code=409,
                detail="当前仍有回复正在生成，请等待完成或停止后再切换模型。",
            )
        try:
            model = normalize_sjtu_model(payload.model)
            runtime.select_model(model)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return model_selection_payload()

    @app.post("/api/model/capabilities/reset")
    def reset_model_capabilities():
        resetter = getattr(runtime.model, "reset_capability_profile", None)
        if not callable(resetter):
            raise HTTPException(status_code=409, detail="当前模型客户端不支持能力重新检测。")
        return {
            "reset": True,
            "message": "模型能力记录已清除，将在后续真实请求中重新检测。",
            "capabilities": resetter(),
        }

    @app.get("/api/stress/report")
    def stress_report(sessionId: str | None = None):
        try:
            session = runtime.store.get(sessionId) if sessionId else runtime.store.current
            compaction = (
                runtime.compactor.estimate(session)
                if runtime.compactor is not None else {"configured": False}
            )
            tool_timeout = (
                getattr(runtime.tool_registry, "max_execution_seconds", None)
                if runtime.tool_registry is not None else None
            )
            attachment_items = attachments.list(session.session_id)
            attachment_audit = attachments.audit(cleanup_orphans=False)
            message_stats = visible_message_stats(session.messages)
            return {
                "sessionId": session.session_id,
                "messages": {
                    "total": message_stats["messageCount"],
                    "user": message_stats["userMessageCount"],
                    "assistant": message_stats["assistantMessageCount"],
                    "stored": len(session.messages),
                },
                "compaction": compaction,
                "attachments": {
                    "count": len(attachment_items),
                    "maxPerSession": attachments.MAX_PER_SESSION,
                    "maxBytes": attachments.MAX_BYTES,
                    "available": sum(1 for item in attachment_items if item.get("available")),
                    "missing": sum(1 for item in attachment_items if not item.get("available")),
                },
                "tools": {
                    "registered": len(runtime.tool_registry.definitions()) if runtime.tool_registry else 0,
                    "timeoutSeconds": tool_timeout,
                    "timeoutEnabled": tool_timeout is not None,
                },
                "storage": {
                    "missingAttachmentReferences": len(attachment_audit["missingReferences"]),
                    "orphanBlobs": len(attachment_audit["orphanBlobs"]),
                },
                "scenarios": _stress_scenarios(
                    compaction,
                    attachment_items,
                    attachment_audit,
                    tool_timeout,
                    getattr(runtime, "max_agent_steps", None),
                    session.messages,
                ),
            }
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _stress_scenarios(
        compaction,
        attachment_items,
        attachment_audit,
        tool_timeout,
        max_agent_steps=None,
        messages=None,
    ):
        attachment_count = len(attachment_items)
        messages = messages or []
        protocol_errors = sum(
            1 for item in messages
            if str(item.get("content") or "").lstrip().startswith("[protocol_error]")
        )
        oversized_messages = compaction.get("oversizedMessages", [])
        missing_references = attachment_audit["missingReferences"]
        orphan_blobs = attachment_audit["orphanBlobs"]
        attachment_ratio = attachment_count / max(AttachmentStore.MAX_PER_SESSION, 1)
        return [
            {
                "id": "oversized_context",
                "title": "超长上下文",
                "status": "warn" if compaction.get("shouldCompact") else "pass",
                "detail": (
                    f"预计分片 {compaction.get('estimatedChunks', 0)} 个；"
                    f"语义消息 {compaction.get('semanticMessages', 0)} 条。"
                ),
            },
            {
                "id": "single_huge_message",
                "title": "单条超长消息",
                "status": "warn" if oversized_messages else "pass",
                "detail": f"检测到 {len(oversized_messages)} 条会被拆分的超长旧消息。",
            },
            {
                "id": "tool_timeout",
                "title": "工具长时间阻塞",
                "status": "pass" if tool_timeout is not None else "warn",
                "detail": f"Registry 层超时：{tool_timeout if tool_timeout is not None else '未启用'} 秒。",
            },
            {
                "id": "agent_loop_limit",
                "title": "模型重复调用 Tool",
                "status": "pass" if max_agent_steps is not None and max_agent_steps >= 1 else "warn",
                "detail": (
                    f"单个 Agent Turn 最多 {max_agent_steps} 轮模型/Tool 循环；"
                    "达到上限会安全暂停并保留审计记录。"
                    if max_agent_steps is not None
                    else "未配置 Agent Loop 安全上限。"
                ),
            },
            {
                "id": "attachment_flood",
                "title": "大量附件上传",
                "status": "warn" if attachment_ratio >= 0.8 else "pass",
                "detail": (
                    f"当前 {attachment_count}/{AttachmentStore.MAX_PER_SESSION} 个附件；"
                    f"单文件上限 {AttachmentStore.MAX_BYTES / (1024 * 1024):g} MB。"
                ),
            },
            {
                "id": "attachment_missing",
                "title": "附件磁盘文件缺失",
                "status": "failed" if missing_references else "warn" if orphan_blobs else "pass",
                "detail": f"缺失引用 {len(missing_references)} 个，孤儿 blob {len(orphan_blobs)} 个。",
            },
            {
                "id": "concurrent_session_writes",
                "title": "多端同时写同一 Session",
                "status": "pass",
                "detail": "Runtime 已按 sessionId 串行化普通对话、审批恢复和手动压缩。",
            },
            {
                "id": "protocol_pollution",
                "title": "协议污染与未执行承诺",
                "status": "pass" if protocol_errors == 0 else "warn",
                "detail": (
                    "当前 Session 没有遗留 protocol_error。"
                    if protocol_errors == 0
                    else f"检测到 {protocol_errors} 条内部 protocol_error；它们不会计入可见消息，但建议重答并检查 Turn Trace。"
                ),
            },
        ]

    @app.post("/api/channels/weixin/login/start")
    def start_weixin_login():
        try:
            return weixin_login.start()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"微信扫码登录启动失败：{exc}") from exc

    @app.post("/api/channels/weixin/login/{session_id}/poll")
    def poll_weixin_login(session_id: str, body: WeixinLoginPollRequest):
        try:
            result = weixin_login.poll(session_id, body.verifyCode)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"微信扫码登录查询失败：{exc}") from exc
        if result.get("status") == "confirmed":
            weixin_connection.start()
        return result

    @app.post("/api/channels/feishu/events")
    async def feishu_events(request: Request):
        try:
            body = await request.body()
            payload = json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=400, detail="飞书事件必须是 JSON。") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="飞书事件必须是 JSON object。")
        try:
            feishu_adapter.validate_security_config(require_webhook=True)
            feishu_adapter.verify_http_request(body, request.headers)
            payload = feishu_adapter._decrypt_payload(payload)
            feishu_adapter.verify_payload(payload)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if isinstance(payload.get("challenge"), str):
            return {"challenge": payload["challenge"]}
        try:
            inbound = feishu_adapter.parse_event(payload)
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if inbound is None:
            return {"code": 0, "ignored": True}

        def process_feishu_event():
            try:
                channel_service.handle(feishu_adapter, inbound)
            except Exception as exc:
                try:
                    feishu_adapter.send(inbound, OutboundEvent("error", f"SJTUClaw 处理失败：{exc}"))
                except Exception:
                    pass

        Thread(target=process_feishu_event, name=f"feishu-{inbound.event_id}", daemon=True).start()
        if inbound.metadata.get("cardAction"):
            return {"toast": {"type": "success", "content": "审批已提交"}}
        return {"code": 0}

    @app.get("/api/sessions")
    def list_sessions():
        current_id = runtime.store.current_id
        def session_payload(item):
            stats = visible_message_stats(item.messages)
            return {
                "sessionId": item.session_id,
                "title": item.title,
                "workspace": item.workspace,
                **stats,
                "storedMessageCount": len(item.messages),
                "attachmentCount": len(item.attachments),
                "createdAt": item.created_at,
                "updatedAt": item.updated_at,
                "activeTurn": active_turn_for_session(item.session_id),
            }
        return {
            "currentSessionId": current_id,
            "sessions": [session_payload(item) for item in runtime.store.list_sessions()],
        }

    @app.post("/api/sessions", status_code=201)
    def create_session(request: CreateSessionRequest):
        # Keep the browser's new draft local until it has an actual turn. A
        # refresh should return to the last Session where the user conversed.
        previous = runtime.store.current
        selected_workspace: str | None = None
        if request.workspace:
            try:
                target = Path(request.workspace).expanduser().resolve()
                if not target.exists():
                    raise FileNotFoundError(f"Workspace 不存在：{target}")
                if not target.is_dir():
                    raise NotADirectoryError(f"Workspace 不是目录：{target}")
                selected_workspace = str(target)
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        session = runtime.store.create(request.title, make_current=False)
        # A new conversation starts with a clean message history, but keeping
        # the last Workspace is the least surprising default for project work
        # and preserves the old CLI/Web behavior for tool calls.
        session.workspace = selected_workspace or previous.workspace
        if session.workspace:
            runtime.store.save(session)
        return session.to_dict()

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        try:
            session = runtime.store.get(session_id)
            data = session.to_dict()
            data.update(visible_message_stats(session.messages))
            data["storedMessageCount"] = len(session.messages)
            data["attachments"] = attachments.list(session_id)
            data["activeTurn"] = active_turn_for_session(session_id)
            return data
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/api/sessions/{session_id}")
    def rename_session(session_id: str, request: RenameSessionRequest):
        try:
            with locks[session_id]:
                session = runtime.store.rename(session_id, request.title)
            return session.to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str):
        try:
            with locks[session_id]:
                return attachments.delete_session(session_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/sessions/{session_id}/compact")
    def compact_session(session_id: str):
        """Manually compact one idle Session and return a UI-ready preview."""
        if runtime.compactor is None:
            raise HTTPException(status_code=503, detail="Compactor 未配置。")
        if active_turn_for_session(session_id):
            raise HTTPException(status_code=409, detail="当前会话仍在生成回复，请在本轮结束后再整理上下文。")
        try:
            with locks[session_id]:
                session = runtime.store.get(session_id)
                # Use the same forced split policy as the operation itself.
                # Otherwise a one-message stress test briefly reports zero
                # old messages even though that oversized message is compacted.
                estimate = runtime.compactor.estimate(session, force=True)
                result = runtime.compactor.compact(session, force=True)
            if result is None:
                semantic_messages = int(estimate.get("semanticMessages") or 0)
                if semantic_messages < 4:
                    message = (
                        f"当前只有 {semantic_messages} 条可整理的对话消息；"
                        "至少需要 4 条，原有上下文未作修改。"
                    )
                else:
                    message = "当前没有可安全移入 Summary 的较早消息，原有上下文未作修改。"
                return {
                    "sessionId": session_id,
                    "compacted": False,
                    "semanticMessages": semantic_messages,
                    "minimumMessages": 4,
                    "message": message,
                }
            summary_preview = (
                result.summary
                if len(result.summary) <= 1200
                else result.summary[:1200] + "…"
            )
            # Manual compaction must be recoverable after a refresh just like
            # automatic compaction. Older versions returned only a transient
            # response, so the web card disappeared as soon as the Session was
            # loaded again.
            with locks[session_id]:
                session = runtime.store.get(session_id)
                # Pin a manual-compaction event to the newest visible message
                # that existed when the command completed.  Without this
                # durable anchor the Web client can only append the restored
                # card at the bottom, causing an old /compact card to follow
                # every later turn.
                anchor_id = f"compact_anchor_{uuid.uuid4().hex[:12]}"
                for message in reversed(session.messages):
                    if is_internal_message(message):
                        continue
                    metadata = dict(message.get("metadata") or {})
                    anchor_ids = list(metadata.get("compactionAnchorIds") or [])
                    anchor_ids.append(anchor_id)
                    metadata["compactionAnchorIds"] = anchor_ids
                    message["metadata"] = metadata
                    break
                runtime._record_activity(
                    session,
                    "compaction",
                    None,
                    {
                        "manual": True,
                        "anchorMessageId": anchor_id,
                        "oldMessages": result.old_messages,
                        "recentMessages": result.recent_messages,
                        "oldTokens": result.old_tokens,
                        "recentTokens": result.recent_tokens,
                        "chunks": result.chunks,
                        "summaryPreview": summary_preview,
                        "summaryVersion": result.summary_version,
                        "coveredMessageStart": result.covered_message_start,
                        "coveredMessageEnd": result.covered_message_end,
                        "qualityWarnings": list(result.quality_warnings),
                    },
                )
                runtime.store.save(session)
            return {
                "sessionId": session_id,
                "compacted": True,
                "oldMessages": result.old_messages,
                "recentMessages": result.recent_messages,
                "chunks": result.chunks,
                "summaryVersion": result.summary_version,
                "summaryPreview": summary_preview,
                "qualityWarnings": list(result.quality_warnings),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CompactionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/chat")
    def chat(request: ChatRequest):
        session_id = request.sessionId or runtime.store.current_id
        try:
            runtime.store.get(session_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            with locks[session_id]:
                result = (
                    runtime.run_skill(request.skillName, request.message, session_id)
                    if request.skillName
                    else runtime.run(request.message, session_id)
                )
            return {
                "status": result.status,
                "sessionId": result.session_id,
                "reply": _append_approval_retry_hint(result.reply, result.tool_events),
                "toolEvents": result.tool_events,
                "pendingApprovals": result.pending_approvals,
                "compaction": result.compaction.__dict__ if result.compaction else None,
                "compactionError": result.compaction_error,
            }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Agent 调用失败：{exc}") from exc

    @app.post("/api/chat/stream")
    def chat_stream(request: ChatRequest):
        session_id = request.sessionId or runtime.store.current_id
        try:
            runtime.store.get(session_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        actual_turn_id = request.turnId or f"turn_{uuid.uuid4().hex[:12]}"
        cancellation = Event()
        run_id = register_active_turn(actual_turn_id, session_id, cancellation, "chat")

        def operation(emit):
            with locks[session_id]:
                if request.skillName:
                    return runtime.run_skill(
                        request.skillName, request.message, session_id,
                        source="web", event_callback=emit,
                        turn_id=actual_turn_id, cancellation_event=cancellation,
                    )
                return runtime.run(
                    request.message, session_id, emit, actual_turn_id, cancellation,
                )

        def done_payload(result):
            return {
                "status": result.status,
                "sessionId": result.session_id,
                "reply": _append_approval_retry_hint(result.reply, result.tool_events),
                "pendingApprovals": result.pending_approvals,
                "compactionError": result.compaction_error,
                "metrics": result.metrics,
            }

        return StreamingResponse(
            stream_turn_operation(
                actual_turn_id, run_id, cancellation, f"chat-{session_id}",
                operation, done_payload, "Agent 执行失败：",
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                "Connection": "keep-alive", "X-SJTUClaw-Turn-Id": actual_turn_id,
            },
        )

    @app.post("/api/chat/replay/stream")
    def chat_replay_stream(request: ReplayChatRequest):
        session_id = request.sessionId or runtime.store.current_id
        try:
            runtime.store.get(session_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        actual_turn_id = request.turnId or f"turn_{uuid.uuid4().hex[:12]}"
        cancellation = Event()
        run_id = register_active_turn(actual_turn_id, session_id, cancellation, "replay")

        def operation(emit):
            with locks[session_id]:
                return runtime.replay(
                    session_id, request.messageIndex, request.message,
                    emit, actual_turn_id, cancellation,
                )

        def done_payload(result):
            return {
                "status": result.status,
                "sessionId": result.session_id,
                "reply": result.reply,
                "pendingApprovals": result.pending_approvals,
                "compactionError": result.compaction_error,
                "metrics": result.metrics,
            }

        return StreamingResponse(
            stream_turn_operation(
                actual_turn_id, run_id, cancellation, f"chat-replay-{session_id}",
                operation, done_payload, "Agent 执行失败：",
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                "Connection": "keep-alive", "X-SJTUClaw-Turn-Id": actual_turn_id,
            },
        )

    @app.get("/api/turns/active")
    def list_active_turns(sessionId: str | None = None):
        return {"turns": active_turn_snapshot(sessionId)}

    @app.get("/api/turns/history")
    def list_turn_history(sessionId: str | None = None, limit: int = 50):
        """Return durable turn checkpoints for diagnostics and reconnect UI."""
        return {"turns": turn_store.list_recent(sessionId, limit)}

    @app.get("/api/turns/{turn_id}")
    def get_turn_checkpoint(turn_id: str):
        record = turn_store.get(turn_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Turn 不存在")
        return record

    @app.get("/api/turns/{turn_id}/events")
    def get_turn_events(turn_id: str, after: int = 0):
        if turn_store.get(turn_id) is None:
            raise HTTPException(status_code=404, detail="Turn 不存在")
        return {"turnId": turn_id, "events": turn_store.events_after(turn_id, after)}

    @app.get("/api/turns/{turn_id}/trace")
    def get_turn_trace(turn_id: str):
        """Return the model/tool/approval parent-child trace for diagnostics."""
        if turn_store.get(turn_id) is None:
            raise HTTPException(status_code=404, detail="Turn not found")
        return turn_store.trace_tree(turn_id)

    @app.get("/api/turns/{turn_id}/stream")
    def stream_active_turn(turn_id: str, after: int = 0):
        queue, cached, live = subscribe_turn(turn_id, max(0, after))

        def event_stream():
            try:
                for _, event_name, payload in cached:
                    yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if not live:
                    return
                while True:
                    try:
                        event_name, payload = queue.get(timeout=15)
                    except Empty:
                        yield ": keep-alive\n\n"
                        continue
                    if event_name == "_finished":
                        break
                    yield f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                unsubscribe_turn(turn_id, queue)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    def request_turn_cancel(turn_id: str, mode: str) -> dict:
        """Persist cancellation first, then signal a live worker if present."""
        record = turn_store.get(turn_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Turn \u4e0d\u5b58\u5728")
        if record.get("status") in {"completed", "cancelled", "error", "failed", "interrupted"}:
            return {
                "turnId": turn_id, "cancelRequested": False,
                "alreadyFinished": True, "status": record.get("status"),
                "phase": record.get("phase"),
            }
        record = turn_store.request_cancel(turn_id, "\u6b63\u5728\u53d6\u6d88")
        with active_turns_lock:
            item = active_turns.get(turn_id)
            already_requested = bool(item and item["cancellation"].is_set())
            if item is not None and not already_requested:
                item["cancelMode"] = mode
                item["cancelRequestedAt"] = record.get("cancel_requested_at") or now_iso()
                item["cancellation"].set()
            elif item is not None:
                mode = item.get("cancelMode") or mode
        if item is not None:
            update_active_turn(turn_id, "cancelling", "\u6b63\u5728\u53d6\u6d88")
            return {
                "turnId": turn_id, "cancelRequested": True,
                "alreadyRequested": already_requested, "mode": mode,
                "status": "cancelling",
            }

        # A suspended approval has no worker. Cancelling it is immediate and
        # must also close every not-yet-executing approval card for the Turn.
        approval_store.cancel_pending_for_turn(turn_id, "Turn \u5df2\u53d6\u6d88\uff0c\u672a\u6267行\u8be5\u5ba1\u6279")
        seq = int(record.get("last_seq", 0) or 0) + 1
        payload = {"turnId": turn_id, "status": "cancelled",
                   "message": "\u672c\u8f6e\u5df2\u53d6\u6d88", "eventSeq": seq}
        turn_store.append_event(turn_id, seq, "cancelled", payload)
        turn_store.finish(turn_id, "cancelled", message=payload["message"])
        return {
            "turnId": turn_id, "cancelRequested": True,
            "alreadyRequested": bool(record.get("cancel_requested_at")),
            "mode": mode, "status": "cancelled",
        }

    @app.post("/api/turns/{turn_id}/cancel")
    def cancel_turn(turn_id: str, request: CancelTurnRequest | None = None):
        return request_turn_cancel(turn_id, request.mode if request else "graceful")

    @app.post("/api/sessions/{session_id}/turn/cancel")
    def cancel_session_turns(session_id: str, request: CancelTurnRequest | None = None):
        """Cancel live and approval-suspended Turns for one Session."""
        try:
            runtime.store.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        mode = request.mode if request is not None else "immediate"
        records = turn_store.list_active(session_id)
        results = [request_turn_cancel(row["turn_id"], mode) for row in records]
        return {
            "sessionId": session_id, "cancelRequested": bool(results),
            "turnIds": [item["turnId"] for item in results],
            "mode": mode, "results": results,
        }

    @app.get("/api/sessions/{session_id}/attachments")
    def list_attachments(session_id: str):
        try:
            return {"sessionId": session_id, "attachments": attachments.list(session_id)}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/sessions/{session_id}/attachments", status_code=201)
    def upload_attachment(session_id: str, file: UploadFile = File(...)):
        try:
            with locks[session_id]:
                metadata = attachments.save(
                    session_id,
                    file.filename or "attachment",
                    file.content_type,
                    file.file,
                )
            return {"sessionId": session_id, "attachment": metadata}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            file.file.close()

    @app.delete("/api/sessions/{session_id}/attachments/{attachment_id}")
    def delete_attachment(session_id: str, attachment_id: str):
        try:
            with locks[session_id]:
                result = attachments.delete(session_id, attachment_id, delete_file=True)
            return {"sessionId": session_id, **result}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除附件失败：{exc}") from exc

    @app.get("/api/sessions/{session_id}/attachments/{attachment_id}/preview")
    def preview_session_attachment(session_id: str, attachment_id: str):
        try:
            metadata, path = attachments.get(session_id, attachment_id)
            base = f"/api/sessions/{session_id}/attachments/{attachment_id}"
            return preview_attachment(path, metadata, f"{base}/raw", f"{base}/content")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}/attachments/{attachment_id}/raw")
    def raw_session_attachment(session_id: str, attachment_id: str):
        try:
            metadata, path = attachments.get(session_id, attachment_id)
            media_type = inline_media_type(str(metadata.get("filename") or ""))
            if media_type is None:
                raise HTTPException(status_code=415, detail="该附件不允许内嵌预览。")
            return FileResponse(
                path, media_type=media_type, filename=metadata["filename"],
                content_disposition_type="inline",
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}/attachments/{attachment_id}/pdf-pages")
    def preview_session_pdf_pages(session_id: str, attachment_id: str):
        try:
            metadata, path = attachments.get(session_id, attachment_id)
            filename = str(metadata.get("filename") or "")
            if not filename.lower().endswith(".pdf"):
                raise HTTPException(status_code=415, detail="该附件不是 PDF。")
            return {
                "attachmentId": attachment_id,
                "filename": filename,
                **render_pdf_preview_pages(path),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}/attachments/{attachment_id}/content")
    def download_session_attachment(session_id: str, attachment_id: str):
        try:
            metadata, path = attachments.get(session_id, attachment_id)
            return FileResponse(
                path, media_type="application/octet-stream", filename=metadata["filename"],
                content_disposition_type="attachment",
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc

    @app.get("/api/attachments/audit")
    def audit_attachments():
        return attachments.audit(cleanup_orphans=False)

    @app.post("/api/attachments/cleanup")
    def cleanup_attachments():
        return attachments.audit(cleanup_orphans=True)

    @app.get("/api/sessions/{session_id}/workspace")
    def get_workspace(session_id: str):
        try:
            return workspace_manager.inspect(session_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/sessions/{session_id}/workspace/migrate")
    def migrate_workspace(session_id: str):
        try:
            return {"migrated": True, **workspace_manager.migrate(session_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/sessions/{session_id}/workspace")
    def set_workspace(session_id: str, request: WorkspaceRequest):
        try:
            return {
                "sessionId": session_id,
                "workspace": workspace_manager.set(session_id, request.path),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/sessions/{session_id}/workspace/pick")
    def pick_workspace(session_id: str, request: Request):
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(status_code=403, detail="目录选择器仅允许本机访问。")
        try:
            session = runtime.store.get(session_id)
            selected = choose_directory(session.workspace)
            if not selected:
                return {"sessionId": session_id, "workspace": session.workspace, "cancelled": True}
            return {
                "sessionId": session_id,
                "workspace": workspace_manager.set(session_id, selected),
                "cancelled": False,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/workspace/pick")
    def pick_draft_workspace(payload: DraftWorkspacePickRequest, request: Request):
        """Choose a Workspace for a browser draft without creating a Session."""
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise HTTPException(status_code=403, detail="目录选择器仅允许本机访问。")
        try:
            selected = choose_directory(payload.initialPath)
            if not selected:
                return {"workspace": payload.initialPath, "cancelled": True}
            target = Path(selected).expanduser().resolve()
            if not target.exists():
                raise FileNotFoundError(f"Workspace 不存在：{target}")
            if not target.is_dir():
                raise NotADirectoryError(f"Workspace 不是目录：{target}")
            return {"workspace": str(target), "cancelled": False}
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/approvals")
    def list_approvals(sessionId: str | None = None, status: str | None = None):
        try:
            if sessionId:
                runtime.store.get(sessionId)
            resolving = {
                turn["approvalId"]: turn
                for turn in active_turn_snapshot(sessionId)
                if turn.get("approvalId")
            }
            items = approval_store.list(sessionId, status)
            return {
                "approvals": [
                    item.to_dict() for item in items if item.approval_id not in resolving
                ],
                "resolvingApprovals": list(resolving.values()),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/approvals/{approval_id}/retry")
    def retry_approval(approval_id: str):
        """Create a new approval after an approved tool execution failed.

        The original approval remains an immutable idempotency boundary.  A
        retry always receives a new approval id, so a stale browser card can
        never execute a side-effecting tool twice.
        """
        try:
            original = approval_store.get(approval_id)
            with locks[original.session_id]:
                retry = runtime.retry_approval(approval_id)
            return {
                "status": "approval_required",
                "sessionId": retry.session_id,
                "approval": retry.to_dict(),
                "pendingApprovals": [retry.to_dict()],
                "retryOf": approval_id,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Approval retry failed: {exc}") from exc

    @app.get("/api/memory-candidates")
    def list_memory_candidates(sessionId: str | None = None, status: str | None = "pending"):
        if memory_candidate_store is None:
            return {"candidates": []}
        if status not in {None, "pending", "accepted", "rejected"}:
            raise HTTPException(status_code=400, detail="无效的候选记忆状态。")
        try:
            if sessionId:
                runtime.store.get(sessionId)
            return {"candidates": [
                item.to_dict() for item in memory_candidate_store.list(status, sessionId)
            ]}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/memory-candidates/{candidate_id}/decision")
    def decide_memory_candidate(candidate_id: str, request: MemoryCandidateDecisionRequest):
        if memory_candidate_store is None:
            raise HTTPException(status_code=503, detail="候选记忆服务未配置。")
        try:
            candidate, memory = memory_candidate_store.resolve(candidate_id, request.accepted)
            return {
                "candidate": candidate.to_dict(),
                "memory": memory.to_dict() if memory else None,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.patch("/api/memory-candidates/{candidate_id}")
    def update_memory_candidate(candidate_id: str, request: MemoryCandidateUpdateRequest):
        if memory_candidate_store is None:
            raise HTTPException(status_code=503, detail="候选记忆服务未配置。")
        try:
            return memory_candidate_store.update(
                candidate_id, request.content, request.type
            ).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/approvals/{approval_id}/decision")
    def decide_approval(approval_id: str, request: ApprovalDecisionRequest):
        try:
            approval = approval_store.get(approval_id)
            with locks[approval.session_id]:
                result = runtime.resolve_approval(
                    approval_id, request.approved, request.reason
                )
            return {
                "status": result.status,
                "sessionId": result.session_id,
                "reply": result.reply,
                "toolEvents": result.tool_events,
                "pendingApprovals": result.pending_approvals,
                "alreadyResolved": result.already_resolved,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Approval 执行失败：{exc}") from exc

    @app.post("/api/approvals/{approval_id}/decision/stream")
    def decide_approval_stream(approval_id: str, request: ApprovalDecisionRequest):
        try:
            approval = approval_store.get(approval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        existing = active_turn_for_approval(approval_id)
        if existing is not None:
            raise HTTPException(status_code=409, detail={
                "code": "approval_in_progress",
                "message": "该审批正在执行，请恢复现有 Turn。",
                "activeTurn": existing,
            })

        actual_turn_id = approval.turn_id or request.turnId or f"turn_{uuid.uuid4().hex[:12]}"
        if approval.status != "pending":
            # Double clicks and browser retries only reconcile durable state;
            # they must never execute an approved side effect twice.
            def resolved_stream():
                resolved_payload = {
                    "approvalId": approval.approval_id,
                    "decision": approval.decision or approval.status,
                    "executionStatus": approval.execution_status,
                    "reason": approval.reason,
                    "toolResult": approval.result,
                    "turnId": actual_turn_id,
                    "alreadyResolved": True,
                }
                yield "event: approval_result\ndata: " + json.dumps(resolved_payload, ensure_ascii=False) + "\n\n"
                done_payload = {
                    "status": "failed" if approval.status == "interrupted" else "completed",
                    "sessionId": approval.session_id,
                    "reply": None,
                    "pendingApprovals": [],
                    "alreadyResolved": True,
                    "turnId": actual_turn_id,
                }
                yield "event: done\ndata: " + json.dumps(done_payload, ensure_ascii=False) + "\n\n"

            return StreamingResponse(
                resolved_stream(), media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                    "X-SJTUClaw-Turn-Id": actual_turn_id,
                },
            )

        cancellation = Event()
        run_id = register_active_turn(
            actual_turn_id, approval.session_id, cancellation, "approval",
            approval_id=approval_id,
        )

        def operation(emit):
            with locks[approval.session_id]:
                return runtime.resolve_approval(
                    approval_id, request.approved, request.reason,
                    emit, actual_turn_id, cancellation,
                )

        def done_payload(result):
            return {
                "status": result.status,
                "sessionId": result.session_id,
                "reply": result.reply,
                "pendingApprovals": result.pending_approvals,
                "alreadyResolved": result.already_resolved,
            }

        return StreamingResponse(
            stream_turn_operation(
                actual_turn_id, run_id, cancellation, f"approval-{approval_id}",
                operation, done_payload, "审批后执行失败：",
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                "Connection": "keep-alive", "X-SJTUClaw-Turn-Id": actual_turn_id,
            },
        )

    @app.get("/api/downloads/{download_id}")
    def download(download_id: str):
        try:
            item = download_store.resolve(download_id)
            return FileResponse(item["path"], filename=item["filename"])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc

    @app.get("/api/skills")
    def list_skills():
        if skill_registry is None:
            return {"skills": []}
        return {
            "skills": [
                item.basic_info(skill_registry.diagnose(item.name))
                for item in skill_registry.list()
            ]
        }

    @app.get("/api/skills/doctor")
    def diagnose_skills():
        if skill_registry is None:
            return {"skills": []}
        return {
            "skills": [
                {
                    "name": item.name,
                    "health": skill_registry.diagnose(item.name),
                }
                for item in skill_registry.list()
            ]
        }

    @app.get("/api/skills/{skill_name}")
    def get_skill(skill_name: str):
        if skill_registry is None:
            raise HTTPException(status_code=404, detail="Skill System 未配置。")
        try:
            item = skill_registry.get(skill_name)
            return item.basic_info(skill_registry.diagnose(skill_name))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/skills/{skill_name}/doctor")
    def diagnose_skill(skill_name: str):
        if skill_registry is None:
            raise HTTPException(status_code=404, detail="Skill System 未配置。")
        try:
            return {
                "name": skill_name,
                "health": skill_registry.diagnose(skill_name),
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}/skill-usage")
    def get_skill_usage(session_id: str):
        try:
            session = runtime.store.get(session_id)
            return {"sessionId": session_id, "usage": session.skill_usage}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/skills/{skill_name}/run")
    def run_skill(skill_name: str, request: SkillRunRequest):
        try:
            with locks[request.sessionId]:
                result = runtime.run_skill(skill_name, request.task, request.sessionId)
            return {
                "status": result.status,
                "sessionId": result.session_id,
                "reply": result.reply,
                "toolEvents": result.tool_events,
                "pendingApprovals": result.pending_approvals,
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Skill 执行失败：{exc}") from exc

    @app.get("/api/tasks")
    def list_tasks(sessionId: str | None = None):
        try:
            if sessionId is not None:
                runtime.store.get(sessionId)
            return {"tasks": [task.to_dict() for task in task_store.list(sessionId)]}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str):
        try:
            return task_store.get(task_id).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/tasks", status_code=201)
    def create_task(request: CreateTaskRequest):
        try:
            if request.taskType == "once":
                if request.runAt is None:
                    raise ValueError("一次性任务必须提供 runAt。")
                task = task_store.create_once(
                    request.content, request.sessionId, request.runAt,
                    execution_context=request.executionContext,
                    delivery_mode=request.deliveryMode,
                    delivery_channel=request.deliveryChannel,
                    delivery_channels=request.deliveryChannels,
                )
            elif request.taskType == "interval":
                if request.intervalSeconds is None:
                    raise ValueError("周期任务必须提供 intervalSeconds。")
                task = task_store.create_interval(
                    request.content,
                    request.sessionId,
                    request.intervalSeconds,
                    request.startAt or request.runAt,
                    request.endAt,
                    request.maxRuns,
                    execution_context=request.executionContext,
                    delivery_mode=request.deliveryMode,
                    delivery_channel=request.deliveryChannel,
                    delivery_channels=request.deliveryChannels,
                )
            else:
                if not request.cronExpression:
                    raise ValueError("Cron 任务必须提供 cronExpression。")
                task = task_store.create_cron(
                    request.content,
                    request.sessionId,
                    request.cronExpression,
                    request.startAt,
                    request.endAt,
                    request.maxRuns,
                    request.timezone,
                    execution_context=request.executionContext,
                    delivery_mode=request.deliveryMode,
                    delivery_channel=request.deliveryChannel,
                    delivery_channels=request.deliveryChannels,
                )
            return task.to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str):
        try:
            return task_store.cancel(task_id).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/tasks/{task_id}/pause")
    def pause_task(task_id: str):
        try:
            return task_store.pause(task_id).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/tasks/{task_id}/resume")
    def resume_task(task_id: str):
        try:
            return task_store.resume(task_id).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/tasks/{task_id}/run", status_code=202)
    def run_task_now(task_id: str):
        try:
            return scheduler_service.run_task_now(task_id).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/events/stream")
    async def gateway_event_stream(request: Request):
        """Fan session, task and approval updates into one browser event stream.

        Stores remain the source of truth.  The compact snapshots make this
        endpoint useful to every producer (Web, Scheduler and external
        channels) without coupling those producers to browser-specific code.
        """

        async def stream():
            previous: dict[str, str] = {}
            sequence = 0
            heartbeat = 0
            yield "retry: 2000\n\n"
            try:
                while (
                    not shutdown_requested.is_set()
                    and not await request.is_disconnected()
                ):
                    snapshots = {
                        "sessions": list_sessions(),
                        "tasks": list_tasks(),
                        "approvals": list_approvals(status="pending"),
                    }
                    for event_name, payload in snapshots.items():
                        signature = json.dumps(
                            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        )
                        if previous.get(event_name) == signature:
                            continue
                        previous[event_name] = signature
                        sequence += 1
                        yield (
                            f"id: {sequence}\n"
                            f"event: {event_name}\n"
                            f"data: {signature}\n\n"
                        )
                    heartbeat += 1
                    if heartbeat >= 15:
                        heartbeat = 0
                        yield ": keep-alive\n\n"
                    # Wake promptly when Ctrl+C asks the Gateway to close.
                    # A single one-second sleep here used to leave Uvicorn
                    # waiting on the long-lived SSE response.
                    for _ in range(10):
                        if shutdown_requested.is_set():
                            break
                        await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    static_dir = Path(web_dir) if web_dir is not None else BASE_DIR / "web"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")
    return app


app = create_app()


def run_gateway_server(server, app_instance=app, output_fn=print) -> None:
    """Run Uvicorn and treat Ctrl+C cancellation as a normal shutdown.

    Python 3.13 may surface ``CancelledError`` as the context of the
    ``KeyboardInterrupt`` raised by ``asyncio.run``.  Letting that exception
    escape prints a frightening traceback even though the Gateway has already
    stopped normally.
    """
    try:
        server.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        shutdown_event = getattr(getattr(app_instance, "state", None), "shutdown_requested", None)
        if shutdown_event is not None:
            shutdown_event.set()
    output_fn("Gateway 已关闭。")


if __name__ == "__main__":
    import uvicorn

    class GatewayServer(uvicorn.Server):
        """Close Gateway-owned streams before Uvicorn waits for connections."""

        def handle_exit(self, sig, frame):
            app.state.shutdown_requested.set()
            super().handle_exit(sig, frame)

    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
    run_gateway_server(GatewayServer(config))
