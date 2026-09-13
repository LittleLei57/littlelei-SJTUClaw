"""Step 5：模型 Tool Call/Final 协议及兼容解析。

优先路径由 ``LLMClient`` 使用供应商原生 Function Calling；当模型或网关不支持
时，本模块解析 ``tool_call``、``tool_calls`` 和 ``final`` JSON。解析器会拆除
常见 Markdown 外壳、限制每批调用数量，并拒绝无法确认的结构，避免把普通文本
误当成执行动作。
"""

from dataclasses import dataclass
import json
from typing import Any


MAX_CALLS_PER_BATCH = 5


class ProtocolError(ValueError):
    """模型输出无法安全解释为约定协议。"""

    pass


@dataclass(frozen=True)
class ToolCall:
    """一个已解析的 Tool 名称、参数和可选供应商 call ID。"""

    tool: str
    args: dict[str, Any]
    # Native Function Calling providers supply a stable ID.  Text-protocol
    # calls may omit it; Runtime assigns a deterministic per-turn fallback.
    call_id: str | None = None


@dataclass(frozen=True)
class ModelAction:
    """一轮模型输出：最终文本或一组 Tool Call。"""

    action_type: str
    content: str = ""
    calls: tuple[ToolCall, ...] = ()
    raw_json: dict[str, Any] | None = None


def _load_relaxed_json_object(text: str) -> dict[str, Any] | None:
    """Decode a top-level JSON object, tolerating raw newlines in strings.

    A few OpenAI-compatible providers occasionally return a JSON envelope
    whose ``content`` contains literal line breaks instead of escaped
    ``\\n``.  That is invalid JSON, but the intent is unambiguous.  Repair
    only control characters *inside* strings so ordinary prose and LaTeX
    backslashes are left untouched.
    """
    stripped = str(text or "").strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return None
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        repaired: list[str] = []
        in_string = False
        escaped = False
        for char in stripped:
            if in_string and char in {"\n", "\r", "\t"}:
                repaired.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[char])
                escaped = False
                continue
            repaired.append(char)
            if escaped:
                escaped = False
            elif char == "\\" and in_string:
                escaped = True
            elif char == '"':
                in_string = not in_string
        try:
            value = json.loads("".join(repaired))
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def unwrap_final_content(text: str, *, max_depth: int = 3) -> str:
    """Remove accidental nested ``type=final`` protocol envelopes.

    This helper is intentionally conservative: only a whole top-level final
    object is unwrapped.  JSON examples, Tool Calls and arbitrary user JSON
    remain visible text.
    """
    content = str(text or "").strip()
    for _ in range(max(1, max_depth)):
        payload = _load_relaxed_json_object(content)
        if not (
            isinstance(payload, dict)
            and payload.get("type") == "final"
            and isinstance(payload.get("content"), str)
            and payload["content"].strip()
        ):
            break
        content = payload["content"].strip()
    return content


