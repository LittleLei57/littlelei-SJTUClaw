"""Build a semantic conversation view without completed tool-protocol noise."""

from __future__ import annotations

import json

from llm_client import Message


INTERNAL_USER_PREFIXES = (
    "[tool_results]", "[approval_required]", "[approval_result]", "[protocol_error]",
    "[approval_retry_required]", "[scheduler_task_failed",
)
INTERNAL_SYSTEM_PREFIXES = (
    "[protocol_error]",
    "[tool_retry_hint]",
    "[approval_retry_required]",
    "[approval_result]",
)
MAX_INTERNAL_CONTEXT_CHARS = 12_000
MAX_TOOL_OUTPUT_CHARS = 6_000
MAX_ACTIVE_READ_FILE_CHARS = 100_000
MAX_ACTIVE_ATTACHMENT_CHARS = 65_000


def is_internal_message(message: Message) -> bool:
    metadata = message.get("metadata") or {}
    # Assistant process segments are persisted only so the UI can replay the
    # text -> Tool -> text timeline. They must never be fed back to the model
    # or counted as visible user/assistant turns.
    if metadata.get("kind") == "assistant_segment" or metadata.get("displayOnly"):
        return True
    content = message.get("content", "").lstrip()
    if message.get("role") == "system":
        return content.startswith(INTERNAL_SYSTEM_PREFIXES)
    if message.get("role") == "user":
        return content.startswith(INTERNAL_USER_PREFIXES)
    if message.get("role") != "assistant":
        return False
    if content.startswith("[deferred_action_promise]"):
        return True
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("type") in {"tool_call", "tool_calls"}


def semantic_context(
    messages: list[Message],
    *,
    preserve_active_internal: bool = True,
) -> list[Message]:
    """Return conversation content with protocol chatter removed.

    The model still needs an unfinished tool chain (the most recent user
    request followed by its tool call/result) in order to continue a turn, so
    callers building an LLM context keep that tail by default.  Compaction is
    different: its ``keep_recent`` budget is a *visible conversation* budget,
    not a tool-trace budget.  It opts out of the active tail below so tool
    calls/results can never become the eight retained messages.
    """
    visible_indexes = [index for index, item in enumerate(messages) if not is_internal_message(item)]
    active_start = None
    if (
        preserve_active_internal
        and visible_indexes
        and messages[visible_indexes[-1]].get("role") == "user"
    ):
        active_start = visible_indexes[-1]
    return [
        _context_message(
            item,
            preserve_active_read_file=(active_start is not None and index >= active_start),
        )
        for index, item in enumerate(messages)
        if (
            not is_internal_message(item)
            or (
                active_start is not None
                and index >= active_start
                and not _is_display_only_message(item)
            )
        )
    ]


def _is_display_only_message(message: Message) -> bool:
    metadata = message.get("metadata") or {}
    return bool(metadata.get("kind") == "assistant_segment" or metadata.get("displayOnly"))


def visible_message_count(messages: list[Message]) -> int:
    """Count messages a user can see in the conversation transcript.

    Tool calls/results, approvals, protocol retries and other internal
    bookkeeping remain persisted for recovery and auditing, but they should
    not inflate the Session message count shown by the CLI or Web UI.
    """
    return visible_message_stats(messages)["messageCount"]


def visible_message_stats(messages: list[Message]) -> dict[str, int]:
    """Return the transcript count and its user/assistant breakdown.

    A channel turn can legitimately be temporarily unpaired: the inbound
    message is persisted before the model finishes, and a failed or paused
    turn may therefore leave one visible user message without an assistant
    answer.  Exposing the breakdown prevents a surprising odd count from
    being mistaken for tool-protocol noise or a storage corruption.
    """
    visible = [
        item for item in messages
        if item.get("role") in {"user", "assistant"}
        and not is_internal_message(item)
    ]
    user_count = sum(1 for item in visible if item.get("role") == "user")
    assistant_count = sum(1 for item in visible if item.get("role") == "assistant")
    return {
        "messageCount": len(visible),
        "userMessageCount": user_count,
        "assistantMessageCount": assistant_count,
        "completedTurnCount": min(user_count, assistant_count),
        "pendingUserCount": max(0, user_count - assistant_count),
    }


