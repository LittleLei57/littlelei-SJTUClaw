"""Step 5 核心：CLI、Gateway 与 Scheduler 共用的 Agent Runtime。

``AgentRuntime`` 实现真正的内部 Agent Loop：构造上下文、调用 LLM、解析
Final/Tool Call、执行本轮 Tool、把 Tool Result 写回 Session，再次构造上下文，
直到得到 Final 或触及安全步数上限。它还统一处理 Approval 恢复、取消、流式事件、
执行证据、自动 Compaction 与失败回滚；所有入口因此复用同一套行为。
"""

from dataclasses import dataclass, field
from copy import deepcopy
import json
from pathlib import Path
from queue import Empty, Queue
import re
from threading import Lock, RLock, Thread
from typing import Callable, Protocol
import time
import uuid

from approval_store import Approval, ApprovalStore
from attachment_store import resolve_attachment_path
from compaction import CompactionError, CompactionResult, Compactor
from config import agent_max_steps
from context_builder import ContextBuilder, _selected_attachment_ids
from execution_evidence import (
    CAPABILITY_LABELS,
    TurnEvidence,
    is_continuation_message,
    repair_hint as execution_repair_hint,
    required_capabilities,
    validate_completion,
)
from gateway_security import redact_sensitive
from goal_state import is_terminal_reply, start_or_continue, update_goal
from llm_client import LLMCancelled, Message
from session_store import Session, SessionStore, utc_now
from task_planner import (
    ensure_plan,
    finalize_plan,
    normalize_plan,
    mark_tool as mark_plan_tool,
    sync_tool_events as sync_plan_tool_events,
)
from tool_protocol import (
    ModelAction,
    ProtocolError,
    ToolCall,
    extract_embedded_tool_action,
    parse_model_action,
    unwrap_final_content,
)
from tools import ToolExecutionContext, ToolRegistry, ToolResult
from vision_input import image_content_part, is_image_attachment


class ChatModel(Protocol):
    """Runtime 所需的同步模型接口。"""

    def complete(self, messages: list[Message]) -> str: ...


class AgentCancelled(RuntimeError):
    """用户请求取消当前 Agent Turn。"""

    pass


class EmptyAssistantResponse(RuntimeError):
    """模型连续返回空内容，无法形成有效动作。"""

    pass


class ModelOutputLimitError(RuntimeError):
    """The provider stopped generation because its output budget was reached.

    This is deliberately separate from ``ProtocolError``.  A response that
    ends with ``finish_reason=length`` is not malformed JSON in the usual
    sense; it is an incomplete response which should be retried with a larger
    budget (and, when possible, a compacted context) before it is shown to the
    user as a failure.
    """

    def __init__(
        self,
        finish_reason: str,
        *,
        output_tokens: int | None = None,
        partial_content: str = "",
        streamed: bool = False,
    ):
        self.finish_reason = finish_reason
        self.output_tokens = output_tokens
        self.partial_content = str(partial_content or "")
        self.streamed = bool(streamed)
        detail = f"model output limit reached ({finish_reason})"
        if output_tokens:
            detail += f" after about {output_tokens} tokens"
        super().__init__(detail)


@dataclass(frozen=True)
class AgentTurnResult:
    """一次完整 Turn 的最终回复、Tool 事件和压缩结果。"""

    reply: str | None
    session_id: str
    tool_events: list[dict] = field(default_factory=list)
    pending_approvals: list[dict] = field(default_factory=list)
    compaction: CompactionResult | None = None
    compaction_error: str | None = None
    turn_id: str | None = None
    metrics: list[dict] = field(default_factory=list)
    already_resolved: bool = False

    @property
    def status(self) -> str:
        return "approval_required" if self.pending_approvals else "completed"


