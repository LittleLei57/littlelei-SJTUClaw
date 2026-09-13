"""Gateway、显式 Session 路由与附件隔离测试。"""

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from attachment_store import AttachmentStore
from compaction import Compactor
from context_builder import ContextBuilder
from memory_candidates import MemoryCandidateStore
from memory_store import MemoryStore
from gateway import create_app
from runtime import AgentRuntime
from session_store import SessionStore


class EchoModel:
    def __init__(self):
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return f"echo: {messages[-1]['content']}"


class FailingModel:
    def complete(self, messages):
        raise RuntimeError("model offline")


class SummaryModel:
    def complete(self, messages):
        return (
            "### 当前任务\n- 继续完成项目验收。\n\n"
            "### 已完成\n- 已检查早期会话内容。\n\n"
            "### 下一步\n- 根据最近消息继续处理。"
        )


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root / "data")
        self.model = EchoModel()
        self.runtime = AgentRuntime(self.model, self.store, ContextBuilder())
        self.client = TestClient(
            create_app(self.runtime, AttachmentStore(self.store), Path(__file__).parents[1] / "web")
        )

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_health_and_web_ui(self):
        health = self.client.get("/api/health").json()
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["apiProtocolVersion"], 2)
        self.assertRegex(health["gatewayVersion"], r"^\d+\.\d+\.\d+$")
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("SJTUClaw", page.text)
        self.assertNotIn("LLM_API_KEY", page.text)
        self.assertIn("no-store", page.headers["cache-control"])

    def test_stress_report_is_read_only_and_summarizes_limits(self):
        prompt = self.root / "compact.md"
        prompt.write_text("compact", encoding="utf-8")
        self.runtime.compactor = Compactor(
            self.model,
            self.store,
            prompt,
            max_messages=2,
            keep_recent=1,
            chunk_characters=3_000,
        )
        session = self.store.current
        session.messages = [
            {"role": "user", "content": "A" * 4_000},
            {"role": "assistant", "content": "B" * 4_000},
            {"role": "user", "content": "recent"},
        ]
        self.store.save(session)
        response = self.client.get("/api/stress/report?sessionId=default")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["sessionId"], "default")
        self.assertEqual(data["messages"]["total"], 3)
        self.assertEqual(data["messages"]["stored"], 3)
        self.assertEqual(data["attachments"]["maxPerSession"], AttachmentStore.MAX_PER_SESSION)
        self.assertTrue(data["compaction"]["shouldCompact"])
        self.assertEqual(data["compaction"]["estimatedChunks"], 4)
        self.assertEqual(self.model.calls, [])
        scenarios = {item["id"]: item for item in data["scenarios"]}
        self.assertEqual(scenarios["oversized_context"]["status"], "warn")
        self.assertEqual(scenarios["attachment_flood"]["status"], "pass")
        self.assertIn("10 MB", scenarios["attachment_flood"]["detail"])
        self.assertNotIn("10485760", scenarios["attachment_flood"]["detail"])
        self.assertEqual(scenarios["protocol_pollution"]["status"], "pass")

    def test_static_assets_are_not_stale_cached(self):
        script = self.client.get("/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("no-store", script.headers["cache-control"])
        self.assertIn("REQUIRED_API_PROTOCOL_VERSION", script.text)

    def test_waiting_timer_uses_wall_clock_across_background_tabs(self):
        script = self.client.get("/app.js").text
        waiting_timer = script[
            script.index("function createWaitingIndicator"):
            script.index("async function sendMessage")
        ]
        self.assertIn("let startedAt = Date.now()", waiting_timer)
        self.assertIn("if (!document.hidden) render()", waiting_timer)
        self.assertIn("now - startedAt - pausedDuration - paused", waiting_timer)
        self.assertNotIn("hiddenDuration", waiting_timer)
        self.assertNotIn("performance.now()", waiting_timer)

    def test_web_exposes_slash_command_palette_and_manual_compaction(self):
        page = self.client.get("/")
        script = self.client.get("/app.js")
        self.assertIn('id="slash-command-menu"', page.text)
        self.assertIn('id="composer-attachment-preview"', page.text)
        self.assertIn('command: "/compact"', script.text)
        self.assertIn("runManualCompaction", script.text)
        self.assertIn("settleExecutionPlanCard", script.text)
        self.assertIn("renderPersistedCompactionCards(session)", script.text)
        self.assertIn("card.open = false", script.text)
        self.assertIn("正在计算整理范围…", script.text)
        self.assertIn("Missing telemetry is \"unknown\", not zero.", script.text)
        self.assertIn("renderComposerAttachmentPreview", script.text)
        self.assertIn("composer-attachment-card", script.text)
        self.assertIn(r"\/api\/downloads\/", script.text)

    def test_manual_compaction_persists_activity_for_collapsed_history_card(self):
        prompt = self.root / "compact.md"
        prompt.write_text("compact", encoding="utf-8")
        self.runtime.compactor = Compactor(
            SummaryModel(),
            self.store,
            prompt,
            max_messages=2,
            keep_recent=1,
        )
        session = self.store.current
        session.messages = [
            {"role": "user", "content": "先检查项目结构。"},
            {"role": "assistant", "content": "项目结构已经检查。"},
            {"role": "user", "content": "接下来继续验收。"},
            {"role": "assistant", "content": "我会继续验收。"},
        ]
        self.store.save(session)

        response = self.client.post("/api/sessions/default/compact")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["compacted"])
        restored = self.client.get("/api/sessions/default").json()
        events = [
            item for item in restored["activity"]
            if item.get("type") == "compaction"
        ]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["data"]["manual"])
        self.assertTrue(events[0]["data"]["anchorMessageId"].startswith("compact_anchor_"))
        self.assertIn("继续完成项目验收", events[0]["data"]["summaryPreview"])
        anchored = [
            message for message in restored["messages"]
            if events[0]["data"]["anchorMessageId"]
            in (message.get("metadata") or {}).get("compactionAnchorIds", [])
        ]
        self.assertEqual(len(anchored), 1)

    def test_web_restores_manual_compaction_at_its_historical_anchor(self):
        script = self.client.get("/app.js").text
        self.assertIn("metadata?.compactionAnchorIds", script)
        self.assertIn("payload.anchorMessageId", script)
        self.assertIn("legacyAnchor = retained > 0", script)

    def test_manual_compaction_noop_returns_explanation(self):
        prompt = self.root / "compact.md"
        prompt.write_text("compact", encoding="utf-8")
        self.runtime.compactor = Compactor(
            SummaryModel(),
            self.store,
            prompt,
            max_messages=20,
            keep_recent=8,
        )
        session = self.store.current
        session.messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，有什么需要？"},
        ]
        self.store.save(session)

        response = self.client.post("/api/sessions/default/compact")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["compacted"])
        self.assertEqual(payload["semanticMessages"], 2)
        self.assertEqual(payload["minimumMessages"], 4)
        self.assertIn("原有上下文未作修改", payload["message"])

    def test_web_does_not_render_pending_approval_placeholder_as_failure(self):
        script = self.client.get("/app.js")
        self.assertIn("result?.approvalRequired === true", script.text)
        self.assertIn(
            "result?.deferred || result?.approvalRequired === true",
            script.text,
        )

    def test_live_visible_count_excludes_intermediate_assistant_segments(self):
        script = self.client.get("/app.js")
        self.assertIn(
            '".message:not(.progress):not(.is-intermediate)"',
            script.text,
        )

    def test_desktop_pet_renders_citations_compaction_and_dynamic_user_name(self):
        page = self.client.get("/pet.html")
        script = self.client.get("/pet.js")
        styles = self.client.get("/pet.css")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(script.status_code, 200)
        self.assertEqual(styles.status_code, 200)
        self.assertIn("collectPetCitations", script.text)
        self.assertIn("updatePetCompactionNotice", script.text)
        self.assertIn("compaction_started", script.text)
        self.assertIn("sjtuclaw.displayName.user", script.text)
        self.assertNotIn('"嗨，小雷"', script.text)
        self.assertIn('id="session-switch"', page.text)
        self.assertIn("loadPetSession", script.text)
        self.assertIn("renderPetSessionList", script.text)
        self.assertIn("sjtuclaw-pet-session", script.text)
        self.assertIn(".pet-citation-chip", styles.text)
        self.assertIn(".pet-compaction", styles.text)
        self.assertIn(".pet-session-panel", styles.text)
        # Gateway reloads replace only the renderer; Electron's native mouse
        # pass-through flag survives.  The new page must explicitly reset it
        # or the topmost pet can become visible but impossible to click.
        self.assertIn('setMousePassthrough(false, { force: true });', script.text)

    def test_static_assets_do_not_expose_secret_configuration(self):
        forbidden = ["LLM_API_KEY", "APP_SECRET", "CLIENT_SECRET", "tvly-"]
        for path in ["/", "/app.js", "/styles.css"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            for marker in forbidden:
                self.assertNotIn(marker, response.text)
            self.assertNotRegex(response.text, r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")

    def test_dynamic_attachment_rows_cannot_push_composer_off_screen(self):
        styles = self.client.get("/styles.css")
        self.assertEqual(styles.status_code, 200)
        self.assertRegex(
            styles.text,
            r"\.workspace\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column;",
        )
        self.assertRegex(
            styles.text,
            r"\.messages\s*\{[^}]*min-height:\s*0;[^}]*flex:\s*1 1 auto;",
        )
        self.assertRegex(
            styles.text,
            r"\.composer-wrap\s*\{[^}]*flex:\s*0 0 auto;",
        )

    def test_web_repairs_collapsed_single_line_markdown_tables(self):
        app_script = self.client.get("/app.js")
        pet_script = self.client.get("/pet.js")
        self.assertEqual(app_script.status_code, 200)
        self.assertEqual(pet_script.status_code, 200)
        for script in (app_script.text, pet_script.text):
            self.assertIn("normalizeCollapsedMarkdownTables", script)
            self.assertIn(r'replace(/\|\s*\|/g, "|\n|")', script)
            self.assertIn(r"/^:?-{2,}:?$/", script)

    def test_web_can_edit_memory_candidate_before_decision(self):
        memories = MemoryStore(self.root / "candidate-data")
        candidates = MemoryCandidateStore(self.root / "candidate-data", memories)
        candidate = candidates.propose_from_message("我喜欢简洁回答", "default")[0]
        runtime = AgentRuntime(EchoModel(), self.store, ContextBuilder())
        runtime.memory_candidate_store = candidates
        client = TestClient(create_app(
            runtime, AttachmentStore(self.store), Path(__file__).parents[1] / "web"
        ))
        response = client.patch(
            f"/api/memory-candidates/{candidate.candidate_id}",
            json={"content": "用户偏好简洁的中文回答", "type": "preference"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "用户偏好简洁的中文回答")
        client.close()

    def test_system_prompt_declares_scheduler_capability(self):
        prompt = (Path(__file__).parents[1] / "prompts" / "system_prompt.md").read_text(encoding="utf-8")
        self.assertIn("内置 Scheduler", prompt)
        self.assertIn("不要错误声称", prompt)

    def test_create_list_and_get_session_history(self):
        created = self.client.post("/api/sessions", json={"title": "网页会话"})
        self.assertEqual(created.status_code, 201)
        session_id = created.json()["sessionId"]
        sessions = self.client.get("/api/sessions").json()["sessions"]
        self.assertTrue(any(item["sessionId"] == session_id for item in sessions))
        self.client.post("/api/chat", json={"sessionId": session_id, "message": "hello"})
        history = self.client.get(f"/api/sessions/{session_id}").json()["messages"]
        self.assertEqual([item["role"] for item in history], ["user", "assistant"])

    def test_delete_session_switches_to_remaining_session(self):
        created = self.store.create("to delete")
        response = self.client.delete(f"/api/sessions/{created.session_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deletedSessionId"], created.session_id)
        self.assertEqual(response.json()["currentSessionId"], "default")
        self.assertEqual(self.client.get(f"/api/sessions/{created.session_id}").status_code, 404)

    def test_rename_session_from_web_api(self):
        created = self.store.create("旧名称")
        response = self.client.patch(
            f"/api/sessions/{created.session_id}", json={"title": "feishu"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "feishu")
        self.assertEqual(self.store.get(created.session_id).title, "feishu")

    def test_rename_session_rejects_blank_title(self):
        response = self.client.patch("/api/sessions/default", json={"title": "   "})
        self.assertEqual(response.status_code, 400)

    def test_native_workspace_picker_sets_selected_directory(self):
        selected = self.root / "picked-workspace"
        selected.mkdir()
        client = TestClient(create_app(
            self.runtime,
            AttachmentStore(self.store),
            Path(__file__).parents[1] / "web",
            directory_picker=lambda _initial: str(selected),
        ))
        response = client.post("/api/sessions/default/workspace/pick")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["cancelled"])
        self.assertEqual(Path(response.json()["workspace"]), selected.resolve())
        client.close()

    def test_draft_workspace_picker_does_not_create_session(self):
        selected = self.root / "draft-workspace"
        selected.mkdir()
        before = len(self.store.list_sessions())
        client = TestClient(create_app(
            self.runtime,
            AttachmentStore(self.store),
            Path(__file__).parents[1] / "web",
            directory_picker=lambda initial: str(selected),
        ))
        response = client.post(
            "/api/workspace/pick",
            json={"initialPath": str(self.root)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["cancelled"])
        self.assertEqual(Path(response.json()["workspace"]), selected.resolve())
        self.assertEqual(len(self.store.list_sessions()), before)
        client.close()

    def test_create_session_accepts_draft_workspace(self):
        selected = self.root / "created-workspace"
        selected.mkdir()
        response = self.client.post(
            "/api/sessions",
            json={"title": "带 Workspace 的草稿", "workspace": str(selected)},
        )
        self.assertEqual(response.status_code, 201)
        session = self.store.get(response.json()["sessionId"])
        self.assertEqual(Path(session.workspace), selected.resolve())

    def test_create_session_rejects_missing_draft_workspace_without_orphan(self):
        before = len(self.store.list_sessions())
        response = self.client.post(
            "/api/sessions",
            json={"title": "无效草稿", "workspace": str(self.root / "missing")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.store.list_sessions()), before)

    def test_cancelled_workspace_picker_keeps_current_value(self):
        client = TestClient(create_app(
            self.runtime,
            AttachmentStore(self.store),
            Path(__file__).parents[1] / "web",
            directory_picker=lambda _initial: None,
        ))
        response = client.post("/api/sessions/default/workspace/pick")
        self.assertTrue(response.json()["cancelled"])
        self.assertIsNone(response.json()["workspace"])
        client.close()

    def test_explicit_session_conversation_becomes_global_current(self):
        other = self.store.create("other")
        self.store.set_current_id("default")
        response = self.client.post(
            "/api/chat", json={"sessionId": other.session_id, "message": "only other"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.current_id, other.session_id)
        self.assertEqual(self.store.get("default").messages, [])
        self.assertEqual(len(self.store.get(other.session_id).messages), 2)

    def test_replay_stream_truncates_history_and_allows_edit(self):
        self.client.post("/api/chat", json={"sessionId": "default", "message": "first"})
        self.client.post("/api/chat", json={"sessionId": "default", "message": "second"})
        response = self.client.post(
            "/api/chat/replay/stream",
            json={"sessionId": "default", "messageIndex": 0, "message": "edited first"},
        )
        self.assertEqual(response.status_code, 200)
        messages = self.store.get("default").messages
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["content"], "edited first")
        self.assertEqual(messages[1]["content"], "echo: edited first")

    def test_replay_rejects_assistant_message_index(self):
        self.client.post("/api/chat", json={"sessionId": "default", "message": "first"})
        response = self.client.post(
            "/api/chat/replay/stream",
            json={"sessionId": "default", "messageIndex": 1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("error", response.text)
        self.assertEqual(len(self.store.get("default").messages), 2)

    def test_web_created_draft_does_not_replace_last_conversation(self):
        self.store.set_current_id("default")
        response = self.client.post("/api/sessions", json={"title": "draft"})
        self.assertEqual(response.status_code, 201)
        self.assertNotEqual(response.json()["sessionId"], "default")
        self.assertEqual(self.store.current_id, "default")

    def test_missing_session_returns_error_and_gateway_survives(self):
        response = self.client.post(
            "/api/chat", json={"sessionId": "missing", "message": "hello"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_gateway_exposes_unified_browser_event_stream(self):
        paths = {route.path for route in self.client.app.routes}
        self.assertIn("/api/events/stream", paths)

    def test_agent_failure_is_502_and_gateway_survives(self):
        runtime = AgentRuntime(FailingModel(), self.store, ContextBuilder())
        client = TestClient(create_app(runtime, AttachmentStore(self.store), Path(__file__).parents[1] / "web"))
        response = client.post("/api/chat", json={"sessionId": "default", "message": "hello"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("model offline", response.json()["detail"])
        self.assertEqual(client.get("/api/health").status_code, 200)
        client.close()

    def test_attachment_metadata_is_isolated_by_session(self):
        other = self.store.create("attachments-two")
        response = self.client.post(
            "/api/sessions/default/attachments",
            files={"file": ("notes.txt", b"private notes", "text/plain")},
        )
        self.assertEqual(response.status_code, 201)
        metadata = response.json()["attachment"]
        self.assertEqual(metadata["filename"], "notes.txt")
        self.assertEqual(metadata["size"], 13)
        default_items = self.client.get("/api/sessions/default/attachments").json()["attachments"]
        other_items = self.client.get(
            f"/api/sessions/{other.session_id}/attachments"
        ).json()["attachments"]
        self.assertEqual(len(default_items), 1)
        self.assertEqual(other_items, [])
        stored = self.store.data_dir / "attachments" / "blobs" / metadata["sha256"]
        self.assertEqual(stored.read_bytes(), b"private notes")

    def test_attachment_metadata_enters_only_its_session_context(self):
        other = self.store.create("other")
        self.client.post(
            "/api/sessions/default/attachments",
            files={"file": ("secret-name.txt", b"x", "text/plain")},
        )
        default_context = self.runtime.context_builder.build(self.store.get("default"), "hi")
        other_context = self.runtime.context_builder.build(self.store.get(other.session_id), "hi")
        self.assertIn("secret-name.txt", str(default_context))
        self.assertNotIn("secret-name.txt", str(other_context))

    def test_delete_attachment_metadata_and_file(self):
        uploaded = self.client.post(
            "/api/sessions/default/attachments",
            files={"file": ("remove.txt", b"delete me", "text/plain")},
        ).json()["attachment"]
        stored = self.store.data_dir / "attachments" / "blobs" / uploaded["sha256"]
        response = self.client.delete(
            f"/api/sessions/default/attachments/{uploaded['attachmentId']}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["fileDeleted"])
        self.assertFalse(stored.exists())
        self.assertEqual(self.store.get("default").attachments, [])

    def test_text_attachment_preview_and_download_are_session_scoped(self):
        uploaded = self.client.post(
            "/api/sessions/default/attachments",
            files={"file": ("notes.md", "# 课程笔记".encode(), "text/markdown")},
        ).json()["attachment"]
        base = f"/api/sessions/default/attachments/{uploaded['attachmentId']}"
        preview = self.client.get(f"{base}/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["kind"], "text")
        self.assertIn("课程笔记", preview.json()["content"])
        download = self.client.get(f"{base}/content")
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(
            self.client.get(f"/api/sessions/missing/attachments/{uploaded['attachmentId']}/preview").status_code,
            404,
        )

    def test_only_safe_media_types_can_be_rendered_inline(self):
        image = self.client.post(
            "/api/sessions/default/attachments",
            files={"file": ("pixel.png", b"png bytes", "image/png")},
        ).json()["attachment"]
        image_base = f"/api/sessions/default/attachments/{image['attachmentId']}"
        self.assertEqual(self.client.get(f"{image_base}/preview").json()["kind"], "image")
        self.assertEqual(self.client.get(f"{image_base}/raw").headers["content-type"], "image/png")
        html = self.client.post(
            "/api/sessions/default/attachments",
            files={"file": ("unsafe.html", b"<script>alert(1)</script>", "text/html")},
        ).json()["attachment"]
        html_base = f"/api/sessions/default/attachments/{html['attachmentId']}"
        self.assertEqual(self.client.get(f"{html_base}/preview").json()["kind"], "text")
        self.assertEqual(self.client.get(f"{html_base}/raw").status_code, 415)

    def test_pdf_preview_pages_render_as_images(self):
        import fitz

        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "PDF Preview")
        pdf_bytes = document.tobytes()
        document.close()

        uploaded = self.client.post(
            "/api/sessions/default/attachments",
            files={"file": ("preview.pdf", pdf_bytes, "application/pdf")},
        ).json()["attachment"]
        base = f"/api/sessions/default/attachments/{uploaded['attachmentId']}"
        preview = self.client.get(f"{base}/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["kind"], "pdf")
        pages = self.client.get(f"{base}/pdf-pages")
        self.assertEqual(pages.status_code, 200)
        self.assertEqual(pages.json()["renderedPages"], 1)
        self.assertTrue(pages.json()["pages"][0]["src"].startswith("data:image/png;base64,"))

    def test_identical_attachments_share_blob_until_last_reference_is_deleted(self):
        other = self.store.create("shared attachment")
        first = self.client.post(
            "/api/sessions/default/attachments",
            files={"file": ("first.txt", b"same bytes", "text/plain")},
        ).json()["attachment"]
        second = self.client.post(
            f"/api/sessions/{other.session_id}/attachments",
            files={"file": ("second.txt", b"same bytes", "text/plain")},
        ).json()["attachment"]
        self.assertEqual(first["sha256"], second["sha256"])
        blob = self.store.data_dir / "attachments" / "blobs" / first["sha256"]
        self.assertTrue(blob.is_file())
        first_delete = self.client.delete(
            f"/api/sessions/default/attachments/{first['attachmentId']}"
        ).json()
        self.assertFalse(first_delete["fileDeleted"])
        self.assertEqual(first_delete["remainingReferences"], 1)
        self.assertTrue(blob.is_file())
        second_delete = self.client.delete(
            f"/api/sessions/{other.session_id}/attachments/{second['attachmentId']}"
        ).json()
        self.assertTrue(second_delete["fileDeleted"])
        self.assertFalse(blob.exists())

    def test_attachment_audit_reports_missing_and_cleans_orphans(self):
        uploaded = self.client.post(
            "/api/sessions/default/attachments",
            files={"file": ("missing.txt", b"content", "text/plain")},
        ).json()["attachment"]
        blob = self.store.data_dir / "attachments" / "blobs" / uploaded["sha256"]
        blob.unlink()
        orphan = self.store.data_dir / "attachments" / "blobs" / ("f" * 64)
        orphan.write_bytes(b"orphan")
        audit = self.client.get("/api/attachments/audit").json()
        self.assertEqual(audit["missingReferences"][0]["attachmentId"], uploaded["attachmentId"])
        self.assertIn(orphan.name, audit["orphanBlobs"])
        cleanup = self.client.post("/api/attachments/cleanup").json()
        self.assertIn(orphan.name, cleanup["removedOrphans"])
        self.assertFalse(orphan.exists())

    def test_upload_limit_and_filename_sanitization(self):
        too_large = b"x" * (AttachmentStore.MAX_BYTES + 1)
        response = self.client.post(
            "/api/sessions/default/attachments",
            files={"file": ("../bad.txt", too_large, "text/plain")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.get("default").attachments, [])

    def test_attachment_count_limit_per_session(self):
        for index in range(AttachmentStore.MAX_PER_SESSION):
            response = self.client.post(
                "/api/sessions/default/attachments",
                files={"file": (f"{index}.txt", b"x", "text/plain")},
            )
            self.assertEqual(response.status_code, 201)
        response = self.client.post(
            "/api/sessions/default/attachments",
            files={"file": ("overflow.txt", b"x", "text/plain")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("附件数量已达上限", response.json()["detail"])
        self.assertEqual(len(self.store.get("default").attachments), AttachmentStore.MAX_PER_SESSION)


if __name__ == "__main__":
    unittest.main()