def _context_message(
    message: Message,
    *,
    preserve_active_read_file: bool = False,
) -> Message:
    if not is_internal_message(message):
        return message
    content = message.get("content", "")
    for prefix in ("[tool_results]", "[approval_required]"):
        if content.lstrip().startswith(prefix):
            return {
                "role": message.get("role", "user"),
                "content": _compact_prefixed_json(
                    content,
                    prefix,
                    preserve_active_read_file=preserve_active_read_file,
                ),
            }
    if content.lstrip().startswith("[approval_result]"):
        return {
            "role": message.get("role", "user"),
            "content": _compact_prefixed_json(content, "[approval_result]"),
        }
    if len(content) > MAX_INTERNAL_CONTEXT_CHARS:
        return {
            "role": message.get("role", "user"),
            "content": _truncate_text(content, MAX_INTERNAL_CONTEXT_CHARS),
        }
    return message


def _compact_prefixed_json(
    content: str,
    prefix: str,
    *,
    preserve_active_read_file: bool = False,
) -> str:
    leading = content[: len(content) - len(content.lstrip())]
    stripped = content.lstrip()
    if not stripped.startswith(prefix):
        return _truncate_text(content, MAX_INTERNAL_CONTEXT_CHARS)
    payload = stripped[len(prefix):].strip()
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return _truncate_text(content, MAX_INTERNAL_CONTEXT_CHARS)
    has_read_file = _contains_tool(data, "read_file")
    has_read_attachment = _contains_tool(data, "read_attachment")
    if preserve_active_read_file and has_read_file:
        output_limit = MAX_ACTIVE_READ_FILE_CHARS
        context_limit = MAX_ACTIVE_READ_FILE_CHARS
    elif preserve_active_read_file and has_read_attachment:
        # Attachment Tools emit resumable 60k chunks. Keep the whole current
        # chunk so the model can summarize it before following nextOffset or
        # nextPage; completed turns still use the compact 6k representation.
        output_limit = MAX_ACTIVE_ATTACHMENT_CHARS
        context_limit = MAX_ACTIVE_ATTACHMENT_CHARS
    else:
        output_limit = MAX_TOOL_OUTPUT_CHARS
        context_limit = MAX_INTERNAL_CONTEXT_CHARS
    compacted = _limit_json_value(data, output_limit=output_limit)
    encoded = json.dumps(compacted, ensure_ascii=False)
    result = f"{leading}{prefix} {encoded}"
    return _truncate_text(result, context_limit)


def _limit_json_value(value, *, output_limit: int = MAX_TOOL_OUTPUT_CHARS):
    if isinstance(value, str):
        return _truncate_text(value, output_limit)
    if isinstance(value, list):
        return [_limit_json_value(item, output_limit=output_limit) for item in value]
    if isinstance(value, dict):
        compacted = {}
        for key, item in value.items():
            if key in {"output", "content", "text", "raw", "stdout", "stderr", "toolResult"}:
                compacted[key] = _limit_json_value(item, output_limit=output_limit)
            elif isinstance(item, (dict, list)):
                compacted[key] = _limit_json_value(item, output_limit=output_limit)
            elif isinstance(item, str) and len(item) > 1_000:
                compacted[key] = _truncate_text(item, 1_000)
            else:
                compacted[key] = item
        return compacted
    return value


def _contains_tool(value, tool_name: str) -> bool:
    if isinstance(value, dict):
        if value.get("tool") == tool_name:
            return True
        return any(_contains_tool(item, tool_name) for item in value.values())
    if isinstance(value, list):
        return any(_contains_tool(item, tool_name) for item in value)
    return False


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = max(0, limit - 220)
    omitted = len(text) - head
    return (
        text[:head]
        + f"\n\n[上下文保护：此处省略 {omitted} 个字符。完整结果仍保存在 session 日志/tool trace 中；"
        "如需精确内容请重新读取更小范围。]"
    )
