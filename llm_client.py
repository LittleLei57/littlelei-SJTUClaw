"""Step 0/5：OpenAI 兼容的 LLM API 客户端。

Step 0 的最小能力是把 messages 发给 SJTU API 并取得 assistant 回复；
Step 5 在此基础上增加流式输出、取消、重试、原生 Function Calling 和旧式
JSON Tool 协议回退。客户端只负责供应商协议，不保存 Session，也不执行 Tool。
"""

from collections.abc import Iterable, Sequence
from queue import Empty, Queue
from threading import Event, Thread, local
import json
import re
import time
from typing import Any, Optional, TypedDict

from openai import OpenAI

from config import (
    API_BASE_URL,
    DEFAULT_MODEL,
    load_api_key,
    model_supports_vision,
    normalize_sjtu_model,
)


class Message(TypedDict):
    """传给模型的最小消息结构；role/content 与 OpenAI 协议一致。"""

    role: str
    content: Any


class LLMCancelled(RuntimeError):
    """Raised when the caller cancels an in-flight provider stream."""


def _close_provider_stream(stream) -> None:
    """Best-effort close for OpenAI-compatible synchronous streams.

    ``SyncStream.close`` is available in current versions of the OpenAI SDK;
    the response fallbacks keep cancellation effective for older SDK/client
    wrappers that expose only the underlying HTTP response.
    """
    candidates = [
        stream,
        getattr(stream, "_response", None),
        getattr(stream, "response", None),
    ]
    for candidate in candidates:
        close = getattr(candidate, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
            return


def _iter_cancellable(stream, cancellation_event=None):
    """Iterate a blocking provider stream while watching cancellation.

    A cooperative ``Event`` alone cannot interrupt ``next(stream)`` while the
    provider is silent. A tiny daemon watcher closes the HTTP stream as soon
    as the user presses Stop, allowing the worker to leave the provider call
    instead of waiting for its full network timeout.
    """
    if cancellation_event is None:
        yield from stream
        return
    if cancellation_event.is_set():
        _close_provider_stream(stream)
        raise LLMCancelled("用户已取消当前模型请求。")

    watcher_done = Event()

    def watch() -> None:
        while not watcher_done.wait(0.05):
            if cancellation_event.is_set():
                _close_provider_stream(stream)
                return

    watcher = Thread(target=watch, name="llm-cancel-watch", daemon=True)
    watcher.start()
    try:
        for chunk in stream:
            if cancellation_event.is_set():
                _close_provider_stream(stream)
                raise LLMCancelled("用户已取消当前模型请求。")
            yield chunk
    except LLMCancelled:
        raise
    except Exception as exc:
        if cancellation_event.is_set():
            raise LLMCancelled("用户已取消当前模型请求。") from exc
        raise
    finally:
        watcher_done.set()
        if cancellation_event.is_set():
            _close_provider_stream(stream)


def _create_cancellable(create_fn, cancellation_event=None):
    """Run a synchronous provider request without blocking Stop.

    The OpenAI-compatible SDK performs the initial HTTP request before it
    returns a stream.  That means ``_iter_cancellable`` cannot help while the
    first response headers are pending.  Run that one blocking call in a
    daemon thread and let the runtime observe cancellation independently.
    """
    if cancellation_event is None:
        return create_fn()
    if cancellation_event.is_set():
        raise LLMCancelled("model request cancelled")

    result_queue: Queue[tuple[str, Any]] = Queue(maxsize=1)

    def invoke() -> None:
        try:
            result_queue.put(("ok", create_fn()))
        except BaseException as exc:  # propagate provider errors to caller
            result_queue.put(("error", exc))

    Thread(target=invoke, name="llm-request", daemon=True).start()
    while True:
        if cancellation_event.is_set():
            # The SDK call itself may finish later, but the agent turn must
            # stop now.  If it already produced a response, close it so the
            # underlying HTTP connection is not kept alive unnecessarily.
            try:
                kind, value = result_queue.get_nowait()
            except Empty:
                pass
            else:
                if kind == "ok":
                    _close_provider_stream(value)
            raise LLMCancelled("model request cancelled")
        try:
            kind, value = result_queue.get(timeout=0.05)
        except Empty:
            continue
        if kind == "error":
            raise value
        if cancellation_event.is_set():
            _close_provider_stream(value)
            raise LLMCancelled("model request cancelled")
        return value


class LLMClient:
    """Stateless model client used by the Runtime and Compactor.

    The SJTU endpoint accepts the OpenAI ``tools``/``tool_choice`` fields for
    models that support native Function Calling.  Responses are normalised to
    the Runtime's existing ``type=final/tool_calls`` JSON protocol so the
    execution loop remains one code path.  If a model rejects native fields,
    the client remembers that capability and retries without them.
    """

    supports_native_tools = True

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        max_attempts: int = 3,
        sleep_fn=time.sleep,
        client=None,
        capability_store=None,
        base_url: str = API_BASE_URL,
    ):
        self.model = model
        self.base_url = base_url
        self.max_attempts = max(1, max_attempts)
        self._sleep = sleep_fn
        self._metrics = local()
        self.capability_store = capability_store
        inferred_vision = model_supports_vision(model)
        stored = (
            capability_store.get(base_url, model)
            if capability_store is not None else {}
        )
        observed = stored.get("observed", {}) if isinstance(stored, dict) else {}
        self.native_tools: bool | None = (
            observed.get("nativeTools")
            if isinstance(observed.get("nativeTools"), bool) else None
        )
        self.supports_vision = (
            observed.get("vision")
            if isinstance(observed.get("vision"), bool) else inferred_vision
        )
        self.client = client or OpenAI(
            api_key=api_key or load_api_key(),
            base_url=base_url,
            max_retries=0,
        )

    def capability_profile(self) -> dict:
        inferred_vision = model_supports_vision(self.model)
        if self.capability_store is None:
            return {
                "model": self.model,
                "provider": self.base_url,
                "nativeTools": {
                    "supported": self.native_tools, "source": "runtime"
                },
                "vision": {
                    "supported": self.supports_vision,
                    "source": "inferred" if self.supports_vision == inferred_vision else "runtime",
                },
                "streaming": {"supported": None, "source": "unknown"},
                "jsonProtocol": {"supported": None, "source": "unknown"},
                "updatedAt": None,
            }
        return self.capability_store.describe(
            self.base_url, self.model, inferred_vision=inferred_vision
        )

    def select_model(self, model: str) -> dict:
        """Switch to another configured model on the same API endpoint."""
        selected = normalize_sjtu_model(model)
        stored = (
            self.capability_store.get(self.base_url, selected)
            if self.capability_store is not None else {}
        )
        observed = stored.get("observed", {}) if isinstance(stored, dict) else {}
        self.model = selected
        self.native_tools = (
            observed.get("nativeTools")
            if isinstance(observed.get("nativeTools"), bool) else None
        )
        self.supports_vision = (
            observed.get("vision")
            if isinstance(observed.get("vision"), bool)
            else model_supports_vision(selected)
        )
        return self.capability_profile()

    def reset_capability_profile(self) -> dict:
        if self.capability_store is not None:
            self.capability_store.reset(self.base_url, self.model)
        self.native_tools = None
        self.supports_vision = model_supports_vision(self.model)
        return self.capability_profile()

    def _observe_capabilities(self, **values) -> None:
        if self.capability_store is None:
            return
        try:
            self.capability_store.update(self.base_url, self.model, **values)
        except Exception:
            # Capability caching must never make a successful model request fail.
            pass

    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        cancellation_event=None,
    ) -> str:
        """Send a completion and return Runtime-compatible assistant text."""
        started = time.perf_counter()
        response, native_used = self._create_optional_native(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            cancellation_event=cancellation_event,
        )
        if not response.choices:
            raise RuntimeError("模型响应中缺少 assistant 内容。")
        choice = response.choices[0]
        message = choice.message
        content = getattr(message, "content", None)
        native_calls = _normalise_native_tool_calls(getattr(message, "tool_calls", None))
        if native_used and native_calls:
            content = json.dumps(
                {"type": "tool_calls", "calls": native_calls}, ensure_ascii=False
            )
        elif native_used and content is not None:
            content = _native_content_to_protocol(content)
        elif tools and content is not None:
            # A provider may reject native ``tools`` once and leave this
            # client in the legacy text-protocol mode. Tool-enabled callers
            # Tool-enabled callers still require a
            # machine-readable action, so normalize the fallback response
            # instead of returning prose that later fails strict JSON parsing.
            try:
                fallback_payload = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                fallback_payload = None
            if not (
                isinstance(fallback_payload, dict)
                and fallback_payload.get("type") in {"final", "tool_call", "tool_calls"}
            ):
                content = _native_content_to_protocol(content)
        if content is None:
            raise RuntimeError("模型响应中缺少 assistant 内容。")
        if not content.strip():
            raise RuntimeError("模型返回了空 assistant 回复，请重试。")
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        usage = getattr(response, "usage", None)
        self._metrics.value = {
            "model": self.model,
            "durationMs": duration_ms,
            "inputTokens": getattr(usage, "prompt_tokens", None) if usage else None,
            "outputTokens": getattr(usage, "completion_tokens", None) if usage else None,
            "totalTokens": getattr(usage, "total_tokens", None) if usage else None,
            # OpenAI-compatible providers use ``finish_reason`` to distinguish
            # a normal stop from a context/output-limit truncation.  Preserve
            # it even when the endpoint returns no usage object so Runtime can
            # expose a useful diagnostic instead of treating a partial answer
            # as an ordinary completion.
            "finishReason": getattr(choice, "finish_reason", None),
        }
        try:
            protocol = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            protocol = None
        if (
            isinstance(protocol, dict)
            and protocol.get("type") in {"final", "tool_call", "tool_calls"}
        ):
            self._observe_capabilities(jsonProtocol=True)
        return content

    def pop_metrics(self) -> dict | None:
        metrics = getattr(self._metrics, "value", None)
        self._metrics.value = None
        return metrics

    def complete_stream(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int | None = None,
        cancellation_event=None,
    ) -> Iterable[str]:
        """Stream text, buffering native tool deltas until the call is known.

        ``cancellation_event`` is intentionally optional for compatibility
        with lightweight test/fake models. The real provider stream is closed
        from a watcher thread when it is set, so Stop does not wait for the
        provider's full HTTP timeout.
        """
        started = time.perf_counter()
        output_chars = 0
        first_token_ms = None
        chunk_count = 0
        max_chunk_chars = 0
        finish_reason = None
        stream, native_used = self._create_optional_native(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            stream=True,
            cancellation_event=cancellation_event,
        )
        native_content: list[str] = []
        native_calls: dict[int, dict[str, Any]] = {}
        native_mode: str | None = None
        native_buffer: list[str] = []
        native_prefix = '{"type":"final","content":"'
        for chunk in _iter_cancellable(stream, cancellation_event):
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            chunk_finish_reason = _value(choice, "finish_reason")
            if chunk_finish_reason:
                finish_reason = chunk_finish_reason
            delta = choice.delta
            content = getattr(delta, "content", None)
            if native_used:
                for item in getattr(delta, "tool_calls", None) or []:
                    native_mode = "tool"
                    index = int(_value(item, "index", 0) or 0)
                    entry = native_calls.setdefault(
                        index, {"id": None, "tool": "", "arguments": ""}
                    )
                    entry["id"] = entry["id"] or _value(item, "id")
                    function = _value(item, "function")
                    if function is not None:
                        entry["tool"] += _value(function, "name", "") or ""
                        entry["arguments"] += _value(function, "arguments", "") or ""
                if content:
                    native_content.append(content)
                    native_buffer.append(content)
                    if native_mode is None:
                        buffered = "".join(native_buffer)
                        # Native models commonly follow the legacy JSON
                        # instruction in message content. Stream that JSON
                        # directly so Runtime can expose final.content
                        # incrementally; plain-text native finals are wrapped
                        # into the same protocol envelope after a short probe.
                        stripped = buffered.lstrip()
                        if stripped.startswith("{"):
                            native_mode = "protocol"
                            if buffered:
                                if first_token_ms is None:
                                    first_token_ms = round((time.perf_counter() - started) * 1000, 2)
                                chunk_count += 1
                                max_chunk_chars = max(max_chunk_chars, len(buffered))
                                output_chars += len(buffered)
                                yield buffered
                            native_buffer.clear()
                        elif stripped.startswith("["):
                            # A subset of SJTU-compatible models returns
                            # ``[{"name": ..., "parameters": ...}]`` in
                            # assistant content even with native tools
                            # enabled. Buffer the whole array so it can be
                            # normalized into Runtime's Tool protocol without
                            # ever flashing raw JSON in the conversation.
                            native_mode = "function_array"
                        elif len(buffered) >= 64:
                            native_mode = "text"
                            encoded = json.dumps(buffered, ensure_ascii=False)[1:-1]
                            if encoded:
                                if first_token_ms is None:
                                    first_token_ms = round((time.perf_counter() - started) * 1000, 2)
                                chunk_count += 1
                                max_chunk_chars = max(max_chunk_chars, len(native_prefix) + len(encoded))
                                output_chars += len(native_prefix) + len(encoded)
                                yield native_prefix + encoded
                            native_buffer.clear()
                    elif native_mode == "protocol":
                        if first_token_ms is None:
                            first_token_ms = round((time.perf_counter() - started) * 1000, 2)
                        chunk_count += 1
                        max_chunk_chars = max(max_chunk_chars, len(content))
                        output_chars += len(content)
                        yield content
                    elif native_mode == "text":
                        encoded = json.dumps(content, ensure_ascii=False)[1:-1]
                        if encoded:
                            if first_token_ms is None:
                                first_token_ms = round((time.perf_counter() - started) * 1000, 2)
                            chunk_count += 1
                            max_chunk_chars = max(max_chunk_chars, len(encoded))
                            output_chars += len(encoded)
                            yield encoded
                    elif native_mode == "function_array":
                        # Kept in ``native_content`` and emitted once, after
                        # validation/normalization at end of stream.
                        pass
                continue
            if content:
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - started) * 1000, 2)
                chunk_count += 1
                max_chunk_chars = max(max_chunk_chars, len(content))
                output_chars += len(content)
                yield content
        if native_used:
            calls = [_native_call_to_dict(item) for item in native_calls.values()]
            if calls:
                payload = json.dumps(
                    {"type": "tool_calls", "calls": calls}, ensure_ascii=False
                )
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - started) * 1000, 2)
                chunk_count += 1
                max_chunk_chars = max(max_chunk_chars, len(payload))
                output_chars += len(payload)
                yield payload
            elif native_mode == "protocol":
                # The complete protocol payload was already forwarded as
                # stream chunks above.
                pass
            elif native_mode == "function_array":
                payload = _native_content_to_protocol("".join(native_content))
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - started) * 1000, 2)
                chunk_count += 1
                max_chunk_chars = max(max_chunk_chars, len(payload))
                output_chars += len(payload)
                yield payload
            elif native_mode == "text":
                # Close the incremental final JSON envelope.  Runtime's
                # partial-final parser can display the content as it arrives.
                payload = '"}'
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - started) * 1000, 2)
                chunk_count += 1
                max_chunk_chars = max(max_chunk_chars, len(payload))
                output_chars += len(payload)
                yield payload
            elif native_content:
                payload = _native_content_to_protocol("".join(native_content))
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - started) * 1000, 2)
                chunk_count += 1
                max_chunk_chars = max(max_chunk_chars, len(payload))
                output_chars += len(payload)
                yield payload
        self._metrics.value = {
            "model": self.model,
            "durationMs": round((time.perf_counter() - started) * 1000, 2),
            "inputTokens": None,
            "outputTokens": max(1, output_chars // 4),
            "totalTokens": None,
            "timeToFirstTokenMs": first_token_ms,
            "chunkCount": chunk_count,
            "maxChunkChars": max_chunk_chars,
            "finishReason": finish_reason,
        }
        self._observe_capabilities(streaming=True)
        if native_mode == "protocol":
            self._observe_capabilities(jsonProtocol=True)

    def _create_optional_native(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        stream: bool = False,
        cancellation_event=None,
    ):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
        }
        if stream:
            kwargs["stream"] = True
        if max_tokens is not None:
            # ``max_tokens`` is supported by the SJTU OpenAI-compatible
            # endpoint and by the older OpenAI Chat Completions schema.
            kwargs["max_tokens"] = max_tokens
        if timeout_seconds is not None:
            if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
                raise ValueError("timeout_seconds 必须是正数。")
            kwargs["timeout"] = float(timeout_seconds)
        if tools and self.native_tools is not False:
            native_kwargs = dict(kwargs)
            native_kwargs["tools"] = list(tools)
            if tool_choice is not None:
                native_kwargs["tool_choice"] = tool_choice
            try:
                response = self._create_with_retry(
                    cancellation_event=cancellation_event, **native_kwargs
                )
                self.native_tools = True
                observed = {"nativeTools": True}
                if _messages_have_images(messages):
                    self.supports_vision = True
                    observed["vision"] = True
                self._observe_capabilities(**observed)
                return response, True
            except Exception as exc:
                if _messages_have_images(messages) and _is_vision_unsupported(exc):
                    self.supports_vision = False
                    native_kwargs["messages"] = _without_images(messages)
                    response = self._create_with_retry(
                        cancellation_event=cancellation_event, **native_kwargs
                    )
                    self.native_tools = True
                    self._observe_capabilities(nativeTools=True, vision=False)
                    return response, True
                if not _is_native_tools_unsupported(exc):
                    raise
                self.native_tools = False
                self._observe_capabilities(nativeTools=False)
        try:
            response = self._create_with_retry(
                cancellation_event=cancellation_event, **kwargs
            )
            if _messages_have_images(messages):
                self.supports_vision = True
                self._observe_capabilities(vision=True)
            return response, False
        except Exception as exc:
            if not (_messages_have_images(messages) and _is_vision_unsupported(exc)):
                raise
            self.supports_vision = False
            self._observe_capabilities(vision=False)
            kwargs["messages"] = _without_images(messages)
            return self._create_with_retry(
                cancellation_event=cancellation_event, **kwargs
            ), False

    def _create_with_retry(self, *, cancellation_event=None, **kwargs):
        """Retry transient capacity/rate-limit failures with a bounded delay."""
        for attempt in range(1, self.max_attempts + 1):
            if cancellation_event is not None and cancellation_event.is_set():
                raise LLMCancelled("model request cancelled")
            try:
                return _create_cancellable(
                    lambda: self.client.chat.completions.create(**kwargs),
                    cancellation_event,
                )
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                if status is None:
                    match = re.search(r"(?:Error code|HTTP)\s*[: ]\s*(\d{3})", str(exc), re.I)
                    status = int(match.group(1)) if match else None
                if status not in {429, 502, 503, 504} or attempt >= self.max_attempts:
                    raise
                delay = _retry_delay(exc, attempt)
                if cancellation_event is None:
                    self._sleep(delay)
                elif cancellation_event.wait(delay):
                    raise LLMCancelled("model request cancelled")

    def chat(self, message: str, system_prompt: str = "你是一个有帮助的助手。") -> str:
        """兼容 Step 0 的单轮调用接口。"""
        return self.complete(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ]
        )


