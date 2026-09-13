"""Opt-in real-browser tests for SJTUClaw Web UI.

Run with:
    $env:RUN_E2E="1"
    python -m unittest test.test_web_e2e
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
from threading import Thread
import tempfile
import time
import unittest

import uvicorn

try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import Error as PlaywrightError
except ImportError:  # pragma: no cover - optional development dependency
    sync_playwright = None
    PlaywrightError = RuntimeError

from approval_store import ApprovalStore
from attachment_store import AttachmentStore
from context_builder import ContextBuilder
from runtime import AgentRuntime
from session_store import SessionStore, utc_now
from tools import Tool, ToolRegistry


RUN_E2E = os.getenv("RUN_E2E") == "1"


def _system_browsers() -> list[Path]:
    """Return installed browsers in a deterministic fallback order.

    Playwright's bundled Chromium is intentionally optional in this project.
    On Windows, launching an installed Chrome can also fail with the rather
    opaque ``spawn UNKNOWN`` error (for example when Chrome is managed by an
    existing desktop policy), while Edge remains available.  Keep trying the
    other installed browsers instead of treating the first executable as the
    only option.
    """
    candidates = [
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"),
    ]
    result: list[Path] = []
    for path in candidates:
        if path.is_file() and path not in result:
            result.append(path)
    return result


class BrowserModel:
    def complete(self, _messages):
        return '{"type":"final","content":"测试回复已完成。\\n\\n> 设函数如下：\\n> \\\\[\\n> f(n)=\\\\sum_{d\\\\mid n}g(d)\\n> \\\\]"}'

    def complete_stream(self, _messages):
        yield '{"type":"final","content":"测试'
        yield '回复已完成。\\n\\n> 设函数如下：\\n> \\\\[\\n> f(n)=\\\\sum_{d\\\\mid n}g(d)\\n> \\\\]"}'


@unittest.skipUnless(RUN_E2E and sync_playwright is not None, "设置 RUN_E2E=1 后运行真实浏览器测试")
class WebEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.previous_data_dir = os.environ.get("SJTUCLAW_DATA_DIR")
        # Importing gateway constructs a module-level app, so configure the
        # disposable data directory before importing it. This keeps a direct
        # RUN_E2E=1 test run isolated from real sessions.
        os.environ["SJTUCLAW_DATA_DIR"] = str(cls.root / "data")
        from gateway import create_app

        cls.create_app = create_app
        cls.store = SessionStore(cls.root / "data")
        cls.approvals = ApprovalStore(cls.root / "data")
        registry = ToolRegistry()
        registry.register(Tool(
            "e2e_confirm", "E2E approval tool",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda: {"message": "审批执行成功"}, safety_level="approval_required",
        ))
        runtime = AgentRuntime(
            BrowserModel(), cls.store,
            ContextBuilder(tool_definitions=registry.definitions()),
            tool_registry=registry, approval_store=cls.approvals,
        )
        cls.approvals.create("e2e_batch", "default", "e2e_confirm", {})
        app = cls.create_app(
            runtime,
            AttachmentStore(cls.store),
            Path(__file__).parents[1] / "web",
            start_background_services=False,
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        cls.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=cls.port, log_level="error"
        ))
        cls.server_thread = Thread(target=cls.server.run, daemon=True)
        cls.server_thread.start()
        deadline = time.time() + 10
        while not cls.server.started and time.time() < deadline:
            time.sleep(0.05)
        if not cls.server.started:
            raise RuntimeError("E2E Gateway 启动超时")
        cls.playwright = sync_playwright().start()
        launch_errors: list[str] = []
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:  # optional browser dependency; try system browsers below
            launch_errors.append(f"bundled: {exc}")
            cls.browser = None

        if cls.browser is None:
            for browser_path in _system_browsers():
                try:
                    cls.browser = cls.playwright.chromium.launch(
                        headless=True,
                        executable_path=str(browser_path),
                        args=["--disable-gpu"],
                    )
                    break
                except Exception as exc:  # try the next installed browser
                    launch_errors.append(f"{browser_path}: {exc}")

        if cls.browser is None:
            # E2E is opt-in and requires a launchable local browser.  Report a
            # skip with the actionable reason instead of turning the whole
            # unit-test suite red on machines where Chrome is policy-managed.
            cls.playwright.stop()
            cls.server.should_exit = True
            cls.server_thread.join(timeout=5)
            cls.temp.cleanup()
            raise unittest.SkipTest(
                "没有可启动的 Playwright/系统浏览器；可安装 Chromium，或检查 Chrome/Edge 启动权限。"
            )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.server_thread.join(timeout=5)
        if cls.previous_data_dir is None:
            os.environ.pop("SJTUCLAW_DATA_DIR", None)
        else:
            os.environ["SJTUCLAW_DATA_DIR"] = cls.previous_data_dir
        cls.temp.cleanup()

    def setUp(self):
        # Playwright injects string predicates/eval for a few drag-and-drop
        # assertions. Bypass CSP only inside this disposable test context; the
        # production response headers are verified separately.
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 800}, bypass_csp=True
        )
        self.page = self.context.new_page()
        self.page.goto(f"http://127.0.0.1:{self.port}")
        self.page.wait_for_selector("#session-list .session-row")

    def tearDown(self):
        self.context.close()

    def test_user_message_preserves_composer_line_breaks(self):
        page = self.page
        source = "第一行\n第二行\n\n第四行"
        page.locator("#message-input").fill(source)
        page.locator("#composer").evaluate("(form) => form.requestSubmit()")
        user_message = page.locator("#messages .message.user .message-content").last
        user_message.wait_for(state="visible")
        self.assertEqual(user_message.inner_text(), source)
        self.assertEqual(user_message.locator("br").count(), 1)
        self.assertEqual(user_message.locator("p").count(), 2)

    def test_attachment_and_download_strips_keep_composer_visible(self):
        page = self.page
        page.set_viewport_size({"width": 1280, "height": 720})
        page.evaluate(
            """() => {
                const attachments = document.querySelector('#attachment-strip');
                const downloads = document.querySelector('#download-strip');
                attachments.hidden = false;
                downloads.hidden = false;
                document.querySelector('#attachment-list').innerHTML =
                  '<span class="attachment-pill">large-paper-a.pdf · 4.3 MB</span>'
                  + '<span class="attachment-pill">large-paper-b.pdf · 4.5 MB</span>';
                document.querySelector('#download-list').innerHTML =
                  '<a class="attachment-pill download-link">下载 report.md</a>';
            }"""
        )
        composer = page.locator(".composer-wrap").bounding_box()
        messages = page.locator("#messages").bounding_box()
        self.assertIsNotNone(composer)
        self.assertIsNotNone(messages)
        self.assertGreater(composer["height"], 0)
        self.assertLessEqual(composer["y"] + composer["height"], 721)
        self.assertLessEqual(messages["y"] + messages["height"], composer["y"] + 1)

    def test_collapsed_single_line_markdown_table_is_repaired(self):
        sample = (
            "| 模型 | 解决率 | |:--|:--:| "
            "| Claude 3 Opus | 3.79% | | **Claude 2** | **1.96%** || "
            "SWE-Llama 13b | 0.70% | | GPT-4 | 1.31% | | ChatGPT-3.5 | 0.17% |"
        )
        rendered = self.page.evaluate(
            "(source) => { const host = document.createElement('div'); "
            "host.innerHTML = renderMarkdown(source); return host.innerHTML; }",
            sample,
        )
        self.assertIn('<div class="table-wrap">', rendered)
        self.assertEqual(rendered.count("<th "), 2)
        self.assertEqual(rendered.count("<tr>"), 6)
        self.assertIn("<strong>Claude 2</strong>", rendered)

    def test_long_turn_waiting_indicator_exposes_stage_and_heartbeat_copy(self):
        state = self.page.evaluate("""
          () => {
            const indicator = createWaitingIndicator("正在请求模型", {
              longWork: expectsLongTurn("请生成一份完整的课程报告文件"),
              phase: "model_call",
            });
            document.body.append(indicator.element);
            indicator.setLabel("正在执行文件操作…", {
              phase: "file_work",
              longWork: true,
            });
            const result = {
              longWorkDetected: expectsLongTurn("请生成一份完整的课程报告文件"),
              shortChatDetected: expectsLongTurn("你好"),
              headline: indicator.element.querySelector(".turn-waiting-headline")?.textContent,
              hasPulse: Boolean(indicator.element.querySelector(".turn-waiting-pulse")),
              liveRole: indicator.element.getAttribute("role"),
            };
            indicator.destroy();
            indicator.element.remove();
            return result;
          }
        """)
        self.assertTrue(state["longWorkDetected"])
        self.assertFalse(state["shortChatDetected"])
        self.assertIn("正在执行文件操作", state["headline"])
        self.assertTrue(state["hasPulse"])
        self.assertEqual(state["liveRole"], "status")

    def test_mobile_overflow_tool_group_and_gateway_event_stream(self):
        page = self.page
        page.wait_for_function("state.gatewayEventsConnected === true", timeout=8000)

        page.set_viewport_size({"width": 390, "height": 780})
        self.assertTrue(page.locator("#topbar-more-toggle").is_visible())
        self.assertFalse(page.locator("#timeline-toggle").is_visible())
        page.locator("#topbar-more-toggle").click()
        self.assertTrue(page.locator("#topbar-more-menu").is_visible())
        page.locator("#topbar-more-menu [data-trigger='stress-toggle']").click()
        page.wait_for_selector("#stress-panel.open")
        page.locator("#stress-close").click()

        page.evaluate("""
          () => {
            const call = createToolCard({tool: "web_search", args: {query: "first"}, phase: "call"});
            const result = createToolCard({
              tool: "web_search",
              phase: "result",
              result: {tool: "web_search", success: true, output: {answer: "done"}, error: null},
            });
            const secondCall = createToolCard({tool: "web_search", args: {query: "second"}, phase: "call"});
            const secondResult = createToolCard({
              tool: "web_search",
              phase: "result",
              result: {tool: "web_search", success: true, output: {answer: "done again"}, error: null},
            });
            insertToolCard(call);
            insertToolCard(result);
            insertToolCard(secondCall);
            insertToolCard(secondResult);
          }
        """)
        group = page.locator("#messages > .tool-event-group").last
        self.assertEqual(group.locator(".tool-event").count(), 4)
        status = group.locator(".tool-event-group-status").inner_text()
        self.assertIn("调用", status)
        self.assertIn("2", status)
        self.assertIn("已完成", status)

    def test_chat_stream_session_rename_attachment_drag_approval_and_error_layout(self):
        page = self.page
        self.assertIn("已连接", page.locator("#gateway-status").inner_text())
        self.assertEqual(page.locator("#workspace-button").count(), 0)
        page.locator("#composer-add").click()
        self.assertTrue(page.locator("#composer-add-menu").is_visible())
        self.assertTrue(page.locator("#composer-select-attachments").is_disabled())
        self.assertIn("上传文件或图片", page.locator("#composer-add-menu").inner_text())
        page.locator("#composer-add").click()
        self.assertFalse(page.locator("#composer-add-menu").is_visible())
        page.route(
            "**/api/skills",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "skills": [{
                        "name": "course-report",
                        "description": "生成结构化课程报告",
                    }]
                }, ensure_ascii=False),
            ),
        )
        page.locator("#composer-add").click()
        page.locator("#composer-skill-menu-toggle").click()
        page.wait_for_selector("#composer-skill-options .composer-skill-option")
        page.locator("#composer-skill-options .composer-skill-option").click()
        self.assertTrue(page.locator("#composer-skill-chip").is_visible())
        self.assertIn("course-report", page.locator("#composer-skill-chip").inner_text())
        page.locator("#composer-skill-clear").click()
        self.assertFalse(page.locator("#composer-skill-chip").is_visible())
        page.unroute("**/api/skills")
        # The Web client intentionally opens on an unsaved draft.  Enter an
        # existing session before asserting session-scoped metadata.
        page.locator("#session-list .session-row").first.click()
        page.wait_for_selector(".workspace-chip[role='button']")
        self.assertIn("Workspace", page.locator(".workspace-chip").first.inner_text())

        page.locator("#message-input").fill("你好")
        page.locator("#composer").press("Enter")
        page.wait_for_selector(".message.assistant")
        page.wait_for_selector(".message.user .message-source")
        self.assertIn("Web", page.locator(".message.user .message-source").first.inner_text())
        page.wait_for_function("document.querySelector('.message.assistant')?.textContent.includes('测试回复已完成')")
        page.wait_for_selector(".message.assistant .katex-display")
        page.wait_for_selector(".message.assistant blockquote")
        self.assertIn("f(n)", page.locator(".message.assistant .katex-display").inner_text())
        self.assertFalse(page.locator(".message.assistant blockquote").inner_text().lstrip().startswith(">"))
        self.assertFalse(page.locator(".message.assistant").get_attribute("class").endswith("is-streaming"))
        session = self.store.get("default")
        session.messages.append({"role": "user", "content": "来自 QQ 的后台消息", "metadata": {"source": "qqbot"}})
        session.messages.append({"role": "assistant", "content": "参考来源 [W6]"})
        first_sources = [
            {"title": f"Chip Source {index}", "url": f"https://example.com/chip-{index}"}
            for index in range(1, 6)
        ]
        second_sources = [{"title": "Chip Source 6", "url": "https://example.com/chip-6"}]
        for index, sources in enumerate([first_sources, second_sources], start=1):
            session.tool_trace.append({
                "timestamp": utc_now(),
                "tool": "web_search",
                "args": {"query": f"chip {index}"},
                "result": {
                    "tool": "web_search",
                    "success": True,
                    "output": {
                        "results": sources,
                        "citations": [
                            {"label": f"[W{source_index}]", "title": item["title"], "url": item["url"]}
                            for source_index, item in enumerate(sources, start=1)
                        ],
                    },
                    "error": None,
                },
            })
        session.updated_at = utc_now()
        self.store.save(session)
        page.wait_for_function("document.body.innerText.includes('来自 QQ 的后台消息')", timeout=6000)
        self.assertIn("QQ", page.locator(".message.user .message-source").last.inner_text())
        page.wait_for_selector(".citation-chip")
        self.assertEqual(page.locator(".citation-chip").first.get_attribute("href"), "https://example.com/chip-6")
        self.assertIn("Chip Source 6", page.locator(".citation-chip").first.get_attribute("title"))
        session.messages.append({"role": "assistant", "content": "鏈湴寮曠敤[O1][P2][S3][D4][A5]"})
        session.tool_trace.append({
            "timestamp": utc_now(),
            "tool": "ocr_image",
            "args": {"attachment_id": "att_img"},
            "result": {
                "tool": "ocr_image",
                "success": True,
                "output": {
                    "citations": [
                        {"label": "[O1]", "kind": "ocr_block", "filename": "image.png", "block": 1, "confidence": 0.99},
                        {"label": "[P2]", "kind": "pdf_page", "filename": "paper.pdf", "page": 2},
                        {"label": "[S3]", "kind": "slide", "filename": "deck.pptx", "slide": 3},
                        {"label": "[D4]", "kind": "document_section", "filename": "report.docx", "section": 4},
                        {"label": "[A5]", "kind": "attachment", "filename": "notes.txt"},
                    ],
                },
                "error": None,
            },
        })
        session.updated_at = utc_now()
        self.store.save(session)
        page.wait_for_function("document.body.innerText.includes('O1') && document.body.innerText.includes('P2')")
        self.assertEqual(page.locator(".citation-chip-local").count(), 5)
        self.assertIn("OCR 块 1", page.locator(".citation-chip-local").first.get_attribute("title"))
        page.wait_for_selector("#message-index:not([hidden])")
        self.assertIn("索引", page.locator("#message-index-toggle").inner_text())
        page.locator("#message-index-toggle").click()
        page.wait_for_selector("#message-index:not(.collapsed) .message-index-item")
        page.locator(".message-index-item").first.click()
        page.wait_for_selector(".message.user.message-highlight")
        page.locator("#message-index-search").fill("测试回复")
        page.wait_for_function("document.querySelector('.message-index-item')?.textContent.includes('S ·')")
        page.locator(".message-index-item").first.click()
        page.wait_for_selector(".message.assistant.message-highlight")
        page.locator("#stress-toggle").click()
        page.wait_for_selector("#stress-panel.open .stress-health")
        self.assertIn("Session 健康", page.locator("#stress-panel").inner_text())
        self.assertIn("关键状态", page.locator("#stress-panel").inner_text())
        self.assertIn("需要关注", page.locator("#stress-panel").inner_text())
        self.assertIn("查看技术详情", page.locator("#stress-panel").inner_text())
        page.locator("#stress-technical > summary").click()
        page.wait_for_selector("#stress-technical[open] .stress-card")
        page.locator("#stress-close").click()
        page.wait_for_selector("#stress-panel:not(.open)")

        avatar_file = self.root / "avatar.svg"
        avatar_file.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="#c6533d"/></svg>',
            encoding="utf-8",
        )
        with page.expect_file_chooser() as chooser:
            page.locator(".message.assistant .avatar").first.click()
        chooser.value.set_files(str(avatar_file))
        page.wait_for_selector(".message.assistant .avatar.has-image img")
        self.assertIn(
            "data:image/svg+xml",
            page.locator(".message.assistant .avatar img").first.get_attribute("src"),
        )
        page.locator(".message.assistant .avatar").first.click(button="right")
        page.wait_for_function("!document.querySelector('.message.assistant .avatar.has-image')")

        row = page.locator("#session-list .session-row").first
        row.hover()
        page.once("dialog", lambda dialog: dialog.accept("E2E 会话"))
        row.locator(".session-rename").click()
        page.wait_for_function("document.querySelector('#session-title')?.textContent === 'E2E 会话'")

        page.evaluate("""
          () => {
            const transfer = new DataTransfer();
            transfer.items.add(new File(['clipboard image'], '', {type:'image/png'}));
            const event = new Event('paste', {bubbles:true, cancelable:true});
            Object.defineProperty(event, 'clipboardData', {value:transfer});
            document.querySelector('#message-input').dispatchEvent(event);
          }
        """)
        page.wait_for_selector(".attachment-pill")
        pasted = page.locator(".attachment-pill").first
        self.assertEqual(pasted.get_attribute("aria-pressed"), "true")
        self.assertRegex(pasted.inner_text(), r"粘贴图片-.*\.png")
        page.locator(".attachment-preview-button").first.click()
        page.wait_for_selector("#attachment-preview-dialog[open] .attachment-preview-body img")
        self.assertIn("粘贴图片-", page.locator("#attachment-preview-title").inner_text())
        page.locator("#attachment-preview-close").click()

        pdf_file = self.root / "preview.pdf"
        import fitz
        document = fitz.open()
        pdf_page = document.new_page()
        pdf_page.insert_text((72, 72), "PDF Preview")
        pdf_file.write_bytes(document.tobytes())
        document.close()
        page.locator("#file-input").set_input_files(str(pdf_file))
        page.wait_for_function("document.body.innerText.includes('preview.pdf')")
        page.locator(".attachment-entry").filter(has_text="preview.pdf").locator(".attachment-preview-button").click()
        page.wait_for_selector("#attachment-preview-dialog[open] .pdf-preview-page img")
        self.assertEqual(
            page.locator("#attachment-preview-body").evaluate("node => node.scrollTop"),
            0,
        )
        self.assertIn("第 1 页", page.locator("#attachment-preview-dialog .pdf-preview-page figcaption").first.inner_text())
        self.assertTrue(
            page.locator("#attachment-preview-dialog .pdf-preview-page img").get_attribute("src").startswith("data:image/png;base64,")
        )
        page.locator("#attachment-preview-close").click()

        page.evaluate("""
          () => {
            const transfer = new DataTransfer();
            transfer.items.add(new File(['drag upload'], 'drag.txt', {type: 'text/plain'}));
            const target = document.querySelector('.workspace');
            target.dispatchEvent(new DragEvent('dragenter', {bubbles:true, cancelable:true, dataTransfer:transfer}));
            target.dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer:transfer}));
          }
        """)
        page.wait_for_selector(".attachment-pill")
        attachment = page.locator(".attachment-pill").first
        self.assertEqual(attachment.get_attribute("aria-pressed"), "true")
        attachment.click()
        page.locator("#attachment-clear-selection").click()
        page.wait_for_function("document.querySelectorAll('.attachment-pill[aria-pressed=\"true\"]').length === 0")
        # No selection is intentionally rendered as an empty mode chip; the
        # old “按需读取” hint was removed from the UI to reduce redundancy.
        self.assertEqual(page.locator("#attachment-mode").inner_text(), "")
        page.locator("#attachment-select-all").click()
        page.wait_for_function("document.querySelectorAll('.attachment-pill[aria-pressed=\"true\"]').length >= 2")
        self.assertIn("本轮将读取", page.locator("#attachment-mode").inner_text())
        page.locator("#attachment-clear-selection").click()
        page.wait_for_function("document.querySelectorAll('.attachment-pill[aria-pressed=\"true\"]').length === 0")

        approval = page.locator(".approval-card")
        self.assertTrue(approval.is_visible())
        approval.locator(".approval-approve").click()
        page.wait_for_function("document.querySelectorAll('.approval-card').length === 0")

        composer_before = page.locator("#composer").bounding_box()
        page.route(
            "**/api/sessions/*/attachments",
            lambda route: route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "E2E upload failure"})),
        )
        failure_file = self.root / "failure.txt"
        failure_file.write_text("failure", encoding="utf-8")
        page.locator("#file-input").set_input_files(str(failure_file))
        page.wait_for_selector("#error-banner:not([hidden])")
        composer_after = page.locator("#composer").bounding_box()
        self.assertAlmostEqual(composer_before["y"], composer_after["y"], delta=1)
        page.unroute("**/api/sessions/*/attachments")

    def test_selected_attachments_render_on_their_user_turn(self):
        page = self.page
        page.locator("#session-list .session-row").first.click()
        page.wait_for_selector("#message-input")
        image_file = self.root / "turn-image.svg"
        image_file.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">'
            '<rect width="120" height="80" fill="#d46b52"/>'
            '<circle cx="60" cy="40" r="20" fill="#fff4e9"/></svg>',
            encoding="utf-8",
        )
        pdf_file = self.root / "turn-notes.pdf"
        import fitz
        document = fitz.open()
        pdf_page = document.new_page()
        pdf_page.insert_text((72, 72), "Turn attachment")
        pdf_file.write_bytes(document.tobytes())
        document.close()
        page.locator("#file-input").set_input_files([str(image_file), str(pdf_file)])
        page.wait_for_function(
            "document.querySelectorAll('.attachment-pill[aria-pressed=\"true\"]').length === 2"
        )
        page.locator("#message-input").fill("请结合这两个附件回答")
        page.locator("#composer").press("Enter")
        page.wait_for_function(
            "document.querySelector('#composer-attachment-preview').hidden"
        )
        page.wait_for_function(
            "document.querySelectorAll('.attachment-pill[aria-pressed=\"true\"]').length === 0"
        )
        page.wait_for_selector(".message.user .message-attachment-card.image img")
        page.wait_for_selector(".message.user .message-attachment-card.pdf")
        self.assertIn(
            "turn-notes.pdf",
            page.locator(".message.user .message-attachment-card.pdf").inner_text(),
        )
        page.wait_for_function("!document.querySelector('#stop-button:not([hidden])')")
        page.evaluate("loadSession(state.currentSessionId)")
        page.wait_for_selector(".message.user .message-attachment-card.image img")
        page.wait_for_selector(".message.user .message-attachment-card.pdf")


if __name__ == "__main__":
    unittest.main()
