"""Compaction 测试。"""

import json
from pathlib import Path
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch

from compaction import CompactionError, Compactor
from context_builder import ContextBuilder
from main import run_cli
from runtime import AgentRuntime
from session_store import SessionStore


class FakeModel:
    def __init__(self, replies=None, error=None):
        self.replies = iter(replies or [])
        self.error = error
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return next(self.replies)


class SlowEchoModel:
    def __init__(self, delay=0.05):
        self.delay = delay
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        time.sleep(self.delay)
        user = next(item["content"] for item in reversed(messages) if item["role"] == "user")
        return f"reply:{user}"


class CompactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = SessionStore(root / "data")
        self.prompt = root / "compact.md"
        self.prompt.write_text("COMPACT-ONLY-CONVERSATION", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def seed_messages(self, count=12):
        session = self.store.current
        session.messages = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"m{index}"}
            for index in range(count)
        ]
        self.store.save(session)
        return session

    def make_compactor(self, model, **kwargs):
        return Compactor(
            model,
            self.store,
            self.prompt,
            max_messages=kwargs.get("max_messages", 10),
            max_characters=kwargs.get("max_characters", 10_000),
            keep_recent=kwargs.get("keep_recent", 4),
            chunk_characters=kwargs.get("chunk_characters"),
            max_tokens=kwargs.get("max_tokens"),
            chunk_tokens=kwargs.get("chunk_tokens"),
        )

    def test_success_merges_summary_and_keeps_recent_messages(self):
        session = self.seed_messages()
        session.summary = "OLD-SUMMARY"
        self.store.save(session)
        model = FakeModel(["NEW-SUMMARY"])

        result = self.make_compactor(model).compact(session)

        restored = self.store.current
        self.assertEqual(result.old_messages, 8)
        self.assertEqual(restored.summary, "NEW-SUMMARY")
        self.assertEqual([item["content"] for item in restored.messages], ["m8", "m9", "m10", "m11"])
        self.assertIn("OLD-SUMMARY", model.calls[0][1]["content"])
        self.assertNotIn("System Rules", str(model.calls[0]))

        self.assertEqual(result.summary_version, 1)
        self.assertEqual((result.covered_message_start, result.covered_message_end), (1, 8))
        self.assertEqual(restored.summary_meta["version"], 1)
        self.assertEqual(restored.summary_meta["coveredMessageRange"], {"start": 1, "end": 8})
        self.assertTrue(restored.summary_meta["qualityWarnings"])

    def test_summary_quality_warnings_check_empty_sections_and_sources(self):
        summary = """### 当前任务
- 整理附件

### 已完成
- 无

### 用户偏好与约束
- 无

### 关键事实与证据
- 事实已核对

### 附件与文件来源
- 无

### 待解决问题
- 无

### 下一步
- 继续读取

### 不应记住
- 无
"""
        warnings = Compactor._summary_quality_warnings(
            summary,
            [{"role": "user", "content": "请读取 att_demo123 和 plan.pdf"}],
        )
        self.assertIn("栏目为空：已完成", warnings)
        self.assertIn("栏目为空：附件与文件来源", warnings)
        self.assertIn("来源未在摘要中出现：att_demo123", warnings)
        self.assertIn("来源未在摘要中出现：plan.pdf", warnings)

    def test_contentless_summary_does_not_replace_substantive_history(self):
        session = self.store.current
        session.messages = [
            {"role": "user", "content": "Please preserve the project plan and its agreed constraints."},
            {"role": "assistant", "content": "A" * 260},
            {"role": "user", "content": "Continue from the plan after compaction."},
            {"role": "assistant", "content": "B" * 260},
            {"role": "user", "content": "recent user message"},
            {"role": "assistant", "content": "recent assistant message"},
            {"role": "user", "content": "another recent message"},
            {"role": "assistant", "content": "another recent reply"},
        ]
        self.store.save(session)
        placeholder = "### Current\n- None\n### Completed\n- None\n### Next\n- Waiting for user"

        with self.assertRaisesRegex(CompactionError, "有效任务或事实"):
            self.make_compactor(FakeModel([placeholder]), keep_recent=4).compact(session, force=True)

        self.assertEqual(len(self.store.current.messages), 8)
        self.assertEqual(self.store.current.summary, "")

    def test_real_compact_prompt_is_structured_for_continuation(self):
        prompt = (Path(__file__).parents[1] / "prompts" / "compact_prompt.md").read_text(encoding="utf-8")
        for heading in [
            "### 当前任务",
            "### 已完成",
            "### 用户偏好与约束",
            "### 关键事实与证据",
            "### 附件与文件来源",
            "### 待解决问题",
            "### 下一步",
            "### 不应记住",
        ]:
            self.assertIn(heading, prompt)
        self.assertIn("后来的纠正", prompt)
        self.assertIn("附件 ID", prompt)
        self.assertIn("不要把不同附件", prompt)
        self.assertIn("不要编造", prompt)

    def test_large_history_is_summarized_in_chunks_then_merged(self):
        session = self.store.current
        session.summary = "OLD-SUMMARY"
        session.messages = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"message-{index}-" + ("x" * 700)}
            for index in range(14)
        ]
        self.store.save(session)
        model = FakeModel(["PART-1", "PART-2", "PART-3", "FINAL-SUMMARY"])

        result = self.make_compactor(
            model,
            max_messages=4,
            keep_recent=2,
            chunk_characters=3_000,
        ).compact(session)

        self.assertEqual(result.chunks, 3)
        self.assertEqual(result.summary, "FINAL-SUMMARY")
        self.assertEqual(self.store.current.summary, "FINAL-SUMMARY")
        self.assertEqual(len(self.store.current.messages), 2)
        self.assertEqual(len(model.calls), 4)
        self.assertIn("第 1/3 个旧消息分片", model.calls[0][1]["content"])
        self.assertIn("PART-1", model.calls[-1][1]["content"])
        self.assertIn("PART-3", model.calls[-1][1]["content"])

    def test_token_budget_catches_cjk_history_even_below_character_limit(self):
        session = self.store.current
        session.messages = [
            {"role": "user", "content": "请保留这条重要事实。" * 30},
            {"role": "assistant", "content": "已记录。"},
            {"role": "user", "content": "最近的问题"},
            {"role": "assistant", "content": "最近的回答"},
        ]
        self.store.save(session)
        compactor = self.make_compactor(
            FakeModel(["TOKEN-SUMMARY"]),
            max_messages=100,
            max_characters=50_000,
            max_tokens=100,
            keep_recent=2,
            chunk_tokens=500,
        )

        estimate = compactor.estimate(session)

        self.assertGreater(estimate["semanticTokens"], 100)
        self.assertTrue(estimate["shouldCompact"])
        self.assertEqual(compactor.compact(session).summary, "TOKEN-SUMMARY")

    def test_single_huge_message_is_split_before_compaction(self):
        session = self.store.current
        session.messages = [
            {"role": "user", "content": "A" * 9_000},
            {"role": "assistant", "content": "recent answer"},
            {"role": "user", "content": "recent question"},
        ]
        self.store.save(session)
        model = FakeModel(["P1", "P2", "P3", "P4", "MERGED"])

        result = self.make_compactor(
            model,
            max_messages=1,
            keep_recent=2,
            chunk_characters=3_000,
        ).compact(session)

        self.assertEqual(result.chunks, 4)
        self.assertEqual(result.summary, "MERGED")
        self.assertIn("原始单条消息过长，分片 1/4", model.calls[0][1]["content"])
        self.assertEqual([item["content"] for item in self.store.current.messages], ["recent answer", "recent question"])

    def test_manual_compaction_accepts_one_oversized_message(self):
        session = self.store.current
        session.messages = [{"role": "user", "content": "A" * 9_000}]
        self.store.save(session)
        model = FakeModel(["P1", "P2", "P3", "P4", "MERGED"])
        compactor = self.make_compactor(
            model,
            max_messages=20,
            max_characters=3_000,
            keep_recent=8,
            chunk_characters=3_000,
        )

        estimate = compactor.estimate(session, force=True)
        result = compactor.compact(session, force=True)

        self.assertEqual(estimate["oldMessages"], 1)
        self.assertEqual(estimate["recentMessages"], 0)
        self.assertEqual(estimate["estimatedChunks"], 4)
        self.assertEqual(result.summary, "MERGED")
        self.assertEqual(result.old_messages, 1)
        self.assertEqual(result.recent_messages, 0)
        self.assertEqual(self.store.current.messages, [])

    def test_auto_compaction_keeps_latest_answer_after_oversized_user_input(self):
        session = self.store.current
        session.messages = [
            {"role": "user", "content": "A" * 9_000},
            {"role": "assistant", "content": "latest answer"},
        ]
        self.store.save(session)
        model = FakeModel(["P1", "P2", "P3", "P4", "MERGED"])
        compactor = self.make_compactor(
            model,
            max_messages=20,
            max_characters=3_000,
            keep_recent=8,
            chunk_characters=3_000,
        )

        result = compactor.compact(session)

        self.assertEqual(result.summary, "MERGED")
        self.assertEqual(result.old_messages, 1)
        self.assertEqual(result.recent_messages, 1)
        self.assertEqual(
            [item["content"] for item in self.store.current.messages],
            ["latest answer"],
        )

    def test_model_failure_preserves_all_messages_and_summary(self):
        session = self.seed_messages()
        session.summary = "SAFE-SUMMARY"
        self.store.save(session)
        original = session.to_dict()
        compactor = self.make_compactor(FakeModel(error=RuntimeError("offline")))

        with self.assertRaisesRegex(CompactionError, "原消息已保留"):
            compactor.compact(session)
        self.assertEqual(self.store.current.to_dict(), original)

    def test_empty_summary_preserves_history(self):
        session = self.seed_messages()
        with self.assertRaisesRegex(CompactionError, "摘要结果为空"):
            self.make_compactor(FakeModel(["  "])).compact(session)
        self.assertEqual(len(self.store.current.messages), 12)

    def test_save_failure_restores_in_memory_and_disk_history(self):
        session = self.seed_messages()
        original = session.to_dict()
        compactor = self.make_compactor(FakeModel(["summary"]))
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(CompactionError, "保存失败"):
                compactor.compact(session)
        self.assertEqual(session.to_dict(), original)
        self.assertEqual(self.store.current.to_dict(), original)

    def test_context_contains_summary_before_recent_messages(self):
        session = self.seed_messages(2)
        session.summary = "SESSION-SUMMARY"
        context = ContextBuilder("RULE", "SOUL").build(session, "next")
        self.assertIn("SESSION-SUMMARY", context[0]["content"])
        self.assertEqual(context[1]["content"], "m0")

    def test_completed_tool_protocol_does_not_enter_future_context(self):
        session = self.store.current
        session.messages = [
            {"role": "user", "content": "查询课程"},
            {"role": "assistant", "content": '{"type":"tool_call","tool":"web_search","args":{"query":"course"}}'},
            {"role": "user", "content": '[tool_results] [{"tool":"web_search","output":"very large raw result"}]'},
            {"role": "assistant", "content": "课程结论"},
        ]
        context = ContextBuilder().build(session, "继续解释")
        contents = [item["content"] for item in context]
        self.assertIn("查询课程", contents)
        self.assertIn("课程结论", contents)
        self.assertNotIn("very large raw result", str(contents))
        self.assertNotIn('"type":"tool_call"', str(contents))

    def test_unfinished_turn_keeps_tool_observations(self):
        session = self.store.current
        session.messages = [
            {"role": "user", "content": "读取附件"},
            {"role": "assistant", "content": '{"type":"tool_call","tool":"read_attachment","args":{"attachment_id":"a1"}}'},
            {"role": "user", "content": '[tool_results] [{"tool":"read_attachment","output":"document text"}]'},
        ]
        context = ContextBuilder().build(session)
        self.assertIn("document text", context[-1]["content"])
        self.assertIn("read_attachment", context[-2]["content"])

    def test_unfinished_huge_tool_observation_is_truncated_in_context_only(self):
        huge = "X" * 40_000
        session = self.store.current
        session.messages = [
            {"role": "user", "content": "运行普通工具"},
            {"role": "assistant", "content": '{"type":"tool_call","tool":"echo","args":{}}'},
            {
                "role": "user",
                "content": "[tool_results] " + json.dumps(
                    [{"tool": "echo", "success": True, "output": huge}],
                    ensure_ascii=False,
                ),
            },
        ]
        context = ContextBuilder().build(session)
        self.assertLess(len(context[-1]["content"]), 13_000)
        self.assertIn("上下文保护", context[-1]["content"])
        self.assertEqual(session.messages[-1]["content"].count("X"), 40_000)

    def test_active_attachment_chunk_is_preserved_for_progressive_summary(self):
        content = "attachment chunk\n" + ("甲" * 55_000)
        session = self.store.current
        session.messages = [
            {"role": "user", "content": "完整读取长附件"},
            {
                "role": "assistant",
                "content": '{"type":"tool_call","tool":"read_attachment",'
                '"args":{"attachment_id":"a1"}}',
            },
            {
                "role": "user",
                "content": "[tool_results] " + json.dumps(
                    [{
                        "tool": "read_attachment",
                        "success": True,
                        "output": {
                            "content": content,
                            "truncated": True,
                            "nextOffset": 55_017,
                        },
                    }],
                    ensure_ascii=False,
                ),
            },
        ]
        context = ContextBuilder().build(session)
        self.assertIn("甲" * 55_000, context[-1]["content"])
        self.assertNotIn("上下文保护", context[-1]["content"])

    def test_active_read_file_result_keeps_normal_sized_file_complete(self):
        content = "context_builder.py\n" + ("x" * 12_900)
        session = self.store.current
        session.messages = [
            {"role": "user", "content": "\u8bfb\u53d6 context_builder.py"},
            {"role": "assistant", "content": '{"type":"tool_call","tool":"read_file","args":{"path":"context_builder.py"}}'},
            {
                "role": "user",
                "content": "[tool_results] " + json.dumps(
                    [{"tool": "read_file", "success": True, "output": {"content": content, "truncated": False}}],
                    ensure_ascii=False,
                ),
            },
        ]
        context = ContextBuilder().build(session)
        self.assertIn("x" * 12_900, context[-1]["content"])
        self.assertNotIn("\u4e0a\u4e0b\u6587\u4fdd\u62a4", context[-1]["content"])

    def test_tool_chatter_does_not_trigger_compaction_or_consume_recent_slots(self):
        session = self.store.current
        session.messages = [{"role": "user", "content": "问题"}]
        for index in range(30):
            session.messages.extend([
                {"role": "assistant", "content": f'{{"type":"tool_call","tool":"echo","args":{{"n":{index}}}}}'},
                {"role": "user", "content": f'[tool_results] [{{"tool":"echo","output":"{index}"}}]'},
            ])
        session.messages.append({"role": "assistant", "content": "最终正文"})
        compactor = self.make_compactor(FakeModel(["summary"]), max_messages=10)
        self.assertFalse(compactor.should_compact(session))

        # Add enough real dialogue to compact; recent slots must contain prose.
        for index in range(6):
            session.messages.extend([
                {"role": "user", "content": f"正文问题 {index}"},
                {"role": "assistant", "content": f"正文回答 {index}"},
            ])
        result = compactor.compact(session)
        self.assertIsNotNone(result)
        self.assertEqual(
            [item["content"] for item in session.messages],
            ["正文问题 4", "正文回答 4", "正文问题 5", "正文回答 5"],
        )

    def test_pending_tool_tail_never_occupies_compaction_recent_slots(self):
        session = self.store.current
        session.messages = [{"role": "user", "content": "读取这些文件"}]
        for index in range(12):
            session.messages.extend([
                {
                    "role": "assistant",
                    "content": f'{{"type":"tool_call","tool":"read_file","args":{{"path":"f{index}.txt"}}}}',
                },
                {
                    "role": "user",
                    "content": f'[tool_results] [{{"tool":"read_file","output":"内容 {index}"}}]',
                },
            ])
        self.store.save(session)

        compactor = self.make_compactor(FakeModel(["summary"]), max_messages=1, keep_recent=1)

        # The active tool chain is retained for a future model call, but it is
        # not a visible dialogue message and therefore cannot trigger/consume
        # compaction slots.
        self.assertEqual(compactor.estimate(session)["semanticMessages"], 1)
        self.assertFalse(compactor.should_compact(session))
        self.assertIsNone(compactor.compact(session, force=True))
        self.assertEqual(len(session.messages), 25)

    def test_runtime_automatically_compacts_after_successful_turn(self):
        session = self.seed_messages(10)
        model = FakeModel(["normal reply", "auto summary"])
        compactor = self.make_compactor(model)
        runtime = AgentRuntime(model, self.store, ContextBuilder(), compactor)

        reply = runtime.send("new question")

        self.assertEqual(reply, "normal reply")
        self.assertIsNotNone(runtime.last_compaction)
        self.assertEqual(len(self.store.current.messages), 4)

    def test_auto_compaction_emits_summary_preview_event(self):
        self.seed_messages(10)
        model = FakeModel(["normal reply", "auto summary"])
        runtime = AgentRuntime(model, self.store, ContextBuilder(), self.make_compactor(model))
        events = []

        result = runtime.run("new question", event_callback=events.append)

        compact_events = [item for item in events if item.get("type") == "compaction"]
        started_events = [item for item in events if item.get("type") == "compaction_started"]
        self.assertIsNotNone(result.compaction)
        self.assertEqual(len(started_events), 1)
        self.assertEqual(len(compact_events), 1)
        # The final answer must be visible before the post-turn compaction
        # card.  Compaction is bookkeeping for the next turn, not part of
        # the current answer's generation.
        self.assertLess(
            [item["type"] for item in events].index("assistant_final"),
            [item["type"] for item in events].index("compaction_started"),
        )
        self.assertLess(
            [item["type"] for item in events].index("compaction_started"),
            [item["type"] for item in events].index("compaction"),
        )
        self.assertEqual(compact_events[0]["summaryPreview"], "auto summary")

    def test_manual_compact_command_does_not_enter_conversation(self):
        self.seed_messages(12)
        model = FakeModel(["manual summary"])
        runtime = AgentRuntime(
            model,
            self.store,
            ContextBuilder(),
            self.make_compactor(model),
        )
        outputs = []
        inputs = iter(["/compact", "/exit"])

        result = run_cli(runtime, input_fn=lambda _prompt: next(inputs), output_fn=outputs.append)

        self.assertEqual(result, 0)
        self.assertEqual(len(model.calls), 1)
        self.assertIn(
            "[system] 正在整理上下文并生成 Summary，请稍候…",
            outputs,
        )
        self.assertIn("[system] compact session default", "\n".join(outputs))
        self.assertEqual(self.store.current.summary, "manual summary")
        self.assertNotIn("/compact", json.dumps(self.store.current.messages, ensure_ascii=False))

    def test_forced_compaction_allows_short_but_meaningful_history(self):
        session = self.seed_messages(6)
        model = FakeModel(["manual short-history summary"])
        compactor = self.make_compactor(model)

        result = compactor.compact(session, force=True)

        self.assertIsNotNone(result)
        self.assertEqual(result.old_messages, 3)
        self.assertEqual(result.recent_messages, 3)
        self.assertEqual(session.summary, "manual short-history summary")

    def test_runtime_serializes_concurrent_turns_for_same_session(self):
        runtime = AgentRuntime(SlowEchoModel(), self.store, ContextBuilder())
        errors = []

        def send(message):
            try:
                runtime.run(message, "default")
            except Exception as exc:
                errors.append(exc)

        threads = [Thread(target=send, args=(f"m{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        messages = self.store.get("default").messages
        self.assertEqual(len(messages), 4)
        self.assertEqual([item["role"] for item in messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(messages[1]["content"], f"reply:{messages[0]['content']}")
        self.assertEqual(messages[3]["content"], f"reply:{messages[2]['content']}")
        self.assertEqual({messages[0]["content"], messages[2]["content"]}, {"m0", "m1"})

    def test_old_session_without_summary_is_backward_compatible(self):
        path = self.store.sessions_dir / "default.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("summary")
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.store.get("default").summary, "")


if __name__ == "__main__":
    unittest.main()