def _value(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _messages_have_images(messages: Sequence[Message]) -> bool:
    return any(
        isinstance(message.get("content"), list)
        and any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in message["content"]
        )
        for message in messages
    )


def _without_images(messages: Sequence[Message]) -> list[Message]:
    """Drop unsupported image blocks while instructing the model to use OCR."""
    output: list[Message] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            output.append(dict(message))
            continue
        text = "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
        if any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in content
        ):
            text += (
                "\n\n[vision_unavailable] 当前模型接口拒绝了原生图片输入。"
                "请直接调用 ocr_image 读取本轮选中的图片；不要声称已经看见原图。"
            )
        output.append({"role": message.get("role", "user"), "content": text})
    return output


def _is_vision_unsupported(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status not in {400, 415, 422} and not re.search(r"\b(?:400|415|422)\b", text):
        return False
    return any(
        token in text
        for token in (
            "image_url",
            "image input",
            "image content",
            "multimodal",
            "vision",
            "content must be a string",
            "invalid content type",
        )
    )


def _native_call_to_dict(entry: dict[str, Any]) -> dict[str, Any]:
    raw_args = entry.get("arguments") or "{}"
    try:
        args = json.loads(raw_args)
    except (TypeError, json.JSONDecodeError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    result = {"tool": entry.get("tool", ""), "args": args}
    if entry.get("id"):
        result["id"] = entry["id"]
    return result


def _normalise_native_tool_calls(calls) -> list[dict[str, Any]]:
    if not calls:
        return []
    result = []
    for item in calls:
        function = _value(item, "function")
        result.append(
            _native_call_to_dict(
                {
                    "id": _value(item, "id"),
                    "tool": _value(function, "name", "") if function is not None else "",
                    "arguments": _value(function, "arguments", "{}") if function is not None else "{}",
                }
            )
        )
    return result


def _legacy_function_payload_to_protocol(payload: Any) -> dict[str, Any] | None:
    """Normalize SJTU-compatible ``[{name, parameters}]`` tool output.

    Some models accept OpenAI ``tools``/``tool_choice`` but return the chosen
    functions in assistant content instead of ``message.tool_calls``.  The
    shape is not OpenAI's wire format, yet it is unambiguous enough to recover
    when every array item contains a function name and an argument object.
    Unknown names are intentionally left for ToolRegistry validation.
    """
    items = payload if isinstance(payload, list) else [payload]
    if not items or len(items) > 5:
        return None
    calls: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        args = item.get("parameters", item.get("arguments", item.get("args", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return None
        if not isinstance(args, dict):
            return None
        call: dict[str, Any] = {"tool": name.strip(), "args": args}
        call_id = item.get("id") or item.get("callId")
        if isinstance(call_id, str) and call_id:
            call["id"] = call_id
        calls.append(call)
    return {"type": "tool_calls", "calls": calls}


def _native_content_to_protocol(content: str) -> str:
    """Avoid double-wrapping legacy JSON, including prose-prefixed output."""
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and payload.get("type") in {"final", "tool_call", "tool_calls"}:
        return json.dumps(payload, ensure_ascii=False)
    legacy_calls = _legacy_function_payload_to_protocol(payload)
    if legacy_calls is not None:
        return json.dumps(legacy_calls, ensure_ascii=False)
    # Some OpenAI-compatible models honour native ``tools`` but still follow
    # the old prompt and emit a sentence before a JSON Tool Call.  Extract the
    # first valid protocol object so Runtime can execute it instead of showing
    # the raw JSON as an answer.
    decoder = json.JSONDecoder()
    for index, char in enumerate(content or ""):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("type") in {
            "final", "tool_call", "tool_calls"
        }:
            return json.dumps(candidate, ensure_ascii=False)
        legacy_calls = _legacy_function_payload_to_protocol(candidate)
        if legacy_calls is not None:
            return json.dumps(legacy_calls, ensure_ascii=False)
    return json.dumps({"type": "final", "content": content}, ensure_ascii=False)


def _is_native_tools_unsupported(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status not in {400, 404, 422}:
        return False
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "tool",
            "function call",
            "function_call",
            "tool_choice",
            "unsupported",
            "not support",
            "unknown field",
            "extra inputs",
        )
    )


def _retry_delay(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("retry-after") if hasattr(headers, "get") else None
    try:
        if retry_after is not None:
            return min(10.0, max(0.1, float(retry_after)))
    except (TypeError, ValueError):
        pass
    match = re.search(r"try again in\s+([\d.]+)\s*seconds?", str(exc), re.I)
    if match:
        return min(10.0, max(0.1, float(match.group(1))))
    return min(4.0, float(2 ** (attempt - 1)))
