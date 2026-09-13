"""Session 持久化与隔离测试。"""

from pathlib import Path
import json
import os
import tempfile
import unittest

from main import handle_session_command, run_cli
from runtime import AgentRuntime
from session_store import SessionStore


class FakeModel:
    def __init__(self):
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return f"reply-{len(self.calls)}"


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name) / "data"
        self.model = FakeModel()
        self.store = SessionStore(self.data_dir)
        self.runtime = AgentRuntime(self.model, self.store)

    def tearDown(self):
        self.temp.cleanup()

    def test_sessions_persist_across_store_restart(self):
        created = self.store.create("数据库作业")
        self.runtime.send("记住关系代数")

        restored = SessionStore(self.data_dir)
        self.assertEqual(restored.current_id, created.session_id)
        self.assertEqual(restored.current.title, "数据库作业")
        self.assertEqual(len(restored.current.messages), 2)

    def test_newer_temp_snapshot_is_recovered_after_interrupted_save(self):
        session = self.store.current
        snapshot = session.to_dict()
        snapshot["messages"] = [{"role": "user", "content": "飞书未丢失的消息"}]
        snapshot["updatedAt"] = "2099-01-01T00:00:00+00:00"
        temp_path = self.data_dir / "sessions" / "default.json.tmp"
        temp_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        target = self.data_dir / "sessions" / "default.json"
        os.utime(temp_path, (target.stat().st_mtime + 2, target.stat().st_mtime + 2))

        restored = SessionStore(self.data_dir)
        self.assertEqual(restored.current.messages[0]["content"], "飞书未丢失的消息")
        self.assertFalse(temp_path.exists())

    def test_session_ids_start_at_one_and_are_not_reused(self):
        first = self.store.create("first")
        second = self.store.create("second")
        self.assertEqual(first.session_id, "session_1")
        self.assertEqual(second.session_id, "session_2")

        self.store.delete(second.session_id)
        restored = SessionStore(self.data_dir)
        third = restored.create("third")
        self.assertEqual(third.session_id, "session_3")

    def test_legacy_random_ids_are_migrated_with_references(self):
        legacy_dir = Path(self.temp.name) / "legacy"
        sessions_dir = legacy_dir / "sessions"
        sessions_dir.mkdir(parents=True)
        legacy = {
            "sessionId": "session_24882251", "title": "legacy", "messages": [],
            "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-01T00:00:00+00:00",
        }
        (sessions_dir / "session_24882251.json").write_text(json.dumps(legacy), encoding="utf-8")
        (legacy_dir / "state.json").write_text(
            json.dumps({"currentSessionId": "session_24882251"}), encoding="utf-8"
        )
        (legacy_dir / "tasks.json").write_text(
            json.dumps([{"sessionId": "session_24882251"}]), encoding="utf-8"
        )
        (legacy_dir / "session-sequence.json").write_text(
            json.dumps({"nextSessionNumber": 24882252}), encoding="utf-8"
        )

        migrated = SessionStore(legacy_dir)
        self.assertEqual(migrated.current_id, "session_1")
        self.assertEqual(migrated.get("session_1").title, "legacy")
        self.assertFalse((sessions_dir / "session_24882251.json").exists())
        self.assertEqual(json.loads((legacy_dir / "tasks.json").read_text())[0]["sessionId"], "session_1")
        self.assertEqual(migrated.create().session_id, "session_2")

    def test_session_histories_are_isolated(self):
        self.runtime.send("default message")
        other = self.store.create("另一个话题")
        self.runtime.send("other message")

        self.assertEqual(len(self.store.get("default").messages), 2)
        self.assertEqual(len(self.store.get(other.session_id).messages), 2)
        self.assertNotIn("default message", str(self.model.calls[-1]))

    def test_internal_command_is_not_sent_to_model(self):
        inputs = iter(["/session new 测试会话", "/session list", "/exit"])
        outputs = []
        result = run_cli(self.runtime, input_fn=lambda _: next(inputs), output_fn=outputs.append)
        self.assertEqual(result, 0)
        self.assertEqual(self.model.calls, [])

    def test_session_list_displays_beijing_time(self):
        session = self.store.current
        session.updated_at = "2026-07-07T15:33:50+00:00"
        self.store.save(session)
        output = handle_session_command(self.runtime, "/session list")
        self.assertIn("updated=2026-07-07 23:33:50 北京时间", output)
        self.assertNotIn("+00:00", output)

    def test_session_show_displays_current_history_without_model_call(self):
        self.runtime.send("查看测试")
        self.model.calls.clear()
        output = handle_session_command(self.runtime, "/session show")
        self.assertIn("Session: default", output)
        self.assertIn("History:", output)
        self.assertIn("user> 查看测试", output)
        self.assertIn("assistant> reply-1", output)
        self.assertEqual(self.model.calls, [])

    def test_session_show_can_target_specific_session(self):
        session = self.store.create("目标会话")
        self.runtime.send("目标内容")
        handle_session_command(self.runtime, "/session switch default")
        output = handle_session_command(self.runtime, f"/session show {session.session_id}")
        self.assertIn(f"Session: {session.session_id}", output)
        self.assertIn("Title: 目标会话", output)
        self.assertIn("目标内容", output)

    def test_rename_switch_and_delete(self):
        session = self.store.create()
        handle_session_command(self.runtime, f"/session rename {session.session_id} 新标题")
        self.assertEqual(self.store.get(session.session_id).title, "新标题")
        handle_session_command(self.runtime, "/session switch default")
        handle_session_command(self.runtime, f"/session delete {session.session_id}")
        with self.assertRaises(KeyError):
            self.store.get(session.session_id)

    def test_corrupt_json_is_reported_and_preserved(self):
        path = self.data_dir / "sessions" / "broken.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "JSON 文件损坏"):
            self.store.list_sessions()
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
