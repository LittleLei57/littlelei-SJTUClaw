"""SSE 可审计事件流测试。"""

from pathlib import Path
from threading import Event, Thread
import json
import tempfile
import unittest

from fastapi.testclient import TestClient

from approval_store import ApprovalStore
from context_builder import ContextBuilder
from gateway import create_app
from runtime import AgentRuntime
from session_store import SessionStore
from tools import Tool, ToolRegistry


class ScriptedModel:
    def __init__(self, replies=None, error=None):
        self.replies = iter(replies or [])
        self.error = error

    def complete(self, messages):
        if self.error:
            raise self.error
        return next(self.replies)


class ExactMetricsModel:
    model = "metrics-model"

    def complete(self, messages):
        return "measured"

    def pop_metrics(self):
        return {
            "model": self.model,
            "durationMs": 12.5,
            "inputTokens": 10,
            "outputTokens": 3,
            "totalTokens": 13,
        }


class LengthStopModel:
    """A provider-like model that reports an output budget stop once."""

    def __init__(self):
        self.calls = []
        self._finish_reason = "length"

    def complete(self, messages, **kwargs):
        self.calls.append(dict(kwargs))
        if len(self.calls) == 1:
            return '{"type":"final","content":"partial"}'
        return '{"type":"final","content":"完整回答"}'

    def pop_metrics(self):
        reason = self._finish_reason
        self._finish_reason = "stop"
        return {
            "model": "length-stop-model",
            "durationMs": 1,
            "inputTokens": 10,
            "outputTokens": 512,
            "totalTokens": 522,
            "finishReason": reason,
        }


