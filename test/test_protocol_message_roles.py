"""Automatic protocol feedback is internal system context, not user input."""

import tempfile
import unittest

from context_builder import ContextBuilder
from conversation_view import visible_message_count, visible_message_stats
from session_store import Session, SessionStore


class ProtocolMessageRoleTests(unittest.TestCase):
    def test_visible_message_count_excludes_internal_protocol_records(self):
        messages = [
            {"role": "system", "content": "[approval_retry_required] {\"tool\":\"install_skill\"}", "metadata": {"internal": True}},
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": '{"type":"tool_call","tool":"list_dir","args":{}}'},
            {"role": "user", "content": '[tool_results] [{"tool":"list_dir"}]'},
            {"role": "system", "content": "[protocol_error] 请重试"},
            {"role": "assistant", "content": "最终回答"},
        ]
        self.assertEqual(visible_message_count(messages), 2)

    def test_visible_message_stats_explains_unpaired_channel_turn(self):
        messages = [
            {"role": "user", "content": "第一条"},
            {"role": "assistant", "content": "已完成"},
            {"role": "user", "content": "QQ 中途断开的请求", "metadata": {"source": "qqbot"}},
        ]
        self.assertEqual(visible_message_stats(messages), {
            "messageCount": 3,
            "userMessageCount": 2,
            "assistantMessageCount": 1,
            "completedTurnCount": 1,
            "pendingUserCount": 1,
        })

    def test_legacy_protocol_error_is_migrated_on_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(tmp)
            session = Session(
                "legacy", "legacy",
                messages=[
                    {"role": "user", "content": "真实提问"},
                    {"role": "user", "content": "[protocol_error] 请重试"},
                ],
            )
            store.save(session)
            restored = store.get("legacy")
            self.assertEqual(restored.messages[0]["role"], "user")
            self.assertEqual(restored.messages[1]["role"], "system")
            self.assertTrue(restored.messages[1]["metadata"]["internal"])

    def test_protocol_feedback_stays_out_of_provider_context(self):
        session = Session(
            "active", "active",
            messages=[
                {"role": "user", "content": "列出项目文件"},
                {"role": "assistant", "content": "[deferred_action_promise] 我去看看。"},
                {"role": "system", "content": "[protocol_error] 请直接调用 list_dir。", "metadata": {"internal": True}},
            ],
        )

        messages = ContextBuilder().build(session)

        self.assertEqual(messages[0]["role"], "system")
        self.assertNotIn("[protocol_error]", messages[0]["content"])
        self.assertEqual([item["role"] for item in messages], ["system", "user", "assistant"])

    def test_relaxed_tool_retry_hint_never_becomes_a_mid_conversation_system_message(self):
        session = Session(
            "active", "active",
            messages=[
                {"role": "user", "content": "看一下附件"},
                {
                    "role": "assistant",
                    "content": "[deferred_action_promise] 我去看看。",
                },
                {
                    "role": "system",
                    "content": "[tool_retry_hint] Execute the tool now.",
                    "metadata": {"internal": True, "kind": "tool_retry_hint"},
                },
            ],
        )

        messages = ContextBuilder().build(
            session,
            runtime_hint="请直接调用读取工具。",
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Runtime Repair Hint", messages[0]["content"])
        self.assertNotIn("[tool_retry_hint]", messages[0]["content"])
        self.assertNotIn("system", [item["role"] for item in messages[1:]])


if __name__ == "__main__":
    unittest.main()
