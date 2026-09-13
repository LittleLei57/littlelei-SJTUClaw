"""Stable Context 与 Memory 测试。"""

from pathlib import Path
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from context_builder import ContextBuilder
from main import handle_memory_command, run_cli
from memory_store import MemoryStore
from runtime import AgentRuntime
from session_store import SessionStore


class FakeModel:
    def __init__(self):
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return "ok"


class StableContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.prompt_file = root / "system.md"
        self.soul_file = root / "soul.md"
        self.prompt_file.write_text("SYSTEM-RULE", encoding="utf-8")
        self.soul_file.write_text("SOUL-STYLE", encoding="utf-8")
        self.memory_store = MemoryStore(root / "data")
        self.session_store = SessionStore(root / "data")
        self.builder = ContextBuilder.from_files(
            self.memory_store, self.prompt_file, self.soul_file
        )
        self.model = FakeModel()
        self.runtime = AgentRuntime(self.model, self.session_store, self.builder)

    def tearDown(self):
        self.temp.cleanup()

    def test_stable_context_precedes_conversation(self):
        memory = self.memory_store.add("用户偏好中文回答")
        self.runtime.send("hello")
        messages = self.model.calls[0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("SYSTEM-RULE", messages[0]["content"])
        self.assertIn("SOUL-STYLE", messages[0]["content"])
        self.assertIn(memory.memory_id, messages[0]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "hello"})

    def test_memory_is_visible_across_sessions_and_restart(self):
        self.memory_store.add("用户正在实现 SJTUClaw")
        self.session_store.create("新会话")
        reloaded_memory = MemoryStore(self.memory_store.path.parent)
        reloaded_builder = ContextBuilder.from_files(
            reloaded_memory, self.prompt_file, self.soul_file
        )
        context = reloaded_builder.build(self.session_store.current, "我在做什么？")
        self.assertIn("用户正在实现 SJTUClaw", context[0]["content"])

    def test_memory_commands_do_not_reach_model(self):
        inputs = iter(["/memory add 长期项目背景", "/memory list", "/exit"])
        outputs = []
        run_cli(
            self.runtime,
            self.memory_store,
            input_fn=lambda _: next(inputs),
            output_fn=outputs.append,
        )
        self.assertEqual(self.model.calls, [])
        self.assertEqual(len(self.memory_store.list()), 1)

    def test_regular_chat_does_not_directly_modify_memory(self):
        self.runtime.send("请记住我喜欢蓝色")
        self.assertEqual(self.memory_store.list(), [])
        self.assertEqual(len(self.session_store.current.messages), 2)

    def test_cli_startup_hint_includes_compact(self):
        outputs = []
        run_cli(
            self.runtime,
            self.memory_store,
            input_fn=lambda _: "/exit",
            output_fn=outputs.append,
        )
        self.assertTrue(any("/compact" in line for line in outputs))

    def test_add_list_delete(self):
        result = handle_memory_command(self.memory_store, "/memory add 用户偏好简洁回答")
        memory_id = result.rsplit("：", 1)[1]
        self.assertIn("用户偏好简洁回答", handle_memory_command(self.memory_store, "/memory list"))
        handle_memory_command(self.memory_store, f"/memory delete {memory_id}")
        self.assertEqual(self.memory_store.list(), [])

    def test_memory_ids_start_at_one_and_increment(self):
        first = self.memory_store.add("第一条记忆")
        second = self.memory_store.add("第二条记忆")
        self.assertEqual(first.memory_id, "mem_1")
        self.assertEqual(second.memory_id, "mem_2")

    def test_concurrent_memory_adds_do_not_overwrite_each_other(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            memories = list(executor.map(
                lambda index: self.memory_store.add(f"并发记忆 {index}"), range(20)
            ))
        self.assertEqual(len(memories), 20)
        self.assertEqual(len(self.memory_store.list()), 20)
        self.assertEqual(len({item.memory_id for item in memories}), 20)

    def test_memory_list_time_is_beijing_time(self):
        self.memory_store.add("显示北京时间")
        output = handle_memory_command(self.memory_store, "/memory list")
        self.assertIn("北京时间", output)

    def test_old_memory_format_remains_compatible(self):
        legacy_root = Path(self.temp.name) / "legacy-memory"
        legacy_root.mkdir()
        legacy_path = legacy_root / "memories.json"
        legacy_path.write_text(json.dumps([{
            "memoryId": "mem_old", "content": "旧格式记忆", "createdAt": "2026-01-01T00:00:00+00:00"
        }]), encoding="utf-8")
        migrated = MemoryStore(legacy_root)
        memory = migrated.list()[0]
        self.assertEqual(memory.memory_type, "fact")
        self.assertEqual(memory.importance, 3)
        self.assertFalse(legacy_path.exists())
        self.assertTrue((legacy_root / "memories.json.legacy.bak").exists())
        self.assertTrue((legacy_root / "state.sqlite3").exists())

    def test_retrieval_prefers_relevant_and_pinned_memory(self):
        course = self.memory_store.add("数据库课程学习事务与索引", memory_type="course")
        preference = self.memory_store.add("用户喜欢中文简洁回答", memory_type="preference")
        self.memory_store.add("周末天气不错", memory_type="fact")
        results = self.memory_store.search("数据库索引怎么复习", limit=2)
        self.assertEqual(results[0].memory_id, course.memory_id)
        self.assertIn(preference.memory_id, [item.memory_id for item in results])

    def test_expired_memory_is_forgotten_during_retrieval(self):
        expired = self.memory_store.add(
            "一次性验证码 123456", expires_at="2020-01-01T00:00:00+00:00"
        )
        self.assertNotIn(expired.memory_id, [item.memory_id for item in self.memory_store.search("验证码")])
        self.assertIn(expired.memory_id, [item.memory_id for item in self.memory_store.list()])

    def test_update_and_natural_language_delete(self):
        memory = self.memory_store.add("用户喜欢吃苹果", memory_type="preference")
        updated = self.memory_store.update(memory.memory_id, content="用户不再喜欢吃苹果")
        self.assertEqual(updated.content, "用户不再喜欢吃苹果")
        deleted = self.memory_store.delete_by_text("请删除用户不再喜欢吃苹果这条记忆")
        self.assertEqual(deleted.memory_id, memory.memory_id)

    def test_scheduler_failure_notice_is_not_sent_to_model(self):
        session = self.session_store.current
        session.messages.append({
            "role": "user",
            "content": "[scheduler_task_failed taskId=task_1]\n任务：提醒开会\n错误：429",
        })
        self.session_store.save(session)
        context = self.builder.build(session, "定时任务支持吗？")
        self.assertNotIn("scheduler_task_failed", str(context))
        self.assertEqual(context[-1]["content"], "定时任务支持吗？")

    def test_missing_prompt_file_is_clear_error(self):
        with self.assertRaisesRegex(OSError, "System Prompt"):
            ContextBuilder.from_files(
                self.memory_store, Path(self.temp.name) / "missing.md", self.soul_file
            )


if __name__ == "__main__":
    unittest.main()