class LengthStopStreamingModel:
    """Emit a visible partial answer before one length-stop retry."""

    def __init__(self):
        self.calls = 0
        self._finish_reason = "length"

    def complete_stream(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield '{"type":"final","content":"已经展示的有效分析'
            yield '，不应在重试时消失。"}'
            return
        yield '{"type":"final","content":"重试后的完整回答"}'

    def pop_metrics(self):
        reason = self._finish_reason
        self._finish_reason = "stop"
        return {
            "model": "length-stop-streaming-model",
            "durationMs": 1,
            "inputTokens": 10,
            "outputTokens": 512,
            "totalTokens": 522,
            "finishReason": reason,
        }


class BlockingModel:
    def __init__(self):
        self.started = Event()
        self.release = Event()

    def complete(self, messages):
        self.started.set()
        self.release.wait(timeout=5)
        return "should not be committed"


class TrueStreamingModel:
    def __init__(self, chunks):
        self.chunks = chunks
        self.complete_called = False

    def complete(self, messages):
        self.complete_called = True
        raise AssertionError("true streaming path should not call complete")

    def complete_stream(self, messages):
        yield from self.chunks


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "data")

    def tearDown(self):
        self.temp.cleanup()

    def test_runtime_emits_status_and_answer_deltas(self):
        events = []
        runtime = AgentRuntime(
            ScriptedModel(['{"type":"final","content":"这是一个超过二十四个字符的回答，用于验证多个回答增量事件。"}']),
            self.store,
            ContextBuilder(),
            tool_registry=ToolRegistry(),
        )
        result = runtime.run("hello", event_callback=events.append)
        self.assertEqual(result.status, "completed")
        self.assertEqual(events[0]["type"], "status")
        deltas = [item["delta"] for item in events if item["type"] == "assistant_delta"]
        self.assertGreater(len(deltas), 1)
        self.assertEqual("".join(deltas), result.reply)
        types = [item["type"] for item in events]
        self.assertLess(types.index("assistant_final"), types.index("status", types.index("assistant_final") + 1))
        self.assertEqual(events[-1]["phase"], "completed")

    def test_output_limit_is_retried_with_a_larger_budget(self):
        model = LengthStopModel()
        events = []
        runtime = AgentRuntime(
            model,
            self.store,
            ContextBuilder(),
            tool_registry=ToolRegistry(),
        )
        result = runtime.run("请给出完整回答", event_callback=events.append)
        self.assertEqual(result.reply, "完整回答")
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(model.calls[0], {})
        self.assertGreaterEqual(model.calls[1]["max_tokens"], 1024)
        self.assertTrue(any(item.get("phase") == "model_output_limit_retry" for item in events))

    def test_streamed_output_limit_preserves_visible_partial_before_retry(self):
        model = LengthStopStreamingModel()
        events = []
        runtime = AgentRuntime(
            model,
            self.store,
            ContextBuilder(),
            tool_registry=ToolRegistry(),
        )
        result = runtime.run("请给出完整回答", event_callback=events.append)
        self.assertEqual(result.reply, "重试后的完整回答")
        session = self.store.get(result.session_id)
        segments = [
            item for item in session.messages
            if (item.get("metadata") or {}).get("kind") == "assistant_segment"
        ]
        self.assertEqual(len(segments), 1)
        self.assertIn("已经展示的有效分析", segments[0]["content"])
        self.assertEqual(segments[0]["metadata"]["phase"], "model_output_limit")
        resets = [item for item in events if item.get("type") == "assistant_reset"]
        self.assertTrue(any(item.get("preserved") for item in resets))

    def test_runtime_consumes_real_model_stream_for_plain_final(self):
        events = []
        model = TrueStreamingModel(['{"type":"final","content":"第一段', "，第二段", '，完成。"}'])
        runtime = AgentRuntime(model, self.store, ContextBuilder(), tool_registry=ToolRegistry())
        result = runtime.run("hello", event_callback=events.append)
        deltas = [item["delta"] for item in events if item["type"] == "assistant_delta"]
        self.assertFalse(model.complete_called)
        self.assertTrue(any(item.get("phase") == "model_stream" for item in events))
        self.assertEqual(deltas, ["第一段", "，第二段", "，完成。"])
        self.assertEqual(result.reply, "第一段，第二段，完成。")

    def test_fenced_protocol_response_still_streams_final_content(self):
        events = []
        model = TrueStreamingModel([
            "```json\n",
            '{"type":"final","content":"',
            "第一段",
            "，第二段",
            '"}\n```',
        ])
        runtime = AgentRuntime(model, self.store, ContextBuilder(), tool_registry=ToolRegistry())
        result = runtime.run("hello", event_callback=events.append)
        deltas = [item["delta"] for item in events if item["type"] == "assistant_delta"]
        self.assertEqual(deltas, ["第一段", "，第二段"])
        self.assertEqual(result.reply, "第一段，第二段")

    def test_prose_prefixed_final_protocol_still_streams(self):
        events = []
        model = TrueStreamingModel([
            "以下是最终回答：\n",
            '{"type":"final","content":"正文',
            "继续",
            '"}',
        ])
        runtime = AgentRuntime(model, self.store, ContextBuilder(), tool_registry=ToolRegistry())
        result = runtime.run("hello", event_callback=events.append)
        deltas = [item["delta"] for item in events if item["type"] == "assistant_delta"]
        self.assertEqual(deltas, ["正文", "继续"])
        self.assertEqual(result.reply, "正文继续")

    def test_tool_json_stream_is_buffered_and_not_shown_as_answer(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "echo", "echo", {"type": "object", "properties": {}, "additionalProperties": False}, lambda: "ok"
        ))
        model = TrueStreamingModel([
            '{"type":"tool_call",', '"tool":"echo","args":{}}',
            "工具完成。",
        ])
        # A model stream is one response per call, so provide a small multi-call wrapper.
        streams = iter([
            ['{"type":"tool_call",', '"tool":"echo","args":{}}'],
            ['{"type":"final","content":"', '工具完成。"}'],
        ])
        model.complete_stream = lambda _messages: iter(next(streams))
        events = []
        runtime = AgentRuntime(model, self.store, ContextBuilder(tool_definitions=registry.definitions()), tool_registry=registry)
        result = runtime.run("echo", event_callback=events.append)
        deltas = [item["delta"] for item in events if item["type"] == "assistant_delta"]
        self.assertEqual(deltas, ["工具完成。"])
        self.assertEqual(result.reply, "工具完成。")

    def test_prose_before_tool_json_is_never_streamed(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "echo", "echo", {"type": "object", "properties": {}, "additionalProperties": False}, lambda: "ok"
        ))
        streams = iter([
            ["我先查一下。", '{"type":"tool_call","tool":"echo","args":{}}'],
            ['{"type":"final","content":"查询完成。"}'],
        ])
        model = TrueStreamingModel([])
        model.complete_stream = lambda _messages: iter(next(streams))
        events = []
        runtime = AgentRuntime(model, self.store, ContextBuilder(tool_definitions=registry.definitions()), tool_registry=registry)
        result = runtime.run("echo", event_callback=events.append)
        deltas = [item["delta"] for item in events if item["type"] == "assistant_delta"]
        self.assertEqual(deltas, ["查询完成。"])
        self.assertNotIn("tool_call", "".join(deltas))
        self.assertEqual(result.reply, "查询完成。")

    def test_embedded_tool_brace_is_not_persisted_as_intermediate_prose(self):
        """A nested Tool object must not leave its opening brace in the transcript."""
        registry = ToolRegistry()
        registry.register(Tool(
            "echo", "echo", {"type": "object", "properties": {}, "additionalProperties": False}, lambda: "ok"
        ))
        first = json.dumps({
            "type": "final",
            "content": 'I will check it.\n\n{"type":"tool_call","tool":"echo","args":{}}',
        })
        marker = first.index("\\n\\n{") + len("\\n\\n{")
        streams = iter([
            [first[:marker], first[marker:]],
            ['{"type":"final","content":"Done."}'],
        ])
        model = TrueStreamingModel([])
        model.complete_stream = lambda _messages: iter(next(streams))
        events = []
        runtime = AgentRuntime(
            model,
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
            strict_tool_protocol=False,
        )
        result = runtime.run("echo", event_callback=events.append)
        self.assertEqual(result.reply, "Done.")
        self.assertTrue(any(item["type"] == "tool_call" for item in events))
        intermediate = [
            item.get("content", "")
            for item in self.store.current.messages
            if item.get("metadata", {}).get("displayOnly")
        ]
        self.assertTrue(intermediate)
        self.assertNotIn("{", intermediate[0])

    def test_partial_tool_protocol_suffix_is_removed_at_every_stream_boundary(self):
        prose = "I will inspect the workspace."
        suffixes = (
            "{",
            '{"t',
            '{"type":"tool_c',
            '{"type":"tool_call"',
            '{"type":"tool_calls","calls":[',
        )
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                visible = AgentRuntime._strip_partial_tool_protocol(
                    f"{prose}\n\n{suffix}"
                )
                self.assertEqual(visible, prose)

    def test_partial_tool_protocol_suffix_is_not_persisted(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "echo", "echo", {"type": "object", "properties": {}, "additionalProperties": False}, lambda: "ok"
        ))
        first = json.dumps({
            "type": "final",
            "content": 'I will inspect it.\n\n{"type":"tool_calls","calls":[{"tool":"echo","args":{}}]}',
        })
        split_at = first.index("tool_calls") + len("tool_c")
        streams = iter([
            [first[:split_at], first[split_at:]],
            ['{"type":"final","content":"Done."}'],
        ])
        model = TrueStreamingModel([])
        model.complete_stream = lambda _messages: iter(next(streams))
        events = []
        runtime = AgentRuntime(
            model,
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
            strict_tool_protocol=False,
        )

        result = runtime.run("echo", event_callback=events.append)

        self.assertEqual(result.reply, "Done.")
        segments = [
            item.get("content", "")
            for item in self.store.current.messages
            if item.get("metadata", {}).get("displayOnly")
        ]
        self.assertTrue(segments)
        self.assertEqual(segments[0], "I will inspect it.")
        self.assertNotIn('"type"', segments[0])

    def test_promised_search_without_tool_is_reset_and_retried(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "web_search", "search",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
            lambda query: {"query": query, "results": []},
        ))
        events = []
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"好的，我来搜索相关资料。现在开始搜索。"}',
                '{"type":"tool_call","tool":"web_search","args":{"query":"SJTU"}}',
                '{"type":"final","content":"搜索完成。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        result = runtime.run("帮我搜索", event_callback=events.append)
        types = [item["type"] for item in events]
        self.assertIn("assistant_reset", types)
        self.assertIn("tool_call", types)
        self.assertEqual(result.reply, "搜索完成。")

    def test_completed_search_with_optional_followup_is_not_retried(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "web_search", "search",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
            lambda query: {"query": query, "results": [{"title": "结果"}]},
        ))
        events = []
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"tool_call","tool":"web_search","args":{"query":"上海 AI"}}',
                '{"type":"final","content":"根据最新搜索到的信息，上海 AI 行业近期有多项公开进展。这里是已经核实的摘要和来源说明，内容足够回答本轮问题，不需要重新请求。要不要我再帮你查一下其他方向？"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )

        result = runtime.run("帮我搜索上海 AI", event_callback=events.append)

        self.assertIn("根据最新搜索到的信息", result.reply)
        self.assertEqual(len([item for item in events if item["type"] == "tool_call"]), 1)
        self.assertNotIn("assistant_reset", [item["type"] for item in events])

    def test_past_search_evidence_is_not_mistaken_for_a_new_promise(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "web_search", "search",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            lambda query: {"query": query, "results": [{"title": "repo"}]},
        ))
        events = []
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"tool_call","tool":"web_search","args":{"query":"taste-skill"}}',
                '{"type":"final","content":"通过搜索我拿到了这个仓库的完整信息！来看看吧。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        result = runtime.run("请搜索 taste-skill", event_callback=events.append)
        self.assertIn("完整信息", result.reply)
        self.assertEqual(len([item for item in events if item["type"] == "tool_call"]), 1)
        self.assertNotIn("assistant_reset", [item["type"] for item in events])

    def test_completed_search_with_future_search_policy_is_not_retried(self):
        """A future promise must not invalidate an answer backed by a Tool result."""
        registry = ToolRegistry()
        registry.register(Tool(
            "web_search", "search",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
            lambda query: {"query": query, "results": [{"title": "result"}]},
        ))
        events = []
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"tool_call","tool":"web_search","args":{"query":"上海 距离"}}',
                '{"type":"final","content":"根据搜索结果，拉萨距离上海约 4150 km，之前的 2300 km 判断不准确。以后这类问题我会先搜再说。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )

        result = runtime.run("核实上海到拉萨的距离", event_callback=events.append)

        self.assertIn("4150", result.reply)
        self.assertNotIn("assistant_reset", [item["type"] for item in events])

    def test_truncated_single_character_final_is_retried(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "echo", "echo",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: "ok",
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"L"}',
                '{"type":"final","content":"这是完整的搜索核实结果。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        events = []

        result = runtime.run("请给出完整结果", event_callback=events.append)

        self.assertEqual(result.reply, "这是完整的搜索核实结果。")
        self.assertIn("truncated_final_retry", [item.get("phase") for item in events if item["type"] == "status"])
        self.assertNotIn("L", [item.get("content") for item in events if item["type"] == "assistant_final"])

    def test_combining_mark_fragment_is_retried(self):
        """A streamed retry must not persist the provider's ``L̆试试`` fragment."""
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"L\\u0306\\u8bd5\\u8bd5"}',
                '{"type":"final","content":"这是完整答案。"}',
            ]),
            self.store,
            ContextBuilder(),
            tool_registry=ToolRegistry(),
        )

        result = runtime.run("请继续", event_callback=lambda _: None)

        self.assertEqual(result.reply, "这是完整答案。")

    def test_search_confirmation_recovers_a_narrated_promise(self):
        queries = []
        registry = ToolRegistry()
        registry.register(Tool(
            "web_search", "search",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
            lambda query, **_: queries.append(query) or {"query": query, "results": [{"title": "verified"}]},
        ))
        session = self.store.current
        session.messages.extend([
            {"role": "user", "content": "那什么地方离上海2300km", "metadata": {"source": "web"}},
            {"role": "assistant", "content": "上一条回答"},
        ])
        self.store.save(session)
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"我现在真的上网搜一下，给你更准的答案。需要我搜一搜吗？"}',
                '{"type":"final","content":"根据搜索结果，之前的估计需要修正。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        events = []

        result = runtime.run("你搜了吗？还是你靠知识回答的呀？", event_callback=events.append)

        self.assertEqual(result.reply, "根据搜索结果，之前的估计需要修正。")
        self.assertEqual(queries, ["那什么地方离上海2300km"])
        self.assertNotIn("protocol_retry", [item["type"] for item in events])

    def test_subjectless_search_promise_is_also_retried(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "web_search", "search",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
            lambda query: {"query": query},
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"好问题，我帮你对比一下。\\n\\n先搜一下 IPADS 的最新情况。"}',
                '{"type":"tool_call","tool":"web_search","args":{"query":"IPADS"}}',
                '{"type":"final","content":"对比完成。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        result = runtime.run("对比 IPADS")
        self.assertEqual(result.reply, "对比完成。")
        self.assertTrue(any(item["type"] == "tool_call" for item in self.store.current.activity))

    def test_workspace_browsing_promise_is_retried_with_list_dir(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "list_dir", "list directory",
            {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False},
            lambda path=".": {"path": path, "entries": []},
        ))
        events = []
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"好嘞，让我看看当前 Workspace 里有什么文件和目录！"}',
                '{"type":"tool_call","tool":"list_dir","args":{"path":"."}}',
                '{"type":"final","content":"当前目录中有 gateway.py。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )

        result = runtime.run("你 Workspace 里有什么？", event_callback=events.append)

        self.assertEqual(result.reply, "当前目录中有 gateway.py。")
        self.assertIn("assistant_reset", [item["type"] for item in events])
        self.assertTrue(any(item["type"] == "tool_call" and item["tool"] == "list_dir" for item in events))

    def test_empty_completion_after_protocol_retry_retries_instead_of_stranding_turn(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_file", "read file",
            {"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False},
            lambda path=".": {"path": path, "content": "source"},
        ))
        events = []
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"让我看看当前 Workspace 里的文件。"}',
                "",
                '{"type":"tool_call","tool":"read_file","args":{"path":"gateway.py"}}',
                '{"type":"final","content":"已读取文件。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )

        result = runtime.run("读一下这些文件", event_callback=events.append)

        self.assertEqual(result.reply, "已读取文件。")
        self.assertIn("empty_response_retry", [item.get("phase") for item in events if item["type"] == "status"])
        self.assertTrue(any(item["type"] == "tool_call" and item["tool"] == "read_file" for item in events))

    def test_direct_file_read_request_cannot_finish_with_greeting_only(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_file", "read file",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
            lambda path: {"path": path, "content": "gateway source"},
        ))
        events = []
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"Ciallo ～"}',
                '{"type":"tool_call","tool":"read_file","args":{"path":"gateway.py"}}',
                '{"type":"final","content":"我已读到 gateway.py。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )

        result = runtime.run("读一下这些文件吧", event_callback=events.append)

        self.assertEqual(result.reply, "我已读到 gateway.py。")
        self.assertIn("assistant_reset", [item["type"] for item in events])
        self.assertTrue(any(item["type"] == "tool_call" and item["tool"] == "read_file" for item in events))

    def test_execution_evidence_retry_keeps_substantive_streamed_draft(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "create_file", "create file",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            lambda path, content: {"path": path, "created": True},
        ))
        streams = iter([
            [
                '{"type":"final","content":"我先说明实现思路：会创建一个独立文件，'
                '写入经过校验的内容。文件已经创建完成。"}',
            ],
            [
                '{"type":"tool_call","tool":"create_file","args":'
                '{"path":"answer.txt","content":"ok"}}',
            ],
            ['{"type":"final","content":"文件已经根据真实工具结果创建完成。"}'],
        ])
        model = TrueStreamingModel([])
        model.complete_stream = lambda _messages, **_kwargs: iter(next(streams))
        events = []
        runtime = AgentRuntime(
            model,
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
            strict_tool_protocol=False,
        )

        result = runtime.run("请创建 answer.txt", event_callback=events.append)

        self.assertEqual(result.reply, "文件已经根据真实工具结果创建完成。")
        drafts = [
            item for item in self.store.current.messages
            if item.get("metadata", {}).get("kind") == "assistant_segment"
            and item.get("metadata", {}).get("phase") == "execution_evidence"
        ]
        self.assertEqual(len(drafts), 1)
        self.assertIn("我先说明实现思路", drafts[0]["content"])
        reset = next(
            item for item in events
            if item.get("type") == "assistant_reset"
            and item.get("phase") == "execution_evidence"
        )
        self.assertTrue(reset.get("preserved"))

    def test_plain_text_is_protocol_error_in_tool_mode_not_a_final_answer(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_file", "read file",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
            lambda path: {"path": path, "content": "gateway source"},
        ))
        events = []
        runtime = AgentRuntime(
            ScriptedModel([
                "Ciallo ~ (≧ω≦)☆",
                '{"type":"tool_call","tool":"read_file","args":{"path":"gateway.py"}}',
                '{"type":"final","content":"已读取 gateway.py。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )

        result = runtime.run("读一下这些文件吧", event_callback=events.append)

        self.assertEqual(result.reply, "已读取 gateway.py。")
        self.assertIn("assistant_reset", [item["type"] for item in events])
        self.assertTrue(any(item["type"] == "tool_call" for item in events))
        self.assertFalse(any(
            message.get("role") == "assistant" and "Ciallo" in message.get("content", "")
            for message in self.store.current.messages
        ))
        self.assertTrue(any(
            message.get("role") == "system" and message.get("metadata", {}).get("kind") == "protocol_error"
            for message in self.store.current.messages
        ))

    def test_plain_markdown_after_tool_result_is_accepted_as_final(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "current_time", "current time",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: {"iso": "2026-07-13T15:53:34+08:00"},
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"tool_call","tool":"current_time","args":{}}',
                "现在是 2026 年 7 月 13 日 15:53（中国标准时间）。",
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )

        result = runtime.run("查询当前时间")

        self.assertEqual(result.reply, "现在是 2026 年 7 月 13 日 15:53（中国标准时间）。")

    def test_ordinary_chat_with_tools_keeps_plain_text_compatibility(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "current_time", "current time",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: {"iso": "2026-07-13T15:53:34+08:00"},
        ))
        runtime = AgentRuntime(
            ScriptedModel(["你好，我在呢～"]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )

        result = runtime.run("你好")

        self.assertEqual(result.reply, "你好，我在呢～")

    def test_explicit_filename_request_repairs_non_json_tool_promise(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_file", "read file",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
            lambda path: {"path": path, "content": "source"},
        ))
        events = []
        runtime = AgentRuntime(
            ScriptedModel([
                "好的，我来读取 compaction.py，先看看里面的实现。",
                "已读取 compaction.py，核心逻辑如下。",
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )

        result = runtime.run("读一下 compaction.py 吧", event_callback=events.append)

        self.assertEqual(result.reply, "已读取 compaction.py，核心逻辑如下。")
        self.assertTrue(any(item["type"] == "tool_call" and item["tool"] == "read_file" for item in events))

    def test_search_promise_after_introductory_clause_is_retried(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "web_search", "search",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
            lambda query: {"query": query},
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"天气信息如下。至于最近有没有台风要来，我帮你查一下最新情况。"}',
                '{"type":"tool_call","tool":"web_search","args":{"query":"2026 上海 最新台风预警"}}',
                '{"type":"final","content":"目前没有查到生效中的台风预警。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        result = runtime.run("最近有台风要来上海吗")
        self.assertEqual(result.reply, "目前没有查到生效中的台风预警。")
        calls = [item for item in self.store.current.activity if item["type"] == "tool_call"]
        self.assertEqual(calls[-1]["data"]["tool"], "web_search")

    def test_direct_attachment_read_promise_is_retried(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_attachment", "read attachment",
            {"type": "object", "properties": {"attachment_id": {"type": "string"}}, "required": ["attachment_id"], "additionalProperties": False},
            lambda attachment_id: {"attachmentId": attachment_id, "content": "PDF text"},
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"我来直接读取 `att_pdf` 这个附件："}',
                '{"type":"tool_call","tool":"read_attachment","args":{"attachment_id":"att_pdf"}}',
                '{"type":"final","content":"PDF 已读取完成。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        result = runtime.run("读取 PDF")
        self.assertEqual(result.reply, "PDF 已读取完成。")
        self.assertTrue(any(item["type"] == "tool_call" for item in self.store.current.activity))

    def test_truncated_attachment_is_resumed_before_final_answer(self):
        registry = ToolRegistry()
        reads = []

        def read_attachment(attachment_id, start_char=0):
            reads.append((attachment_id, start_char))
            if start_char == 0:
                return {
                    "attachmentId": attachment_id,
                    "content": "first chunk",
                    "truncated": True,
                    "nextOffset": 11,
                }
            return {
                "attachmentId": attachment_id,
                "content": "second chunk",
                "truncated": False,
                "nextOffset": None,
            }

        registry.register(Tool(
            "read_attachment", "read attachment",
            {
                "type": "object",
                "properties": {
                    "attachment_id": {"type": "string"},
                    "start_char": {"type": "integer"},
                },
                "required": ["attachment_id"],
                "additionalProperties": False,
            },
            read_attachment,
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"tool_call","tool":"read_attachment","args":{"attachment_id":"att_long"}}',
                '{"type":"final","content":"第一段已经够用了。"}',
                '{"type":"final","content":"附件已完整读取并汇总。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        result = runtime.run("完整读取长附件")
        self.assertEqual(reads, [("att_long", 0), ("att_long", 11)])
        self.assertEqual(result.reply, "附件已完整读取并汇总。")

    def test_named_tool_promise_with_chinese_period_is_retried(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_attachment", "read attachment",
            {"type": "object", "properties": {"attachment_id": {"type": "string"}}, "required": ["attachment_id"], "additionalProperties": False},
            lambda attachment_id: {"content": "PDF body"},
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"好的，我直接用 `read_attachment` 来读取这个 PDF 附件。"}',
                '{"type":"tool_call","tool":"read_attachment","args":{"attachment_id":"att_pdf"}}',
                '{"type":"final","content":"读取完成。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        result = runtime.run("再试试读取 PDF")
        self.assertEqual(result.reply, "读取完成。")

    def test_completed_tool_description_is_not_retried(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_attachment", "read attachment",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: {},
        ))
        runtime = AgentRuntime(
            ScriptedModel(['{"type":"final","content":"我已经使用 read_attachment 读取完成，结论如下。"}']),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        result = runtime.run("总结结果")
        self.assertIn("结论如下", result.reply)

    def test_newly_attached_file_is_read_before_first_model_call(self):
        registry = ToolRegistry()
        reads = []
        registry.register(Tool(
            "read_attachment", "read attachment",
            {"type": "object", "properties": {"attachment_id": {"type": "string"}}, "required": ["attachment_id"], "additionalProperties": False},
            lambda attachment_id: reads.append(attachment_id) or {"content": "verified PDF content"},
        ))

        class InspectingModel:
            def __init__(self):
                self.messages = None

            def complete(self, messages):
                self.messages = messages
                return '{"type":"final","content":"已根据真实附件回答。"}'

        model = InspectingModel()
        runtime = AgentRuntime(
            model, self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        message = (
            '分析这个 PDF\n\n[attached_files] '
            '[{"attachmentId":"att_new","filename":"plan.pdf","contentType":"application/pdf"}]'
        )
        result = runtime.run(message)
        self.assertEqual(reads, ["att_new"])
        self.assertIn("verified PDF content", model.messages[-1]["content"])
        self.assertEqual(result.reply, "已根据真实附件回答。")
        calls = [item for item in self.store.current.activity if item["type"] == "tool_call"]
        self.assertEqual(calls[0]["data"]["automatic"], "attached_files")

    def test_subjectless_attachment_read_promise_is_retried(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_attachment", "read attachment",
            {"type": "object", "properties": {"attachment_id": {"type": "string"}}, "required": ["attachment_id"], "additionalProperties": False},
            lambda attachment_id: {"content": attachment_id},
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"先读取第二个附件 `att_two`："}',
                '{"type":"tool_call","tool":"read_attachment","args":{"attachment_id":"att_two"}}',
                '{"type":"final","content":"读取完成。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        self.assertEqual(runtime.run("读取第二个附件").reply, "读取完成。")

    def test_short_retry_reuses_latest_attached_files(self):
        registry = ToolRegistry()
        reads = []
        registry.register(Tool(
            "read_attachment", "read attachment",
            {"type": "object", "properties": {"attachment_id": {"type": "string"}}, "required": ["attachment_id"], "additionalProperties": False},
            lambda attachment_id: reads.append(attachment_id) or {"content": "real content"},
        ))
        session = self.store.current
        session.messages.extend([
            {"role": "user", "content": (
                '这是培养方案\n\n[attached_files] '
                '[{"attachmentId":"att_latest","filename":"plan.pdf","contentType":"application/pdf"}]'
            )},
            {"role": "assistant", "content": "上一次回答失败。"},
        ])
        self.store.save(session)
        runtime = AgentRuntime(
            ScriptedModel(['{"type":"final","content":"这次基于真实内容回答。"}']),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        result = runtime.run("重试")
        self.assertEqual(reads, ["att_latest"])
        self.assertEqual(result.reply, "这次基于真实内容回答。")

    def test_re_read_promise_with_adverb_is_retried(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_attachment", "read attachment",
            {"type": "object", "properties": {"attachment_id": {"type": "string"}}, "required": ["attachment_id"], "additionalProperties": False},
            lambda attachment_id: {"content": attachment_id},
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"final","content":"明白了，我先重新读取你上传的第二个附件。"}',
                '{"type":"tool_call","tool":"read_attachment","args":{"attachment_id":"att_two"}}',
                '{"type":"final","content":"读取成功。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        self.assertEqual(runtime.run("重新读").reply, "读取成功。")

    def test_tool_events_are_emitted_before_final_answer(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "echo", "echo input",
            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
            lambda text: text,
        ))
        events = []
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"tool_call","tool":"echo","args":{"text":"ok"}}',
                '{"type":"final","content":"done"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        runtime.run("echo", event_callback=events.append)
        types = [item["type"] for item in events]
        self.assertLess(types.index("tool_call"), types.index("tool_result"))
        self.assertLess(types.index("tool_result"), types.index("assistant_delta"))

    def test_duplicate_tool_call_reuses_first_result(self):
        calls = []
        registry = ToolRegistry()
        registry.register(Tool(
            "echo", "echo input",
            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
            lambda text: calls.append(text) or {"echo": text},
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"tool_call","tool":"echo","args":{"text":"same"}}',
                '{"type":"tool_call","tool":"echo","args":{"text":"same"}}',
                '{"type":"final","content":"已完成。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )
        events = []

        result = runtime.run("echo", event_callback=events.append)

        self.assertEqual(result.reply, "已完成。")
        self.assertEqual(calls, ["same"])
        duplicate_results = [
            item for item in events
            if item["type"] == "tool_result" and item.get("deduplicated")
        ]
        self.assertEqual(len(duplicate_results), 1)
        self.assertTrue(duplicate_results[0]["result"]["success"])

    def test_repeated_attachment_read_advances_from_next_page(self):
        calls = []
        registry = ToolRegistry()

        def read_attachment(attachment_id, start_page=None):
            calls.append((attachment_id, start_page))
            if start_page is None:
                return {
                    "attachmentId": attachment_id,
                    "content": "page one",
                    "truncated": True,
                    "nextPage": 2,
                }
            return {
                "attachmentId": attachment_id,
                "content": "page two",
                "truncated": False,
                "nextPage": None,
            }

        registry.register(Tool(
            "read_attachment",
            "read attachment",
            {
                "type": "object",
                "properties": {
                    "attachment_id": {"type": "string"},
                    "start_page": {"type": "integer"},
                },
                "required": ["attachment_id"],
                "additionalProperties": False,
            },
            read_attachment,
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"tool_call","tool":"read_attachment","args":{"attachment_id":"att_large"}}',
                '{"type":"tool_call","tool":"read_attachment","args":{"attachment_id":"att_large"}}',
                '{"type":"final","content":"两段均已读取。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )

        result = runtime.run("读取这个大附件")

        self.assertEqual(result.reply, "两段均已读取。")
        self.assertEqual(calls, [("att_large", None), ("att_large", 2)])
        self.assertEqual(result.tool_events[1]["args"]["start_page"], 2)

    def test_repeated_text_like_attachment_advances_from_next_offset(self):
        calls = []
        registry = ToolRegistry()

        def read_attachment(attachment_id, start_char=0):
            calls.append((attachment_id, start_char))
            if start_char == 0:
                return {
                    "attachmentId": attachment_id,
                    "content": "first text chunk",
                    "truncated": True,
                    "nextOffset": 16,
                }
            return {
                "attachmentId": attachment_id,
                "content": "second text chunk",
                "truncated": False,
                "nextOffset": None,
            }

        registry.register(Tool(
            "read_attachment",
            "read attachment",
            {
                "type": "object",
                "properties": {
                    "attachment_id": {"type": "string"},
                    "start_char": {"type": "integer"},
                },
                "required": ["attachment_id"],
                "additionalProperties": False,
            },
            read_attachment,
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"tool_call","tool":"read_attachment","args":{"attachment_id":"att_docx"}}',
                '{"type":"tool_call","tool":"read_attachment","args":{"attachment_id":"att_docx"}}',
                '{"type":"final","content":"文档两段均已读取。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )

        result = runtime.run("读取这个长 DOCX")

        self.assertEqual(result.reply, "文档两段均已读取。")
        self.assertEqual(calls, [("att_docx", 0), ("att_docx", 16)])
        self.assertEqual(result.tool_events[1]["args"]["start_char"], 16)

    def test_successful_read_recovers_earlier_bad_pagination_attempt(self):
        registry = ToolRegistry()

        def read_attachment(attachment_id, start_page=None):
            if start_page == 0:
                raise ValueError("bad page")
            return {
                "attachmentId": attachment_id,
                "content": "usable",
                "truncated": False,
            }

        registry.register(Tool(
            "read_attachment",
            "read attachment",
            {
                "type": "object",
                "properties": {
                    "attachment_id": {"type": "string"},
                    "start_page": {"type": "integer"},
                },
                "required": ["attachment_id"],
                "additionalProperties": False,
            },
            read_attachment,
        ))
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"tool_call","tool":"read_attachment","args":{"attachment_id":"att_pdf","start_page":0}}',
                '{"type":"tool_call","tool":"read_attachment","args":{"attachment_id":"att_pdf","start_page":1}}',
                '{"type":"final","content":"已经完整读取文件内容。"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
        )

        result = runtime.run("读取 PDF")

        self.assertEqual(result.reply, "已经完整读取文件内容。")

    def test_event_callback_failure_does_not_break_agent(self):
        runtime = AgentRuntime(ScriptedModel(["safe answer"]), self.store, ContextBuilder())
        result = runtime.run("hello", event_callback=lambda _: (_ for _ in ()).throw(RuntimeError("ui gone")))
        self.assertEqual(result.reply, "safe answer")

    def test_sse_endpoint_contains_named_events_and_done(self):
        runtime = AgentRuntime(ScriptedModel(["streamed answer"]), self.store, ContextBuilder())
        client = TestClient(create_app(runtime, web_dir=Path(__file__).parents[1] / "web"))
        response = client.post(
            "/api/chat/stream", json={"sessionId": "default", "message": "hello"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertIn("event: status", response.text)
        self.assertIn("event: assistant_delta", response.text)
        self.assertIn("event: done", response.text)
        self.assertIn("streamed answer", response.text)
        client.close()

    def test_sse_agent_error_is_an_event_not_process_failure(self):
        runtime = AgentRuntime(
            ScriptedModel(error=RuntimeError("offline")), self.store, ContextBuilder()
        )
        client = TestClient(create_app(runtime, web_dir=Path(__file__).parents[1] / "web"))
        response = client.post(
            "/api/chat/stream", json={"sessionId": "default", "message": "hello"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: error", response.text)
        self.assertIn("offline", response.text)
        self.assertEqual(client.get("/api/health").status_code, 200)
        client.close()

    def test_sse_exposes_approval_event_without_executing_tool(self):
        registry = ToolRegistry()
        called = []
        registry.register(Tool(
            "protected", "protected action",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: called.append(True),
            "approval_required",
        ))
        runtime = AgentRuntime(
            ScriptedModel(['{"type":"tool_call","tool":"protected","args":{}}']),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
            approval_store=ApprovalStore(self.root / "data"),
        )
        client = TestClient(create_app(runtime, web_dir=Path(__file__).parents[1] / "web"))
        response = client.post(
            "/api/chat/stream", json={"sessionId": "default", "message": "do it"}
        )
        self.assertIn("event: approval_required", response.text)
        self.assertIn('"status": "approval_required"', response.text)
        self.assertEqual(called, [])
        client.close()

    def test_approval_resume_streams_tool_result_and_followup_answer(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "protected", "protected action",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: {"message": "executed"},
            "approval_required",
        ))
        approvals = ApprovalStore(self.root / "data")
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"tool_call","tool":"protected","args":{}}',
                '{"type":"final","content":"审批后任务完成。"}',
            ]),
            self.store, ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry, approval_store=approvals,
        )
        pending = runtime.run("do it")
        approval_id = pending.pending_approvals[0]["approvalId"]
        client = TestClient(create_app(
            runtime, web_dir=Path(__file__).parents[1] / "web",
            start_background_services=False,
        ))
        response = client.post(
            f"/api/approvals/{approval_id}/decision/stream",
            json={"approved": True, "turnId": "turn_approval_stream"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: tool_result", response.text)
        self.assertIn("event: assistant_delta", response.text)
        self.assertIn("审批后任务完成。", response.text)
        self.assertIn('"status": "completed"', response.text)
        client.close()

    def test_approval_refresh_resume_keeps_one_logical_turn_and_executes_once(self):
        registry = ToolRegistry()
        calls = []

        def protected():
            calls.append("executed")
            return {"message": "executed"}

        registry.register(Tool(
            "protected", "protected action",
            {"type": "object", "properties": {}, "additionalProperties": False},
            protected,
            "approval_required",
        ))
        approvals = ApprovalStore(self.root / "data")
        runtime = AgentRuntime(
            ScriptedModel([
                '{"type":"tool_call","tool":"protected","args":{}}',
                '{"type":"final","content":"done after approval"}',
            ]),
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
            approval_store=approvals,
        )
        app = create_app(
            runtime,
            web_dir=Path(__file__).parents[1] / "web",
            start_background_services=False,
        )
        with TestClient(app) as client:
            first = client.post(
                "/api/chat/stream",
                json={
                    "sessionId": "default",
                    "message": "do it",
                    "turnId": "turn_approval_refresh",
                },
            )
            self.assertEqual(first.status_code, 200)
            self.assertIn("event: approval_required", first.text)
            approval = approvals.list(session_id="default")[0]
            self.assertEqual(approval.turn_id, "turn_approval_refresh")
            self.assertEqual(calls, [])

            # A refreshed page rebuilds this suspended state from SQLite.
            active = client.get(
                "/api/turns/active?sessionId=default"
            ).json()["turns"]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["turnId"], "turn_approval_refresh")
            self.assertEqual(active[0]["status"], "awaiting_approval")
            self.assertEqual(active[0]["approvalId"], approval.approval_id)

            resumed = client.post(
                f"/api/approvals/{approval.approval_id}/decision/stream",
                json={"approved": True, "turnId": "wrong_client_turn"},
            )
            self.assertEqual(resumed.status_code, 200)
            self.assertIn("done after approval", resumed.text)
            self.assertEqual(calls, ["executed"])
            checkpoint = client.get(
                "/api/turns/turn_approval_refresh"
            ).json()
            self.assertEqual(checkpoint["status"], "completed")
            self.assertEqual(checkpoint["resume_count"], 1)

            # Double-click/retry reconciles the durable outcome only.
            duplicate = client.post(
                f"/api/approvals/{approval.approval_id}/decision/stream",
                json={"approved": True},
            )
            self.assertEqual(duplicate.status_code, 200)
            self.assertIn('"alreadyResolved": true', duplicate.text)
            self.assertEqual(calls, ["executed"])

    def test_favicon_is_served(self):
        runtime = AgentRuntime(ScriptedModel([]), self.store, ContextBuilder())
        client = TestClient(create_app(runtime, web_dir=Path(__file__).parents[1] / "web"))
        response = client.get("/favicon.svg")
        self.assertEqual(response.status_code, 200)
        self.assertIn("image/svg+xml", response.headers["content-type"])
        client.close()

    def test_exact_metrics_are_persisted_in_activity(self):
        runtime = AgentRuntime(ExactMetricsModel(), self.store, ContextBuilder())
        result = runtime.run("metrics")
        self.assertEqual(result.metrics[0]["totalTokens"], 13)
        self.assertFalse(result.metrics[0]["estimated"])
        activity = self.store.current.activity
        self.assertEqual(activity[0]["type"], "turn_started")
        self.assertTrue(any(item["type"] == "model_call" for item in activity))
        self.assertEqual(activity[-1]["type"], "turn_completed")

    def test_running_sse_turn_can_be_cancelled(self):
        model = BlockingModel()
        runtime = AgentRuntime(model, self.store, ContextBuilder())
        app = create_app(runtime, web_dir=Path(__file__).parents[1] / "web")
        stream_client = TestClient(app)
        control_client = TestClient(app)
        holder = {}

        def send_stream():
            holder["response"] = stream_client.post(
                "/api/chat/stream",
                json={
                    "sessionId": "default",
                    "message": "long request",
                    "turnId": "turn_cancel_test",
                },
            )

        thread = Thread(target=send_stream)
        thread.start()
        self.assertTrue(model.started.wait(timeout=3))
        cancelled = control_client.post("/api/turns/turn_cancel_test/cancel")
        self.assertEqual(cancelled.status_code, 200)
        # The public Turn must settle without waiting for a blocking provider
        # call to return. The abandoned daemon call is run-fenced, so a late
        # result can no longer commit messages or terminal state.
        thread.join(timeout=1.5)
        self.assertFalse(thread.is_alive())
        model.release.set()
        self.assertIn("event: cancelled", holder["response"].text)
        # Cancellation keeps the accepted user request for audit/retry, while
        # never committing a partial assistant answer as if it had completed.
        self.assertEqual(len(self.store.current.messages), 1)
        self.assertEqual(self.store.current.messages[0]["role"], "user")
        self.assertEqual(self.store.current.messages[0]["content"], "long request")
        # The browser can retry its stop request after the SSE terminal event;
        # the durable checkpoint makes that race a successful no-op.
        repeated = control_client.post("/api/turns/turn_cancel_test/cancel")
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.json()["alreadyFinished"])
        self.assertEqual(repeated.json()["status"], "cancelled")
        stream_client.close()
        control_client.close()


if __name__ == "__main__":
    unittest.main()
