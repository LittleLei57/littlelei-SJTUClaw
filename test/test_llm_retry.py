"""验证 LLM 临时错误、限流与中断时的重试策略。"""

import json
import unittest

from llm_client import LLMClient


class _Completions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Client:
    def __init__(self, outcomes):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions(outcomes)


class _Response:
    def __init__(self, content="ok", finish_reason=None):
        self.choices = [type("Choice", (), {
            "message": type("Message", (), {"content": content})(),
            "finish_reason": finish_reason,
        })()]
        self.usage = None


class _NativeResponse:
    def __init__(self, *, content=None, tool_calls=None, finish_reason=None):
        message = type("Message", (), {
            "content": content,
            "tool_calls": tool_calls,
        })()
        self.choices = [type("Choice", (), {
            "message": message,
            "finish_reason": finish_reason,
        })()]
        self.usage = None


def _native_call(name="current_time", arguments="{}", call_id="call_1"):
    function = type("Function", (), {"name": name, "arguments": arguments})()
    return type("ToolCall", (), {"id": call_id, "function": function})()


class _StatusError(RuntimeError):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


def _chunk(content, finish_reason=None):
    delta = type("Delta", (), {"content": content})()
    return type("Chunk", (), {
        "choices": [type("Choice", (), {
            "delta": delta,
            "finish_reason": finish_reason,
        })()]
    })()


def _native_chunk(*, name="", arguments="", call_id=None, index=0):
    function = type("Function", (), {"name": name, "arguments": arguments})()
    item = type("ToolDelta", (), {
        "index": index,
        "id": call_id,
        "function": function,
    })()
    delta = type("Delta", (), {"content": None, "tool_calls": [item]})()
    return type("Chunk", (), {"choices": [type("Choice", (), {"delta": delta})()]})()


