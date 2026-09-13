"""Skill Registry、显式/自动选择和 Usage 测试。"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from advanced_tools import AdvancedTools, register_advanced_tools
from approval_store import ApprovalStore
from context_builder import ContextBuilder
from download_store import DownloadStore
from gateway import create_app
from main import handle_skill_info_command
from runtime import AgentRuntime
from session_store import SessionStore
from shell_manager import ShellManager
from skill_system import SkillRegistry, SkillService, register_skill_tool
from tools import ToolExecutionContext, ToolRegistry, create_read_only_registry
from workspace import WorkspaceManager


PROJECT_SKILLS = Path(__file__).parents[1] / "skills"


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return next(self.replies)


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = SessionStore(self.root / "data")
        self.skill_registry = SkillRegistry(PROJECT_SKILLS)
        self.skill_service = SkillService(self.skill_registry, self.sessions)
        self.approvals = ApprovalStore(self.root / "data")

    def tearDown(self):
        self.temp.cleanup()

    def make_runtime(self, replies):
        registry = ToolRegistry()
        register_skill_tool(registry, self.skill_service)
        model = ScriptedModel(replies)
        runtime = AgentRuntime(
            model,
            self.sessions,
            ContextBuilder(
                tool_definitions=registry.definitions(),
                skill_index=self.skill_registry.index(),
            ),
            tool_registry=registry,
            approval_store=self.approvals,
            skill_service=self.skill_service,
        )
        runtime.skill_registry = self.skill_registry
        return runtime, model

    def test_registry_scans_project_skills_and_discloses_resource_manifest(self):
        names = {item.name for item in self.skill_registry.list()}
        # The five course skills are the Step 9 baseline.  Additional
        # community skills (for example agent-browser) may be installed in
        # the project and should not make the baseline test brittle.
        self.assertTrue(
            {"course-report", "document-reader", "material-summary", "pdf-reader", "presentation-outline"}
            <= names
        )
        index = str(self.skill_registry.index())
        self.assertNotIn("## Workflow", index)
        full = self.skill_registry.load("course-report")
        self.assertIn("## SKILL.md", full)
        self.assertIn("assets/report-template.md", full)
        self.assertIn("references/checklist.md", full)
        self.assertNotIn("## Resource: assets/report-template.md", full)

    def test_active_skill_resource_is_read_progressively_and_persisted(self):
        registry = ToolRegistry()
        register_skill_tool(registry, self.skill_service)
        self.skill_service.activate(
            "default", "course-report", "生成报告", "explicit"
        )
        result = registry.execute(
            "read_skill_resource",
            {
                "path": "references/checklist.md",
                "offset": 0,
                "max_chars": 1000,
            },
            context=ToolExecutionContext("default"),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.output["offset"], 0)
        self.assertIn("content", result.output)
        active = self.sessions.current.active_skill
        self.assertEqual(
            active["resourceReads"][0]["path"],
            "references/checklist.md",
        )

    def test_skill_resource_reader_accepts_redundant_skill_md_but_rejects_escape(self):
        registry = ToolRegistry()
        register_skill_tool(registry, self.skill_service)
        self.skill_service.activate(
            "default", "course-report", "生成报告", "explicit"
        )
        skill_md = registry.execute(
            "read_skill_resource",
            {"path": "SKILL.md"},
            context=ToolExecutionContext("default"),
        )
        self.assertTrue(skill_md.success)
        self.assertTrue(skill_md.output["alreadyInContext"])
        self.assertIn("course-report", skill_md.output["content"])
        for path in ("../README.md", "README.md"):
            result = registry.execute(
                "read_skill_resource",
                {"path": path},
                context=ToolExecutionContext("default"),
            )
            self.assertFalse(result.success, path)

    def test_skill_tools_are_exposed_only_in_the_valid_session_phase(self):
        registry = ToolRegistry()
        register_skill_tool(registry, self.skill_service)
        builder = ContextBuilder(
            tool_definitions=registry.definitions(),
            skill_index=self.skill_registry.index(),
        )
        before = builder.build(self.sessions.current, "course-report 怎么用？")[0]["content"]
        self.assertIn('"name": "use_skill"', before)
        self.assertNotIn('"name": "read_skill_resource"', before)
        self.assertNotIn('"name": "run_skill_script"', before)
        self.assertIn("都已经安装并注册", before)

        self.skill_service.activate(
            "default", "course-report", "生成报告", "explicit"
        )
        after = builder.build(self.sessions.current)[0]["content"]
        self.assertNotIn('"name": "use_skill"', after)
        self.assertIn('"name": "read_skill_resource"', after)
        self.assertIn('"name": "run_skill_script"', after)

    def test_native_tools_hide_install_for_a_named_registered_skill(self):
        runtime, model = self.make_runtime([])
        model.supports_native_tools = True
        kwargs = runtime._native_model_kwargs(
            self.sessions.current,
            "course-report 这个 skill 怎么用？",
            [],
        )
        names = {
            item["function"]["name"]
            for item in kwargs["tools"]
        }
        self.assertIn("use_skill", names)
        self.assertNotIn("install_skill", names)
        self.assertNotIn("read_skill_resource", names)

    def test_long_skill_keeps_running_after_progressive_resource_read(self):
        runtime, model = self.make_runtime([
            (
                '{"type":"tool_call","tool":"read_skill_resource","args":'
                '{"path":"references/checklist.md","offset":0,"max_chars":1000}}'
            ),
            '{"type":"final","content":"已根据清单给出报告写作建议"}',
        ])
        result = runtime.run_skill(
            "course-report", "读取检查清单后给出报告写作建议", "default"
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.reply, "已根据清单给出报告写作建议")
        self.assertEqual(len(model.calls), 2)
        second_context = str(model.calls[1])
        self.assertIn("references/checklist.md", second_context)
        self.assertIn("read_skill_resource", second_context)
        self.assertEqual(
            self.sessions.current.skill_usage[0]["status"],
            "completed",
        )

    def test_active_skill_script_runner_is_bounded_and_shell_free(self):
        skill_dir = self.root / "script-skills" / "script-test"
        (skill_dir / "scripts").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: script-test\n"
            "description: Run a test validator.\n"
            "runtime: tool-assisted\n"
            "---\n",
            encoding="utf-8",
        )
        (skill_dir / "scripts" / "echo.py").write_text(
            "import sys\nprint('value=' + sys.argv[1])\n",
            encoding="utf-8",
        )
        service = SkillService(SkillRegistry(skill_dir.parent), self.sessions)
        service.activate("default", "script-test", "运行验证器", "explicit")
        registry = ToolRegistry()
        register_skill_tool(registry, service)
        result = registry.execute(
            "run_skill_script",
            {"path": "scripts/echo.py", "args": ["safe-value"]},
            context=ToolExecutionContext("default"),
        )
        self.assertTrue(result.success)
        self.assertIn("value=safe-value", result.output["stdout"])
        rejected = registry.execute(
            "run_skill_script",
            {"path": "scripts/echo.py", "args": [str(self.root.resolve())]},
            context=ToolExecutionContext("default"),
        )
        self.assertFalse(rejected.success)

    def test_skill_doctor_reports_ready_unverified_and_missing_dependencies(self):
        root = self.root / "doctor-skills"
        for name, extra in (
            ("ready", "runtime: prompt-only\n"),
            ("unknown", ""),
            (
                "missing",
                "runtime: external\n"
                "requires-commands: command-that-does-not-exist-sjtuclaw\n"
                "requires-env: SJTUCLAW_TEST_MISSING_ENV\n",
            ),
        ):
            directory = root / name
            directory.mkdir(parents=True)
            (directory / "SKILL.md").write_text(
                "---\n"
                f"name: {name}\n"
                f"description: {name} test\n"
                f"{extra}"
                "---\n\n"
                "# Test\n",
                encoding="utf-8",
            )
        registry = SkillRegistry(root)
        self.assertEqual(registry.diagnose("ready")["status"], "ready")
        self.assertEqual(registry.diagnose("unknown")["status"], "unverified")
        with patch.dict("os.environ", {}, clear=False):
            health = registry.diagnose("missing")
        self.assertEqual(health["status"], "unavailable")
        self.assertTrue(any(
            item["key"] == "command:command-that-does-not-exist-sjtuclaw"
            and item["status"] == "failed"
            for item in health["checks"]
        ))

    def test_unselected_skill_body_is_not_in_normal_context(self):
        runtime, model = self.make_runtime(['{"type":"final","content":"普通回答"}'])
        runtime.run("普通问题")
        system = model.calls[0][0]["content"]
        self.assertIn("Available Skills", system)
        self.assertIn("course-report", system)
        self.assertNotIn("Produce an evidence-aware course report", system)

    def test_explicit_skill_loads_without_skill_approval_and_records_usage(self):
        runtime, model = self.make_runtime(['{"type":"final","content":"报告草稿"}'])
        result = runtime.run_skill("course-report", "写课程报告", "default")
        self.assertEqual(result.reply, "报告草稿")
        self.assertIn("Produce an evidence-aware course report", model.calls[0][0]["content"])
        usage = self.sessions.current.skill_usage[0]
        self.assertEqual(usage["source"], "explicit")
        self.assertEqual(usage["status"], "completed")
        self.assertEqual(usage["finalOutput"], "报告草稿")
        self.assertIsNone(self.sessions.current.active_skill)

    def test_auto_selection_requires_approval_then_loads_skill(self):
        runtime, model = self.make_runtime([
            '{"type":"tool_call","tool":"use_skill","args":{"name":"material-summary","reason":"用户要求汇总材料"}}',
            '{"type":"final","content":"材料总结完成"}',
        ])
        pending = runtime.run("帮我总结这些课堂材料", "default")
        self.assertEqual(pending.status, "approval_required")
        self.assertEqual(pending.pending_approvals[0]["tool"], "use_skill")
        self.assertEqual(self.sessions.current.skill_usage, [])

        final = runtime.resolve_approval(pending.pending_approvals[0]["approvalId"], True)
        self.assertEqual(final.status, "completed")
        self.assertEqual(final.pending_approvals, [])
        self.assertEqual(final.reply, "材料总结完成")
        self.assertIn("Build a traceable synthesis", model.calls[1][0]["content"])
        usage = self.sessions.current.skill_usage[0]
        self.assertEqual(usage["source"], "auto")
        self.assertEqual(usage["reason"], "用户要求汇总材料")

    def test_course_report_file_save_still_requires_update_approval(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        workspaces = WorkspaceManager(self.sessions)
        workspaces.set("default", str(workspace))
        downloads = DownloadStore(self.root / "data")
        shells = ShellManager(workspaces)
        advanced = AdvancedTools(self.sessions, workspaces, shells, downloads)
        registry = create_read_only_registry(advanced)
        register_advanced_tools(registry, advanced)
        register_skill_tool(registry, self.skill_service)
        model = ScriptedModel([
            '{"type":"tool_call","tool":"create_file","args":{"path":"report.md","content":"# Report"}}',
            '{"type":"final","content":"报告已保存"}',
        ])
        runtime = AgentRuntime(
            model, self.sessions,
            ContextBuilder(tool_definitions=registry.definitions(), skill_index=self.skill_registry.index()),
            tool_registry=registry, approval_store=self.approvals,
            skill_service=self.skill_service,
        )
        pending = runtime.run_skill("course-report", "写报告并保存到 report.md")
        self.assertFalse((workspace / "report.md").exists())
        self.assertEqual(pending.pending_approvals[0]["tool"], "create_file")
        final = runtime.resolve_approval(pending.pending_approvals[0]["approvalId"], True)
        self.assertTrue((workspace / "report.md").exists())
        self.assertIn("报告已保存", final.reply)
        self.assertIn("[⬇ 下载 report.md](/api/downloads/", final.reply)
        self.assertEqual(self.sessions.current.skill_usage[0]["savePath"], "report.md")
        shells.close_all()

    def test_cli_info_commands(self):
        runtime, _ = self.make_runtime([])
        self.assertIn("course-report", handle_skill_info_command(runtime, "/skill list"))
        self.assertIn("resources", handle_skill_info_command(runtime, "/skill show course-report"))
        self.assertIn("暂无记录", handle_skill_info_command(runtime, "/skill usage"))

    def test_gateway_skill_list_run_and_usage(self):
        runtime, _ = self.make_runtime(['{"type":"final","content":"outline done"}'])
        client = TestClient(create_app(runtime, web_dir=Path(__file__).parents[1] / "web"))
        skills = client.get("/api/skills").json()["skills"]
        self.assertGreaterEqual(len(skills), 5)
        self.assertIn("health", skills[0])
        doctor = client.get("/api/skills/doctor")
        self.assertEqual(doctor.status_code, 200)
        self.assertGreaterEqual(len(doctor.json()["skills"]), 5)
        response = client.post(
            "/api/skills/presentation-outline/run",
            json={"sessionId": "default", "task": "生成五分钟展示大纲"},
        )
        self.assertEqual(response.json()["reply"], "outline done")
        usage = client.get("/api/sessions/default/skill-usage").json()["usage"]
        self.assertEqual(usage[0]["skillName"], "presentation-outline")
        client.close()

    def test_stream_chat_can_activate_explicit_skill_for_same_turn(self):
        runtime, model = self.make_runtime([
            '{"type":"final","content":"课程报告的写作方法如下"}'
        ])
        client = TestClient(create_app(runtime, web_dir=Path(__file__).parents[1] / "web"))
        response = client.post(
            "/api/chat/stream",
            json={
                "sessionId": "default",
                # This case verifies same-turn Skill activation, not file
                # creation.  A request to actually generate/save a report is
                # covered separately and must produce write Tool evidence.
                "message": "请说明课程报告的写作方法",
                "turnId": "turn_skill_stream",
                "skillName": "course-report",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: done", response.text)
        self.assertIn("课程报告的写作方法如下", response.text)
        self.assertIn("Produce an evidence-aware course report", model.calls[0][0]["content"])
        usage = client.get("/api/sessions/default/skill-usage").json()["usage"]
        self.assertEqual(usage[0]["skillName"], "course-report")
        self.assertEqual(usage[0]["source"], "explicit")
        self.assertEqual(usage[0]["status"], "completed")
        client.close()


if __name__ == "__main__":
    unittest.main()
