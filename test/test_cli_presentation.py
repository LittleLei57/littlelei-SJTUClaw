"""CLI welcome text and Markdown presentation tests."""

import unittest
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from attachment_store import AttachmentStore
from daily_quotes import DAILY_QUOTES
from main import (
    CliTheme,
    CliStreamRenderer,
    collect_multiline_input,
    format_cli_history,
    format_tool_event,
    format_turn_metrics,
    handle_attachment_command,
    handle_export_command,
    handle_help_command,
    render_terminal_markdown_fallback,
    output_assistant_reply,
    run_cli,
    _read_cli_input,
    _is_shell_environment_injection,
)
from session_store import SessionStore


class CliPresentationTests(unittest.TestCase):

    def test_interactive_prompt_is_flushed_before_blocking_read(self):
        terminal = StringIO()
        with patch("builtins.input", return_value="hello") as builtin_input:
            with patch("main.sys.stdout", terminal):
                value = _read_cli_input(builtin_input, "\nUser> ")
        self.assertEqual(value, "hello")
        self.assertEqual(terminal.getvalue(), "\nUser> ")
        builtin_input.assert_called_once_with("")

    def test_ignores_vscode_conda_activation_injection(self):
        self.assertTrue(_is_shell_environment_injection(
            r"C:\Users\demo\miniconda3\Scripts\conda.EXE activate base"
        ))
        self.assertTrue(_is_shell_environment_injection("conda activate agent"))
        self.assertFalse(_is_shell_environment_injection("请运行 conda activate agent"))

    def test_tool_events_are_concise_by_default_and_verbose_on_request(self):
        event = {
            "tool": "weather_forecast",
            "args": {"location": "上海", "days": 2},
            "result": {"success": True, "output": {"location": {"name": "上海"}, "daily": [{"date": "2026-07-14", "weather_text": "小雨", "temperature_2m_min": 27, "temperature_2m_max": 35}]}},
        }
        concise = format_tool_event(event)
        self.assertIn("上海 2026-07-14", concise)
        self.assertNotIn('"daily"', concise)
        verbose = format_tool_event(event, verbose=True)
        self.assertIn('"daily"', verbose)
        self.assertIn("参数：", verbose)
        self.assertIn("结果：", verbose)

    def test_calculator_tool_event_shows_expression_and_result(self):
        event = {
            "tool": "calculate",
            "args": {"expression": "hypot(3, 4)"},
            "result": {
                "success": True,
                "output": {
                    "expression": "hypot(3, 4)",
                    "result": 5.0,
                    "formatted": "5",
                },
            },
        }
        concise = format_tool_event(event)
        self.assertIn("hypot(3, 4) = 5", concise)
        self.assertNotIn('"formatted"', concise)

    def test_cli_welcome_blocks_are_separated_by_blank_lines(self):
        class FakeRuntime:
            store = SimpleNamespace(current_id="default")

        outputs = []
        run_cli(
            FakeRuntime(), memory_store=object(),
            input_fn=lambda _prompt: "/exit", output_fn=outputs.append,
        )
        self.assertGreaterEqual(len(outputs), 6)
        self.assertEqual(outputs[1], "")
        self.assertEqual(outputs[3], "")
        self.assertEqual(outputs[5], "")
    def test_cli_welcome_uses_shared_daily_quote(self):
        class FakeRuntime:
            store = SimpleNamespace(current_id="default")

        outputs = []
        run_cli(
            FakeRuntime(), memory_store=object(),
            input_fn=lambda _prompt: "/exit", output_fn=outputs.append,
        )
        self.assertTrue(any(item == f"🦞 {quote}" for quote in DAILY_QUOTES for item in outputs))

    def test_help_explains_commands_and_supports_focused_topics(self):
        full = handle_help_command("/help")
        self.assertIn("会话与回答", full)
        self.assertIn("/history [条数]", full)
        self.assertIn("查看当前会话最近的可见消息", full)
        self.assertIn("Slash 命令只在本地执行", full)
        focused = handle_help_command("/help session")
        self.assertIn("/session switch", focused)
        with self.assertRaisesRegex(ValueError, "未知帮助分类"):
            handle_help_command("/help mystery")

    def test_multiline_input_preserves_line_breaks(self):
        inputs = iter(["第一行", "", "第三行", "."])
        outputs = []
        value = collect_multiline_input(lambda _prompt: next(inputs), outputs.append)
        self.assertEqual(value, "第一行\n\n第三行")
        self.assertTrue(any("单独输入一行 ." in item for item in outputs))

    def test_history_hides_internal_records_and_limits_output(self):
        session = SimpleNamespace(messages=[
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "system", "content": "内部协议", "metadata": {"internal": True}},
            {"role": "user", "content": "[tool_results] hidden", "metadata": {"internal": True}},
            {"role": "user", "content": "第二问\n继续"},
        ])
        rendered = format_cli_history(session, 2)
        self.assertIn("第一答", rendered)
        self.assertIn("第二问 继续", rendered)
        self.assertNotIn("内部协议", rendered)
        self.assertNotIn("tool_results", rendered)

    def test_unknown_slash_command_is_not_sent_to_runtime(self):
        class FakeRuntime:
            store = SimpleNamespace(current_id="default")

            def run(self, *_args, **_kwargs):
                raise AssertionError("unknown command must not reach Runtime")

        inputs = iter(["/sesion list", "/exit"])
        outputs = []
        run_cli(
            FakeRuntime(),
            memory_store=object(),
            input_fn=lambda _prompt: next(inputs),
            output_fn=outputs.append,
        )
        self.assertTrue(any("未知命令：/sesion" in item for item in outputs))

    def test_injected_output_keeps_markdown_source_for_cli_tests(self):
        outputs = []
        output_assistant_reply("## 标题\n\n- 条目", outputs.append)
        self.assertEqual(outputs, ["Assistant> ## 标题\n\n- 条目"])

    def test_fallback_renderer_formats_headings_and_tables(self):
        rendered = render_terminal_markdown_fallback(
            "## 技能\n\n| 名称 | 说明 |\n| --- | --- |\n| course-report | **课程报告** |"
        )
        self.assertNotIn("##", rendered)
        self.assertNotIn("---", rendered)
        self.assertIn("技能", rendered)
        self.assertIn("course-report", rendered)
        self.assertIn("课程报告", rendered)

    def test_stream_renderer_keeps_tool_cards_and_final_reply_ordered(self):
        outputs = []
        renderer = CliStreamRenderer(outputs.append)
        renderer.on_event({"type": "tool_call", "tool": "current_time", "args": {}})
        renderer.on_event(
            {
                "type": "tool_result",
                "tool": "current_time",
                "args": {},
                "result": {
                    "success": True,
                    "output": {"iso": "2026-07-15T20:00:00+08:00"},
                },
            }
        )
        renderer.on_event({"type": "assistant_delta", "delta": "晚上好"})
        renderer.on_event({"type": "assistant_delta", "delta": "！"})
        renderer.on_event({"type": "assistant_final", "content": "晚上好！"})
        renderer.finish("晚上好！")

        joined = "\n".join(outputs)
        self.assertIn("current_time", joined)
        self.assertIn("2026-07-15T20:00:00+08:00", joined)
        self.assertIn("Assistant> 晚上好！", joined)
        self.assertEqual(joined.count("Assistant> 晚上好！"), 1)

    def test_stream_renderer_uses_one_spaced_activity_group(self):
        outputs = []
        renderer = CliStreamRenderer(outputs.append)
        renderer.on_event({"type": "tool_call", "tool": "new_shell", "args": {"command": "dir"}})
        renderer.on_event({
            "type": "approval_required",
            "tool": "new_shell",
            "approvalId": "approval_demo",
        })
        renderer.flush_for_status()
        renderer.on_event({
            "type": "tool_result",
            "tool": "new_shell",
            "result": {"success": True, "output": {"message": "Shell 已启动"}},
        })
        renderer.finish("完成")

        joined = "\n".join(outputs)
        self.assertIn("╭─ 工具执行", joined)
        self.assertIn("approval_required", joined)
        self.assertIn("╰─", joined)
        self.assertNotIn("[tool_result]", joined)

    def test_live_stream_closes_tool_group_before_answer_and_renders_table(self):
        captured = StringIO()
        with redirect_stdout(captured):
            renderer = CliStreamRenderer(print)
            renderer.on_event({"type": "tool_call", "tool": "web_search", "args": {}})
            renderer.on_event({
                "type": "tool_result",
                "tool": "web_search",
                "result": {"success": True, "output": {"results": []}},
            })
            renderer.on_event({
                "type": "assistant_delta",
                "delta": "结果如下：\n| 项目 | 说明 |\n| --- | --- |\n| A | B |",
            })
            renderer.on_event({"type": "assistant_final", "content": "结果如下"})
            renderer.finish("结果如下")
        text = captured.getvalue()
        self.assertIn("项目", text)
        self.assertIn("A", text)
        self.assertNotIn("| --- |", text)
        self.assertLess(text.find("╰─"), text.find("Assistant>"))

    def test_stream_renderer_does_not_leak_split_bold_markers(self):
        renderer = CliStreamRenderer(print)
        self.assertEqual(renderer._render_live_markdown("**粗"), "")
        self.assertEqual(renderer._render_live_markdown("体**文本"), "粗体文本")
        renderer._flush_markdown_pending()

    def test_live_stream_does_not_overwrite_numbered_lines_on_carriage_return(self):
        captured = StringIO()
        with redirect_stdout(captured):
            renderer = CliStreamRenderer(print)
            renderer.on_event({
                "type": "assistant_delta",
                "delta": "1. 第一项\r\n2. 第二项\r3. 第三项\n",
            })
            renderer.on_event({"type": "assistant_final", "content": "done"})
            renderer.finish("done")
        text = captured.getvalue()
        self.assertIn("1. 第一项", text)
        self.assertIn("2. 第二项", text)
        self.assertIn("3. 第三项", text)

    def test_live_stream_flushes_unmatched_markdown_at_line_boundary(self):
        captured = StringIO()
        with redirect_stdout(captured):
            renderer = CliStreamRenderer(print)
            renderer.on_event({
                "type": "assistant_delta",
                "delta": "1. **未闭合强调\n2. 后续仍可见\n",
            })
            renderer.on_event({"type": "assistant_final", "content": "done"})
            renderer.finish("done")
        text = captured.getvalue()
        self.assertIn("未闭合强调", text)
        self.assertIn("2. 后续仍可见", text)

    def test_stream_renderer_formats_compaction_progress(self):
        outputs = []
        renderer = CliStreamRenderer(outputs.append)
        renderer.on_event(
            {
                "type": "compaction_started",
                "oldMessages": 14,
                "recentMessages": 8,
                "chunks": 2,
            }
        )
        renderer.on_event(
            {
                "type": "compaction",
                "oldMessages": 14,
                "recentMessages": 8,
                "chunks": 2,
                "summaryPreview": "## 当前任务\n\n- 保持 CLI 可恢复",
            }
        )
        joined = "\n".join(outputs)
        self.assertIn("正在整理上下文", joined)
        self.assertIn("上下文整理完成", joined)
        self.assertIn("保持 CLI 可恢复", joined)
        self.assertNotIn("## 当前任务", joined)

    def test_cli_theme_has_ascii_fallback_and_no_color_for_plain_output(self):
        theme = CliTheme(unicode=False, ansi=False)
        rendered = theme.paint("🦞 ✦ ⚙ 🔐 ─ ✓")
        self.assertEqual(rendered, "[SJTUClaw] * [tool] [approval] - OK")

    def test_cli_theme_does_not_add_ansi_to_injected_output(self):
        theme = CliTheme.detect([].append)
        self.assertFalse(theme.ansi)

    def test_attachment_command_lists_uploaded_files(self):
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            attachments = AttachmentStore(store)
            metadata = attachments.save(
                store.current_id,
                "notes.txt",
                "text/plain",
                BytesIO("hello".encode()),
            )
            runtime = SimpleNamespace(store=store, attachment_store=attachments)
            listing = handle_attachment_command(runtime, "/attachment list")
            self.assertIn(metadata["attachmentId"], listing)
            self.assertIn("notes.txt", listing)
            self.assertIn("可用", listing)

    def test_session_export_writes_markdown_and_json(self):
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.current
            session.messages.extend(
                [
                    {"role": "user", "content": "导出这段"},
                    {"role": "assistant", "content": "已完成"},
                    {"role": "user", "content": "[tool_results] internal"},
                ]
            )
            store.save(session)
            runtime = SimpleNamespace(store=store)
            markdown_result = handle_export_command(runtime, "/export markdown")
            markdown_path = Path(root) / "exports" / "session_default.md"
            self.assertTrue(markdown_path.exists())
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("导出这段", markdown)
            self.assertNotIn("[tool_results]", markdown)
            self.assertIn("已导出 Session", markdown_result)
            handle_export_command(runtime, "/export json session.json")
            self.assertTrue((Path(root) / "exports" / "session.json").exists())

    def test_session_export_does_not_duplicate_session_prefix(self):
        with TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.create("测试导出")
            runtime = SimpleNamespace(store=store)
            handle_export_command(runtime, "/export")
            self.assertTrue((Path(root) / "exports" / f"{session.session_id}.md").exists())
            self.assertFalse(
                (Path(root) / "exports" / f"session_{session.session_id}.md").exists()
            )

    def test_turn_metrics_are_compact(self):
        turn = SimpleNamespace(
            metrics=[{"durationMs": 125.4, "inputTokens": 10, "outputTokens": 5}],
            tool_events=[{"tool": "current_time"}],
        )
        rendered = format_turn_metrics(turn)
        self.assertIn("model calls=1", rendered)
        self.assertIn("tools=1", rendered)
        self.assertIn("tokens 10/5", rendered)


if __name__ == "__main__":
    unittest.main()
