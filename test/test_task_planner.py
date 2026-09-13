"""验证复杂任务 Planner 的步骤、证据和状态更新。"""

import unittest

from goal_state import new_goal
from task_planner import (
    ensure_plan,
    finalize_plan,
    mark_tool,
    normalize_plan,
    should_create_plan,
)


class TaskPlannerTests(unittest.TestCase):
    def test_simple_question_stays_without_visible_plan(self):
        self.assertFalse(should_create_plan("你好", set()))
        goal = new_goal("解释一下什么是事务", source="web")
        goal, changed = ensure_plan(goal, "解释一下什么是事务", set())
        self.assertFalse(changed)
        self.assertIsNone(goal.get("plan"))

    def test_simple_image_question_does_not_get_plan_from_transport_boilerplate(self):
        message = (
            "这张图画了什么？\n\n[attached_files] "
            '[{"attachmentId":"att_1","filename":"image.png","contentType":"image/png"}]\n'
            + "必须先调用工具读取附件。" * 20
        )
        self.assertFalse(should_create_plan(message, {"read"}))

    def test_conceptual_multi_part_question_without_tools_has_no_plan(self):
        self.assertFalse(
            should_create_plan("分别解释 TCP 和 UDP，并说明它们有什么区别", set())
        )

    def test_complex_request_creates_plan_execute_and_verify_phases(self):
        goal = new_goal("读取项目，运行测试并修复问题", source="web")
        goal, changed = ensure_plan(
            goal,
            "先读取项目文件，然后运行测试并修复问题",
            {"read", "write", "execute"},
        )
        self.assertTrue(changed)
        plan = goal["plan"]
        self.assertEqual(
            [step["phase"] for step in plan["steps"]],
            ["plan", "execute", "execute", "execute", "verify"],
        )
        self.assertEqual(
            [step.get("capability") for step in plan["steps"][1:-1]],
            ["read", "write", "execute"],
        )
        self.assertTrue(all(step["expectedEvidence"] for step in plan["steps"]))

    def test_tool_call_only_starts_step_success_supplies_evidence(self):
        goal = new_goal("读取并总结文件", source="web")
        goal, _ = ensure_plan(goal, "先读取文件再总结", {"read"})
        goal, changed = mark_tool(
            goal, "read_file", state="call", call_id="call_1"
        )
        self.assertTrue(changed)
        read_step = next(
            step for step in goal["plan"]["steps"]
            if step.get("capability") == "read"
        )
        self.assertEqual(read_step["status"], "in_progress")
        self.assertFalse(read_step["evidence"])

        goal, changed = mark_tool(
            goal, "read_file", state="success", call_id="call_1"
        )
        self.assertTrue(changed)
        read_step = next(
            step for step in goal["plan"]["steps"]
            if step.get("capability") == "read"
        )
        self.assertEqual(read_step["status"], "completed")
        self.assertEqual(read_step["evidence"][-1]["tool"], "read_file")
        self.assertTrue(read_step["evidence"][-1]["success"])

    def test_failure_does_not_mark_step_complete(self):
        goal = new_goal("运行项目测试", source="web")
        goal, _ = ensure_plan(goal, "先运行项目测试，然后检查失败原因", {"execute"})
        goal, _ = mark_tool(
            goal,
            "run_command",
            state="failed",
            call_id="call_2",
            error="exit 1",
        )
        step = next(
            item for item in goal["plan"]["steps"]
            if item.get("capability") == "execute"
        )
        self.assertEqual(step["status"], "failed")
        self.assertFalse(step["evidence"][-1]["success"])

    def test_finalize_marks_verification_from_runtime_decision(self):
        goal = new_goal("读取并总结文件", source="web")
        goal, _ = ensure_plan(goal, "先读取文件再总结", {"read"})
        goal, _ = mark_tool(goal, "read_file", state="success")
        goal = finalize_plan(goal, success=True)
        plan = normalize_plan(goal["plan"])
        self.assertEqual(plan["status"], "completed")
        self.assertEqual(plan["steps"][-1]["status"], "completed")
        self.assertTrue(plan["steps"][-1]["evidence"][-1]["success"])

    def test_finalize_refuses_completion_without_tool_evidence(self):
        goal = new_goal("inspect and fix the project", source="web")
        goal, _ = ensure_plan(
            goal,
            "read project files, run tests, and fix the problems",
            {"read", "write", "execute"},
        )
        goal = finalize_plan(goal, success=True)
        plan = normalize_plan(goal["plan"])
        read_step = next(
            item for item in plan["steps"]
            if item.get("capability") == "read"
        )
        self.assertEqual(plan["status"], "blocked")
        self.assertEqual(read_step["status"], "blocked")
        self.assertEqual(plan["steps"][-1]["status"], "blocked")
        self.assertFalse(plan["steps"][-1]["evidence"][-1]["success"])


if __name__ == "__main__":
    unittest.main()