class AgentRuntime:
    """编排 LLM、Tool、Session、Approval 与 Compaction 的共享运行时。"""

    # Bound automatic Skill repair attempts.  The initial attempt plus two
    # corrections is enough to fix a bad source URL without looping forever.
    MAX_INSTALL_SKILL_FAILURES = 3
    def __init__(
        self,
        model: ChatModel,
        store: SessionStore,
        context_builder: ContextBuilder | None = None,
        compactor: Compactor | None = None,
        tool_registry: ToolRegistry | None = None,
        approval_store: ApprovalStore | None = None,
        skill_service=None,
        max_agent_steps: int | None = None,
        strict_tool_protocol: bool = True,
    ):
        self.model = model
        self.store = store
        self.context_builder = context_builder or ContextBuilder()
        self.compactor = compactor
        self.tool_registry = tool_registry
        self.approval_store = approval_store
        self.skill_service = skill_service
        # Gateway/CLI pass False here by default.  The constructor keeps True
        # for backwards compatibility with embedders that relied on strict
        # protocol tests.  Some compatible providers mix a perfectly valid
        # answer with phrases such as “我先去查一下”; treating that prose as
        # a protocol failure can erase a useful answer and strand the turn.
        # Structured Tool JSON and native function calls remain fully supported
        # in relaxed mode.
        self.strict_tool_protocol = bool(strict_tool_protocol)
        configured_steps = agent_max_steps() if max_agent_steps is None else max_agent_steps
        if not isinstance(configured_steps, int) or isinstance(configured_steps, bool) or configured_steps < 1:
            raise ValueError("max_agent_steps 必须是正整数。")
        self.max_agent_steps = configured_steps
        self.last_compaction: CompactionResult | None = None
        self.last_compaction_error: str | None = None
        self.last_tool_events: list[dict] = []
        # Integrations such as Scheduler can observe an approval resolution
        # without making the Agent Loop or approval store depend on them.
        self.approval_result_callback = None
        self._session_locks: dict[str, RLock] = {}
        self._session_locks_guard = Lock()

    @property
    def current_session(self) -> Session:
        return self.store.current

    def select_model(self, model: str) -> dict:
        """Switch the shared Runtime model and persist the selection."""
        selector = getattr(self.model, "select_model", None)
        if not callable(selector):
            raise RuntimeError("当前模型客户端不支持运行时切换。")
        profile = selector(model)
        selection_store = getattr(self, "model_selection_store", None)
        if selection_store is not None:
            selection_store.select(profile["model"])
        return profile

    def _approval_state_hint(
        self,
        session_id: str,
        user_message: str | None,
    ) -> str | None:
        """Return an authoritative, ephemeral Approval snapshot.

        A Tool whose policy is ``approval_required`` is not necessarily
        waiting for approval. Only pending ApprovalStore rows represent live
        requests. Keeping this distinction in Runtime prevents the model from
        reconstructing status from old prose or historical Tool cards.
        """
        text = str(user_message or "")
        if not re.search(r"审批|批准|待审批|approve|approval", text, re.I):
            return None

        pending: list[Approval] = []
        recent: list[Approval] = []
        if self.approval_store is not None:
            pending = self.approval_store.list(
                session_id=session_id, status="pending"
            )
            recent = [
                item
                for item in self.approval_store.list(session_id=session_id)
                if item.status != "pending"
            ][:8]

        approval_tools: list[str] = []
        if self.tool_registry is not None:
            approval_tools = sorted(
                item["name"]
                for item in self.tool_registry.definitions()
                if item.get("safety_level") == "approval_required"
            )

        snapshot = {
            "pendingCount": len(pending),
            "pending": [
                {
                    "approvalId": item.approval_id,
                    "tool": item.tool,
                    "status": item.status,
                    "attempt": item.attempt,
                }
                for item in pending
            ],
            "recentResolved": [
                {
                    "approvalId": item.approval_id,
                    "tool": item.tool,
                    "status": item.status,
                    "executionSuccess": (
                        item.result.get("success")
                        if isinstance(item.result, dict)
                        else None
                    ),
                }
                for item in recent
            ],
            "toolsRequiringApprovalWhenCalled": approval_tools,
        }
        return (
            "下面是 Runtime 直接提供的权威审批快照。回答审批状态时只使用该快照，"
            "不要根据旧对话、Tool 卡片或记忆猜测，也不要调用无关 Tool 来核实。\n"
            "`toolsRequiringApprovalWhenCalled` 表示这些 Tool 被实际调用时需要确认，"
            "不表示它们当前处于 pending；只有 `pending` 数组中的记录才正在等待用户审批。\n"
            + json.dumps(snapshot, ensure_ascii=False, indent=2)
        )

    def run(
        self,
        user_message: str,
        session_id: str | None = None,
        event_callback: Callable[[dict], None] | None = None,
        turn_id: str | None = None,
        cancellation_event=None,
        make_current: bool = True,
        source: str = "web",
        scheduler_context: dict | None = None,
    ) -> AgentTurnResult:
        target_session_id = session_id or self.store.current_id
        with self._session_lock(target_session_id):
            return self._run_unlocked(
                user_message,
                target_session_id,
                event_callback,
                turn_id,
                cancellation_event,
                make_current,
                source,
                scheduler_context,
            )

    def replay(
        self,
        session_id: str,
        message_index: int,
        replacement_message: str | None = None,
        event_callback: Callable[[dict], None] | None = None,
        turn_id: str | None = None,
        cancellation_event=None,
        make_current: bool = True,
        source: str = "web",
    ) -> AgentTurnResult:
        """Regenerate from a previous user turn, optionally editing that turn."""
        with self._session_lock(session_id):
            session = self.store.get(session_id)
            if message_index < 0 or message_index >= len(session.messages):
                raise ValueError("Message index out of range.")
            original = session.messages[message_index]
            if original.get("role") != "user":
                raise ValueError("Only user messages can be replayed.")
            message = (
                replacement_message
                if replacement_message is not None
                else original.get("content", "")
            )
            if not isinstance(message, str) or not message.strip():
                raise ValueError("Replay message cannot be empty.")
            original_message_count = len(session.messages)
            session.messages = session.messages[:message_index]
            self._record_activity(
                session,
                "replay_started",
                turn_id,
                {
                    "messageIndex": message_index,
                    "edited": replacement_message is not None,
                    "removedMessages": max(0, original_message_count - message_index),
                },
            )
            session.updated_at = utc_now()
            self.store.save(session)
            return self._run_unlocked(
                message,
                session_id,
                event_callback,
                turn_id,
                cancellation_event,
                make_current,
                source,
            )

    def _run_unlocked(
        self,
        user_message: str,
        session_id: str | None = None,
        event_callback: Callable[[dict], None] | None = None,
        turn_id: str | None = None,
        cancellation_event=None,
        make_current: bool = True,
        source: str = "web",
        scheduler_context: dict | None = None,
    ) -> AgentTurnResult:
        message = user_message.strip()
        if not message:
            raise ValueError("消息不能为空。")
        session = self.store.get(session_id) if session_id else self.store.current
        if make_current and session.session_id != self.store.current_id:
            self.store.set_current_id(session.session_id)
        actual_turn_id = turn_id or f"turn_{uuid.uuid4().hex[:12]}"
        self._check_cancelled(cancellation_event)
        # The in-flight user message is persisted so refresh/restart can recover
        # the active Turn.  If the Turn terminates without a final answer or an
        # approval suspension, roll the conversational state back atomically;
        # the durable Turn/activity audit still records the failure.
        message_checkpoint = len(session.messages)
        goal_checkpoint = deepcopy(getattr(session, "goal_state", None))
        self._record_activity(
            session,
            "turn_started",
            actual_turn_id,
            {"message": message[:500]},
        )
        session.messages.append(self._user_message(message, source))
        self._start_goal_state(session, message, source, actual_turn_id, event_callback)
        session.updated_at = utc_now()
        self.store.save(session)
        self._emit(event_callback, "status", phase="analyzing", message="正在分析请求")
        tool_events: list[dict] = []
        try:
            if self.tool_registry is None:
                reply = self._send_without_tools(
                    session, message, event_callback, actual_turn_id, cancellation_event, source,
                    user_committed=True,
                    scheduler_context=scheduler_context,
                )
                approvals: list[Approval] = []
            else:
                reply, approvals = self._run_agent_loop(
                    session,
                    message,
                    tool_events,
                    event_callback,
                    actual_turn_id,
                    cancellation_event,
                    source,
                    user_committed=True,
                    scheduler_context=scheduler_context,
                )
        except AgentCancelled:
            # CLI Ctrl+C means discard the unfinished input.  In graphical and
            # channel surfaces the sent user message remains visible, matching
            # familiar chat-stop semantics and allowing an immediate retry.
            if source == "cli":
                session.messages = session.messages[:message_checkpoint]
                session.goal_state = goal_checkpoint
            self._abort_goal_state(
                session, actual_turn_id, "cancelled", "本轮已由用户取消", event_callback
            )
            self._record_activity(
                session, "turn_cancelled", actual_turn_id, {"reason": "user_requested"}
            )
            session.updated_at = utc_now()
            self.store.save(session)
            raise
        except BaseException as exc:
            session.messages = session.messages[:message_checkpoint]
            session.goal_state = goal_checkpoint
            self._abort_goal_state(
                session, actual_turn_id, "blocked", "本轮执行失败", event_callback
            )
            self._record_activity(
                session, "turn_failed", actual_turn_id,
                {"stage": "runtime", "error": str(exc)},
            )
            session.updated_at = utc_now()
            self.store.save(session)
            raise
        if approvals:
            self._mark_goal_waiting(session, actual_turn_id, approvals, event_callback)
            self._emit(
                event_callback,
                "status",
                phase="approval_required",
                message="等待用户审批",
            )
            self._record_activity(
                session,
                "turn_paused",
                actual_turn_id,
                {"reason": "approval_required"},
            )
            self.store.save(session)
            return self._result(
                session, reply, tool_events, approvals, turn_id=actual_turn_id
            )
        self._finalize_goal_state(session, reply, actual_turn_id, tool_events, event_callback)
        # Auto-compaction is a post-turn bookkeeping step.  The model answer
        # has already been emitted and persisted by the paths above, so a
        # summary-model call cannot interrupt or dilute the current answer.
        # It remains visible through the event stream for the next turn.
        compaction_estimate = None
        if self.compactor is not None:
            try:
                estimate = self.compactor.estimate(session)
                if estimate.get("shouldCompact"):
                    compaction_estimate = estimate
                    self._emit(
                        event_callback,
                        "compaction_started",
                        turnId=actual_turn_id,
                        sessionId=session.session_id,
                        oldMessages=estimate.get("oldMessages", 0),
                        recentMessages=estimate.get("recentMessages", 0),
                        oldTokens=estimate.get("oldTokens", 0),
                        recentTokens=estimate.get("recentTokens", 0),
                        maxTokens=estimate.get("maxTokens"),
                        chunks=estimate.get("estimatedChunks", 0),
                    )
            except (OSError, ValueError, TypeError):
                # Estimation is only UI telemetry; the real compaction path
                # below remains the source of truth.
                compaction_estimate = None
        compaction, compaction_error = self._try_auto_compaction(session)
        if compaction is not None:
            # Compaction is a user-observable state transition. In addition to
            # the persistent timeline record, emit it so every interactive
            # surface can show the result immediately instead of silently
            # shrinking the message count in the background.
            self._emit(
                event_callback,
                "compaction",
                turnId=actual_turn_id,
                sessionId=compaction.session_id,
                oldMessages=compaction.old_messages,
                recentMessages=compaction.recent_messages,
                oldTokens=compaction.old_tokens,
                recentTokens=compaction.recent_tokens,
                chunks=compaction.chunks,
                summaryPreview=compaction.preview,
                summaryVersion=compaction.summary_version,
                coveredMessageStart=compaction.covered_message_start,
                coveredMessageEnd=compaction.covered_message_end,
                qualityWarnings=list(compaction.quality_warnings),
            )
            self._record_activity(
                session,
                "compaction",
                actual_turn_id,
                {
                    "oldMessages": compaction.old_messages,
                    "recentMessages": compaction.recent_messages,
                    "oldTokens": compaction.old_tokens,
                    "recentTokens": compaction.recent_tokens,
                    "chunks": compaction.chunks,
                    "summaryPreview": compaction.preview,
                    "summaryVersion": compaction.summary_version,
                    "coveredMessageStart": compaction.covered_message_start,
                    "coveredMessageEnd": compaction.covered_message_end,
                    "qualityWarnings": list(compaction.quality_warnings),
                },
            )
        elif compaction_error and compaction_estimate is not None:
            self._emit(
                event_callback,
                "compaction_failed",
                turnId=actual_turn_id,
                sessionId=session.session_id,
                oldMessages=compaction_estimate.get("oldMessages", 0),
                recentMessages=compaction_estimate.get("recentMessages", 0),
                oldTokens=compaction_estimate.get("oldTokens", 0),
                recentTokens=compaction_estimate.get("recentTokens", 0),
                maxTokens=compaction_estimate.get("maxTokens"),
                chunks=compaction_estimate.get("estimatedChunks", 0),
                error=compaction_error,
            )
            self._record_activity(
                session,
                "compaction_failed",
                actual_turn_id,
                {
                    "oldMessages": compaction_estimate.get("oldMessages", 0),
                    "recentMessages": compaction_estimate.get("recentMessages", 0),
                    "oldTokens": compaction_estimate.get("oldTokens", 0),
                    "recentTokens": compaction_estimate.get("recentTokens", 0),
                    "maxTokens": compaction_estimate.get("maxTokens"),
                    "chunks": compaction_estimate.get("estimatedChunks", 0),
                    "error": compaction_error,
                },
            )
        self._record_activity(
            session,
            "turn_completed",
            actual_turn_id,
            {"compacted": compaction is not None, "compactionError": compaction_error},
        )
        self.store.save(session)
        candidate_store = getattr(self, "memory_candidate_store", None)
        if candidate_store is not None:
            try:
                candidates = candidate_store.propose_from_message(message, session.session_id)
                if candidates:
                    self._emit(
                        event_callback, "memory_candidates",
                        candidates=[item.to_dict() for item in candidates],
                    )
            except (OSError, ValueError):
                # Candidate extraction must never make an otherwise successful turn fail.
                pass
        self._emit(event_callback, "status", phase="completed", message="回答完成")
        return self._result(
            session,
            reply,
            tool_events,
            [],
            compaction,
            compaction_error,
            actual_turn_id,
        )

    def _emit_goal_state(self, session: Session, turn_id: str | None, event_callback=None) -> None:
        goal = getattr(session, "goal_state", None)
        if not goal:
            return
        self._record_activity(
            session,
            "goal_state",
            turn_id,
            {"goal": goal},
        )
        self._emit(event_callback, "goal_state", turnId=turn_id, sessionId=session.session_id, goal=goal)

    def _start_goal_state(self, session: Session, message: str, source: str,
                          turn_id: str | None, event_callback=None) -> None:
        existing = getattr(session, "goal_state", None)
        if is_continuation_message(message) and isinstance(existing, dict):
            goal = update_goal(
                existing,
                status="active",
                step="继续处理上一项尚未完成的动作",
                turn_id=turn_id,
            )
            if goal is not None:
                goal, _ = ensure_plan(
                    goal,
                    str(goal.get("objective") or message),
                    required_capabilities(message, goal),
                )
                session.goal_state = goal
                self._emit_goal_state(session, turn_id, event_callback)
                return
        goal, changed = start_or_continue(
            existing, message, source=source, turn_id=turn_id
        )
        if goal is None:
            session.goal_state = None
            return
        goal, plan_changed = ensure_plan(
            goal,
            message,
            required_capabilities(message, goal),
        )
        session.goal_state = goal
        if changed or plan_changed:
            self._record_activity(session, "goal_started", turn_id, {"goal": goal})
            self._emit_goal_state(session, turn_id, event_callback)
        elif goal.get("status") == "active":
            self._emit_goal_state(session, turn_id, event_callback)

    def _update_plan_tool(
        self,
        session: Session,
        turn_id: str | None,
        tool: str,
        state: str,
        event_callback=None,
        *,
        call_id: str | None = None,
        error: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        """Advance the public execution plan from observable Runtime events."""
        goal = getattr(session, "goal_state", None)
        if not isinstance(goal, dict):
            return
        goal, changed = mark_plan_tool(
            goal,
            tool,
            state=state,
            call_id=call_id,
            error=error,
            timestamp=timestamp,
        )
        if not changed:
            return
        session.goal_state = goal
        self._emit_goal_state(session, turn_id, event_callback)

    def _mark_goal_waiting(self, session: Session, turn_id: str | None,
                           approvals: list[Approval], event_callback=None) -> None:
        goal = update_goal(
            getattr(session, "goal_state", None),
            status="awaiting_approval",
            step="等待用户审批后继续执行",
            next_actions=[f"批准后继续执行：{item.tool}" for item in approvals],
            turn_id=turn_id,
        )
        if goal is not None:
            for approval in approvals:
                goal, _ = mark_plan_tool(
                    goal,
                    approval.tool,
                    state="approval",
                    call_id=approval.approval_id,
                )
            session.goal_state = goal
            self._emit_goal_state(session, turn_id, event_callback)

    def _abort_goal_state(
        self,
        session: Session,
        turn_id: str | None,
        status: str,
        step: str,
        event_callback=None,
    ) -> None:
        """Settle a visible plan when a Turn is cancelled or crashes."""
        current = getattr(session, "goal_state", None)
        if not isinstance(current, dict):
            return
        goal = update_goal(
            current,
            status=status,
            step=step,
            next_actions=[] if status == "cancelled" else ["检查失败原因后重试"],
            turn_id=turn_id,
        )
        if goal is None:
            return
        plan = normalize_plan(goal.get("plan"))
        if plan is not None:
            plan["status"] = status
            for item in plan.get("steps", []):
                if item.get("status") in {"pending", "in_progress", "awaiting_approval"}:
                    item["status"] = "cancelled" if status == "cancelled" else "blocked"
                    item["updatedAt"] = utc_now()
            plan["updatedAt"] = utc_now()
            goal["plan"] = plan
        session.goal_state = goal
        self._emit_goal_state(session, turn_id, event_callback)

    def _finalize_goal_state(self, session: Session, reply: str | None,
                             turn_id: str | None, tool_events: list[dict],
                             event_callback=None) -> None:
        if not getattr(session, "goal_state", None):
            return
        evidence = TurnEvidence.from_goal(session.goal_state).merge(
            TurnEvidence.from_events(tool_events)
        )
        goal, _ = sync_plan_tool_events(session.goal_state, tool_events)
        goal["executionEvidence"] = evidence.to_goal_dict()
        session.goal_state = goal
        requirements = required_capabilities(
            str(session.goal_state.get("objective") or ""),
            session.goal_state,
        )
        execution_verified = not requirements or requirements.issubset(
            evidence.successful_capabilities
        )
        terminal = is_terminal_reply(reply) and execution_verified
        if terminal:
            goal = update_goal(
                session.goal_state,
                status="completed",
                step="已生成本轮结果",
                completed="完成本轮请求并生成结果",
                next_actions=[],
                turn_id=turn_id,
            )
            if goal is not None:
                goal = finalize_plan(goal, success=True)
        else:
            missing = requirements - evidence.successful_capabilities
            missing_labels = [
                CAPABILITY_LABELS.get(item, item) for item in sorted(missing)
            ]
            goal = update_goal(
                session.goal_state,
                status="blocked",
                step="本轮尚未取得全部可验证执行结果",
                next_actions=(
                    [f"通过 Tool 完成：{item}" for item in missing_labels]
                    or ["检查错误原因后重试，或补充缺失信息"]
                ),
                turn_id=turn_id,
            )
            if goal is not None:
                goal = finalize_plan(
                    goal,
                    success=False,
                    missing_capabilities=missing,
                )
        if goal is not None:
            session.goal_state = goal
            self._emit_goal_state(session, turn_id, event_callback)

    def send(self, user_message: str) -> str:
        result = self.run(user_message, source="cli")
        self.last_tool_events = result.tool_events
        self.last_compaction = result.compaction
        self.last_compaction_error = result.compaction_error
        if result.pending_approvals:
            return f"等待用户审批：{', '.join(item['approvalId'] for item in result.pending_approvals)}"
        return result.reply or ""

    def run_skill(
        self,
        skill_name: str,
        task: str,
        session_id: str | None = None,
        source: str = "web",
        event_callback=None,
        turn_id: str | None = None,
        cancellation_event=None,
    ) -> AgentTurnResult:
        if self.skill_service is None:
            raise RuntimeError("Skill System 未配置。")
        target_id = session_id or self.store.current_id
        self.skill_service.activate(target_id, skill_name, task, "explicit")
        try:
            return self.run(
                task,
                target_id,
                event_callback=event_callback,
                turn_id=turn_id,
                cancellation_event=cancellation_event,
                source=source,
            )
        except Exception as exc:
            self.skill_service.fail_active(target_id, str(exc))
            raise

    def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        reason: str | None = None,
        event_callback=None,
        turn_id: str | None = None,
        cancellation_event=None,
    ) -> AgentTurnResult:
        if self.approval_store is None:
            raise RuntimeError("Approval 系统未配置。")
        approval = self.approval_store.get(approval_id)
        with self._session_lock(approval.session_id):
            result = self._resolve_approval_unlocked(
                approval_id,
                approved,
                reason,
                event_callback,
                turn_id,
                cancellation_event,
            )
        callback = getattr(self, "approval_result_callback", None)
        if callable(callback):
            try:
                callback(approval_id, result)
            except Exception:
                # A notification/integration hook must never turn a completed
                # approval into an API failure or strand the Agent response.
                pass
        return result

    def _retry_approval_legacy(self, approval_id: str, *, turn_id: str | None = None) -> Approval:
        """Create a fresh approval for a previously failed approval.

        Approval decisions are intentionally idempotent: clicking an old
        card must never execute a side-effecting tool twice.  A failed,
        approved operation can still be retried explicitly, but it receives a
        new id/card so every execution has a new user decision boundary.
        """
        if self.approval_store is None:
            raise RuntimeError("Approval 系统未配置。")
        original = self.approval_store.get(approval_id)
        if original.status == "pending":
            raise ValueError("该审批仍在等待决定，请直接批准或拒绝。")
        result = original.result if isinstance(original.result, dict) else {}
        if original.status != "approved" or result.get("success") is not False:
            raise ValueError("只有已批准但执行失败的审批可以重试。")
        if original.tool == "install_skill" and original.attempt >= self.MAX_INSTALL_SKILL_FAILURES:
            raise ValueError("install_skill 已达到重试上限，请修改 Skill 来源后再试。")
        with self._session_lock(original.session_id):
            retry = self.approval_store.create(
                f"retry_{uuid.uuid4().hex[:12]}",
                original.session_id,
                original.tool,
                dict(original.args),
                retry_of=original.approval_id,
                attempt=original.attempt + 1,
                turn_id=turn_id or original.turn_id,
                call_id=original.call_id,
            )
            session = self.store.get(original.session_id)
            session.messages.append({
                "role": "system",
                "content": "[approval_retry_required] " + json.dumps(
                    {
                        "approvalId": retry.approval_id,
                        "retryOf": original.approval_id,
                        "tool": original.tool,
                        "attempt": retry.attempt,
                    },
                    ensure_ascii=False,
                ),
                "metadata": {"internal": True, "kind": "approval_retry"},
            })
            self._record_activity(
                session,
                "approval_retry_required",
                turn_id,
                {
                    "approvalId": retry.approval_id,
                    "retryOf": original.approval_id,
                    "tool": original.tool,
                    "attempt": retry.attempt,
                },
            )
            session.updated_at = utc_now()
            self.store.save(session)
            return retry

    def retry_approval(self, approval_id: str) -> Approval:
        """Create a fresh approval for a previously failed execution.

        Approval decisions are intentionally idempotent: clicking an old card
        must never execute a side-effecting tool twice.  A retry therefore gets
        a new id and a new pending card, which is safe for channel clients that
        cannot reliably keep the original card interactive.
        """
        if self.approval_store is None:
            raise RuntimeError("Approval 系统未配置。")
        original = self.approval_store.get(approval_id)
        if original.status == "pending":
            return original
        if original.status != "approved" or not isinstance(original.result, dict):
            raise ValueError("只有已批准但执行失败的审批可以重试。")
        if original.result.get("success") is not False:
            raise ValueError("该审批没有可重试的失败结果。")
        # Keep retries explicit and bounded at the interaction layer.  The
        # model can still issue a corrected tool call after seeing the error.
        if original.attempt >= 3:
            raise ValueError("同一审批最多允许重试 2 次，请先修改参数后再发起新的操作。")
        with self._session_lock(original.session_id):
            retry = self.approval_store.create(
                f"batch_retry_{uuid.uuid4().hex[:12]}",
                original.session_id,
                original.tool,
                dict(original.args),
                retry_of=original.approval_id,
                attempt=original.attempt + 1,
                turn_id=original.turn_id,
                call_id=original.call_id,
            )
            session = self.store.get(original.session_id)
            session.messages.append({
                "role": "system",
                "content": "[approval_retry_required] " + json.dumps(
                    {
                        "approvalId": retry.approval_id,
                        "retryOf": original.approval_id,
                        "tool": retry.tool,
                        "args": retry.args,
                        "attempt": retry.attempt,
                    },
                    ensure_ascii=False,
                ),
                "metadata": {"internal": True, "kind": "approval_retry"},
            })
            self._record_activity(
                session,
                "approval_retry_required",
                None,
                {
                    "approvalId": retry.approval_id,
                    "retryOf": original.approval_id,
                    "tool": retry.tool,
                    "attempt": retry.attempt,
                },
            )
            session.updated_at = utc_now()
            self.store.save(session)
            return retry

    @staticmethod
    def _approval_resume_fallback(approval: Approval, result: ToolResult, exc: Exception) -> str:
        """Return a safe, actionable answer when the post-approval model call fails.

        Approval execution and the follow-up model call are two separate
        failure domains. A provider timeout/rate-limit must not turn an
        already-recorded tool result into an SSE error with no assistant
        message; the user should see what happened and be able to retry.
        """
        error_text = str(exc or "").lower()
        rate_limited = any(token in error_text for token in ("429", "rate limit", "限流", "too many requests"))
        if result.success:
            if rate_limited:
                detail = "工具已执行成功，但模型暂时触发了限流"
            else:
                detail = "工具已执行成功，但模型暂时无法继续生成说明"
            return (
                f"审批已完成：{approval.tool} 已执行成功。{detail}，工具结果已保存；"
                "稍后点击重答即可继续。"
            )
        detail = redact_sensitive(result.error or "未知错误")
        if len(detail) > 240:
            detail = detail[:240] + "…"
        return (
            f"审批已完成，但 {approval.tool} 执行失败：{detail}。"
            "请检查参数后重试。"
        )

    def _mark_approval_resume_blocked(
        self,
        session: Session,
        approval: Approval,
        result: ToolResult,
        exc: Exception,
        turn_id: str | None,
        event_callback=None,
    ) -> str:
        """Persist and emit a recoverable assistant fallback for approval resumes."""
        fallback = self._approval_resume_fallback(approval, result, exc)
        session.messages.append({"role": "assistant", "content": fallback})
        self._record_activity(
            session,
            "approval_resume_failed",
            turn_id,
            {
                "approvalId": approval.approval_id,
                "tool": approval.tool,
                "toolSuccess": result.success,
                "error": redact_sensitive(str(exc))[:500],
                "fallback": "approval_resume_error",
            },
        )
        self._record_activity(
            session,
            "assistant_final",
            turn_id,
            {"contentPreview": fallback, "fallback": "approval_resume_error"},
        )
        goal = update_goal(
            getattr(session, "goal_state", None),
            status="blocked",
            step="审批工具已处理，但模型暂时无法继续",
            next_actions=["稍后点击重答继续", "必要时修改参数后重新发起操作"],
            turn_id=turn_id,
        )
        if goal is not None:
            session.goal_state = goal
            self._emit_goal_state(session, turn_id, event_callback)
        session.updated_at = utc_now()
        self.store.save(session)
        self._emit(
            event_callback,
            "status",
            phase="approval_resume_failed",
            message="审批工具已完成，但模型暂时无法继续；结果已保存，可稍后重答",
            approvalId=approval.approval_id,
            tool=approval.tool,
        )
        # A provider may have emitted a provisional prefix before failing.
        # Clear that prefix before showing the durable fallback so the UI does
        # not concatenate half a response with the recovery message.
        self._emit(event_callback, "assistant_reset")
        self._emit_text(event_callback, fallback)
        self._emit(event_callback, "assistant_final", content=fallback)
        return fallback

    def _resolve_approval_unlocked(
        self,
        approval_id: str,
        approved: bool,
        reason: str | None = None,
        event_callback=None,
        turn_id: str | None = None,
        cancellation_event=None,
    ) -> AgentTurnResult:
        if self.approval_store is None or self.tool_registry is None:
            raise RuntimeError("Approval 系统未配置。")
        approval = self.approval_store.get(approval_id)
        if approval.status != "pending":
            # A repeated click or request replay is a no-op. An executing row
            # has already crossed the durable side-effect boundary.
            remaining = self.approval_store.pending_batch(approval.batch_id)
            return AgentTurnResult(
                reply=None,
                session_id=approval.session_id,
                pending_approvals=[item.to_dict() for item in remaining],
                turn_id=approval.turn_id or turn_id,
                already_resolved=True,
            )
        session = self.store.get(approval.session_id)
        try:
            self._check_cancelled(cancellation_event)
        except AgentCancelled:
            self._abort_goal_state(
                session,
                turn_id or approval.turn_id,
                "cancelled",
                "\u5ba1\u6279\u6062\u590d\u5df2\u7531\u7528\u6237\u53d6\u6d88",
                event_callback,
            )
            session.updated_at = utc_now()
            self.store.save(session)
            raise
        context = ToolExecutionContext(session.session_id)
        if approved:
            executor_token = turn_id or f"approval-run-{uuid.uuid4().hex}"
            approval, acquired = self.approval_store.claim(
                approval_id, executor_token
            )
            if not acquired:
                remaining = self.approval_store.pending_batch(approval.batch_id)
                return AgentTurnResult(
                    reply=None,
                    session_id=approval.session_id,
                    pending_approvals=[item.to_dict() for item in remaining],
                    turn_id=approval.turn_id or turn_id,
                    already_resolved=True,
                )
            try:
                result = self._run_cancellable(
                    lambda: self.tool_registry.execute(
                        approval.tool, approval.args, context
                    ),
                    cancellation_event,
                )
            except AgentCancelled:
                self.approval_store.interrupt_claim(
                    approval_id, executor_token,
                    "\u5de5\u5177\u6267\u884c\u671f\u95f4\u6536\u5230\u53d6\u6d88\u8bf7\u6c42\uff1b\u6267\u884c\u7ed3\u679c\u672a\u77e5\uff0c\u4e0d\u4f1a\u81ea\u52a8\u91cd\u8bd5\u3002",
                )
                self._abort_goal_state(
                    session,
                    turn_id or approval.turn_id,
                    "cancelled",
                    "\u5ba1\u6279\u5de5\u5177\u6267\u884c\u5df2\u7531\u7528\u6237\u53d6\u6d88\uff1b\u7ed3\u679c\u672a\u77e5",
                    event_callback,
                )
                session.updated_at = utc_now()
                self.store.save(session)
                raise
            except Exception as exc:
                result = ToolResult(
                    approval.tool, False, error=f"\u5de5\u5177\u6267\u884c\u5f02\u5e38\uff1a{exc}",
                    error_code="handler_error", retryable=False,
                )
        else:
            result = ToolResult(
                approval.tool, False,
                error="\u7528\u6237\u62d2\u7edd\u6267\u884c\uff1a" + (reason or "\u672a\u63d0\u4f9b\u539f\u56e0"),
                error_code="user_rejected",
            )
        result_dict = self._serialise_tool_result(session, result)
        resolved = self.approval_store.resolve(
            approval_id, approved, reason, result_dict
        )
        # Contextual handlers such as use_skill may persist Session state.
        session = self.store.get(approval.session_id)
        event = {
            "timestamp": utc_now(),
            "tool": approval.tool,
            "callId": approval_id,
            "args": approval.args,
            "approvalId": approval_id,
            "result": result_dict,
        }
        session.tool_trace.append(self._audit_tool_event(event))
        self._record_activity(
            session,
            "approval_resolved",
            turn_id,
            {
                "approvalId": approval_id,
                "tool": approval.tool,
                "decision": resolved.decision or resolved.status,
                "executionStatus": resolved.execution_status,
                "reason": reason,
                "success": result.success,
            },
        )
        self._record_activity(
            session,
            "tool_result",
            turn_id,
            {
                "tool": approval.tool,
                "callId": approval_id,
                "args": self._preview(approval.args),
                "success": result.success,
                "error": result.error,
                "errorCode": result.error_code,
                "retryable": result.retryable,
                "attempts": result.attempts,
                "durationMs": result.duration_ms,
                "outputPreview": self._preview(result.output),
            },
        )
        session.messages.append(
            {
                "role": "system",
                "content": "[approval_result] " + json.dumps(
                    {
                        "approvalId": approval_id,
                        "decision": resolved.decision or resolved.status,
                        "executionStatus": resolved.execution_status,
                        "reason": reason,
                        "toolResult": result_dict,
                    },
                    ensure_ascii=False,
                ),
                "metadata": {"internal": True, "kind": "approval_result"},
            }
        )
        session.updated_at = utc_now()
        self.store.save(session)
        self._emit(event_callback, "tool_result", **event)
        remaining = self.approval_store.pending_batch(approval.batch_id)
        if remaining:
            self._mark_goal_waiting(session, turn_id, remaining, event_callback)
            session.updated_at = utc_now()
            self.store.save(session)
            return self._result(session, None, [event], remaining)
        events = [event]
        # Approval pauses split one logical user turn into several Runtime
        # entries.  Rebuild the evidence recorded since the last genuine user
        # message so pre-approval read-only results remain valid even when the
        # task was too small to create a Planner goal.
        resume_evidence = TurnEvidence.from_goal(
            getattr(session, "goal_state", None)
        ).merge(self._approval_turn_evidence(session))
        try:
            reply, new_approvals = self._run_agent_loop(
                session,
                None,
                events,
                event_callback,
                turn_id,
                cancellation_event,
                evidence_seed=resume_evidence,
            )
        except AgentCancelled:
            # A user stop is not a model/provider failure. Preserve the
            # cancellation semantics so the UI can close the active turn.
            self._abort_goal_state(
                session,
                turn_id or approval.turn_id,
                "cancelled",
                "\u5ba1\u6279\u540e\u7684\u6a21\u578b\u7eed\u8dd1\u5df2\u7531\u7528\u6237\u53d6\u6d88",
                event_callback,
            )
            session.updated_at = utc_now()
            self.store.save(session)
            raise
        except Exception as exc:
            if self.skill_service is not None:
                try:
                    self.skill_service.fail_active(session.session_id, str(exc))
                except Exception:
                    pass
            # The approval itself has already been resolved and its Tool
            # Result is durable. Do not surface a transport-level SSE error
            # just because the follow-up model request (often a transient
            # 429) failed; persist a recoverable assistant answer instead.
            fallback = self._mark_approval_resume_blocked(
                session,
                approval,
                result,
                exc,
                turn_id,
                event_callback,
            )
            return self._result(session, fallback, events, [], turn_id=turn_id)
        if new_approvals:
            self._mark_goal_waiting(session, turn_id, new_approvals, event_callback)
            session.updated_at = utc_now()
            self.store.save(session)
            return self._result(session, reply, events, new_approvals)
        self._finalize_goal_state(session, reply, turn_id, events, event_callback)
        compaction_estimate = None
        if self.compactor is not None:
            try:
                estimate = self.compactor.estimate(session)
                if estimate.get("shouldCompact"):
                    compaction_estimate = estimate
                    self._emit(
                        event_callback,
                        "compaction_started",
                        turnId=turn_id,
                        sessionId=session.session_id,
                        oldMessages=estimate.get("oldMessages", 0),
                        recentMessages=estimate.get("recentMessages", 0),
                        oldTokens=estimate.get("oldTokens", 0),
                        recentTokens=estimate.get("recentTokens", 0),
                        maxTokens=estimate.get("maxTokens"),
                        chunks=estimate.get("estimatedChunks", 0),
                    )
            except (OSError, ValueError, TypeError):
                compaction_estimate = None
        compaction, error = self._try_auto_compaction(session)
        if compaction is not None:
            self._emit(
                event_callback,
                "compaction",
                turnId=turn_id,
                sessionId=compaction.session_id,
                oldMessages=compaction.old_messages,
                recentMessages=compaction.recent_messages,
                oldTokens=compaction.old_tokens,
                recentTokens=compaction.recent_tokens,
                chunks=compaction.chunks,
                summaryPreview=compaction.preview,
                summaryVersion=compaction.summary_version,
                coveredMessageStart=compaction.covered_message_start,
                coveredMessageEnd=compaction.covered_message_end,
                qualityWarnings=list(compaction.quality_warnings),
            )
            self._record_activity(
                session,
                "compaction",
                turn_id,
                {
                    "oldMessages": compaction.old_messages,
                    "recentMessages": compaction.recent_messages,
                    "oldTokens": compaction.old_tokens,
                    "recentTokens": compaction.recent_tokens,
                    "chunks": compaction.chunks,
                    "summaryPreview": compaction.preview,
                    "summaryVersion": compaction.summary_version,
                    "coveredMessageStart": compaction.covered_message_start,
                    "coveredMessageEnd": compaction.covered_message_end,
                    "qualityWarnings": list(compaction.quality_warnings),
                },
            )
            self.store.save(session)
        elif error and compaction_estimate is not None:
            self._emit(
                event_callback,
                "compaction_failed",
                turnId=turn_id,
                sessionId=session.session_id,
                oldMessages=compaction_estimate.get("oldMessages", 0),
                recentMessages=compaction_estimate.get("recentMessages", 0),
                oldTokens=compaction_estimate.get("oldTokens", 0),
                recentTokens=compaction_estimate.get("recentTokens", 0),
                maxTokens=compaction_estimate.get("maxTokens"),
                chunks=compaction_estimate.get("estimatedChunks", 0),
                error=error,
            )
            self._record_activity(
                session,
                "compaction_failed",
                turn_id,
                {
                    "oldMessages": compaction_estimate.get("oldMessages", 0),
                    "recentMessages": compaction_estimate.get("recentMessages", 0),
                    "oldTokens": compaction_estimate.get("oldTokens", 0),
                    "recentTokens": compaction_estimate.get("recentTokens", 0),
                    "maxTokens": compaction_estimate.get("maxTokens"),
                    "chunks": compaction_estimate.get("estimatedChunks", 0),
                    "error": error,
                },
            )
            self.store.save(session)
        return self._result(session, reply, events, [], compaction, error)

    def _result(
        self,
        session,
        reply,
        events,
        approvals,
        compaction=None,
        error=None,
        turn_id=None,
    ):
        return AgentTurnResult(
            reply=reply,
            session_id=session.session_id,
            tool_events=events,
            pending_approvals=[item.to_dict() for item in approvals],
            compaction=compaction,
            compaction_error=error,
            turn_id=turn_id,
            metrics=[
                item["data"]
                for item in session.activity
                if item.get("turnId") == turn_id and item.get("type") == "model_call"
            ],
        )

    @staticmethod
    def _approval_turn_evidence(session: Session) -> TurnEvidence:
        """Recover Tool evidence from the currently paused logical turn.

        Tool observations retain ``role=user`` for provider compatibility; approval
        results are internal system messages. Their bracketed protocol prefix distinguishes them
        from the user's real request.  Scan only back to that request so stale
        evidence from an older turn can never prove a new action.
        """
        recovered: list[dict] = []
        for message in reversed(session.messages):
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            if role == "user" and not content.startswith("["):
                break
            if content.startswith("[tool_results] "):
                try:
                    payload = json.loads(content[len("[tool_results] "):])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                for item in payload if isinstance(payload, list) else []:
                    if not isinstance(item, dict) or not item.get("tool"):
                        continue
                    recovered.append({"tool": item["tool"], "result": item})
            elif content.startswith("[approval_result] "):
                try:
                    payload = json.loads(content[len("[approval_result] "):])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                result = payload.get("toolResult") if isinstance(payload, dict) else None
                if isinstance(result, dict) and result.get("tool"):
                    recovered.append({"tool": result["tool"], "result": result})
        recovered.reverse()
        return TurnEvidence.from_events(recovered)

    def _send_without_tools(
        self,
        session: Session,
        message: str,
        event_callback=None,
        turn_id=None,
        cancellation_event=None,
        source: str = "web",
        user_committed: bool = False,
        scheduler_context: dict | None = None,
    ) -> str:
        output_budget = None
        output_limit_error: ModelOutputLimitError | None = None
        reply: str | None = None
        streamed = False
        for attempt in range(3):
            try:
                reply, streamed = self._call_model(
                    session,
                    self.context_builder.build(
                        session,
                        None if user_committed else message,
                        scheduler_context=scheduler_context,
                    ),
                    event_callback,
                    turn_id,
                    cancellation_event,
                    output_budget=output_budget,
                )
                break
            except ModelOutputLimitError as exc:
                output_limit_error = exc
                self._preserve_retry_content(
                    session,
                    turn_id,
                    exc.partial_content,
                    streamed=exc.streamed,
                    phase="approval_output_limit",
                    event_callback=event_callback,
                )
                if attempt >= 2:
                    break
                output_budget = self._next_output_budget(
                    output_budget, exc.output_tokens
                )
                self._emit(
                    event_callback,
                    "status",
                    phase="model_output_limit_retry",
                    message="模型输出达到上限，正在调整输出预算并重试",
                    finishReason=exc.finish_reason,
                    outputBudget=output_budget,
                    attempt=attempt + 1,
                )
        if output_limit_error is not None and reply is None:
            reply = (
                "抱歉，模型连续触发输出长度上限，本轮回答没有完整生成。"
                "请点击重答，或缩小问题范围后再试。"
            )
            streamed = False
            self._record_activity(
                session,
                "assistant_final",
                turn_id,
                {
                    "contentPreview": reply,
                    "fallback": "model_output_limit",
                    "finishReason": output_limit_error.finish_reason,
                    "retries": 2,
                },
            )
        if not streamed:
            self._emit_text(event_callback, reply)
        # The answer is visible before persistence/compaction bookkeeping
        # completes. Interactive clients can stop their streaming caret at
        # this boundary instead of waiting for the transport-level `done`.
        self._emit(event_callback, "assistant_final", content=reply)
        if not user_committed:
            session.messages.append(self._user_message(message, source))
        session.messages.append({"role": "assistant", "content": reply})
        if self.skill_service is not None:
            self.skill_service.finalize(session, reply)
        self._record_activity(
            session,
            "assistant_final",
            turn_id,
            {"contentPreview": reply[:500]},
        )
        session.updated_at = utc_now()
        self.store.save(session)
        return reply

    def _run_agent_loop(
        self,
        session: Session,
        user_message: str | None,
        tool_events: list[dict],
        event_callback=None,
        turn_id=None,
        cancellation_event=None,
        source: str = "web",
        user_committed: bool = False,
        scheduler_context: dict | None = None,
        evidence_seed: TurnEvidence | None = None,
    ) -> tuple[str | None, list[Approval]]:
        pending_user = None if user_committed else user_message
        deferred_action_retries = 0
        failed_observation_retries = 0
        protocol_retries = 0
        empty_response_retries = 0
        truncated_final_retries = 0
        execution_evidence_retries = 0
        output_limit_retries = 0
        output_budget: int | None = None
        runtime_repair_hint: str | None = None
        time_context_checked = False
        current_time_context = None
        time_sensitive_request = self._is_time_sensitive_request(user_message)
        turn_requirements = required_capabilities(
            user_message, getattr(session, "goal_state", None)
        )
        # Goal evidence is cumulative and remains useful for plan progress,
        # but it must never prove that an action happened in *this* turn.
        # Capture the unresolved part for vague continuation messages, while
        # explicit requests always require fresh evidence.
        evidence_before_turn = TurnEvidence.from_goal(
            getattr(session, "goal_state", None)
        )
        explicit_turn_requirements = required_capabilities(user_message, None)
        if is_continuation_message(user_message):
            explicit_turn_requirements = (
                turn_requirements - evidence_before_turn.successful_capabilities
            )
        force_tool_call = False
        progress_enabled = self._should_emit_progress_notes(user_message)
        progress_note_count = 0
        loop_steps = 0
        # Citation labels returned by Tavily are local to one search response
        # (every response starts at [W1]).  Give each search result a unique
        # label for this session before it is fed back to the model; otherwise
        # the UI cannot tell which historical [W1] a final answer refers to.
        citation_counters = self._citation_counters(session)
        # Providers occasionally repeat an identical call after receiving
        # its result. Reuse the first result instead of invoking a Tool twice
        # (particularly important for side-effecting Tools).
        executed_call_results: dict[str, ToolResult] = {}
        duplicate_call_keys: set[str] = set()
        duplicate_call_counts: dict[str, int] = {}

        def current_evidence() -> TurnEvidence:
            return TurnEvidence.from_goal(
                getattr(session, "goal_state", None)
            ).merge(TurnEvidence.from_events(tool_events))

        def turn_evidence() -> TurnEvidence:
            """Return proof produced by this logical user turn.

            A normal request starts with an empty seed, so evidence from an
            older turn can never prove a fresh action.  Approval resolution is
            different: it resumes the same logical turn after the Runtime has
            paused.  In that path ``evidence_seed`` carries the successful
            pre-approval observations (for example current_time/weather) and
            the newly approved result is merged on top.
            """
            fresh = TurnEvidence.from_events(tool_events)
            return evidence_seed.merge(fresh) if evidence_seed is not None else fresh

        def persist_execution_evidence() -> None:
            goal = getattr(session, "goal_state", None)
            if not isinstance(goal, dict):
                return
            goal["executionEvidence"] = current_evidence().to_goal_dict()
            session.goal_state = goal

        def emit_progress_note(text: str, phase: str) -> None:
            nonlocal progress_note_count
            # Tool cards already show the concrete action/result. Keep at most
            # one short bridge for a genuinely long turn.
            if not progress_enabled or progress_note_count >= 1:
                return
            progress_note_count += 1
            self._emit_assistant_note(event_callback, session, turn_id, text, phase)

        attached_calls = self._attached_file_calls(user_message)
        if not attached_calls:
            attached_calls = self._referenced_attachment_calls(session, user_message)
        if not attached_calls and self._is_attachment_retry(user_message):
            for previous in reversed(session.messages):
                if previous.get("role") != "user":
                    continue
                attached_calls = self._attached_file_calls(previous.get("content"))
                if attached_calls:
                    break
        if attached_calls:
            if not user_committed:
                session.messages.append(self._user_message(user_message, source))
                user_committed = True
            pending_user = None
            emit_progress_note("我先读取这轮明确选中的附件，再基于内容继续处理。", "attachment_read")
            session.messages.append({
                "role": "assistant",
                "content": json.dumps(
                    {"type": "tool_calls", "calls": attached_calls}, ensure_ascii=False
                ),
            })
            observations = self._execute_prefetched_attachments(
                session, attached_calls, tool_events, event_callback, turn_id,
                cancellation_event, step=0,
            )
            for call, observation in zip(attached_calls, observations):
                executed_call_results[self._tool_call_fingerprint(
                    call.get("tool", ""), call.get("args", {})
                )] = self._tool_result_from_dict(observation)
            session.messages.append({
                "role": "user",
                "content": "[tool_results] " + json.dumps(observations, ensure_ascii=False),
            })
            session.updated_at = utc_now()
            self.store.save(session)
        while True:
            self._check_cancelled(cancellation_event)
            loop_steps += 1
            if loop_steps > self.max_agent_steps:
                fallback = (
                    f"本轮工具调用已达到安全上限（{self.max_agent_steps} 轮），"
                    "为避免重复调用已暂停。请点击重答，或缩小任务范围后再试。"
                )
                if not user_committed and user_message is not None:
                    session.messages.append(self._user_message(user_message, source))
                    user_committed = True
                session.messages.append({"role": "assistant", "content": fallback})
                self._record_activity(
                    session,
                    "agent_loop_limit",
                    turn_id,
                    {
                        "maxSteps": self.max_agent_steps,
                        "steps": loop_steps - 1,
                        "toolEvents": len(tool_events),
                    },
                )
                session.updated_at = utc_now()
                self.store.save(session)
                self._emit(
                    event_callback,
                    "status",
                    phase="agent_loop_limit",
                    message="工具调用轮数达到安全上限，已暂停本轮",
                    maxSteps=self.max_agent_steps,
                )
                self._emit_text(event_callback, fallback)
                self._emit(event_callback, "assistant_final", content=fallback)
                return fallback, []
            self._emit(
                event_callback,
                "status",
                phase="model_call",
                message="正在请求模型",
            )
            try:
                approval_hint = self._approval_state_hint(
                    session.session_id, user_message
                )
                effective_runtime_hint = "\n\n".join(
                    item
                    for item in (runtime_repair_hint, approval_hint)
                    if item
                ) or None
                raw_response, streamed = self._call_model(
                    session,
                    self.context_builder.build(
                        session,
                        pending_user,
                        scheduler_context=scheduler_context,
                        runtime_hint=effective_runtime_hint,
                    ),
                    event_callback,
                    turn_id,
                    cancellation_event,
                    user_message=user_message,
                    tool_events=tool_events,
                    output_budget=output_budget,
                    force_tool_call=(
                        force_tool_call
                        or bool(
                            explicit_turn_requirements
                            - turn_evidence().successful_capabilities
                            - turn_evidence().failed_capabilities
                        )
                    ),
                )
                force_tool_call = False
            except ModelOutputLimitError as exc:
                # The provider explicitly reports a length stop. Keep useful
                # prose as a display-only process segment before retrying;
                # incomplete protocol JSON is stripped and never treated as
                # a final answer.
                output_limit_retries += 1
                self._preserve_retry_content(
                    session,
                    turn_id,
                    exc.partial_content,
                    streamed=exc.streamed,
                    phase="model_output_limit",
                    event_callback=event_callback,
                )
                if output_limit_retries <= 2:
                    output_budget = self._next_output_budget(
                        output_budget, exc.output_tokens
                    )
                    self._emit(
                        event_callback,
                        "status",
                        phase="model_output_limit_retry",
                        message="妯″瀷杈撳嚭杈惧埌涓婇檺锛岀户缁皾璇曪紝璋冩暣杈撳嚭棰勭畻",
                        finishReason=exc.finish_reason,
                        outputBudget=output_budget,
                        attempt=output_limit_retries,
                    )
                    # If the newly accumulated tool/session context itself is
                    # over the compaction threshold, compact it before the
                    # next model call.  This is best-effort; losing a summary
                    # must never turn a retry into a hard failure.
                    if self.compactor is not None:
                        try:
                            if self.compactor.should_compact(session):
                                self.compactor.compact(session)
                        except Exception:
                            pass
                    pending_user = None
                    continue
                fallback = (
                    "抱歉，模型连续触发输出长度上限，本轮回答没有完整生成。"
                    "请点击重答，或缩小问题范围后再试。"
                )
                if not user_committed and user_message is not None:
                    session.messages.append(self._user_message(user_message, source))
                    user_committed = True
                session.messages.append({"role": "assistant", "content": fallback})
                self._record_activity(
                    session,
                    "assistant_final",
                    turn_id,
                    {
                        "contentPreview": fallback,
                        "fallback": "model_output_limit",
                        "finishReason": exc.finish_reason,
                        "retries": output_limit_retries - 1,
                    },
                )
                session.updated_at = utc_now()
                self.store.save(session)
                self._emit_text(event_callback, fallback)
                self._emit(event_callback, "assistant_final", content=fallback)
                return fallback, []
            except EmptyAssistantResponse:
                inferred_calls = self._infer_tool_calls_from_request(
                    user_message,
                    tool_events,
                )
                if inferred_calls:
                    # Some compatible endpoints finish with
                    # ``finish_reason=tool_calls`` but omit the actual call
                    # payload.  For narrow, read-only requests that the
                    # Runtime can determine safely, continue through the
                    # normal audited Tool path instead of retrying an empty
                    # completion three times.
                    raw_response = json.dumps(
                        {"type": "tool_calls", "calls": inferred_calls},
                        ensure_ascii=False,
                    )
                    streamed = False
                    self._emit(
                        event_callback,
                        "status",
                        phase="empty_response_tool_repair",
                        message="模型遗漏了工具参数，已根据明确请求恢复工具调用",
                    )
                else:
                # A provider can occasionally return an empty completion just
                # after a protocol retry.  Do not strand the browser with the
                # temporary reply already reset: retain an internal recovery
                # instruction and retry the same Agent Turn.
                    empty_response_retries += 1
                    if empty_response_retries <= 2:
                        session.messages.append({
                            "role": "system",
                            "content": (
                                "[protocol_error] 模型刚才返回空内容，不能视为任务完成。"
                                "请直接输出所需 Tool Call；如无需工具，请输出非空 final。"
                            ),
                            "metadata": {"internal": True, "kind": "protocol_error"},
                        })
                        session.updated_at = utc_now()
                        self.store.save(session)
                        self._emit(
                            event_callback,
                            "status",
                            phase="empty_response_retry",
                            message="模型返回为空，正在自动重试",
                        )
                        runtime_repair_hint = (
                            "上一轮没有返回有效内容。请直接输出唯一的 tool_call 或完整的 final，"
                            "不要输出空内容。"
                        )
                        pending_user = None
                        continue
                    fallback = "抱歉，模型连续返回空内容，本轮没有完成。请点击重答或稍后重试。"
                    session.messages.append({"role": "assistant", "content": fallback})
                    self._record_activity(
                        session, "assistant_final", turn_id,
                        {"contentPreview": fallback, "fallback": "empty_response"},
                    )
                    session.updated_at = utc_now()
                    self.store.save(session)
                    self._emit_text(event_callback, fallback)
                    self._emit(event_callback, "assistant_final", content=fallback)
                    return fallback, []

            def preserve_streamed_segment(phase: str, content: str | None = None) -> None:
                """Checkpoint visible pre-tool prose instead of discarding it.

                A streamed model response can contain a useful explanation
                before the structured Tool Call. It is excluded from the next
                model context, but kept as a display-only Session message so
                the Web/CLI transcript can replay the full turn.
                """
                nonlocal streamed
                self._preserve_retry_content(
                    session,
                    turn_id,
                    str(content if content is not None else raw_response or ""),
                    streamed=streamed,
                    phase=phase,
                    event_callback=event_callback,
                )
                streamed = False

            try:
                # Before the first observation, plain prose is ambiguous: it
                # may be an unfinished promise ("我去查询").  Once this turn
                # has a real Tool Result, however, SJTU's compatible endpoint
                # often summarizes in ordinary Markdown instead of returning
                # final JSON.  Accept that prose as the final answer and let
                # the promise/missing-observation guard below validate it.
                try:
                    action = parse_model_action(
                        raw_response,
                        require_json=(
                            self.strict_tool_protocol
                            and self._tool_expected_for_request(user_message)
                            and not bool(tool_events)
                        ),
                    )
                except ProtocolError:
                    inferred_calls = self._infer_tool_calls_from_request(
                        user_message,
                        tool_events,
                    )
                    if not inferred_calls:
                        raise
                    # The SJTU endpoint sometimes ignores the JSON-only
                    # instruction and emits a natural-language promise. For
                    # explicit, deterministic read requests, recover locally
                    # instead of spending three model retries on the same
                    # promise. The normal Tool execution path below remains
                    # responsible for validation, tracing and approvals.
                    action = ModelAction(
                        "tool_calls",
                        calls=tuple(
                            ToolCall(item["tool"], item.get("args", {}))
                            for item in inferred_calls
                        ),
                        raw_json={"type": "tool_calls", "calls": inferred_calls},
                    )
                    preserve_streamed_segment("protocol_repair")
                    self._emit(
                        event_callback,
                        "status",
                        phase="protocol_repair",
                        message="模型未返回工具协议，已根据明确请求恢复工具调用",
                    )
            except ProtocolError as exc:
                protocol_retries += 1
                if not user_committed and user_message is not None:
                    session.messages.append(self._user_message(user_message, source))
                    user_committed = True
                # Preserve malformed provider output only in the audit log:
                # it must not become a visible assistant message or part of
                # the following conversational context.
                session.messages.append({
                    "role": "system",
                    "content": f"[protocol_error] {exc} 请按 Tool Call Protocol 重试。",
                    "metadata": {"internal": True, "kind": "protocol_error"},
                })
                self._record_activity(
                    session,
                    "protocol_retry",
                    turn_id,
                    {
                        "error": str(exc),
                        "outputPreview": raw_response[:500],
                        "attempt": protocol_retries,
                    },
                )
                session.updated_at = utc_now()
                self.store.save(session)
                preserve_streamed_segment("protocol_error")
                if protocol_retries >= 3:
                    fallback = (
                        "抱歉，模型连续未按工具协议返回有效内容，本轮没有完成。"
                        "请点击重答或稍后重试。"
                    )
                    session.messages.append({"role": "assistant", "content": fallback})
                    self._record_activity(
                        session,
                        "assistant_final",
                        turn_id,
                        {"contentPreview": fallback, "fallback": "protocol_error"},
                    )
                    session.updated_at = utc_now()
                    self.store.save(session)
                    self._emit_text(event_callback, fallback)
                    self._emit(event_callback, "assistant_final", content=fallback)
                    return fallback, []
                self._emit(
                    event_callback,
                    "status",
                    phase="protocol_retry",
                    message="模型协议格式有误，正在重试",
                )
                runtime_repair_hint = (
                    "上一轮输出未按 Tool Call 协议完成。若任务需要工具，请现在直接输出唯一的 tool_call；"
                    "不要先用自然语言承诺，也不要把工具结果伪装成最终答案。"
                )
                pending_user = None
                continue
            if action.action_type == "final":
                action = ModelAction(
                    "final",
                    content=unwrap_final_content(action.content),
                    raw_json=action.raw_json,
                )
                if self._looks_like_truncated_final(action.content, raw_response):
                    # A provider can terminate a JSON completion after a
                    # single character (the visible symptom was a lone “L”
                    # or “D：”).  Never persist or expose that fragment as a
                    # successful answer; reset the provisional stream and
                    # give the model a bounded chance to regenerate it.
                    truncated_final_retries += 1
                    protocol_retries += 1
                    if not user_committed and user_message is not None:
                        session.messages.append(self._user_message(user_message, source))
                        user_committed = True
                    session.messages.append({
                        "role": "system",
                        "content": (
                            "[protocol_error] 模型返回的 final 内容疑似被截断（仅含极短片段），"
                            "请重新生成完整答案；若需要工具，请直接输出 Tool Call。"
                        ),
                        "metadata": {"internal": True, "kind": "protocol_error"},
                    })
                    self._record_activity(
                        session,
                        "protocol_retry",
                        turn_id,
                        {
                            "error": "truncated final content",
                            "outputPreview": raw_response[:200],
                            "attempt": protocol_retries,
                            "reason": "truncated_final",
                        },
                    )
                    session.updated_at = utc_now()
                    self.store.save(session)
                    preserve_streamed_segment("truncated_final", action.content)
                    if truncated_final_retries >= 3:
                        fallback = (
                            "抱歉，模型返回的答案疑似被截断，本轮没有完成。"
                            "请点击重答或稍后重试。"
                        )
                        session.messages.append({"role": "assistant", "content": fallback})
                        self._record_activity(
                            session,
                            "assistant_final",
                            turn_id,
                            {"contentPreview": fallback, "fallback": "truncated_final"},
                        )
                        session.updated_at = utc_now()
                        self.store.save(session)
                        self._emit_text(event_callback, fallback)
                        self._emit(event_callback, "assistant_final", content=fallback)
                        return fallback, []
                    self._emit(
                        event_callback,
                        "status",
                        phase="truncated_final_retry",
                        message="模型答案疑似被截断，正在重新生成",
                    )
                    runtime_repair_hint = (
                        "上一轮 final 似乎被截断。请重新输出完整答案；如果任务需要工具，"
                        "请直接发起 tool_call。"
                    )
                    pending_user = None
                    continue
                # A provider may wrap a mixed prose + Tool response in a
                # syntactically valid final envelope.  Recover the nested
                # Tool Call before the promise guard turns it into a
                # protocol_error and discards the useful action.
                embedded_action = extract_embedded_tool_action(action.content)
                embedded_recovery_calls = None
                if embedded_action is not None and (
                    not self.strict_tool_protocol
                    or self._looks_like_unfinished_tool_promise(action.content, tool_events, session)
                ):
                    embedded_recovery_calls = [
                        {
                            "tool": call.tool,
                            "args": call.args,
                            **({"id": call.call_id} if call.call_id else {}),
                        }
                        for call in embedded_action.calls
                    ]
                    if streamed:
                        preserve_streamed_segment("embedded_tool_call", action.content)
                missing_observation = (
                    self._missing_required_tool_observation(user_message, tool_events)
                    if self.strict_tool_protocol else None
                )
                unfinished_promise = (
                    self.strict_tool_protocol
                    and self._looks_like_unfinished_tool_promise(action.content, tool_events, session)
                ) or bool(missing_observation)
                # Gateway runs in relaxed mode to avoid false-positive
                # protocol errors.  Keep a small independent safety net for
                # the unambiguous case: no Tool ran at all, yet the model says
                # it is about to inspect/search/execute one.
                if (
                    not self.strict_tool_protocol
                    and bool(explicit_turn_requirements)
                    and not tool_events
                    and re.search(
                        r"\b(?:read_file|read_document|read_attachment|ocr_image|"
                        r"list_dir|web_search|tavily_search|current_time|use_skill|"
                        r"install_skill|new_shell|run_command|github_read)\b",
                        action.content,
                        re.I,
                    )
                    and any(
                        verb in action.content
                        for verb in (
                            chr(0x8c03) + chr(0x7528),
                            chr(0x4f7f) + chr(0x7528),
                            chr(0x6267) + chr(0x884c),
                            chr(0x53d1) + chr(0x8d77),
                            "use",
                            "call",
                        )
                    )
                ):
                    unfinished_promise = True
                recovered_promise_calls = self._infer_search_call_from_confirmation(
                    session, user_message, action.content, tool_events
                )
                failed_observation_claim = self._failed_tool_observation_claim(
                    action.content, tool_events
                )
                if not recovered_promise_calls and embedded_recovery_calls:
                    recovered_promise_calls = embedded_recovery_calls
                if not recovered_promise_calls:
                    # A model may try to answer after seeing only the first
                    # chunk of a long attachment.  Continue deterministically
                    # from the Tool Result cursor instead of trusting prose to
                    # remember nextOffset/nextPage.
                    attachment_continuation = self._pending_attachment_continuation(
                        tool_events
                    )
                    if attachment_continuation is not None:
                        recovered_promise_calls = [attachment_continuation]
                completion_check = validate_completion(
                    action.content,
                    explicit_turn_requirements,
                    turn_evidence(),
                )
                if not recovered_promise_calls and not completion_check.valid:
                    execution_evidence_retries += 1
                    hint = execution_repair_hint(completion_check)
                    missing_labels = [
                        CAPABILITY_LABELS.get(item, item)
                        for item in completion_check.missing
                    ]
                    self._record_activity(
                        session,
                        "execution_evidence_rejected",
                        turn_id,
                        {
                            "reason": completion_check.reason,
                            "missingCapabilities": list(completion_check.missing),
                            "approvalClaim": completion_check.approval_claim,
                            "attempt": execution_evidence_retries,
                            "contentPreview": action.content[:500],
                        },
                    )
                    # The answer may contain useful analysis before an
                    # unsupported "done" claim. Keep that already-visible
                    # draft as a display-only process segment instead of
                    # making it disappear while the Runtime obtains the
                    # missing execution evidence.
                    preserve_streamed_segment("execution_evidence", action.content)
                    if execution_evidence_retries >= 2:
                        detail = "、".join(missing_labels) or "所需外部操作"
                        fallback = (
                            f"本轮还没有取得“{detail}”的真实工具结果，因此我不能宣称任务已经完成。"
                            "请检查工具参数后重试；如果操作需要审批，只有实际发起工具调用后才会出现审批卡片。"
                        )
                        session.messages.append({
                            "role": "assistant",
                            "content": fallback,
                            "metadata": {
                                "executionVerified": False,
                                "missingCapabilities": list(completion_check.missing),
                            },
                        })
                        self._record_activity(
                            session,
                            "assistant_final",
                            turn_id,
                            {
                                "contentPreview": fallback,
                                "fallback": "missing_execution_evidence",
                            },
                        )
                        session.updated_at = utc_now()
                        self.store.save(session)
                        self._emit_text(event_callback, fallback)
                        self._emit(event_callback, "assistant_final", content=fallback)
                        return fallback, []
                    session.messages.append({
                        "role": "system",
                        "content": "[execution_evidence_required] " + hint,
                        "metadata": {
                            "internal": True,
                            "kind": "execution_evidence_required",
                            "missingCapabilities": list(completion_check.missing),
                        },
                    })
                    goal = update_goal(
                        getattr(session, "goal_state", None),
                        status="active",
                        step="等待真实工具结果后再判断任务是否完成",
                        next_actions=[
                            f"通过 Tool 完成：{label}" for label in missing_labels
                        ] or ["发起真实 Tool Call"],
                        turn_id=turn_id,
                    )
                    if goal is not None:
                        session.goal_state = goal
                        self._emit_goal_state(session, turn_id, event_callback)
                    session.updated_at = utc_now()
                    self.store.save(session)
                    self._emit(
                        event_callback,
                        "status",
                        phase="execution_evidence_retry",
                        message="尚未取得真实工具结果，正在发起实际操作",
                    )
                    runtime_repair_hint = hint
                    force_tool_call = True
                    pending_user = None
                    continue
                if recovered_promise_calls:
                    # The user is explicitly asking whether the previous
                    # answer was searched. If the model only narrates a
                    # search, recover the read-only web call from the last
                    # substantive user request instead of retrying prose.
                    action = ModelAction(
                        "tool_calls",
                        calls=tuple(
                            ToolCall(item["tool"], item.get("args", {}))
                            for item in recovered_promise_calls
                        ),
                        raw_json={
                            "type": "tool_calls",
                            "calls": recovered_promise_calls,
                        },
                    )
                    if streamed:
                        preserve_streamed_segment("tool_promise_repair", action.content)
                elif failed_observation_claim and failed_observation_retries < 1:
                    # A failed read must never be silently converted into a
                    # confident claim that the document was fully read.  Keep
                    # the already streamed explanation visible, then give the
                    # model one fresh turn with the real Tool error in context.
                    failed_observation_retries += 1
                    if not user_committed and user_message is not None:
                        session.messages.append(self._user_message(user_message, source))
                        user_committed = True
                    session.messages.append({
                        "role": "system",
                        "content": "[tool_failure_repair] A read Tool failed. Do not claim that its file or attachment was read successfully; either retry the Tool or explain the failure honestly.",
                        "metadata": {"internal": True, "kind": "tool_failure_repair"},
                    })
                    session.updated_at = utc_now()
                    self.store.save(session)
                    preserve_streamed_segment("tool_failure_repair", action.content)
                    self._emit(
                        event_callback,
                        "status",
                        phase="tool_failure_repair",
                        message="读取工具失败，正在根据真实错误重新判断",
                    )
                    runtime_repair_hint = (
                        "上一轮读取工具返回了失败结果。请不要假设文件内容已读完；"
                        "如仍需要内容，直接重试合适的读取工具，否则明确说明读取失败。"
                    )
                    pending_user = None
                    continue
                elif failed_observation_claim:
                    # Bound the repair loop.  A second unsupported claim is
                    # replaced by a truthful, actionable answer rather than
                    # another protocol retry that can leave the UI spinning.
                    action = ModelAction(
                        "final",
                        content=(
                            "我没有可靠读取到相关文件：读取工具返回失败。"
                            "请检查文件是否存在、权限或附件状态后重试。"
                        ),
                        raw_json={"type": "final", "fallback": "tool_failure"},
                    )
                # One bounded repair is enough for a malformed prose promise.
                # Repeating it several times only delays the user and can
                # create a self-sustaining protocol loop.
                elif deferred_action_retries < 1 and unfinished_promise:
                    deferred_action_retries += 1
                    if not user_committed and user_message is not None:
                        session.messages.append(self._user_message(user_message, source))
                        user_committed = True
                    retry_hint = (
                        missing_observation
                        or "你承诺了搜索/读取等后续操作但没有调用工具。请现在直接调用所需 Tool；若无需工具，请直接给出完整答案。"
                    )
                    session.messages.append({
                        "role": "assistant",
                        "content": "[deferred_action_promise] " + action.content,
                    })
                    if self.strict_tool_protocol:
                        session.messages.append({
                            "role": "system",
                            "content": "[protocol_error] " + retry_hint,
                            "metadata": {"internal": True, "kind": "protocol_error"},
                        })
                    else:
                        session.messages.append({
                            "role": "system",
                            "content": "[tool_retry_hint] The previous response promised a Tool action but did not emit a Tool call. Execute it now or answer without claiming pending work.",
                            "metadata": {"internal": True, "kind": "tool_retry_hint"},
                        })
                    session.updated_at = utc_now()
                    self.store.save(session)
                    preserve_streamed_segment("protocol_retry", action.content)
                    self._emit(
                        event_callback,
                        "status",
                        phase="protocol_retry" if self.strict_tool_protocol else "tool_retry",
                        message="检测到未执行的操作承诺，正在继续",
                    )
                    runtime_repair_hint = (
                        "你刚才描述了后续操作但没有实际调用工具。请现在直接发起所需的 tool_call；"
                        "如果不需要工具，请给出完整最终回答。"
                    )
                    pending_user = None
                    continue
                if not recovered_promise_calls:
                    final_content = self._with_download_links(
                        action.content,
                        self._current_turn_tool_events(session, tool_events),
                    )
                    if not user_committed and user_message is not None:
                        session.messages.append(self._user_message(user_message, source))
                        user_committed = True
                    session.messages.append(
                        self._assistant_message_with_citations(final_content, tool_events)
                    )
                    if not streamed:
                        self._emit_text(event_callback, final_content)
                    # Streaming may have already delivered the complete body;
                    # post-turn persistence can still take a while afterwards.
                    self._emit(event_callback, "assistant_final", content=final_content)
                    if self.skill_service is not None:
                        self.skill_service.finalize(session, final_content)
                    self._record_activity(
                        session,
                        "assistant_final",
                        turn_id,
                        {"contentPreview": final_content[:500]},
                    )
                    session.updated_at = utc_now()
                    self.store.save(session)
                    return final_content, []

            if not user_committed and user_message is not None:
                session.messages.append(self._user_message(user_message, source))
                user_committed = True
            # Native-compatible endpoints occasionally stream a natural
            # language preamble and then return Tool Calls in the same model
            # turn. Keep that preamble as a display-only process segment so
            # the user can follow the text -> Tool -> text chain; it is not
            # included in the next model context.
            if streamed:
                preserve_streamed_segment("tool_call")
            if (
                time_sensitive_request
                and not time_context_checked
                and any(call.tool == "web_search" for call in action.calls)
                and self.tool_registry.get("current_time") is not None
            ):
                current_time_call_id = self._tool_call_id(turn_id, loop_steps, 0)
                session.messages.append({
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "type": "tool_call", "tool": "current_time", "args": {},
                            "id": current_time_call_id,
                        },
                        ensure_ascii=False,
                    ),
                })
                # A search query mentioning “recent/latest/current” must be
                # composed *after* the model has observed the actual clock.
                # Do not execute a stale query from the same model response.
                context = ToolExecutionContext(session.session_id)
                tool = self.tool_registry.get("current_time")
                emit_progress_note("我先确认当前日期和时间，避免把过期信息当成最新结果。", "time_grounding")
                self._update_plan_tool(
                    session,
                    turn_id,
                    "current_time",
                    "call",
                    event_callback,
                    call_id=current_time_call_id,
                )
                self._emit(
                    event_callback, "tool_call", tool="current_time", args={},
                    callId=current_time_call_id, step=loop_steps,
                    safetyLevel=tool.safety_level,
                )
                self._record_activity(
                    session, "tool_call", turn_id,
                    {
                        "tool": "current_time", "args": {},
                        "callId": current_time_call_id, "step": loop_steps,
                        "safetyLevel": tool.safety_level,
                    },
                )
                result = self._run_cancellable(
                    lambda: self.tool_registry.execute("current_time", {}, context),
                    cancellation_event,
                )
                result_dict = self._serialise_tool_result(session, result, citation_counters)
                event = {
                    "timestamp": utc_now(), "tool": "current_time",
                    "callId": current_time_call_id, "step": loop_steps,
                    "args": {}, "result": result_dict,
                }
                session.tool_trace.append(self._audit_tool_event(event))
                tool_events.append(event)
                self._update_plan_tool(
                    session,
                    turn_id,
                    "current_time",
                    "success" if result.success else "failed",
                    event_callback,
                    call_id=current_time_call_id,
                    error=result.error,
                    timestamp=event["timestamp"],
                )
                self._record_activity(
                    session, "tool_result", turn_id,
                    {
                        "tool": "current_time", "args": {},
                        "callId": current_time_call_id,
                        "success": result.success, "error": result.error,
                        "errorCode": result.error_code,
                        "retryable": result.retryable,
                        "attempts": result.attempts,
                        "durationMs": result.duration_ms,
                        "outputPreview": self._preview(result.output),
                    },
                )
                self._emit(event_callback, "tool_result", **event)
                session.messages.append({
                    "role": "user",
                    "content": "[tool_results] " + json.dumps([
                        {**result_dict, "callId": current_time_call_id},
                        {
                            "tool": "web_search",
                            "success": False,
                            "deferred": True,
                            "error": "时间敏感搜索已暂缓。请根据 current_time 的结果重写含正确年份/日期的 query，再调用 web_search。",
                        },
                    ], ensure_ascii=False),
                })
                time_context_checked = True
                current_time_context = result.output if result.success else None
                runtime_repair_hint = None
                pending_user = None
                session.updated_at = utc_now()
                self.store.save(session)
                continue
            session.messages.append({
                "role": "assistant", "content": json.dumps(action.raw_json, ensure_ascii=False)
            })
            observations = []
            approvals: list[Approval] = []
            pending_approval_observations: dict[str, dict] = {}
            batch_id = f"batch_{uuid.uuid4().hex[:12]}"
            context = ToolExecutionContext(session.session_id)
            emit_progress_note(self._tool_progress_message(action.calls), "tool_batch")
            executable: list[tuple[int, ToolCall, str]] = []
            immediate_results: dict[int, ToolResult] = {}
            for call_index, call in enumerate(action.calls):
                self._check_cancelled(cancellation_event)
                call_id = self._tool_call_id(
                    turn_id, loop_steps, call_index, call.call_id
                )
                if (
                    call.tool == "web_search"
                    and time_sensitive_request
                    and current_time_context
                    and isinstance(call.args.get("query"), str)
                ):
                    call.args["query"] = self._ground_time_sensitive_query(
                        call.args["query"], current_time_context, user_message or ""
                    )
                # A provider may repeat the exact same attachment call after
                # receiving a truncated chunk. Replaying the cached first
                # chunk never makes progress, so advance from the Tool
                # Result's cursor before the call is emitted or executed.
                original_fingerprint = self._tool_call_fingerprint(
                    call.tool, call.args
                )
                if (
                    call.tool == "read_attachment"
                    and original_fingerprint in executed_call_results
                ):
                    continuation = self._attachment_continuation_for(
                        tool_events,
                        str(call.args.get("attachment_id") or ""),
                    )
                    if continuation is not None:
                        call.args.clear()
                        call.args.update(continuation["args"])
                tool = self.tool_registry.get(call.tool)
                self._update_plan_tool(
                    session,
                    turn_id,
                    call.tool,
                    "call",
                    event_callback,
                    call_id=call_id,
                )
                self._emit(
                    event_callback,
                    "tool_call",
                    tool=call.tool,
                    args=call.args,
                    callId=call_id,
                    step=loop_steps,
                    safetyLevel=tool.safety_level if tool else None,
                )
                self._record_activity(
                    session,
                    "tool_call",
                    turn_id,
                    {
                        "tool": call.tool,
                        "args": self._preview(call.args),
                        "callId": call_id,
                        "step": loop_steps,
                        "safetyLevel": tool.safety_level if tool else None,
                    },
                )
                if (
                    call.tool == "install_skill"
                    and self._install_skill_failure_count(session)
                    >= self.MAX_INSTALL_SKILL_FAILURES
                ):
                    # Do not create another approval after the bounded repair
                    # budget is exhausted. Feed a structured terminal error
                    # back to the model so it can explain the failure.
                    immediate_results[call_index] = ToolResult(
                        call.tool,
                        False,
                        error="install_skill 已连续失败 3 次，已停止自动重试，请修改 Skill 来源后再试。",
                        error_code="retry_limit",
                        retryable=False,
                    )
                    continue
                fingerprint = self._tool_call_fingerprint(call.tool, call.args)
                if fingerprint in executed_call_results:
                    # Preserve one duplicate observation for provider
                    # compatibility. Further repeats become an explicit
                    # no-progress error instead of fresh-looking successes.
                    duplicate_call_counts[fingerprint] = (
                        duplicate_call_counts.get(fingerprint, 0) + 1
                    )
                    if (
                        duplicate_call_counts[fingerprint] >= 2
                        and (tool is None or tool.effective_side_effect)
                    ):
                        immediate_results[call_index] = ToolResult(
                            call.tool,
                            False,
                            error=(
                                "相同 Tool 与参数已执行过，结果没有新增内容。"
                                "请基于已有 Tool Result 回答；如需继续读取 PDF，"
                                "请使用返回的 nextPage 作为 start_page。"
                            ),
                            error_code="duplicate_no_progress",
                            retryable=False,
                        )
                    else:
                        immediate_results[call_index] = executed_call_results[fingerprint]
                    duplicate_call_keys.add(fingerprint)
                    continue
                if fingerprint in pending_approval_observations:
                    # A duplicated approval request in the same model batch
                    # should point at the original approval, not create a
                    # second approval card.
                    observations.append({
                        **pending_approval_observations[fingerprint],
                        "callId": call_id,
                    })
                    duplicate_call_keys.add(fingerprint)
                    continue
                if tool is None:
                    immediate_results[call_index] = ToolResult(
                        call.tool, False, error=f"未知 Tool：{call.tool}",
                        error_code="unknown_tool",
                    )
                    continue
                try:
                    self.tool_registry.validate_args(tool, call.args)
                except Exception:
                    # Let the Registry produce the canonical structured error.
                    immediate_results[call_index] = self._run_cancellable(
                        lambda call=call: self.tool_registry.execute(
                            call.tool, call.args, context
                        ),
                        cancellation_event,
                    )
                    continue
                if tool.safety_level == "approval_required":
                    if self.approval_store is None:
                        immediate_results[call_index] = ToolResult(
                            call.tool, False, error="Approval 系统未配置。",
                            error_code="approval_unavailable",
                        )
                    else:
                        approval = self.approval_store.create(
                            batch_id,
                            session.session_id,
                            call.tool,
                            call.args,
                            turn_id=turn_id,
                            call_id=call_id,
                        )
                        approvals.append(approval)
                        self._record_activity(
                            session,
                            "approval_required",
                            turn_id,
                            {
                                "approvalId": approval.approval_id,
                                "tool": call.tool,
                                "args": self._preview(call.args),
                            },
                        )
                        self._emit(
                            event_callback,
                            "approval_required",
                            approval=approval.to_dict(),
                            callId=call_id,
                            step=loop_steps,
                        )
                        observations.append({
                            "tool": call.tool,
                            "callId": call_id,
                            "success": False,
                            "approvalRequired": True,
                            "approvalId": approval.approval_id,
                        })
                        pending_approval_observations[fingerprint] = {
                            "tool": call.tool,
                            "success": False,
                            "approvalRequired": True,
                            "approvalId": approval.approval_id,
                        }
                    continue
                # Read-only calls may run in a bounded parallel batch. Any
                # download/side-effecting call remains serial by construction.
                executable.append((call_index, call, call_id))

            can_parallelize = len(executable) > 1 and all(
                (tool := self.tool_registry.get(call.tool)) is not None
                and tool.parallel_safe
                and not tool.effective_side_effect
                and tool.safety_level == "read_only"
                for _, call, _ in executable
            )
            if executable:
                self._check_cancelled(cancellation_event)
                if can_parallelize:
                    results = self._run_cancellable(
                        lambda: self.tool_registry.execute_many(
                            [(call.tool, call.args) for _, call, _ in executable],
                            context,
                            max_workers=min(4, len(executable)),
                        ),
                        cancellation_event,
                    )
                    immediate_results.update({
                        item[0]: result
                        for item, result in zip(executable, results)
                    })
                else:
                    for call_index, call, _ in executable:
                        self._check_cancelled(cancellation_event)
                        immediate_results[call_index] = self._run_cancellable(
                            lambda call=call: self.tool_registry.execute(
                                call.tool, call.args, context
                            ),
                            cancellation_event,
                        )

            for call_index in range(len(action.calls)):
                if call_index not in immediate_results:
                    continue
                call = action.calls[call_index]
                call_id = self._tool_call_id(
                    turn_id, loop_steps, call_index, call.call_id
                )
                result = immediate_results[call_index]
                fingerprint = self._tool_call_fingerprint(call.tool, call.args)
                if fingerprint not in executed_call_results:
                    executed_call_results[fingerprint] = result
                if call.tool == "use_skill" and result.success:
                    persisted = self.store.get(session.session_id)
                    session.__dict__.update(persisted.__dict__)
                result_dict = self._serialise_tool_result(session, result, citation_counters)
                event = {
                    "timestamp": utc_now(), "tool": call.tool,
                    "callId": call_id, "step": loop_steps,
                    "args": call.args, "result": result_dict,
                }
                if fingerprint in duplicate_call_keys:
                    event["deduplicated"] = True
                session.tool_trace.append(self._audit_tool_event(event))
                tool_events.append(event)
                self._update_plan_tool(
                    session,
                    turn_id,
                    call.tool,
                    "success" if result.success else "failed",
                    event_callback,
                    call_id=call_id,
                    error=result.error,
                    timestamp=event["timestamp"],
                )
                self._record_activity(
                    session,
                    "tool_result",
                    turn_id,
                    {
                        "tool": call.tool,
                        "args": self._preview(call.args),
                        "callId": call_id,
                        "step": loop_steps,
                        "success": result.success,
                        "error": result.error,
                        "errorCode": result.error_code,
                        "retryable": result.retryable,
                        "attempts": result.attempts,
                        "durationMs": result.duration_ms,
                        "outputPreview": self._preview(result.output),
                        "deduplicated": fingerprint in duplicate_call_keys,
                    },
                )
                self._emit(event_callback, "tool_result", **event)
                observations.append({**result_dict, "callId": call_id})
                if call.tool == "current_time":
                    time_context_checked = True
                    if result.success:
                        current_time_context = result.output
            # ``observations`` can contain both completed read-only results and
            # placeholders for side-effecting calls that are awaiting approval.
            # Labelling the whole mixed batch as ``approval_required`` made the
            # Web history renderer treat successful tools such as current_time
            # as pending approvals.  Pending approvals already live in the
            # ApprovalStore and are exposed through /api/approvals, while this
            # message is the model's tool-observation payload.
            session.messages.append({
                "role": "user",
                "content": "[tool_results] " + json.dumps(observations, ensure_ascii=False),
            })
            session.updated_at = utc_now()
            persist_execution_evidence()
            self.store.save(session)
            if approvals:
                return None, approvals
            emit_progress_note("已经拿到工具结果，我继续整合判断是否还需要下一步。", "tool_observation")
            repeated_read_only = any(
                count >= 1
                for fingerprint, count in duplicate_call_counts.items()
                if fingerprint in duplicate_call_keys
            )
            runtime_repair_hint = (
                "相同的只读 Tool 与参数已经返回过成功结果；不要再次调用它。"
                "请直接基于已有 Tool Result 完成回答，或改用能产生新信息的参数。"
                if repeated_read_only else None
            )
            if repeated_read_only:
                force_tool_call = False
            pending_user = None

    @staticmethod
    def _is_time_sensitive_request(message: str | None) -> bool:
        if not message:
            return False
        if re.search(
            r"(?:\u6700\u8fd1|\u6700\u65b0|\u8fd1\u671f|\u5f53\u524d|\u76ee\u524d|\u73b0\u5728|\u4eca\u5929|\u660e\u5929|\u6628\u5929|\u622a\u81f3)",
            message,
        ):
            return True
        return bool(re.search(
            r"(?:最近|最新|近期|当前|目前|现在|如今|今年|本年|本月|本周|今天|今日|昨天|明天|截至|"
            r"latest|recent|current|currently|now|today|yesterday|tomorrow|this\s+(?:year|month|week)|up[- ]to[- ]date)",
            message,
            re.I,
        ))

    @staticmethod
    def _tool_expected_for_request(message: str | None) -> bool:
        """Return whether a request clearly asks for an external observation.

        Tool-enabled sessions still support ordinary conversation.  Strict JSON
        is therefore only enforced when the user's wording indicates a likely
        read/search/time/weather/workspace operation.
        """
        if not message:
            return False
        # Explicit user requests to expose/execute a Tool must never be
        # answered with a prose promise. This covers the common recovery
        # wording after a failed approval: “再调用一次工具/发起一次审批”。
        if re.search(
            r"(?:调用(?:一下|一次)?(?:工具|tool|install_skill)|"
            r"再(?:调用|发起)(?:一下|一次)?(?:工具|tool|审批|approve)|"
            r"发起(?:一下|一次)?(?:审批|approve)|"
            r"(?:approve|approval)\s*(?:再|again)?(?:一次)?|"
            r"否则我看不到)",
            message,
            re.I,
        ):
            return True
        if re.search(
            r"(?:^|[，,。！？\s])(?:来|现在|直接|开始|继续|重新|再)"
            r".{0,8}(?:安装|运行|执行|读取|搜索|创建|修改)(?:一下|试试|吧|这个|这些)?",
            message,
            re.I,
        ):
            return True
        # Mere topic words such as “文件”“Skill”“安装” or “推荐” are not
        # enough to force a Tool call.  Explanatory questions keep
        # ``tool_choice=auto``; only explicit external actions are required.
        return bool(required_capabilities(message, None))

    def _infer_tool_calls_from_request(
        self,
        message: str | None,
        tool_events: list[dict],
    ) -> list[dict]:
        """Recover simple deterministic Tool Calls from an explicit request.

        This is deliberately narrow: it handles direct file names, directory
        listing and current-time requests only. It is not a replacement for
        model tool selection and never infers a write or shell operation.
        """
        if not message or tool_events or self.tool_registry is None:
            return []
        available = {item["name"] for item in self.tool_registry.definitions()}
        text = message.strip()

        if "weather_forecast" in available and re.search(
            r"(?:天气|气温|降雨|预报)", text, re.I
        ):
            known_locations = (
                "上海", "北京", "天津", "重庆", "深圳", "广州", "杭州",
                "南京", "苏州", "成都", "武汉", "西安", "长沙", "青岛",
                "厦门", "宁波", "郑州", "合肥", "昆明", "哈尔滨",
            )
            location = next((name for name in known_locations if name in text), None)
            if location is None:
                match = re.search(
                    r"(?:查询|查一下|看看|请查|帮我查)?\s*"
                    r"([\u4e00-\u9fff]{2,12}?)(?="
                    r"(?:当前|今天|明天|未来\s*\d+\s*天|的)?"
                    r"(?:天气|气温|降雨|预报))",
                    text,
                )
                location = match.group(1) if match else None
            if location:
                future = re.search(r"未来\s*(\d+)\s*天", text)
                days = min(7, max(1, int(future.group(1)) + 1)) if future else 2
                return [{
                    "tool": "weather_forecast",
                    "args": {"location": location, "days": days},
                }]

        if "current_time" in available and re.search(
            r"(?:\u5f53\u524d\u65f6\u95f4|\u73b0\u5728\u51e0\u70b9|\u51e0\u70b9|\u4eca\u5929\u51e0\u53f7)",
            text,
        ):
            return [{"tool": "current_time", "args": {}}]
        if "list_dir" in available and re.search(
            r"(?:workspace|\u5de5\u4f5c\u533a|\u76ee\u5f55|\u6587\u4ef6\u7ed3\u6784|\u5217\u51fa\u6587\u4ef6)",
            text,
            re.I,
        ):
            return [{"tool": "list_dir", "args": {"path": "."}}]

        if "current_time" in available and re.search(
            r"(?:当前时间|现在几点|几点了|北京时间|今天几号)", text, re.I
        ):
            return [{"tool": "current_time", "args": {}}]

        if "read_file" in available:
            paths = re.findall(
                r"(?<![\w./-])(?:[\w.-]+[\\/])*[\w.-]+\.(?:py|md|json|txt|js|css|yaml|yml|toml)",
                text,
                re.I,
            )
            unique_paths = list(dict.fromkeys(paths))[:5]
            if unique_paths and re.search(
                r"(?:\u8bfb\u53d6|\u8bfb\u4e00\u4e0b|\u6253\u5f00|\u67e5\u770b|\u5206\u6790|\u6587\u4ef6)",
                text,
            ):
                return [
                    {"tool": "read_file", "args": {"path": path}}
                    for path in unique_paths
                ]
            if unique_paths and re.search(
                r"(?:读|读取|读一下|打开|查看|分析|文件)", text, re.I
            ):
                return [
                    {"tool": "read_file", "args": {"path": path}}
                    for path in unique_paths
                ]

        if "list_dir" in available and re.search(
            r"(?:workspace|工作区|目录|文件结构|列出文件|看看里面有什么)",
            text,
            re.I,
        ):
            return [{"tool": "list_dir", "args": {"path": "."}}]
        return []

    def _infer_search_call_from_confirmation(
        self,
        session: Session,
        message: str | None,
        assistant_content: str,
        tool_events: list[dict],
    ) -> list[dict]:
        """Recover a promised search when the user asks whether we searched.

        This is intentionally narrower than general promise detection.  It is
        only enabled for a read-only ``web_search`` tool and a confirmation
        question (``搜了吗``/``查过了吗``/``联网了吗``).  The query comes from
        the most recent substantive user request, never from assistant prose,
        so the model cannot invent a write or shell operation here.
        """
        if self.tool_registry is None or tool_events:
            return []
        if self.tool_registry.get("web_search") is None:
            return []
        text = (message or "").strip()
        if not re.search(
            r"(?:搜(?:了吗|过吗|过没有)|查(?:了吗|过吗|过没有)|联网(?:了吗|过吗)|"
            r"用(?:tool|工具)了吗|真的搜|实际搜|有没有搜)",
            text,
            re.I,
        ):
            return []
        if not re.search(r"(?:搜|搜索|查|查询|联网|上网)", assistant_content):
            return []
        current_user = text
        for item in reversed(session.messages):
            if item.get("role") != "user":
                continue
            candidate = str(item.get("content") or "").strip()
            if candidate == current_user or candidate.startswith("[tool_results]"):
                continue
            if candidate.startswith("[protocol_error]") or candidate.startswith("[attached_files]"):
                continue
            if len(candidate) < 6 or candidate.lower() in {"hello", "hi", "你好", "哈喽"}:
                continue
            return [{
                "tool": "web_search",
                # Keep the recovery call to the required argument only.  Some
                # deployments expose a minimal web_search schema (query only)
                # even though the built-in implementation accepts optional
                # ranking knobs; adding those optional fields here would make
                # an otherwise valid recovery fail schema validation.
                "args": {"query": candidate},
            }]
        return []

    @staticmethod
    def _ground_time_sensitive_query(query: str, time_output, original_message: str) -> str:
        if not isinstance(time_output, dict):
            return query
        iso = str(time_output.get("iso") or "")
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso)
        if match is None:
            return query
        year, month, day = match.groups()
        grounded = query
        # Respect explicit historical years supplied by the user. Otherwise,
        # stale years invented by the model are corrected deterministically.
        if not re.search(r"\b(?:19|20)\d{2}\b", original_message):
            grounded = re.sub(r"\b(?:19|20)\d{2}\b", year, grounded)
        date_prefix = f"截至 {year}-{month}-{day}"
        if date_prefix not in grounded:
            grounded = f"{date_prefix} {grounded}"
        return grounded

    def _call_model(
        self,
        session: Session,
        messages: list[Message],
        event_callback,
        turn_id,
        cancellation_event,
        user_message: str | None = None,
        tool_events: list[dict] | None = None,
        output_budget: int | None = None,
        force_tool_call: bool = False,
    ) -> tuple[str, bool]:
        self._check_cancelled(cancellation_event)
        messages = self._with_native_images(session, messages, user_message)
        started = time.perf_counter()
        streamed = False
        model_kwargs = self._native_model_kwargs(
            session,
            user_message,
            tool_events or [],
            force_tool_call=force_tool_call,
        )
        if output_budget is not None:
            model_kwargs["max_tokens"] = output_budget
        try:
            stream_method = getattr(self.model, "complete_stream", None)
            if event_callback is not None and callable(stream_method):
                chunks = []
                mode = None
                response_so_far = ""
                visible_length = 0
                received_first_chunk = False
                stream_kwargs = dict(model_kwargs)
                if cancellation_event is not None:
                    stream_kwargs["cancellation_event"] = cancellation_event
                stream = self._invoke_model_method(stream_method, messages, stream_kwargs)
                for chunk in self._iter_cancellable(stream, cancellation_event):
                    self._check_cancelled(cancellation_event)
                    if not isinstance(chunk, str) or not chunk:
                        continue
                    if not received_first_chunk:
                        received_first_chunk = True
                        self._emit(
                            event_callback,
                            "status",
                            phase="model_stream",
                            message="模型已开始生成，正在解析输出",
                        )
                    chunks.append(chunk)
                    response_so_far += chunk
                    if mode is None:
                        # Some compatible models prefix protocol JSON with a
                        # Markdown fence or a short introduction. Do not lock the
                        # stream into legacy mode from its first token; keep
                        # looking for a protocol discriminator while buffering.
                        if re.search(
                            r'"type"\s*:\s*"(?:final|tool_call|tool_calls)"',
                            response_so_far,
                        ):
                            mode = "protocol"
                    if mode == "protocol":
                        visible = self._partial_final_content(response_so_far)
                        if visible is not None and len(visible) > visible_length:
                            self._emit(event_callback, "assistant_delta", delta=visible[visible_length:])
                            visible_length = len(visible)
                            streamed = True
                response = "".join(chunks)
            else:
                completion_kwargs = dict(model_kwargs)
                if cancellation_event is not None:
                    completion_kwargs["cancellation_event"] = cancellation_event
                response = self._run_cancellable(
                    lambda: self._invoke_model_method(
                        self.model.complete, messages, completion_kwargs
                    ),
                    cancellation_event,
                )
        except AgentCancelled:
            raise
        except LLMCancelled as exc:
            raise AgentCancelled(str(exc)) from exc
        except Exception as exc:
            self._record_activity(
                session,
                "turn_failed",
                turn_id,
                {"stage": "model_call", "error": str(exc)},
            )
            self.store.save(session)
            raise
        if not isinstance(response, str) or not response.strip():
            error = EmptyAssistantResponse("模型返回了空 assistant 回复，请重试。")
            self._record_activity(
                session,
                "turn_failed",
                turn_id,
                {"stage": "assistant_validation", "error": str(error)},
            )
            self.store.save(session)
            raise error
        # A provider may finish just after Stop was requested. Fence every
        # side effect so a detached late response cannot mutate the Session.
        self._check_cancelled(cancellation_event)
        measured_ms = round((time.perf_counter() - started) * 1000, 2)
        exact = None
        pop_metrics = getattr(self.model, "pop_metrics", None)
        if callable(pop_metrics):
            exact = pop_metrics()
        input_chars = sum(self._message_content_length(item.get("content", "")) for item in messages)
        metric = {
            "model": (exact or {}).get("model", getattr(self.model, "model", "unknown")),
            "durationMs": (exact or {}).get("durationMs", measured_ms),
            "inputTokens": (exact or {}).get("inputTokens") or max(1, input_chars // 4),
            "outputTokens": (exact or {}).get("outputTokens") or max(1, len(response) // 4),
            "totalTokens": (exact or {}).get("totalTokens"),
            "estimated": not bool(exact and exact.get("totalTokens") is not None),
            "timeToFirstTokenMs": (exact or {}).get("timeToFirstTokenMs"),
            "chunkCount": (exact or {}).get("chunkCount"),
            "maxChunkChars": (exact or {}).get("maxChunkChars"),
            "finishReason": (exact or {}).get("finishReason"),
        }
        if metric["totalTokens"] is None:
            metric["totalTokens"] = metric["inputTokens"] + metric["outputTokens"]
        self._record_activity(session, "model_call", turn_id, metric)
        self.store.save(session)
        self._emit(event_callback, "metrics", **metric)
        if metric.get("finishReason") in {"length", "max_tokens", "max_output_tokens"}:
            # Keep the completion visible for diagnostics.  The normal
            # protocol/truncated-final guards below decide whether a retry is
            # needed; this status is intentionally non-destructive.
            self._emit(
                event_callback,
                "status",
                phase="model_output_limit",
                message="模型输出达到长度上限，当前结果可能不完整",
                finishReason=metric["finishReason"],
            )
            raise ModelOutputLimitError(
                metric["finishReason"],
                output_tokens=metric.get("outputTokens"),
                partial_content=response,
                streamed=streamed,
            )
        self._check_cancelled(cancellation_event)
        return response, streamed

    @staticmethod
    def _next_output_budget(previous: int | None, output_tokens: int | None) -> int:
        """Return a bounded larger budget for a length-stop retry.

        Providers differ in their default completion limits, so the first
        retry is derived from the observed partial size.  A hard ceiling keeps
        a pathological provider from causing an unbounded token request.
        """
        observed = int(output_tokens or 0)
        if previous is None:
            return min(8192, max(2048, observed * 2, observed + 1024))
        return min(16384, max(previous * 2, previous + 1024))

    def _native_model_kwargs(
        self,
        session: Session,
        user_message: str | None,
        tool_events: list[dict],
        *,
        force_tool_call: bool = False,
    ) -> dict:
        """Build native FC arguments only for models that advertise support.

        Scripted/test models and legacy providers keep their old one-argument
        interface.  LLMClient itself handles capability probing and fallback.
        """
        if self.tool_registry is None or not getattr(self.model, "supports_native_tools", False):
            return {}
        hidden_skill_tools = (
            {"use_skill"}
            if session.active_skill
            else {"read_skill_resource", "run_skill_script"}
        )
        mentioned_registered_skill = False
        skill_registry = getattr(self, "skill_registry", None)
        if skill_registry is not None and user_message:
            lowered_message = user_message.lower()
            try:
                mentioned_registered_skill = any(
                    str(item.get("name", "")).lower() in lowered_message
                    for item in skill_registry.index()
                    if item.get("name")
                )
            except (AttributeError, OSError, ValueError):
                mentioned_registered_skill = False
        if mentioned_registered_skill:
            # A named entry from the local Skill index is already installed.
            # Removing install_skill for this turn prevents speculative
            # ClawHub/GitHub downloads and routes the model to use_skill.
            hidden_skill_tools.add("install_skill")

        definitions = [
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item.get("description", ""),
                    "parameters": item.get("input_schema", {
                        "type": "object", "properties": {}, "additionalProperties": False
                    }),
                },
            }
            for item in self.tool_registry.definitions()
            if item.get("name") not in hidden_skill_tools
        ]
        if not definitions:
            return {}
        kwargs = {"tools": definitions}
        install_failures = self._install_skill_failure_count(session)
        retry_tool = self._failed_install_requires_retry(tool_events, install_failures)
        if retry_tool:
            kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": retry_tool},
            }
        elif force_tool_call:
            kwargs["tool_choice"] = "required"
        elif self._tool_expected_for_request(user_message) and not tool_events:
            kwargs["tool_choice"] = "required"
        else:
            kwargs["tool_choice"] = "auto"
        return kwargs

    @staticmethod
    def _failed_install_requires_retry(
        tool_events: list[dict] | None,
        failure_count: int | None = None,
    ) -> str | None:
        """Force a fresh install_skill call after a failed install attempt.

        A failed approval result is not a completed task. On the next internal
        loop iteration the model must repair the source URL and emit a new
        Tool Call, instead of producing another natural-language promise.
        """
        if failure_count is not None and failure_count >= AgentRuntime.MAX_INSTALL_SKILL_FAILURES:
            return None
        for event in reversed(tool_events or []):
            if not isinstance(event, dict) or event.get("tool") != "install_skill":
                continue
            result = event.get("result") or {}
            if isinstance(result, dict) and result.get("success") is False:
                return "install_skill"
            break
        return None

    @classmethod
    def _install_skill_failure_count(cls, session: Session | None) -> int:
        """Count durable failed install_skill executions in one session."""
        if session is None:
            return 0
        return sum(
            1
            for event in (getattr(session, "activity", []) or [])
            if event.get("type") == "tool_result"
            and (event.get("data") or {}).get("tool") == "install_skill"
            and (event.get("data") or {}).get("success") is False
        )

    @staticmethod
    def _invoke_model_method(method, messages, kwargs):
        if not kwargs:
            return method(messages)
        try:
            return method(messages, **kwargs)
        except TypeError as exc:
            # A third-party ChatModel may expose the old one-argument API even
            # if it happens to define ``supports_native_tools``.  Retry only
            # for the signature mismatch; provider TypeErrors must propagate.
            text = str(exc).lower()
            if "unexpected keyword" not in text and "positional argument" not in text:
                raise
            return method(messages)

    def _looks_like_unfinished_tool_promise(
        self,
        content: str,
        tool_events: list[dict] | None = None,
        session: Session | None = None,
    ) -> bool:
        if self.tool_registry is None:
            return False
        available = {item["name"] for item in self.tool_registry.definitions()}
        tail = content.strip()[-600:]
        clean_tail = tail.strip()
        clean_content = content.strip()
        # A completed answer often ends with an optional follow-up offer such
        # as “要不要我再帮你查一下？”.  When this turn already has real Tool
        # observations and the body clearly reports those results, that offer
        # is not an unfinished promise.  Treating it as one used to erase a
        # correct answer and start a needless protocol retry.
        observed_tools = {
            str(item.get("tool") or "")
            for item in (tool_events or [])
        }
        has_observation = bool(observed_tools)
        # A successful observation plus a past-tense evidence marker means
        # the model is reporting the result it already received.  Phrases
        # such as “通过搜索我拿到了……” or “真实搜索结果如下” used to be
        # mistaken for a new promise, which erased a correct answer and
        # started an unrelated recovery search.
        successful_observation = any(
            bool((item.get("result") or {}).get("success"))
            for item in (tool_events or [])
            if isinstance(item, dict)
        )
        grounded_result = re.search(
            r"(?:搜索结果|通过搜索|真实搜索|搜索到的|已经(?:读取|搜索|查到)|"
            r"结果如下|拿到了.+(?:仓库|文件|资料).+(?:信息|内容))",
            clean_content,
            re.I,
        )
        if successful_observation and grounded_result:
            # Only a clearly future/immediate action keeps the promise guard
            # active; a past-tense explanation is already a final answer.
            future_action = re.search(
                r"(?:现在|接下来|随后|然后|继续|再去).{0,16}(?:开始|调用|进行|搜索|查询|读取|查看)",
                clean_tail,
                re.I,
            )
            if not future_action:
                return False
        # Do not let an unrelated observation (for example current_time)
        # authorize a final answer that claims a web search already happened.
        # The retry guard is intentionally tied to the tool family the model
        # says it used, so a missing web_search result is still recoverable.
        if has_observation and re.search(r"(?:搜索|搜到|搜索到|联网查|网上查)", clean_content):
            has_observation = bool(observed_tools & {"web_search", "tavily_search"})
        optional_followup = re.search(
            r"(?:要不要|是否需要|需要我|如果你想).{0,48}"
            r"(?:再|继续)?(?:帮你)?(?:搜|搜索|查|查询|核实|展开|介绍)",
            clean_tail,
            re.I,
        )
        reports_results = re.search(
            r"(?:根据(?:最新)?(?:搜索|查询|查到)|(?:搜索|查询)(?:结果|到的)|"
            r"以下(?:是|为)|汇总|关键(?:情况|信息)|目前(?:情况|来看|是)|"
            r"已(?:查到|获取))",
            clean_content,
            re.I,
        )
        if has_observation and optional_followup and reports_results:
            return False
        # A completed result may also end with a policy-style promise such as
        # “以后这类问题我会先搜再说”.  That is not an instruction to perform
        # another search in the current turn.  Only treat a post-result action
        # as unfinished when it is explicitly immediate (现在/接下来/然后…)
        # rather than a general future commitment.
        if has_observation and reports_results:
            immediate_followup = re.search(
                r"(?:现在|接下来|随后|然后|下一步|立刻|马上|让我(?:再)?).{0,40}"
                r"(?:搜|搜索|查|查询|检索|读取|查看|调用|执行)",
                clean_tail,
                re.I,
            )
            if not immediate_followup:
                return False
        completed = re.search(
            r"(?:已成功|刚才已经|此前已经|已经\s*(?:明确|成功|完成|查到|读取|搜索|查询|使用|调用)|结果如下|"
            r"(?:搜索|查询|分析)结果(?:如下|显示|表明|为|是|包括|：|:)|"
            r"(?:读取|执行|搜索|查询|分析|处理|创建|安装|下载).{0,8}(?:完成|成功|结束|好了)|"
            r"(?:已安装|安装成功|下载完成|下载成功))",
            clean_tail,
            re.I,
        )
        direct_promise = re.search(
            r"(?:^|[。！？\n])\s*(?:好的[，,]?\s*)?"
            r"(?:"
            r"(?:我|我们|让我).{0,36}(?:看一下|看看|查看|读取|打开|搜索|查询|检查|运行|执行|调用|启动|分析|尝试|发起)"
            r"|(?:现在|接下来|下面|先|马上|立即|重新).{0,16}"
            r"(?:看一下|看看|查看|读取|打开|搜索|查询|检查|运行|执行|调用|启动|分析|尝试|发起)"
            r")",
            clean_tail,
            re.I,
        )
        # Keep this deliberately straightforward.  Models frequently use a
        # short natural-language bridge such as “让我看看当前 Workspace”
        # instead of naming list_dir.  It is still an unfinished action, not
        # a final answer, and must re-enter the Tool loop.
        workspace_or_file_promise = re.search(
            r"(?:让我|我来|我先|现在|接下来).{0,40}"
            r"(?:看看|查看|浏览|列出|读取|读一下|打开|检查)"
            r".{0,48}(?:workspace|工作区|目录|文件|文件夹|路径|附件)",
            clean_tail,
            re.I,
        )
        asks_for_permission_instead_of_calling = re.search(
            r"(?:需要|请|麻烦).{0,28}(?:确认|允许|批准|审批).{0,24}(?:命令|操作|执行|运行|Shell|工具)?",
            clean_tail,
            re.I,
        )
        claimed_request_without_protocol = re.search(
            r"(?:重新|再次|已经)?\s*(?:发起|提交).{0,20}(?:Shell|工具|命令|审批).{0,12}(?:请求|申请)",
            clean_tail,
            re.I,
        )
        claimed_approval_without_call = re.search(
            r"(?:我|我们)?\s*(?:已经|已|刚刚)?\s*发起(?:了)?"
            r"(?:了|啦)?\s*.{0,24}(?:approve|approval|审批|批准|安装|执行)",
            clean_tail,
            re.I,
        )
        explicit_tool_promise = re.search(
            r"(?:^|[。！？\n])\s*(?:好的[，,]?\s*)?"
            r"(?:直接|现在|马上|重新|再)?\s*(?:调用|使用|发起)\s*"
            r"(?:`?(?:install_skill|tool|工具|审批|approve)`?)",
            clean_tail,
            re.I,
        )
        failed_install_retry_promise = bool(
            self._failed_install_requires_retry(
                tool_events, self._install_skill_failure_count(session)
            )
        ) and re.search(
            r"(?:换|改|更换).{0,32}(?:格式|URL|链接|地址).{0,32}"
            r"(?:再试|重试|重新).{0,40}(?:approve|审批|安装|install_skill)",
            clean_tail,
            re.I,
        )
        # Installation promises are especially easy to mistake for a final
        # answer: the model often says “我试试用 GitHub 直接装……” and then
        # stops without emitting install_skill.  Treat that sentence as a
        # pending action so the next protocol-repair round can request the
        # actual Tool Call (and therefore create Approval) instead of letting
        # the model claim that Approval was already sent.
        install_promise = re.search(
            r"(?:我|我们|让我|我先|现在|接下来|下面).{0,80}"
            r"(?:试试|尝试|准备|直接|马上|立即|先)?[^。！？!?\n]{0,40}"
            r"(?:安装|装(?:一下|个|它)?|下载).{0,40}"
            r"(?:skill|技能|GitHub|ClawHub|仓库|它)?",
            clean_tail,
            re.I,
        )
        if not completed and (
            direct_promise
            or workspace_or_file_promise
            or asks_for_permission_instead_of_calling
            or claimed_request_without_protocol
            or claimed_approval_without_call
            or explicit_tool_promise
            or failed_install_retry_promise
            or install_promise
        ):
            return True
        # Strong generic signal: the model names an available tool while
        # narrating an action it is about to perform. Completed descriptions
        # (“已经使用…”) are deliberately excluded.
        if not re.search(r"(?:已经|已成功|刚才已经|此前已经).{0,24}(?:使用|调用|通过)", tail, re.I):
            for tool_name in available:
                if not re.search(rf"`?{re.escape(tool_name)}`?", tail, re.I):
                    continue
                if re.search(
                    rf"(?:我|现在|接下来|下面|随后).{{0,24}}"
                    rf"(?:直接|立即|马上|准备|将|会|来)?(?:使用|用|调用|通过).{{0,12}}"
                    rf"`?{re.escape(tool_name)}`?.{{0,40}}(?:读取|搜索|查询|查看|分析|执行|获取)?",
                    tail,
                    re.I,
                ):
                    return True
        patterns = []
        if "web_search" in available:
            patterns.extend([
                r"(?:我来|我先|现在开始|接下来(?:会|将)?|马上)(?:搜索|搜一下|查询|查一下)",
                r"(?:开始|进行)(?:进一步)?搜索[。！!]?\s*$",
            ])
            last_line = next((line.strip().rstrip("。！？!?:：") for line in reversed(content.splitlines()) if line.strip()), "")
            if re.match(
                r"^(?:我(?:来|先|会|将|帮你)?|先|现在(?:开始)?|接下来(?:我)?(?:会|将)?|下面(?:我)?(?:会|将)?)(?:搜|搜索|查|查询|检索)",
                last_line,
                re.I,
            ):
                return True
            # The promise is often introduced by a clause such as
            # “至于台风，我帮你查一下……”. Inspect the final sentence rather
            # than requiring the action phrase to begin the whole line.
            last_sentence = re.split(r"[。！？!?]", last_line.rstrip("。！？!?"))[-1]
            if re.search(
                r"(?:我(?:来|先|会|将|帮你)?|让我们|先|现在|接下来|下面)"
                r".{0,24}(?:再|去)?(?:搜|搜索|查|查询|检索)(?:一下)?[^。！？!?]*$",
                last_sentence,
                re.I,
            ):
                return True
            if re.search(
                r"(?:我|我们|让我们).{0,32}(?:重写|改写|调整|组织).{0,18}"
                r"(?:查询|搜索|检索|query).{0,18}(?:再|去)?(?:搜|搜索|检索)?[^。！？!?]*$",
                last_sentence,
                re.I,
            ):
                return True
        if {"read_file", "read_document"} & available:
            patterns.append(r"(?:我来|我先|现在开始|接下来(?:会|将)?)(?:读取|查看|打开|分析)(?:文件|文档|附件)?")
        if {"read_attachment", "read_document", "ocr_image"} & available:
            last_line = next((line.strip().rstrip("。！？!?:：") for line in reversed(content.splitlines()) if line.strip()), "")
            completed_read = re.search(
                r"(?:已经|已成功|刚才已经).{0,20}(?:读取|查看|打开|分析|识别)(?:完成|完毕|了)?",
                last_line,
                re.I,
            )
            # A final answer often closes with an optional offer such as
            # “有其他文件需要我分析吗？”. It contains “我…分析” but is not
            # a promise to perform another read in the current turn. The old
            # rule reset a complete streamed answer and left only an internal
            # retry segment in the transcript.
            optional_read_offer = re.search(
                r"(?:要不要我|是否需要我|还需要我|有.{0,24}需要我)"
                r".{0,32}(?:读取|查看|打开|分析|识别)(?:一下)?(?:吗|么)?$",
                last_line,
                re.I,
            )
            promised_read = re.search(
                r"(?:我|先|现在|接下来|下面).{0,36}(?:读取|查看|打开|分析|识别)"
                r"[^。！？!?]*$",
                last_line,
                re.I,
            )
            if promised_read and not completed_read and not optional_read_offer:
                return True
        return any(re.search(pattern, content, re.I) for pattern in patterns)

    @staticmethod
    def _missing_required_tool_observation(
        user_message: str | None,
        tool_events: list[dict],
    ) -> str | None:
        """Reject a final answer that ignores an explicit read/browse request."""
        text = (user_message or "").strip()
        if not text:
            return None
        tools_used = {str(item.get("tool") or "") for item in tool_events}
        direct_read = re.search(
            r"(?:读一下|读一读|读取|打开|解析|分析).{0,24}"
            r"(?:这些|这个|该|上述|上面)?(?:文件|文档|附件|pdf|pptx|docx)",
            text,
            re.I,
        )
        read_intent = any(
            chr(codepoint) in text
            for codepoint in (0x8bfb, 0x67e5, 0x6253, 0x89e3, 0x770b)
        )
        file_reference = any(
            marker in text
            for marker in (
                "pdf", "pptx", "docx", "[attached_files]",
                chr(0x6587) + chr(0x4ef6),
                chr(0x6587) + chr(0x6863),
                chr(0x9644) + chr(0x4ef6),
            )
        )
        direct_read = bool(direct_read) or (read_intent and file_reference)
        if direct_read and not tools_used.intersection(
            {"read_file", "read_document", "read_attachment", "ocr_image"}
        ):
            return (
                "用户明确要求读取文件/附件，但本轮尚未得到任何读取 Tool Result。"
                "不得直接给 final；请先调用 read_file、read_document、read_attachment 或 ocr_image。"
            )
        browse_workspace = re.search(
            r"(?:查看|看看|列出|浏览|检查).{0,28}"
            r"(?:workspace|工作区|目录|文件夹|文件)",
            text,
            re.I,
        )
        browse_intent = any(
            marker in text
            for marker in (
                chr(0x67e5) + chr(0x770b), chr(0x770b) + chr(0x770b),
                chr(0x5217) + chr(0x51fa), chr(0x6d4f) + chr(0x89c8),
            )
        )
        workspace_reference = any(
            marker in text
            for marker in ("workspace", chr(0x5de5) + chr(0x4f5c) + chr(0x533a),
                           chr(0x76ee) + chr(0x5f55), chr(0x6587) + chr(0x4ef6))
        )
        browse_workspace = bool(browse_workspace) or (browse_intent and workspace_reference)
        if browse_workspace and not tools_used.intersection(
            {"list_dir", "read_file", "read_document", "read_attachment"}
        ):
            return (
                "用户明确要求查看 Workspace/目录，但本轮尚未得到任何相关 Tool Result。"
                "不得直接给 final；请先调用 list_dir 或相应读取 Tool。"
            )
        return None

    @staticmethod
    def _pending_attachment_continuation(
        tool_events: list[dict],
    ) -> dict | None:
        """Return the next resumable attachment read, if one is still pending.

        State is derived only from successful Tool Results.  A later complete
        chunk clears an earlier pending cursor, while a failed continuation
        blocks automatic retries so malformed files cannot create a loop.
        """
        states: dict[str, dict | None] = {}
        order: list[str] = []
        for event in tool_events or []:
            if str(event.get("tool") or "") != "read_attachment":
                continue
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            attachment_id = str(args.get("attachment_id") or "")
            if not attachment_id:
                continue
            if attachment_id not in order:
                order.append(attachment_id)
            result = event.get("result") if isinstance(event.get("result"), dict) else {}
            if result.get("success") is False or result.get("error"):
                states[attachment_id] = None
                continue
            output = result.get("output")
            if not isinstance(output, dict) or not output.get("truncated"):
                states.pop(attachment_id, None)
                continue

            next_offset = output.get("nextOffset")
            next_page = output.get("nextPage")
            continuation_args: dict[str, object] = {"attachment_id": attachment_id}
            if isinstance(next_offset, int) and next_offset >= 0:
                continuation_args["start_char"] = next_offset
                if isinstance(args.get("max_chars"), int):
                    continuation_args["max_chars"] = args["max_chars"]
            elif isinstance(next_page, int) and next_page >= 1:
                continuation_args["start_page"] = next_page
                if isinstance(args.get("end_page"), int) and args["end_page"] >= next_page:
                    continuation_args["end_page"] = args["end_page"]
            else:
                # The result disclosed truncation but has no safe cursor
                # (for example one exceptionally long PDF page).  The model
                # must report that limitation instead of retrying forever.
                states[attachment_id] = None
                continue
            states[attachment_id] = {
                "tool": "read_attachment",
                "args": continuation_args,
            }

        for attachment_id in order:
            pending = states.get(attachment_id)
            if pending is not None:
                return pending
        return None

    @staticmethod
    def _attachment_continuation_for(
        tool_events: list[dict],
        attachment_id: str,
    ) -> dict | None:
        """Return the latest safe continuation cursor for one attachment.

        Two large attachments may be read in the same turn. Tracking them
        independently prevents a repeated default call from replaying page 1
        while another attachment is still being processed.
        """
        if not attachment_id:
            return None
        state: dict | None = None
        for event in tool_events or []:
            if str(event.get("tool") or "") != "read_attachment":
                continue
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            if str(args.get("attachment_id") or "") != attachment_id:
                continue
            result = event.get("result") if isinstance(event.get("result"), dict) else {}
            if result.get("success") is False or result.get("error"):
                # A malformed retry must not erase an earlier valid cursor.
                continue
            output = result.get("output")
            if not isinstance(output, dict) or not output.get("truncated"):
                state = None
                continue
            continuation_args: dict[str, object] = {
                "attachment_id": attachment_id
            }
            next_offset = output.get("nextOffset")
            next_page = output.get("nextPage")
            if isinstance(next_offset, int) and next_offset >= 0:
                continuation_args["start_char"] = next_offset
            elif isinstance(next_page, int) and next_page >= 1:
                continuation_args["start_page"] = next_page
            else:
                state = None
                continue
            state = {
                "tool": "read_attachment",
                "args": continuation_args,
            }
        return state

    @staticmethod
    def _failed_tool_observation_claim(
        content: str | None,
        tool_events: list[dict],
    ) -> str | None:
        """Return a repair hint when a final claims success after a failed read.

        A model can receive a ``read_file`` error and still continue a prose
        preamble with phrases such as “已经完整读取了内容”.  Counting the
        tool name alone as an observation is unsafe: the result must also be
        successful before the model may make a completion claim.
        """
        successful_targets: set[tuple[str, str]] = set()
        failed_targets: set[tuple[str, str]] = set()
        for event in tool_events or []:
            tool_name = str(event.get("tool") or "")
            if tool_name not in {
                "read_file",
                "read_document",
                "read_attachment",
                "ocr_image",
            }:
                continue
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            target = str(
                args.get("attachment_id")
                or args.get("path")
                or args.get("filename")
                or ""
            )
            key = (tool_name, target)
            result = event.get("result") or {}
            if result.get("success") is False or result.get("error"):
                failed_targets.add(key)
            else:
                successful_targets.add(key)
        # A malformed pagination attempt followed by a usable result is
        # recovered. It must not reopen an otherwise complete answer.
        unresolved_failures = failed_targets - successful_targets
        if not unresolved_failures or not content:
            return None
        def chars(*codepoints: int) -> str:
            return "".join(chr(codepoint) for codepoint in codepoints)

        read_verbs = (
            chars(0x8bfb, 0x53d6), chars(0x8bfb, 0x5b8c),
            chars(0x8bfb, 0x5230), chars(0x770b, 0x5b8c),
        )
        nouns = (
            chars(0x5185, 0x5bb9), chars(0x6587, 0x4ef6),
            chars(0x9644, 0x4ef6), chars(0x9898, 0x76ee),
            chars(0x8d44, 0x6599),
        )
        strong_claim = any(
            marker in content
            for marker in (
                chars(0x5df2), chars(0x5b8c, 0x6574), chars(0x5168, 0x90e8),
                chars(0x90fd), chars(0x6210, 0x529f), chars(0x5b8c, 0x5168),
                chars(0x638c, 0x63e1),
            )
        )
        if (
            any(verb in content for verb in read_verbs)
            and any(noun in content for noun in nouns)
            and strong_claim
        ) or (
            chars(0x5b8c, 0x5168) in content
            and chars(0x638c, 0x63e1) in content
        ):
            return "read Tool returned failure; the final answer must not claim the file was read successfully."
        return None

    @staticmethod
    def _should_emit_progress_notes(user_message: str | None) -> bool:
        text = user_message or ""
        if "[attached_files]" in text:
            return True
        if len(text) >= 40:
            return True
        return bool(re.search(
            r"(?:最新|最近|搜索|查询|查查|检索|读取|附件|文件|PDF|PPT|Word|docx|pptx|pdf|"
            r"代码|报错|错误|修复|运行|检查|分析|对比|整理|总结|生成|导出|天气|台风)",
            text,
            re.I,
        ))

    def _emit_assistant_note(
        self,
        event_callback,
        session: Session,
        turn_id: str | None,
        text: str,
        phase: str,
    ) -> None:
        if not text:
            return
        self._record_activity(
            session,
            "assistant_note",
            turn_id,
            {"content": text, "phase": phase},
        )
        self._emit(event_callback, "assistant_note", content=text, phase=phase)

    @staticmethod
    def _tool_progress_message(calls) -> str:
        names = [getattr(call, "tool", "") for call in calls if getattr(call, "tool", "")]
        unique = []
        for name in names:
            if name not in unique:
                unique.append(name)
        if not unique:
            return "我先调用工具获取必要信息，再继续处理。"
        labels = {
            "web_search": "联网搜索",
            "current_time": "当前时间",
            "weather_forecast": "天气查询",
            "read_attachment": "附件读取",
            "read_file": "文件读取",
            "list_dir": "目录检查",
            "run_shell": "Shell",
            "ocr_image": "OCR",
        }
        readable = "、".join(labels.get(name, name) for name in unique[:3])
        if len(unique) > 3:
            readable += f"等 {len(unique)} 个工具"
        return f"我先用 {readable} 获取必要信息，再继续判断。"

    def _attached_file_calls(self, user_message: str | None) -> list[dict]:
        if not user_message or self.tool_registry is None:
            return []
        match = re.search(r"\[attached_files\]\s*(\[[^\r\n]*\])", user_message)
        if not match:
            return []
        try:
            attachments = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
        if not isinstance(attachments, list):
            return []
        calls = []
        for item in attachments[:5]:
            if not isinstance(item, dict) or not isinstance(item.get("attachmentId"), str):
                continue
            if is_image_attachment(item) and bool(getattr(self.model, "supports_vision", False)):
                # The selected image will be carried by the model request
                # itself, so it does not need an OCR preflight.
                continue
            call = self._attachment_tool_call(item)
            if call:
                calls.append(call)
        return calls

    def _referenced_attachment_calls(self, session: Session, user_message: str | None) -> list[dict]:
        if not user_message or self.tool_registry is None or not session.attachments:
            return []
        normalized = re.sub(r"\s+", "", user_message).lower()
        if not any(keyword in normalized for keyword in (
            "附件", "pdf", "文档", "文件", "培养方案", "课程", "重新读", "重读", "读取", "分析",
        )):
            return []
        matched: list[dict] = []

        index_match = re.search(r"第([一二三四五六七八九十\d]+)个(?:附件|文件|文档|pdf)?", normalized)
        if index_match:
            index = _chinese_or_digit_index(index_match.group(1))
            if index is not None and 0 <= index < len(session.attachments):
                call = self._attachment_tool_call(session.attachments[index])
                return [call] if call else []

        for item in session.attachments:
            filename = str(item.get("filename", ""))
            stem = Path(filename).stem.lower()
            compact_stem = re.sub(r"[\s_\-()（）\[\]【】.·]+", "", stem)
            tokens = [
                token.lower()
                for token in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{2,}", stem)
                if len(token) >= 2
            ]
            if compact_stem and compact_stem in normalized:
                call = self._attachment_tool_call(item)
            elif tokens and any(token in normalized for token in tokens):
                call = self._attachment_tool_call(item)
            else:
                call = None
            if call and call not in matched:
                matched.append(call)
        return matched[:5]

    def _attachment_tool_call(self, item: dict) -> dict | None:
        if not isinstance(item.get("attachmentId"), str) or self.tool_registry is None:
            return None
        filename = str(item.get("filename", "")).lower()
        content_type = str(item.get("contentType", "")).lower()
        is_image = content_type.startswith("image/") or Path(filename).suffix in {
            ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
        }
        tool_name = "ocr_image" if is_image else "read_attachment"
        if self.tool_registry.get(tool_name) is None:
            return None
        return {"tool": tool_name, "args": {"attachment_id": item["attachmentId"]}}

    def _with_native_images(
        self,
        session: Session,
        messages: list[Message],
        user_message: str | None,
    ) -> list[Message]:
        """Attach selected images to the request without persisting base64."""
        if not bool(getattr(self.model, "supports_vision", False)) or not user_message:
            return messages
        selected_ids = _selected_attachment_ids(user_message)
        if not selected_ids:
            return messages
        image_parts: list[dict] = []
        for metadata in session.attachments:
            if metadata.get("attachmentId") not in selected_ids:
                continue
            if not is_image_attachment(metadata):
                continue
            path = resolve_attachment_path(self.store, session.session_id, metadata)
            if not path.is_file():
                continue
            try:
                image_parts.append(image_content_part(path))
            except (OSError, ValueError):
                continue
            if len(image_parts) >= 4:
                break
        if not image_parts:
            return messages

        output = [dict(item) for item in messages]
        target = next(
            (
                index
                for index in range(len(output) - 1, -1, -1)
                if output[index].get("role") == "user"
                and "[attached_files]" in str(output[index].get("content") or "")
            ),
            None,
        )
        if target is None:
            target = next(
                (
                    index
                    for index in range(len(output) - 1, -1, -1)
                    if output[index].get("role") == "user"
                ),
                None,
            )
        if target is None:
            return messages
        text = output[target].get("content", "")
        if not isinstance(text, str):
            return messages
        native_note = (
            "\n\n[native_images_ready] 原始图片像素已随本次请求直接提供给视觉模型。"
            "请直接分析画面；不要调用 OCR 来判断自己是否能看图。"
            "只有用户要求精确抄录大量文字时，才把 OCR 作为补充。"
        )
        output[target]["content"] = [
            {"type": "text", "text": text + native_note},
            *image_parts,
        ]
        return output

    @staticmethod
    def _message_content_length(content) -> int:
        if isinstance(content, str):
            return len(content)
        if not isinstance(content, list):
            return len(str(content))
        return sum(
            len(str(part.get("text") or ""))
            if isinstance(part, dict) and part.get("type") == "text"
            else 0
            for part in content
        )

    @staticmethod
    def _is_attachment_retry(user_message: str | None) -> bool:
        if not user_message:
            return False
        normalized = re.sub(r"[\s。！？!?,，]", "", user_message).lower()
        if bool(re.fullmatch(
            r"(?:重试|再试(?:一次|试)?|重新(?:读|读取|分析)|继续(?:读|读取|分析)|"
            r"重读|再读(?:一次)?|重新来(?:一次)?)",
            normalized,
        )):
            return True
        return any(keyword in normalized for keyword in (
            "没读", "没读取", "读错", "不对", "不是这份", "重新读", "重读",
            "再读", "看错", "混了", "附件混", "凭空", "幻觉",
        ))

    def _execute_prefetched_attachments(
        self, session: Session, calls: list[dict], tool_events: list[dict],
        event_callback, turn_id, cancellation_event, step: int = 0,
    ) -> list[dict]:
        self._emit(event_callback, "status", phase="attachment_read", message="正在读取本轮附件")
        context = ToolExecutionContext(session.session_id)
        observations = []
        for call_index, call in enumerate(calls):
            self._check_cancelled(cancellation_event)
            tool_name, args = call["tool"], call["args"]
            call_id = self._tool_call_id(turn_id, step, call_index, call.get("callId"))
            tool = self.tool_registry.get(tool_name)
            self._emit(
                event_callback, "tool_call", tool=tool_name, args=args,
                callId=call_id, step=step,
                safetyLevel=tool.safety_level if tool else None,
            )
            self._record_activity(session, "tool_call", turn_id, {
                "tool": tool_name, "args": self._preview(args),
                "callId": call_id, "step": step,
                "safetyLevel": tool.safety_level if tool else None,
                "automatic": "attached_files",
            })
            if tool is None:
                result = ToolResult(
                    tool_name, False, error=f"未知 Tool：{tool_name}",
                    error_code="unknown_tool",
                )
            elif tool.safety_level != "read_only":
                result = ToolResult(
                    tool_name, False, error="附件预读取只允许 read_only Tool。",
                    error_code="unsafe_prefetch",
                )
            else:
                result = self.tool_registry.execute(tool_name, args, context)
            result_dict = self._serialise_tool_result(session, result)
            event = {
                "timestamp": utc_now(), "tool": tool_name,
                "callId": call_id, "step": step,
                "args": args, "result": result_dict,
            }
            session.tool_trace.append(self._audit_tool_event(event))
            tool_events.append(event)
            self._record_activity(session, "tool_result", turn_id, {
                "tool": tool_name, "args": self._preview(args),
                "callId": call_id, "step": step,
                "success": result.success, "error": result.error,
                "errorCode": result.error_code,
                "retryable": result.retryable,
                "attempts": result.attempts,
                "durationMs": result.duration_ms,
                "outputPreview": self._preview(result.output),
                "automatic": "attached_files",
            })
            self._emit(event_callback, "tool_result", **event)
            observations.append({**result_dict, "callId": call_id})
        return observations


    @staticmethod
    def _looks_like_truncated_final(content: str, raw_response: str) -> bool:
        """Return True for provider completions that are visibly truncated.

        A one-character final is almost always an interrupted JSON stream,
        not a useful answer.  Keep the guard conservative so a short Chinese
        acknowledgement remains valid while fragments such as ``L`` or
        ``D：`` are retried instead of being persisted.
        """
        value = (content or "").strip()
        if not value:
            return False
        has_cjk = any("\u4e00" <= char <= "\u9fff" for char in value)
        # A one-character ASCII fragment (the common ``L``/``D`` symptom)
        # is not a useful answer.  Keep ordinary short Chinese replies such
        # as ``好的`` valid.
        if len(value) <= 2 and not has_cjk:
            return len((raw_response or "").strip()) <= 160
        # A provider retry can also prepend a stray Latin character or
        # combining mark to a tiny Chinese fragment (for example
        # ``L̆试试``).  Require the combining mark (or a one/two-letter
        # prefix) so normal answers such as ``PDF 已读取完成`` are untouched.
        if len(value) <= 12 and (
            re.search(r"[\u0300-\u036f]", value)
            or re.match(r"^[A-Za-z]{1,2}[\u4e00-\u9fff]", value)
        ):
            return len((raw_response or "").strip()) <= 160
        return False

    @staticmethod
    def _strip_partial_tool_protocol(value: str) -> str:
        """Remove a complete or incomplete Tool JSON object from a prose tail.

        Compatible endpoints occasionally start a structured Tool Call inside
        ``final.content``.  Streaming can stop at any character, so the visible
        suffix may be anything from ``{`` to ``{"type":"tool_calls"...``.
        Such a suffix is protocol framing, not assistant prose.
        """
        text = str(value or "")
        brace = text.rfind("{")
        if brace < 0:
            return text
        compact = re.sub(r"\s+", "", text[brace:])
        targets = ('{"type":"tool_call"', '{"type":"tool_calls"')
        if compact == "{" or any(
            target.startswith(compact) or compact.startswith(target)
            for target in targets
        ):
            return text[:brace].rstrip()
        return text

    @staticmethod
    def _partial_final_content(text: str) -> str | None:
        """Decode the complete prefix of final.content from an incomplete JSON stream."""
        if not re.search(r'"type"\s*:\s*"final"', text):
            return None
        match = re.search(r'"content"\s*:\s*"', text)
        if not match:
            return ""
        source = text[match.end():]
        output = []
        index = 0
        escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
        while index < len(source):
            char = source[index]
            if char == '"':
                break
            if char != "\\":
                output.append(char)
                index += 1
                continue
            if index + 1 >= len(source):
                break
            escaped = source[index + 1]
            if escaped == "u":
                digits = source[index + 2:index + 6]
                if len(digits) < 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                    break
                output.append(chr(int(digits, 16)))
                index += 6
                continue
            if escaped not in escapes:
                break
            output.append(escapes[escaped])
            index += 2
        value = "".join(output)
        # A few compatible endpoints incorrectly put a Tool JSON object after
        # a natural-language prefix inside a ``final`` envelope.  While that
        # malformed response is still streaming, the JSON decoder sees the
        # opening brace as ordinary content and the UI briefly shows a lone
        # ``{``.  Once a Tool discriminator is present, the whole nested
        # object is protocol framing rather than user-facing prose.
        # Providers sometimes escape the nested object because it appears
        # inside ``final.content`` (for example ``{\"type\":\"tool_call\"``).
        # Match both the raw and escaped discriminator before removing its
        # framing brace from the provisional visible prefix.
        embedded = re.search(
            r'(?:^|\n)[ \t]*\{\s*"type"\s*:\s*"(?:tool_call|tool_calls)"',
            value,
        )
        if embedded:
            value = value[:embedded.start()].rstrip()
        return AgentRuntime._strip_partial_tool_protocol(value)

    @staticmethod
    def _strip_protocol_tail(value: str, raw_response: str = "") -> str:
        """Remove malformed embedded Tool JSON from visible process prose."""
        text = str(value or "")
        if re.search(r'\\?"type\\?"\s*:\s*\\?"(?:tool_call|tool_calls)\\?"', raw_response or ""):
            embedded = re.search(
                r'(?:^|\n)[ \t]*\{\s*"type"\s*:\s*"(?:tool_call|tool_calls)"',
                text,
            )
            if embedded:
                return text[:embedded.start()].rstrip()
        return AgentRuntime._strip_partial_tool_protocol(text)

    @staticmethod
    def _check_cancelled(cancellation_event) -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            raise AgentCancelled("用户已取消当前 Agent Turn。")

    def _run_cancellable(self, operation, cancellation_event):
        """Run a possibly blocking Tool without delaying Stop.

        Tool handlers are third-party/network code and cannot be force-killed
        safely.  A daemon worker lets the Agent Turn leave immediately; the
        handler is isolated from Session persistence and may finish in the
        background.  Normal calls keep the old synchronous path.
        """
        if cancellation_event is None:
            return operation()
        self._check_cancelled(cancellation_event)
        result_queue: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def invoke() -> None:
            try:
                result_queue.put((True, operation()))
            except BaseException as exc:
                result_queue.put((False, exc))

        Thread(target=invoke, name="sjtu-tool-call", daemon=True).start()
        while True:
            self._check_cancelled(cancellation_event)
            try:
                ok, value = result_queue.get(timeout=0.05)
            except Empty:
                continue
            if ok:
                return value
            raise value

    def _iter_cancellable(self, iterable, cancellation_event):
        """Bridge a blocking synchronous model stream into a cancellable iterator.

        A single daemon producer owns the SDK iterator. The Agent thread polls
        a queue, so Stop can end the Turn even when the provider blocks in
        ``next()`` or ignores the optional cancellation event. Late chunks are
        isolated from Session persistence and are discarded with the queue.
        """
        if cancellation_event is None:
            yield from iterable
            return
        self._check_cancelled(cancellation_event)
        chunk_queue: Queue[tuple[str, object]] = Queue()

        def produce() -> None:
            try:
                for item in iterable:
                    chunk_queue.put(("item", item))
                chunk_queue.put(("done", None))
            except BaseException as exc:
                chunk_queue.put(("error", exc))

        Thread(target=produce, name="sjtu-model-stream", daemon=True).start()
        while True:
            self._check_cancelled(cancellation_event)
            try:
                kind, value = chunk_queue.get(timeout=0.05)
            except Empty:
                continue
            if kind == "item":
                yield value
            elif kind == "done":
                return
            else:
                raise value

    @staticmethod
    def _record_activity(session: Session, event_type: str, turn_id: str | None, data: dict) -> None:
        session.activity.append(
            {
                "eventId": f"activity_{uuid.uuid4().hex[:12]}",
                "timestamp": utc_now(),
                "type": event_type,
                "turnId": turn_id,
                "data": data,
            }
        )

    @staticmethod
    def _user_message(content: str, source: str = "web") -> Message:
        return {
            "role": "user",
            "content": content,
            "metadata": {"source": source or "web"},
        }

    @classmethod
    def _audit_tool_event(cls, event: dict) -> dict:
        """Bound and redact the durable Tool audit copy.

        The model still receives the complete Tool Result through the current
        context, while ``Session.tool_trace`` is an audit log rather than a
        second unbounded attachment store.  This prevents API credentials,
        bearer tokens and huge document bodies from accumulating in logs.
        """
        return cls._audit_value(event)

    @staticmethod
    def _citation_counters(session: Session) -> dict[str, int]:
        """Return the next durable citation number for each citation family.

        Web search providers commonly restart their labels at ``[W1]`` for
        every request.  The session log, however, is one continuous document;
        scan the already persisted search results so new results receive
        labels that cannot collide with an earlier search in this session.
        """
        counters = {"W": 1}
        pattern = re.compile(r"^\[W(\d+)\]$")
        for citation in getattr(session, "citation_index", []) or []:
            if not isinstance(citation, dict):
                continue
            match = pattern.fullmatch(str(citation.get("label") or ""))
            if match:
                counters["W"] = max(counters["W"], int(match.group(1)) + 1)
        for event in getattr(session, "tool_trace", []) or []:
            if event.get("tool") != "web_search":
                continue
            result = event.get("result") or {}
            output = result.get("output") if isinstance(result, dict) else None
            citations = output.get("citations") if isinstance(output, dict) else None
            if not isinstance(citations, list):
                continue
            for citation in citations:
                label = citation.get("label") if isinstance(citation, dict) else None
                match = pattern.fullmatch(str(label or ""))
                if match:
                    counters["W"] = max(counters["W"], int(match.group(1)) + 1)
        return counters

    @classmethod
    def _citation_refs_from_tool_events(cls, tool_events: list[dict] | None) -> list[dict]:
        """Extract the exact source refs observed during one Agent turn.

        Citation labels are only meaningful inside the turn that produced
        them.  Persisting that small, immutable list on the final assistant
        message prevents compaction (or a later reused ``[W1]`` label) from
        making a visible answer point at an unrelated source.
        """
        refs: list[dict] = []
        seen: set[str] = set()
        for event in tool_events or []:
            result = event.get("result") if isinstance(event, dict) else None
            output = result.get("output") if isinstance(result, dict) else None
            citations = output.get("citations") if isinstance(output, dict) else None
            if not isinstance(citations, list):
                continue
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                label = str(citation.get("label") or "")
                if not label:
                    continue
                item = {
                    key: citation[key]
                    for key in (
                        "label", "kind", "url", "title", "filename", "page",
                        "slide", "section", "block", "attachmentId", "method", "path",
                    )
                    if citation.get(key) is not None
                }
                fingerprint = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                refs.append(item)
        return refs

    @classmethod
    def _assistant_message_with_citations(
        cls,
        content: str,
        tool_events: list[dict] | None = None,
    ) -> dict:
        message = {"role": "assistant", "content": content}
        refs = cls._citation_refs_from_tool_events(tool_events)
        if refs:
            message["metadata"] = {"citationRefs": refs}
        return message

    @staticmethod
    def _with_download_links(
        content: str,
        tool_events: list[dict] | None = None,
    ) -> str:
        """Ensure successful file-producing Tools leave a clickable result.

        Models sometimes describe a generated download without emitting valid
        Markdown, omit ``/api`` from the URL, or invent a stale download id.
        Tool results are authoritative: normalize matching model links, remove
        unverifiable download targets, and append any result the model omitted.
        """
        links: list[tuple[str, str]] = []
        seen: set[str] = set()
        for event in tool_events or []:
            result = event.get("result") if isinstance(event, dict) else None
            if not isinstance(result, dict) or result.get("success") is not True:
                continue
            output = result.get("output")
            if not isinstance(output, dict):
                continue
            url = output.get("downloadUrl")
            if not isinstance(url, str) or not url.startswith("/api/downloads/"):
                continue
            if url in seen:
                continue
            seen.add(url)
            filename = str(output.get("filename") or output.get("path") or "生成文件")
            links.append((filename, url))

        # Normalize download Markdown authored by the model.  In particular,
        # models often emit ``downloads/dl_xxx`` although the Gateway route is
        # ``/api/downloads/dl_xxx``.  Only ids returned by a successful Tool
        # result are allowed to remain clickable.
        authoritative = {url.rsplit("/", 1)[-1]: url for _, url in links}
        download_markdown = re.compile(
            r"(?P<label>\[[^\]\r\n]+\])\("
            r"(?:(?:https?://[^\s)]+)?/?(?:api/)?downloads/)"
            r"(?P<download_id>dl_[A-Za-z0-9_-]+)\)"
        )

        def normalize_download(match: re.Match) -> str:
            url = authoritative.get(match.group("download_id"))
            if url:
                return f"{match.group('label')}({url})"
            # Do not expose an invented or expired-looking link as clickable.
            return match.group("label")

        content = download_markdown.sub(normalize_download, content)

        # Repair a common model formatting mistake such as
        # ``[点击下载 report.md]``: it looks like a link but has no ``(URL)``
        # target and is therefore plain text.  Bind it only to a successful
        # Tool result from this turn; never guess a download id.
        bare_download_label = re.compile(
            r"(?P<label>\[[^\]\r\n]*(?:下载|download)[^\]\r\n]*\])(?!\s*\()",
            re.IGNORECASE,
        )

        def link_bare_download(match: re.Match) -> str:
            label = match.group("label")
            normalized_label = label.casefold()
            matches = [
                url for filename, url in links
                if str(filename).casefold() in normalized_label
            ]
            if len(matches) == 1:
                return f"{label}({matches[0]})"
            if len(links) == 1:
                filename, url = links[0]
                return f"[⬇ 下载 {filename}]({url})"
            return label

        content = bare_download_label.sub(link_bare_download, content)
        missing = [(filename, url) for filename, url in links if url not in content]
        if not missing:
            return content
        suffix = "\n".join(f"[⬇ 下载 {filename}]({url})" for filename, url in missing)
        return f"{content.rstrip()}\n\n{suffix}"

    @staticmethod
    def _current_turn_tool_events(
        session: Session,
        tool_events: list[dict] | None = None,
    ) -> list[dict]:
        """Return every Tool result from the current logical user turn.

        An approval pause splits one Agent turn into several Runtime calls, so
        the in-memory ``tool_events`` list only contains results produced after
        the latest approval. Recover earlier observations from Session messages
        before producing the final answer. This keeps a file created before a
        later Shell approval available as an authoritative download link.
        """
        recovered: list[dict] = []
        for message in reversed(session.messages):
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            content = message.get("content")
            metadata = message.get("metadata")
            # Normal user input marks the beginning of this logical turn.
            # Internal observations have no source metadata and remain part of
            # the scan because they carry results across approval pauses.
            if role == "user" and isinstance(metadata, dict) and metadata.get("source"):
                break
            if role not in {"user", "system"} or not isinstance(content, str):
                continue
            try:
                if content.startswith("[approval_result] "):
                    payload = json.loads(content[len("[approval_result] "):])
                    result = payload.get("toolResult")
                    if isinstance(result, dict):
                        recovered.append({
                            "tool": result.get("tool"),
                            "callId": payload.get("approvalId"),
                            "result": result,
                        })
                elif content.startswith("[tool_results] "):
                    payload = json.loads(content[len("[tool_results] "):])
                    if not isinstance(payload, list):
                        continue
                    for result in payload:
                        if isinstance(result, dict):
                            recovered.append({
                                "tool": result.get("tool"),
                                "callId": result.get("callId"),
                                "result": result,
                            })
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        recovered.reverse()
        recovered.extend(tool_events or [])
        return recovered

    @classmethod
    def _serialise_tool_result(
        cls,
        session: Session,
        result: ToolResult,
        citation_counters: dict[str, int] | None = None,
    ) -> dict:
        """Serialize a ToolResult and make web citations session-unique.

        This is intentionally done before the result is written to either the
        model context or ``tool_trace``.  The model therefore cites the same
        labels the web UI later renders, while older sessions remain readable
        through the UI's per-message citation fallback.
        """
        payload = result.to_dict()
        if result.tool != "web_search" or not isinstance(payload.get("output"), dict):
            return payload
        output = payload["output"]
        citations = output.get("citations")
        if not isinstance(citations, list):
            return payload
        counters = citation_counters if citation_counters is not None else cls._citation_counters(session)
        next_number = int(counters.get("W", 1))
        for citation in citations:
            if not isinstance(citation, dict) or not citation.get("url"):
                continue
            label = str(citation.get("label") or "")
            if re.fullmatch(r"\[W\d+\]", label):
                citation["label"] = f"[W{next_number}]"
                next_number += 1
        counters["W"] = next_number
        # ``compact`` intentionally removes old protocol messages. Keep a
        # small durable URL index so a later answer may cite an earlier
        # search (for example [W5]) without leaving a plain, dead marker.
        index = list(getattr(session, "citation_index", []) or [])
        for citation in citations:
            if not isinstance(citation, dict) or not citation.get("url"):
                continue
            item = {
                "label": str(citation.get("label") or ""),
                "url": str(citation.get("url") or ""),
                "title": str(citation.get("title") or citation.get("url") or ""),
                "kind": str(citation.get("kind") or "web"),
            }
            if not item["label"]:
                continue
            index = [old for old in index if old.get("label") != item["label"]]
            index.append(item)
        session.citation_index = index[-500:]
        return payload

    @classmethod
    def _audit_value(cls, value, *, key: str | None = None, depth: int = 0):
        sensitive = {
            "api_key", "apikey", "access_key", "authorization", "bearer",
            "client_secret", "secret", "token", "password", "private_key",
        }
        if key and key.lower().replace("-", "_") in sensitive:
            return "[已脱敏]"
        if depth > 6:
            return "[审计深度已限制]"
        if isinstance(value, dict):
            return {
                str(name): cls._audit_value(item, key=str(name), depth=depth + 1)
                for name, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._audit_value(item, depth=depth + 1) for item in value[:200]]
        if isinstance(value, str):
            redacted = redact_sensitive(value) or ""
            limit = 20_000 if key in {"output", "content", "raw", "text"} else 4_000
            if len(redacted) > limit:
                return redacted[:limit] + f"…[审计截断，原长 {len(redacted)}]"
            return redacted
        return value

    @staticmethod
    def _preview(value, limit: int = 1000):
        if value is None:
            return None
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return text if len(text) <= limit else text[:limit] + "…"

    @staticmethod
    def _tool_call_fingerprint(tool: str, args) -> str:
        """Return a stable per-turn identity for a Tool name and arguments."""
        try:
            encoded = json.dumps(
                args if isinstance(args, dict) else {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            encoded = repr(args)
        return f"{tool}:{encoded}"

    @staticmethod
    def _tool_result_from_dict(value: dict) -> ToolResult:
        """Rehydrate a prefetched observation for duplicate-call reuse."""
        return ToolResult(
            str(value.get("tool") or "unknown"),
            bool(value.get("success")),
            output=value.get("output"),
            error=value.get("error"),
            error_code=value.get("errorCode"),
            retryable=bool(value.get("retryable")),
            attempts=int(value.get("attempts") or 1),
            duration_ms=value.get("durationMs"),
        )

    @staticmethod
    def _tool_call_id(
        turn_id: str | None,
        step: int,
        index: int,
        explicit: str | None = None,
    ) -> str:
        """Return a stable ID for one Tool Call within an Agent Turn."""
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        owner = re.sub(r"[^A-Za-z0-9_-]", "", str(turn_id or "turn")) or "turn"
        return f"call_{owner}_{max(0, int(step))}_{max(0, int(index)) + 1}"

    @staticmethod
    def _emit(callback, event_type: str, **payload) -> None:
        if callback is None:
            return
        try:
            callback({"type": event_type, **payload})
        except Exception:
            # UI telemetry must never break the Agent Turn.
            pass

    def _persist_assistant_segment(
        self,
        session: Session,
        turn_id: str | None,
        content: str,
        *,
        phase: str,
    ) -> None:
        """Persist a display-only assistant segment between Tool calls.

        These entries are deliberately marked internal/displayOnly. The
        conversation view filters them from model context and message counts,
        while the Web/CLI transcript can still render the complete process
        after a reload or the post-turn session refresh.
        """
        text = str(content or "").strip()
        if not text:
            return
        max_chars = 20_000
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars] + "\n[过程片段已截断，仅保留前 20000 个字符]"
        metadata = {
            "internal": True,
            "displayOnly": True,
            "kind": "assistant_segment",
            "turnId": turn_id,
            "phase": phase,
            "truncated": truncated,
        }
        # A reconnect/retry can encounter the same reset twice. Avoid adding
        # an identical adjacent process bubble to the durable transcript.
        if session.messages:
            previous = session.messages[-1]
            if (
                previous.get("role") == "assistant"
                and (previous.get("metadata") or {}).get("kind") == "assistant_segment"
                and previous.get("content") == text
            ):
                return
        session.messages.append({"role": "assistant", "content": text, "metadata": metadata})
        self._record_activity(
            session,
            "assistant_segment",
            turn_id,
            {
                "phase": phase,
                "contentPreview": text[:500],
                "truncated": truncated,
            },
        )
        session.updated_at = utc_now()
        self.store.save(session)

    def _preserve_retry_content(
        self,
        session: Session,
        turn_id: str | None,
        raw_content: str,
        *,
        streamed: bool,
        phase: str,
        event_callback=None,
    ) -> bool:
        """Checkpoint useful streamed prose before a destructive retry."""
        if not streamed:
            self._emit(event_callback, "assistant_reset", preserved=False, phase=phase)
            return False
        raw = str(raw_content or "")
        candidate = self._partial_final_content(raw) or raw
        segment = self._strip_protocol_tail(candidate.strip(), raw).strip()
        if segment:
            self._persist_assistant_segment(session, turn_id, segment, phase=phase)
        self._emit(
            event_callback,
            "assistant_reset",
            preserved=bool(segment),
            phase=phase,
        )
        return bool(segment)

    @classmethod
    def _emit_text(cls, callback, content: str, chunk_size: int = 24) -> None:
        for start in range(0, len(content), chunk_size):
            cls._emit(
                callback,
                "assistant_delta",
                delta=content[start : start + chunk_size],
            )

    def _try_auto_compaction(self, session: Session):
        if self.compactor is None or not self.compactor.should_compact(session):
            return None, None
        try:
            result = self.compactor.compact(session)
            if result is None:
                # A character/token threshold can request compaction even
                # when there are not enough visible messages to retain a
                # separate recent tail.  Do not leave the UI's pending card
                # spinning forever in that edge case.
                return None, "当前可见消息不足以安全压缩，原有上下文已保留。"
            return result, None
        except CompactionError as exc:
            return None, str(exc)

    def compact_current(self) -> CompactionResult | None:
        if self.compactor is None:
            raise CompactionError("Compactor 未配置。")
        with self._session_lock(self.store.current_id):
            self.last_compaction_error = None
            self.last_compaction = self.compactor.compact(self.store.current, force=True)
            return self.last_compaction

    def _session_lock(self, session_id: str) -> RLock:
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = RLock()
                self._session_locks[session_id] = lock
            return lock


def _chinese_or_digit_index(value: str) -> int | None:
    if not value:
        return None
    if value.isdigit():
        number = int(value)
        return number - 1 if number > 0 else None
    digits = {
        "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    if value in digits:
        return digits[value] - 1
    if value.startswith("十") and len(value) == 2 and value[1] in digits:
        return 10 + digits[value[1]] - 1
    if value.endswith("十") and len(value) == 2 and value[0] in digits:
        return digits[value[0]] * 10 - 1
    if "十" in value:
        left, right = value.split("十", 1)
        if left in digits and right in digits:
            return digits[left] * 10 + digits[right] - 1
    return None