class LLMRetryTests(unittest.TestCase):
    def test_native_tool_call_is_normalised_for_runtime_protocol(self):
        client = _Client([_NativeResponse(tool_calls=[_native_call()])])
        llm = LLMClient(model="test", client=client)
        payload = json.loads(llm.complete(
            [{"role": "user", "content": "what time is it"}],
            tools=[{"type": "function", "function": {"name": "current_time"}}],
            tool_choice="required",
        ))
        self.assertEqual(payload["type"], "tool_calls")
        self.assertEqual(payload["calls"][0]["tool"], "current_time")
        self.assertEqual(payload["calls"][0]["args"], {})
        self.assertEqual(client.chat.completions.calls, 1)

    def test_complete_metrics_preserve_finish_reason(self):
        client = _Client([_Response("partial", finish_reason="length")])
        llm = LLMClient(model="test", client=client)
        self.assertEqual(llm.complete([{"role": "user", "content": "hi"}]), "partial")
        self.assertEqual(llm.pop_metrics()["finishReason"], "length")

    def test_native_tool_capability_rejection_falls_back_once(self):
        client = _Client([
            _StatusError(400, "tools are not supported"),
            _Response("{\"type\":\"final\",\"content\":\"ok\"}"),
        ])
        llm = LLMClient(model="test", client=client)
        result = llm.complete(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "current_time"}}],
        )
        self.assertEqual(result, "{\"type\":\"final\",\"content\":\"ok\"}")
        self.assertFalse(llm.native_tools)
        self.assertEqual(client.chat.completions.calls, 2)

    def test_native_final_does_not_double_wrap_legacy_json(self):
        content = '{"type":"final","content":"done"}'
        client = _Client([_NativeResponse(content=content)])
        llm = LLMClient(model="test", client=client)
        self.assertEqual(
            json.loads(llm.complete(
                [{"role": "user", "content": "hi"}],
                tools=[{"type": "function", "function": {"name": "echo"}}],
            )),
            {"type": "final", "content": "done"},
        )

    def test_native_mixed_prose_and_tool_json_is_recovered(self):
        content = '我先查一下～ {"type":"tool_call","tool":"web_search","args":{"query":"SJTU"}}'
        client = _Client([_NativeResponse(content=content)])
        llm = LLMClient(model="test", client=client)
        payload = json.loads(llm.complete(
            [{"role": "user", "content": "search"}],
            tools=[{"type": "function", "function": {"name": "web_search"}}],
        ))
        self.assertEqual(payload["type"], "tool_call")
        self.assertEqual(payload["tool"], "web_search")

    def test_native_legacy_name_parameters_array_is_recovered(self):
        content = '[{"name":"current_time","parameters":{}}]'
        client = _Client([_NativeResponse(content=content)])
        llm = LLMClient(model="test", client=client)
        payload = json.loads(llm.complete(
            [{"role": "user", "content": "现在几点"}],
            tools=[{"type": "function", "function": {"name": "current_time"}}],
            tool_choice="required",
        ))
        self.assertEqual(payload["type"], "tool_calls")
        self.assertEqual(payload["calls"], [{"tool": "current_time", "args": {}}])

    def test_stream_metrics_record_first_token_and_chunk_shape(self):
        client = _Client([[ _chunk("ab"), _chunk("cdef"), _chunk(None, "stop") ]])
        llm = LLMClient(model="test", client=client)
        self.assertEqual(
            list(llm.complete_stream([{"role": "user", "content": "hi"}])),
            ["ab", "cdef"],
        )
        metrics = llm.pop_metrics()
        self.assertIsNotNone(metrics["timeToFirstTokenMs"])
        self.assertEqual(metrics["chunkCount"], 2)
        self.assertEqual(metrics["maxChunkChars"], 4)
        self.assertEqual(metrics["finishReason"], "stop")

    def test_stream_metrics_mark_output_limit(self):
        client = _Client([[_chunk('{"type":"final","content":"partial', "length")]])
        llm = LLMClient(model="test", client=client)
        list(llm.complete_stream(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "echo"}}],
        ))
        self.assertEqual(llm.pop_metrics()["finishReason"], "length")

    def test_native_tool_stream_is_emitted_as_one_protocol_payload(self):
        client = _Client([[
            _native_chunk(name="current_time", call_id="call_1"),
            _native_chunk(arguments="{}"),
        ]])
        llm = LLMClient(model="test", client=client)
        payload = json.loads(list(llm.complete_stream(
            [{"role": "user", "content": "time"}],
            tools=[{"type": "function", "function": {"name": "current_time"}}],
        ))[0])
        self.assertEqual(payload["type"], "tool_calls")
        self.assertEqual(payload["calls"][0]["tool"], "current_time")
        self.assertEqual(llm.pop_metrics()["chunkCount"], 1)

    def test_native_legacy_name_parameters_stream_is_not_shown_as_text(self):
        client = _Client([[
            _chunk('[{"name":"current_'),
            _chunk('time","parameters":{}}]'),
        ]])
        llm = LLMClient(model="test", client=client)
        chunks = list(llm.complete_stream(
            [{"role": "user", "content": "现在几点"}],
            tools=[{"type": "function", "function": {"name": "current_time"}}],
            tool_choice="required",
        ))
        self.assertEqual(len(chunks), 1)
        payload = json.loads(chunks[0])
        self.assertEqual(payload["type"], "tool_calls")
        self.assertEqual(payload["calls"], [{"tool": "current_time", "args": {}}])

    def test_native_final_stream_is_forwarded_incrementally(self):
        client = _Client([[_chunk("x" * 600), _chunk("y" * 80)]])
        llm = LLMClient(model="test", client=client)
        chunks = list(llm.complete_stream(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "echo"}}],
        ))
        payload = json.loads("".join(chunks))
        self.assertEqual(payload["type"], "final")
        self.assertEqual(payload["content"], "x" * 600 + "y" * 80)
        self.assertGreater(len(chunks), 1)

    def test_native_protocol_final_stream_is_not_buffered_or_duplicated(self):
        client = _Client([[
            _chunk('{"type":"final","content":"'),
            _chunk("hello"),
            _chunk('"}'),
        ]])
        llm = LLMClient(model="test", client=client)
        chunks = list(llm.complete_stream(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "echo"}}],
        ))
        self.assertEqual(json.loads("".join(chunks)), {"type": "final", "content": "hello"})
        self.assertEqual(len(chunks), 3)

    def test_429_retries_using_server_delay(self):
        sleeps = []
        client = _Client([_StatusError(429, "Try again in 5 seconds"), _Response()])
        llm = LLMClient(model="test", client=client, sleep_fn=sleeps.append)
        self.assertEqual(llm.complete([{"role": "user", "content": "hi"}]), "ok")
        self.assertEqual(client.chat.completions.calls, 2)
        self.assertEqual(sleeps, [5.0])

    def test_non_transient_error_is_not_retried(self):
        client = _Client([_StatusError(400, "bad request")])
        llm = LLMClient(model="test", client=client, sleep_fn=lambda _: None)
        with self.assertRaises(_StatusError):
            llm.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(client.chat.completions.calls, 1)

    def test_retry_stops_at_limit(self):
        error = _StatusError(503, "unavailable")
        client = _Client([error, error, error])
        llm = LLMClient(model="test", client=client, max_attempts=3, sleep_fn=lambda _: None)
        with self.assertRaises(_StatusError):
            llm.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(client.chat.completions.calls, 3)


if __name__ == "__main__":
    unittest.main()
