"""验证 Goal 状态的规范化、持久化与上下文表示。"""

import json
import tempfile
import unittest
from pathlib import Path

from context_builder import ContextBuilder
from goal_state import context_text, new_goal, normalize_goal, should_track_goal, start_or_continue, update_goal
from session_store import Session, SessionStore


class GoalStateTests(unittest.TestCase):
    def test_detection_skips_greetings_and_tracks_tasks(self):
        self.assertFalse(should_track_goal("你好"))
        self.assertFalse(should_track_goal("好的，继续吧"))
        self.assertTrue(should_track_goal("请检查附件并生成一份总结"))

    def test_simple_image_question_ignores_attachment_transport_text(self):
        message = (
            "这张图里是什么？\n\n[attached_files] "
            '[{"attachmentId":"att_1","filename":"cat.png","contentType":"image/png"}]\n'
            "以上是用户本轮明确选中的附件，必须先使用工具读取。"
        )
        self.assertFalse(should_track_goal(message))

    def test_state_is_bounded_and_round_trips(self):
        goal = new_goal("请整理项目文件并逐项验收", source="cli", turn_id="turn_1")
        goal = update_goal(
            goal,
            status="active",
            step="正在检查文件",
            completed="已读取 README",
            next_actions=["读取 ROADMAP", "更新报告"],
            turn_id="turn_1",
        )
        restored = normalize_goal(json.loads(json.dumps(goal, ensure_ascii=False)))
        self.assertEqual(restored["status"], "active")
        self.assertIn("已读取 README", restored["completedSteps"])
        self.assertIn("验收条件", context_text(restored))

    def test_session_migrates_missing_goal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(directory)
            session = store.create("任务")
            session.goal_state = new_goal("检查当前项目", source="web")
            store.save(session)
            loaded = store.get(session.session_id)
            self.assertEqual(loaded.goal_state["objective"], "检查当前项目")

    def test_context_contains_goal_without_extra_model_call(self):
        session = Session("goal_test", "Goal")
        session.goal_state = new_goal("读取附件并总结", source="web")
        messages = ContextBuilder().build(session)
        system = messages[0]["content"]
        self.assertIn("Current Task Goal", system)
        self.assertIn("读取附件并总结", system)

    def test_completed_goal_can_be_replaced_by_new_task(self):
        goal = update_goal(new_goal("检查项目", source="web"), status="completed")
        replacement, created = start_or_continue(goal, "请生成中期报告并打包", source="web", turn_id="turn_2")
        self.assertTrue(created)
        self.assertNotEqual(replacement["goalId"], goal["goalId"])


if __name__ == "__main__":
    unittest.main()
