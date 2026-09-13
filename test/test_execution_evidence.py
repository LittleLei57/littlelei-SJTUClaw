"""Execution evidence and goal-aware Tool enforcement tests."""

import json
from pathlib import Path
import tempfile
import unittest

from approval_store import ApprovalStore
from context_builder import ContextBuilder
from execution_evidence import (
    TurnEvidence,
    is_informational_request,
    required_capabilities,
    validate_completion,
)
from goal_state import new_goal
from runtime import AgentRuntime
from session_store import SessionStore
from tools import Tool, ToolRegistry


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return next(self.replies)


class NativeScriptedModel(ScriptedModel):
    supports_native_tools = True


class ExecutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "data")

    def tearDown(self):
        self.temp.cleanup()

    def test_requirements_use_active_goal_for_continue_message(self):
        goal = new_goal("读取 Cramer.cpp，然后编译运行四个程序")
        requirements = required_capabilities("继续吧", goal)
        self.assertEqual(requirements, {"read", "execute"})

    def test_recurring_weather_request_requires_search_and_schedule(self):
        requirements = required_capabilities(
            "以后每天早上8点和晚上18点，查询上海天气并发给我"
        )
        self.assertEqual(requirements, {"search", "schedule"})

    def test_delete_replace_and_cleanup_requests_require_write(self):
        for message in (
            "把报告里的[W数字]都删掉",
            "替换文档中的旧引用标记",
            "清理这个文件里的残留内容",
        ):
            with self.subTest(message=message):
                self.assertIn("write", required_capabilities(message))

    def test_explanatory_questions_do_not_force_external_actions(self):
        messages = (
            "course-report 这个 Skill 怎么用？能干嘛？",
            "解释一下如何创建 Markdown 文件",
            "read_file 和 read_document 有什么区别？",
            "为什么安装 Skill 需要审批？",
        )
        for message in messages:
            with self.subTest(message=message):
                self.assertTrue(is_informational_request(message))
                self.assertEqual(required_capabilities(message), set())

    def test_explicit_imperatives_still_require_real_execution(self):
        cases = {
            "请帮我创建 hello.md 文件": "write",
            "帮我读取这个附件": "read",
            "现在安装这个 Skill": "install",
            "请运行一下项目测试": "execute",
        }
        for message, capability in cases.items():
            with self.subTest(message=message):
                self.assertIn(capability, required_capabilities(message))

    def test_success_claim_requires_matching_tool_evidence(self):
        check = validate_completion(
            "好的，两个源码已经全部看清楚了，现在四个程序也编译成功。",
            {"read", "execute"},
            TurnEvidence.from_events([]),
        )
        self.assertFalse(check.valid)
        self.assertEqual(set(check.missing), {"read", "execute"})

        events = [
            {"tool": "read_file", "result": {"success": True}},
            {"tool": "run_command", "result": {"success": True}},
        ]
        verified = validate_completion(
            "读取和编译均已完成。",
            {"read", "execute"},
            TurnEvidence.from_events(events),
        )
        self.assertTrue(verified.valid)

    def test_claimed_full_file_verification_requires_fresh_read_evidence(self):
        only_edit = TurnEvidence.from_events([
            {"tool": "edit_file", "result": {"success": True}},
        ])
        check = validate_completion(
            "已经替换完成，并重新读取全文确认没有任何残留。",
            {"write"},
            only_edit,
        )
        self.assertFalse(check.valid)
        self.assertEqual(check.missing, ("read",))

        edit_and_read = TurnEvidence.from_events([
            {"tool": "edit_file", "result": {"success": True}},
            {"tool": "read_file", "result": {"success": True}},
        ])
        verified = validate_completion(
            "已经替换完成，并重新读取全文确认没有任何残留。",
            {"write"},
            edit_and_read,
        )
        self.assertTrue(verified.valid)

        read_before_edit = TurnEvidence.from_events([
            {"tool": "read_file", "result": {"success": True}},
            {"tool": "edit_file", "result": {"success": True}},
        ])
        wrong_order = validate_completion(
            "已经替换完成，并重新读取全文确认没有任何残留。",
            {"write"},
            read_before_edit,
        )
        self.assertFalse(wrong_order.valid)
        self.assertEqual(wrong_order.missing, ("read",))

    def test_approval_status_explanation_is_not_execution_claim(self):
        check = validate_completion(
            "当前没有待审批项；create_file 被实际调用时需要审批。",
            set(),
            TurnEvidence.from_events([]),
        )
        self.assertTrue(check.valid)
        self.assertFalse(check.approval_claim)

    def test_tutorial_examples_after_search_are_not_execution_claims(self):
        evidence = TurnEvidence.from_events([
            {"tool": "web_search", "result": {"success": True}},
        ])
        tutorial = (
            "Agentic RL 会调用 API、执行代码，并可把测试通过作为中间奖励。"
            "例如：读取代码 → 执行测试 → 测试通过都给分。"
        )

        check = validate_completion(tutorial, set(), evidence)

        self.assertTrue(check.valid)
        self.assertEqual(check.missing, ())

        real_claim = validate_completion("代码编译成功。", set(), evidence)
        self.assertFalse(real_claim.valid)
        self.assertEqual(real_claim.missing, ("execute",))

    def test_fake_read_completion_is_rejected_then_real_tool_runs(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_file",
            "read",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            lambda path: {"path": path, "content": "real"},
        ))
        model = ScriptedModel([
            json.dumps({"type": "final", "content": "文件已经全部读完了。"}, ensure_ascii=False),
            json.dumps({
                "type": "tool_call",
                "tool": "read_file",
                "args": {"path": "Cramer.cpp"},
            }, ensure_ascii=False),
            json.dumps({"type": "final", "content": "已根据真实读取结果完成检查。"}, ensure_ascii=False),
        ])
        runtime = AgentRuntime(
            model,
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
            strict_tool_protocol=False,
        )

        result = runtime.run("请读取并检查 Cramer.cpp 文件")

        self.assertEqual([item["tool"] for item in result.tool_events], ["read_file"])
        self.assertIn("真实读取结果", result.reply)
        rejected = [
            item for item in self.store.current.activity
            if item.get("type") == "execution_evidence_rejected"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["data"]["missingCapabilities"], ["read"])

    def test_stale_goal_evidence_cannot_prove_a_new_cleanup_turn(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "edit_file",
            "edit",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
            lambda path, old_text, new_text: {"path": path, "replacements": 1},
        ))
        registry.register(Tool(
            "read_file",
            "read",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            lambda path: {"path": path, "content": "clean"},
        ))
        model = ScriptedModel([
            json.dumps({
                "type": "final",
                "content": "全部清理完毕，我重新读取全文确认没有任何残留。",
            }, ensure_ascii=False),
            json.dumps({
                "type": "tool_call",
                "tool": "edit_file",
                "args": {
                    "path": "report.md",
                    "old_text": "[W12]",
                    "new_text": "",
                },
            }, ensure_ascii=False),
            json.dumps({
                "type": "tool_call",
                "tool": "read_file",
                "args": {"path": "report.md"},
            }, ensure_ascii=False),
            json.dumps({
                "type": "final",
                "content": "已根据真实修改结果清理，并重新读取全文确认没有残留。",
            }, ensure_ascii=False),
        ])
        runtime = AgentRuntime(
            model,
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
            strict_tool_protocol=False,
        )
        # Simulate an old successful write stored by a previous turn.  It
        # must not satisfy the new request.
        self.store.current.goal_state = {
            "objective": "旧任务",
            "status": "completed",
            "executionEvidence": {
                "successfulTools": ["edit_file"],
                "failedTools": [],
                "successfulCapabilities": ["write"],
                "failedCapabilities": [],
            },
        }
        self.store.save(self.store.current)

        result = runtime.run("把报告里的[W数字]都删掉")

        self.assertEqual(
            [item["tool"] for item in result.tool_events],
            ["edit_file", "read_file"],
        )
        self.assertIn("真实修改结果", result.reply)
        rejected = [
            item for item in self.store.current.activity
            if item.get("type") == "execution_evidence_rejected"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(
            set(rejected[0]["data"]["missingCapabilities"]),
            {"read", "write"},
        )

    def test_fake_approval_is_rejected_until_real_approval_exists(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "create_file",
            "create",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            lambda path, content: {"path": path},
            safety_level="approval_required",
            side_effect=True,
        ))
        approvals = ApprovalStore(self.root / "data")
        model = ScriptedModel([
            json.dumps({
                "type": "final",
                "content": "我已经发起 create_file 了，请帮我点一下审批。",
            }, ensure_ascii=False),
            json.dumps({
                "type": "tool_call",
                "tool": "create_file",
                "args": {"path": "answer.html", "content": "<h1>answer</h1>"},
            }, ensure_ascii=False),
        ])
        runtime = AgentRuntime(
            model,
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
            approval_store=approvals,
            strict_tool_protocol=False,
        )

        result = runtime.run("请创建 answer.html 文件")

        self.assertEqual(result.status, "approval_required")
        self.assertEqual(len(result.pending_approvals), 1)
        self.assertEqual(result.pending_approvals[0]["tool"], "create_file")

    def test_native_tool_choice_is_required_for_unmet_action(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_file",
            "read",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            lambda path: {"path": path},
        ))
        model = NativeScriptedModel([
            json.dumps({
                "type": "tool_call",
                "tool": "read_file",
                "args": {"path": "a.txt"},
            }),
            json.dumps({"type": "final", "content": "读取完成。"}, ensure_ascii=False),
        ])
        runtime = AgentRuntime(
            model,
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
            strict_tool_protocol=False,
        )

        runtime.run("请读取 a.txt 文件")

        self.assertEqual(model.calls[0]["kwargs"]["tool_choice"], "required")
        self.assertEqual(model.calls[1]["kwargs"]["tool_choice"], "auto")

    def test_native_tool_choice_stays_auto_for_tool_explanation(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_file",
            "read",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            lambda path: {"path": path},
        ))
        model = NativeScriptedModel([
            json.dumps({
                "type": "final",
                "content": "read_file 用于读取 Workspace 内的文本文件。",
            }, ensure_ascii=False),
        ])
        runtime = AgentRuntime(
            model,
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
            strict_tool_protocol=False,
        )

        result = runtime.run("read_file 和 read_document 有什么区别？")

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0]["kwargs"]["tool_choice"], "auto")

    def test_complex_turn_plan_advances_only_from_real_tool_results(self):
        registry = ToolRegistry()
        registry.register(Tool(
            "read_file",
            "read",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            lambda path: {"path": path, "content": "print('ok')"},
        ))
        registry.register(Tool(
            "run_command",
            "run",
            {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
            lambda command: {"command": command, "exitCode": 0},
        ))
        model = ScriptedModel([
            json.dumps({
                "type": "tool_call",
                "tool": "read_file",
                "args": {"path": "app.py"},
            }),
            json.dumps({
                "type": "tool_call",
                "tool": "run_command",
                "args": {"command": "python app.py"},
            }),
            json.dumps({
                "type": "final",
                "content": "源码读取与运行验证均已完成。",
            }, ensure_ascii=False),
        ])
        runtime = AgentRuntime(
            model,
            self.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry,
            strict_tool_protocol=False,
        )

        result = runtime.run("先读取 app.py，然后运行程序并验证结果")

        self.assertEqual(result.status, "completed")
        plan = self.store.current.goal_state["plan"]
        self.assertEqual(plan["status"], "completed")
        by_capability = {
            step.get("capability"): step
            for step in plan["steps"]
            if step.get("capability")
        }
        self.assertEqual(by_capability["read"]["status"], "completed")
        self.assertEqual(by_capability["execute"]["status"], "completed")
        self.assertEqual(
            by_capability["read"]["evidence"][-1]["tool"], "read_file"
        )
        self.assertEqual(
            by_capability["execute"]["evidence"][-1]["tool"], "run_command"
        )


if __name__ == "__main__":
    unittest.main()