def _extract_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    parsed: list[tuple[int, int, dict[str, Any]]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, consumed = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("type") in {"final", "tool_call", "tool_calls"}:
            parsed.append((index, index + consumed, value))
    # Do not mistake an object written inside a JSON string (for example an
    # illustrative Tool Call inside final.content) for a second top-level
    # protocol frame.
    candidates: list[dict[str, Any]] = []
    for index, _end, value in parsed:
        if any(start < index < end for start, end, _ in parsed):
            continue
        candidates.append(value)
    if not candidates:
        return None
    # When a provider emits a prose/final preamble followed by a real Tool
    # object, executing the Tool is safer than treating the first preamble as
    # a completed answer.  A single final object keeps the normal behavior.
    return next(
        (item for item in candidates if item.get("type") in {"tool_call", "tool_calls"}),
        candidates[0],
    )


def parse_model_action(text: str, *, require_json: bool = False) -> ModelAction:
    """把模型文本解析为 Final 或 Tool Calls；严格模式拒绝普通文本。"""

    payload = _extract_json_object(text)
    if payload is None:
        unwrapped = unwrap_final_content(text)
        if unwrapped and unwrapped != text.strip():
            return ModelAction("final", content=unwrapped)
        # Tool-enabled turns must not silently downgrade a conversational
        # sentence (for example, "我去看看") into a completed answer.
        # Keeping the compatibility path for the no-tools CLI/runtime still
        # lets simple text-only models work as before.
        if require_json:
            raise ProtocolError(
                "工具模式下模型输出必须是 type=final/tool_call/tool_calls 的 JSON 对象"
            )
        # Plain text remains a valid final answer for text-only compatibility.
        if text.strip():
            content = text.strip()
            if content.startswith("FINAL:"):
                content = content[len("FINAL:"):].lstrip()
            return ModelAction("final", content=content)
        raise ProtocolError("模型返回为空。")

    action_type = payload["type"]
    if action_type == "final":
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProtocolError("final.content 必须是非空字符串。")
        return ModelAction(
            "final", content=unwrap_final_content(content), raw_json=payload
        )

    raw_calls = [payload] if action_type == "tool_call" else payload.get("calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        raise ProtocolError("tool_calls.calls 必须是非空数组。")
    if len(raw_calls) > MAX_CALLS_PER_BATCH:
        raise ProtocolError(f"单批最多允许 {MAX_CALLS_PER_BATCH} 个 Tool Call。")

    calls = []
    for index, item in enumerate(raw_calls):
        if not isinstance(item, dict):
            raise ProtocolError(f"第 {index + 1} 个 Tool Call 必须是 object。")
        name = item.get("tool")
        args = item.get("args", {})
        if not isinstance(name, str) or not name:
            raise ProtocolError(f"第 {index + 1} 个 Tool Call 缺少 tool。")
        if not isinstance(args, dict):
            raise ProtocolError(f"Tool {name} 的 args 必须是 object。")
        call_id = item.get("id") or item.get("callId")
        if call_id is not None and not isinstance(call_id, str):
            raise ProtocolError(f"Tool {name} 的 call id 必须是字符串。")
        calls.append(ToolCall(name, args, call_id=call_id))
    return ModelAction("tool_calls", calls=tuple(calls), raw_json=payload)


def extract_embedded_tool_action(text: str) -> ModelAction | None:
    """Recover a Tool Call embedded in a model's prose/final envelope.

    Some OpenAI-compatible providers stream a provisional natural-language
    prefix and then place the actual protocol object inside
    ``{"type":"final","content":"..."}``.  Treating that nested object as
    ordinary final prose causes the Runtime's promise guard to emit a
    protocol retry and can discard an otherwise valid Tool action.  This
    helper only returns Tool actions (never embedded final examples), so a
    user asking about the protocol cannot accidentally be converted into a
    tool execution.
    """
    decoder = json.JSONDecoder()
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate, dict):
            continue
        if candidate.get("type") not in {"tool_call", "tool_calls"}:
            continue
        try:
            return parse_model_action(json.dumps(candidate, ensure_ascii=False))
        except ProtocolError:
            continue
    return None


TOOL_PROTOCOL_INSTRUCTIONS = """# Tool Call Protocol
你可以使用下方 Available Tools 获取真实外部信息。需要工具时，只输出一个 JSON object：
单个调用：{"type":"tool_call","tool":"工具名","args":{}}
多个调用：{"type":"tool_calls","calls":[{"id":"call_x","tool":"工具名","args":{}}]}
单批最多 5 个调用。收到 tool_result observation 后，继续判断是否调用工具；完成时输出：
{"type":"final","content":"最终回答正文"}
如果上游提供了 Tool Call id，后续结果必须使用对应的 callId；没有 id 时由 Runtime 自动补齐。
需要调用工具时只能输出 Tool Call JSON，Tool Call 前后不得添加解释、过渡语或其他文字。
不得在 FINAL 中承诺稍后执行操作，例如“我来搜索”“现在开始读取”“接下来调用工具”。如果任务需要工具，必须在当前轮直接输出 Tool Call；不要用搜索计划代替搜索结果。
不要伪造 tool_result，不要用 Markdown code fence 包裹 Tool Call JSON。"""
