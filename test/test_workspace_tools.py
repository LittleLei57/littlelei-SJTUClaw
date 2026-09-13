"""Workspace、Advanced Tools、Approval 与 Download 测试。"""

from io import BytesIO
import json
from pathlib import Path
import tempfile
from threading import Barrier, Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient

from advanced_tools import AdvancedTools, register_advanced_tools
from approval_store import ApprovalStore
from attachment_store import AttachmentStore
from context_builder import ContextBuilder
from download_store import DownloadStore
from gateway import create_app, run_gateway_server
from main import handle_workspace_command, run_cli
from runtime import AgentCancelled, AgentRuntime
from session_store import SessionStore
from shell_manager import ShellManager
from tools import ToolExecutionContext, ToolResult, create_read_only_registry
from workspace import WorkspaceManager


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return next(self.replies)


class GatewayShutdownTests(unittest.TestCase):
    def test_keyboard_interrupt_is_reported_as_clean_shutdown(self):
        class InterruptedServer:
            def run(self):
                raise KeyboardInterrupt()

        shutdown_requested = Event()
        fake_app = SimpleNamespace(
            state=SimpleNamespace(shutdown_requested=shutdown_requested)
        )
        outputs = []
        run_gateway_server(InterruptedServer(), fake_app, outputs.append)
        self.assertTrue(shutdown_requested.is_set())
        self.assertEqual(outputs, ["Gateway 已关闭。"])


class Step8Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = SessionStore(self.data)
        self.workspaces = WorkspaceManager(self.sessions)
        self.downloads = DownloadStore(self.data)
        self.shells = ShellManager(self.workspaces)
        self.advanced = AdvancedTools(
            self.sessions, self.workspaces, self.shells, self.downloads
        )
        self.registry = create_read_only_registry(self.advanced)
        register_advanced_tools(self.registry, self.advanced)
        self.approvals = ApprovalStore(self.data)
        self.workspaces.set("default", str(self.workspace))

    def tearDown(self):
        self.shells.close_all()
        self.temp.cleanup()

    def make_runtime(self, replies):
        model = ScriptedModel(replies)
        runtime = AgentRuntime(
            model,
            self.sessions,
            ContextBuilder(tool_definitions=self.registry.definitions()),
            tool_registry=self.registry,
            approval_store=self.approvals,
        )
        runtime.workspace_manager = self.workspaces
        runtime.download_store = self.downloads
        runtime.shell_manager = self.shells
        return runtime, model


class WorkspaceAndToolTests(Step8Base):
    def test_cli_workspace_set_preserves_windows_backslashes(self):
        runtime, _ = self.make_runtime(["unused"])
        result = handle_workspace_command(runtime, f"/workspace set {self.workspace}")
        self.assertEqual(runtime.store.current.workspace, str(self.workspace.resolve()))
        self.assertIn(str(self.workspace.resolve()), result)

    def test_cli_workspace_set_accepts_quoted_path(self):
        target = self.root / "workspace with spaces"
        target.mkdir()
        runtime, _ = self.make_runtime(["unused"])
        handle_workspace_command(runtime, f'/workspace set "{target}"')
        self.assertEqual(runtime.store.current.workspace, str(target.resolve()))

    def test_read_pdf_attachment_without_shell(self):
        import fitz

        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "Database Systems Course Plan 2025")
        pdf_bytes = document.tobytes()
        document.close()
        metadata = AttachmentStore(self.sessions).save(
            "default", "course-plan.pdf", "application/pdf", BytesIO(pdf_bytes)
        )
        result = self.registry.execute(
            "read_attachment", {"attachment_id": metadata["attachmentId"]},
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.output["format"], "pdf")
        self.assertIn("Database Systems", result.output["content"])
        self.assertEqual(result.output["ocrPages"], [])
        self.assertEqual(result.output["citations"][0]["label"], "[P1]")
        self.assertEqual(result.output["citations"][0]["page"], 1)
        self.assertEqual(
            result.output["citations"][0]["attachmentId"],
            metadata["attachmentId"],
        )

    def test_read_pdf_attachment_supports_page_range(self):
        import fitz

        document = fitz.open()
        for index in range(5):
            page = document.new_page()
            page.insert_text((72, 72), f"Page {index + 1} Course Marker")
        pdf_bytes = document.tobytes()
        document.close()
        metadata = AttachmentStore(self.sessions).save(
            "default", "multi-page.pdf", "application/pdf", BytesIO(pdf_bytes)
        )
        result = self.registry.execute(
            "read_attachment",
            {"attachment_id": metadata["attachmentId"], "pages": "3-4"},
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.output["selectedPages"], [3, 4])
        self.assertIn("Page 3 Course Marker", result.output["content"])
        self.assertIn("Page 4 Course Marker", result.output["content"])
        self.assertNotIn("Page 2 Course Marker", result.output["content"])
        self.assertEqual([item["page"] for item in result.output["citations"]], [3, 4])
        self.assertTrue(all(
            item["attachmentId"] == metadata["attachmentId"]
            for item in result.output["citations"]
        ))

    def test_scanned_pdf_page_uses_local_ocr(self):
        import fitz

        document = fitz.open()
        document.new_page()
        pdf_bytes = document.tobytes()
        document.close()
        metadata = AttachmentStore(self.sessions).save(
            "default", "scan.pdf", "application/pdf", BytesIO(pdf_bytes)
        )
        self.advanced.local_ocr.extract_image = lambda image, filename: {
            "text": "扫描页课程信息", "blocks": [], "processing": "local_rapidocr"
        }
        result = self.registry.execute(
            "read_attachment", {"attachment_id": metadata["attachmentId"]},
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success)
        self.assertIn("扫描页课程信息", result.output["content"])
        self.assertEqual(result.output["ocrPages"], [1])
        self.assertEqual(result.output["citations"][0]["method"], "local_ocr")

    def test_read_text_session_attachment(self):
        metadata = AttachmentStore(self.sessions).save(
            "default", "notes.md", "text/markdown", BytesIO("课程重点：事务与索引".encode("utf-8"))
        )
        result = self.registry.execute(
            "read_attachment", {"attachment_id": metadata["attachmentId"]},
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success)
        self.assertIn("事务与索引", result.output["content"])
        self.assertEqual(result.output["citations"][0]["label"], "[A1]")

    def test_long_text_attachment_can_resume_from_next_offset(self):
        original = "开头-" + ("甲乙丙丁" * 35_000) + "-结尾"
        metadata = AttachmentStore(self.sessions).save(
            "default", "long-notes.txt", "text/plain", BytesIO(original.encode("utf-8"))
        )
        first = self.registry.execute(
            "read_attachment", {"attachment_id": metadata["attachmentId"]},
            ToolExecutionContext("default"),
        )
        self.assertTrue(first.success)
        self.assertTrue(first.output["truncated"])
        self.assertEqual(first.output["nextOffset"], len(first.output["content"]))

        chunks = [first.output["content"]]
        current = first
        while current.output["truncated"]:
            current = self.registry.execute(
                "read_attachment",
                {
                    "attachment_id": metadata["attachmentId"],
                    "start_char": current.output["nextOffset"],
                },
                ToolExecutionContext("default"),
            )
            self.assertTrue(current.success)
            chunks.append(current.output["content"])
        self.assertEqual("".join(chunks), original)

    def test_long_office_attachments_can_resume_from_next_offset(self):
        cases = {
            "long.docx": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "word/document.xml",
                (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                    'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{}</w:t>'
                    "</w:r></w:p></w:body></w:document>"
                ),
            ),
            "long.pptx": (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "ppt/slides/slide1.xml",
                (
                    '<p:sld xmlns:p="http://schemas.openxmlformats.org/'
                    'presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/'
                    'drawingml/2006/main"><p:cSld><a:t>{}</a:t></p:cSld></p:sld>'
                ),
            ),
        }
        for filename, (content_type, member, template) in cases.items():
            with self.subTest(filename=filename):
                marker = "课程内容" * 40_000
                archive_bytes = BytesIO()
                with ZipFile(archive_bytes, "w") as archive:
                    archive.writestr(member, template.format(marker))
                metadata = AttachmentStore(self.sessions).save(
                    "default", filename, content_type, BytesIO(archive_bytes.getvalue())
                )
                first = self.registry.execute(
                    "read_attachment", {"attachment_id": metadata["attachmentId"]},
                    ToolExecutionContext("default"),
                )
                self.assertTrue(first.success)
                self.assertTrue(first.output["truncated"])
                self.assertIsInstance(first.output["nextOffset"], int)
                chunks = [first.output["content"]]
                current = first
                while current.output["truncated"]:
                    current = self.registry.execute(
                        "read_attachment",
                        {
                            "attachment_id": metadata["attachmentId"],
                            "start_char": current.output["nextOffset"],
                        },
                        ToolExecutionContext("default"),
                    )
                    self.assertTrue(current.success)
                    chunks.append(current.output["content"])
                combined = "".join(chunks)
                self.assertIn(marker, combined)

    def test_scanned_pdf_continues_after_per_call_ocr_page_budget(self):
        import fitz

        document = fitz.open()
        for _ in range(25):
            document.new_page()
        pdf_bytes = document.tobytes()
        document.close()
        metadata = AttachmentStore(self.sessions).save(
            "default", "long-scan.pdf", "application/pdf", BytesIO(pdf_bytes)
        )
        self.advanced.local_ocr.extract_image = lambda image, filename: {
            "text": f"OCR {filename}", "blocks": [], "processing": "local_rapidocr"
        }
        first = self.registry.execute(
            "read_attachment", {"attachment_id": metadata["attachmentId"]},
            ToolExecutionContext("default"),
        )
        self.assertTrue(first.success)
        self.assertTrue(first.output["truncated"])
        self.assertEqual(first.output["includedPages"], list(range(1, 21)))
        self.assertEqual(first.output["nextPage"], 21)

        second = self.registry.execute(
            "read_attachment",
            {
                "attachment_id": metadata["attachmentId"],
                "start_page": first.output["nextPage"],
            },
            ToolExecutionContext("default"),
        )
        self.assertTrue(second.success)
        self.assertFalse(second.output["truncated"])
        self.assertEqual(second.output["includedPages"], list(range(21, 26)))

    def test_ocr_image_uses_local_ocr(self):
        service = AdvancedTools(
            self.sessions, self.workspaces, self.shells, self.downloads
        )
        calls = []
        service.local_ocr.extract = lambda path, filename: (
            calls.append((path, filename))
            or {
                "text": "课程名称：数据库系统",
                "blocks": [{"text": "课程名称：数据库系统", "confidence": 0.99}],
                "processing": "local_rapidocr",
                "citations": [{"label": "[O1]", "kind": "ocr_block", "filename": filename, "block": 1}],
            }
        )
        registry = create_read_only_registry(service)
        register_advanced_tools(registry, service)
        metadata = AttachmentStore(self.sessions).save(
            "default", "schedule.png", "image/png", BytesIO(b"fake-png-for-request-shape")
        )
        result = registry.execute(
            "ocr_image",
            {"attachment_id": metadata["attachmentId"]},
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success)
        self.assertIn("数据库系统", result.output["text"])
        self.assertEqual(result.output["processing"], "local_rapidocr")
        self.assertEqual(result.output["citations"][0]["label"], "[O1]")
        self.assertEqual(calls[0][1], "schedule.png")
        self.assertEqual(
            result.output["citations"][0]["attachmentId"],
            metadata["attachmentId"],
        )

    def test_ocr_image_reads_workspace_image(self):
        image_path = self.workspace / "screenshots" / "formula.png"
        image_path.parent.mkdir()
        image_path.write_bytes(b"fake-workspace-image")
        calls = []
        self.advanced.local_ocr.extract = lambda path, filename: (
            calls.append((path, filename))
            or {
                "text": "x² + y² = 1",
                "blocks": [{"text": "x² + y² = 1", "confidence": 0.98}],
                "processing": "local_rapidocr",
                "citations": [
                    {
                        "label": "[O1]",
                        "kind": "ocr_block",
                        "filename": filename,
                        "block": 1,
                    }
                ],
            }
        )
        result = self.registry.execute(
            "ocr_image",
            {"path": "screenshots/formula.png"},
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.output["source"], "workspace")
        self.assertEqual(result.output["path"], str(Path("screenshots") / "formula.png"))
        self.assertEqual(result.output["text"], "x² + y² = 1")
        self.assertEqual(result.output["citations"][0]["kind"], "workspace_ocr")
        self.assertEqual(
            result.output["citations"][0]["path"],
            str(Path("screenshots") / "formula.png"),
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].samefile(image_path))
        self.assertEqual(calls[0][1], "formula.png")

    def test_ocr_image_requires_exactly_one_source(self):
        context = ToolExecutionContext("default")
        missing = self.registry.execute("ocr_image", {}, context)
        self.assertFalse(missing.success)
        self.assertIn("必须且只能提供一个", missing.error)

        both = self.registry.execute(
            "ocr_image",
            {"attachment_id": "att_missing", "path": "image.png"},
            context,
        )
        self.assertFalse(both.success)
        self.assertIn("必须且只能提供一个", both.error)

    def test_ocr_image_workspace_path_cannot_escape(self):
        result = self.registry.execute(
            "ocr_image",
            {"path": "../outside.png"},
            ToolExecutionContext("default"),
        )
        self.assertFalse(result.success)
        self.assertIn("Workspace 边界", result.error)

    def test_read_pptx_from_workspace(self):
        pptx = self.workspace / "slides.pptx"
        with ZipFile(pptx, "w") as archive:
            archive.writestr(
                "ppt/slides/slide1.xml",
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:t>课程标题</a:t><a:t>核心结论</a:t></p:sld>',
            )
        result = self.registry.execute(
            "read_document", {"path": "slides.pptx"}, ToolExecutionContext("default")
        )
        self.assertTrue(result.success)
        self.assertIn("第 1 页", result.output["content"])
        self.assertIn("核心结论", result.output["content"])
        self.assertEqual(result.output["citations"][0]["label"], "[S1]")

    def test_read_docx_directly_from_session_attachment(self):
        data = BytesIO()
        with ZipFile(data, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>实验报告正文</w:t></w:r></w:p></w:body></w:document>',
            )
        data.seek(0)
        metadata = AttachmentStore(self.sessions).save(
            "default", "report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", data
        )
        result = self.registry.execute(
            "read_document",
            {"attachment_id": metadata["attachmentId"]},
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.output["source"], "attachment")
        self.assertIn("实验报告正文", result.output["content"])
        self.assertEqual(result.output["citations"][0]["label"], "[D1]")

    def test_document_reader_rejects_xml_entity_declarations(self):
        docx = self.workspace / "unsafe.docx"
        with ZipFile(docx, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<!DOCTYPE x [<!ENTITY boom "expanded">]>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>&boom;</w:t></w:r></w:p></w:body></w:document>',
            )
        result = self.registry.execute(
            "read_document", {"path": "unsafe.docx"}, ToolExecutionContext("default")
        )
        self.assertFalse(result.success)
        self.assertIn("XML 实体", result.error)

    def test_workspace_rejects_absolute_and_parent_escape(self):
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            self.workspaces.resolve("default", str(self.root / "outside.txt"))
        with self.assertRaisesRegex(ValueError, "Workspace 边界"):
            self.workspaces.resolve("default", "../outside.txt")

    def test_workspace_accepts_absolute_path_inside_its_own_boundary(self):
        target = self.workspace / "nested" / "notes.txt"
        resolved = self.workspaces.resolve("default", str(target))
        self.assertEqual(resolved, target.resolve())

    def test_read_and_update_tools_are_workspace_scoped(self):
        context = ToolExecutionContext("default")
        (self.workspace / "a.txt").write_text("old", encoding="utf-8")
        read = self.registry.execute("read_file", {"path": "a.txt"}, context)
        self.assertTrue(read.success)
        self.assertEqual(read.output["content"], "old")
        absolute_list = self.registry.execute(
            "list_dir", {"path": str(self.workspace)}, context
        )
        self.assertTrue(absolute_list.success)
        self.assertEqual(absolute_list.output["path"], ".")
        escaped = self.registry.execute("read_file", {"path": "../outside.txt"}, context)
        self.assertFalse(escaped.success)
        created = self.registry.execute(
            "create_file", {"path": "nested/new.txt", "content": "hello"}, context
        )
        self.assertTrue(created.success)
        self.registry.execute(
            "edit_file",
            {"path": "nested/new.txt", "old_text": "hello", "new_text": "world"},
            context,
        )
        self.assertEqual((self.workspace / "nested/new.txt").read_text(encoding="utf-8"), "world")

    def test_read_file_supports_precise_line_ranges(self):
        target = self.workspace / "long.txt"
        target.write_text(
            "".join(f"line {index}\n" for index in range(1, 31)),
            encoding="utf-8",
        )
        result = self.registry.execute(
            "read_file",
            {"path": "long.txt", "start_line": 20, "end_line": 23},
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(
            result.output["content"].splitlines(),
            ["line 20", "line 21", "line 22", "line 23"],
        )
        self.assertEqual(result.output["startLine"], 20)
        self.assertEqual(result.output["endLine"], 23)
        self.assertEqual(result.output["totalLines"], 30)

    def test_apply_patch_updates_and_adds_files_transactionally(self):
        (self.workspace / "report.md").write_text(
            "# Report\n\nOld paragraph.\n\nKeep this.\n",
            encoding="utf-8",
        )
        patch = """*** Begin Patch
*** Update File: report.md
@@
 # Report
 
-Old paragraph.
+Revised paragraph.
 
 Keep this.
*** Add File: notes/checklist.md
+# Checklist
+
+- [ ] Verify sources
*** End Patch"""
        result = self.registry.execute(
            "apply_patch",
            {"patch": patch},
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.output["filesChanged"], 2)
        self.assertEqual(result.output["linesAdded"], 4)
        self.assertEqual(result.output["linesRemoved"], 1)
        self.assertEqual(
            (self.workspace / "report.md").read_text(encoding="utf-8"),
            "# Report\n\nRevised paragraph.\n\nKeep this.\n",
        )
        self.assertEqual(
            (self.workspace / "notes" / "checklist.md").read_text(encoding="utf-8"),
            "# Checklist\n\n- [ ] Verify sources\n",
        )

    def test_apply_patch_conflict_keeps_every_file_unchanged(self):
        target = self.workspace / "report.md"
        target.write_text("current text\n", encoding="utf-8")
        patch = """*** Begin Patch
*** Add File: should-not-exist.txt
+temporary
*** Update File: report.md
@@
-stale text
+replacement
*** End Patch"""
        result = self.registry.execute(
            "apply_patch",
            {"patch": patch},
            ToolExecutionContext("default"),
        )
        self.assertFalse(result.success)
        self.assertIn("上下文不匹配", result.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "current text\n")
        self.assertFalse((self.workspace / "should-not-exist.txt").exists())

    def test_apply_patch_supports_move_delete_and_crlf_preservation(self):
        source = self.workspace / "old.txt"
        source.write_bytes(b"one\r\ntwo\r\n")
        obsolete = self.workspace / "obsolete.txt"
        obsolete.write_text("remove me\n", encoding="utf-8")
        patch = """*** Begin Patch
*** Update File: old.txt
*** Move to: archive/new.txt
@@
 one
-two
+second
*** Delete File: obsolete.txt
*** End Patch"""
        result = self.registry.execute(
            "apply_patch",
            {"patch": patch},
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        self.assertFalse(source.exists())
        self.assertFalse(obsolete.exists())
        self.assertEqual(
            (self.workspace / "archive" / "new.txt").read_bytes(),
            b"one\r\nsecond\r\n",
        )

    def test_apply_patch_rejects_escape_and_ambiguous_context(self):
        escaped = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Add File: ../outside.txt\n"
                    "+no\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertFalse(escaped.success)
        self.assertIn("不能包含 ..", escaped.error)

        target = self.workspace / "duplicate.txt"
        target.write_text("same\nmiddle\nsame\n", encoding="utf-8")
        ambiguous = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: duplicate.txt\n"
                    "@@\n"
                    "-same\n"
                    "+changed\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertFalse(ambiguous.success)
        self.assertIn("无法安全确定位置", ambiguous.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "same\nmiddle\nsame\n")

    def test_apply_patch_uses_exact_context_when_section_label_is_stale(self):
        target = self.workspace / "report.md"
        target.write_text(
            "# Report\n\nOriginal paragraph.\n",
            encoding="utf-8",
        )
        result = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: report.md\n"
                    "@@ -3,1 +3,1 @@ paraphrased heading not in file\n"
                    "-Original paragraph.\n"
                    "+Revised paragraph.\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "# Report\n\nRevised paragraph.\n",
        )

    def test_apply_patch_rejects_noop_and_reports_actual_context(self):
        target = self.workspace / "report.md"
        target.write_text("first\nactual\nthird\n", encoding="utf-8")
        noop = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: report.md\n"
                    "@@\n"
                    " first\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertFalse(noop.success)
        self.assertIn("没有任何增删内容", noop.error)

        mismatch = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: report.md\n"
                    "@@ -2,1 +2,1 @@\n"
                    "-guessed\n"
                    "+replacement\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertFalse(mismatch.success)
        self.assertIn("hunk #1", mismatch.error)
        self.assertIn("actual_context", mismatch.error)
        self.assertIn("2: actual", mismatch.error)

    def test_apply_patch_repairs_unprefixed_numbered_whole_file_replacement(self):
        target = self.workspace / "report.md"
        target.write_text("old one\nold two\n", encoding="utf-8")
        result = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: report.md\n"
                    "@@ -1,2 +1,3 @@\n"
                    " new one\n"
                    " new two\n"
                    " new three\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "new one\nnew two\nnew three\n",
        )
        self.assertEqual(result.output["linesAdded"], 3)
        self.assertEqual(result.output["linesRemoved"], 2)

    def test_apply_patch_accepts_split_file_header(self):
        target = self.workspace / "report.md"
        target.write_text("old\n", encoding="utf-8")
        result = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File\n"
                    "*** File: report.md\n"
                    "@@\n"
                    "-old\n"
                    "+new\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_apply_patch_uses_explicit_path_for_missing_header_path(self):
        target = self.workspace / "report.md"
        target.write_text("old\n", encoding="utf-8")
        result = self.registry.execute(
            "apply_patch",
            {
                "path": "report.md",
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File\n"
                    "@@\n"
                    "-old\n"
                    "+new\n"
                    "*** End Patch"
                ),
            },
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_apply_patch_uses_latest_read_file_for_missing_header_path(self):
        target = self.workspace / "report.md"
        target.write_text("old\n", encoding="utf-8")
        session = self.sessions.get("default")
        session.messages.append(
            {
                "role": "user",
                "content": (
                    '[tool_results] [{"tool":"read_file","success":true,'
                    '"output":{"path":"report.md","content":"old\\n"}}]'
                ),
            }
        )
        self.sessions.save(session)
        result = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File\n"
                    "@@\n"
                    "-old\n"
                    "+new\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_apply_patch_does_not_guess_unprefixed_local_replacement(self):
        target = self.workspace / "report.md"
        target.write_text("first\nold\nthird\n", encoding="utf-8")
        result = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: report.md\n"
                    "@@ -2,1 +2,1 @@\n"
                    " replacement\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertFalse(result.success)
        self.assertIn("局部修改必须显式使用 - 和 +", result.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "first\nold\nthird\n")

    def test_apply_patch_relaxes_only_unique_trailing_whitespace(self):
        target = self.workspace / "report.md"
        target.write_text("title  \nold\n", encoding="utf-8")
        result = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: report.md\n"
                    "@@\n"
                    " title\n"
                    "-old\n"
                    "+new\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "title\nnew\n")

    def test_apply_patch_accepts_wrapped_protocol_and_quoted_path(self):
        target = self.workspace / "report.md"
        target.write_text("old\n", encoding="utf-8")
        result = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "下面是补丁：\n"
                    "*** Begin Patch\n"
                    "*** Update File: `report.md`\n"
                    "@@\n"
                    "-old\n"
                    "+new\n"
                    "*** End Patch\n"
                    "补丁结束。"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_apply_patch_accepts_bare_header_followed_by_raw_path(self):
        target = self.workspace / "report.md"
        target.write_text("old\n", encoding="utf-8")
        result = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File:\n"
                    "report.md\n"
                    "@@\n"
                    "-old\n"
                    "+new\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_apply_patch_recovers_unique_change_when_outer_context_drifted(self):
        target = self.workspace / "report.md"
        target.write_text(
            "# Current heading\n\nunique old sentence\n\nCurrent footer\n",
            encoding="utf-8",
        )
        result = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: report.md\n"
                    "@@\n"
                    " # Stale heading\n"
                    " \n"
                    "-unique old sentence\n"
                    "+replacement sentence\n"
                    " \n"
                    " Stale footer\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertTrue(result.success, result.error)
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            "# Current heading\n\nreplacement sentence\n\nCurrent footer\n",
        )

    def test_apply_patch_does_not_recover_ambiguous_changed_block(self):
        target = self.workspace / "report.md"
        target.write_text("same\nmiddle\nsame\n", encoding="utf-8")
        result = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: report.md\n"
                    "@@\n"
                    " stale context\n"
                    "-same\n"
                    "+changed\n"
                    " stale ending\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertFalse(result.success)
        self.assertIn("上下文不匹配", result.error)
        self.assertIn('"start_line"', result.error)
        self.assertEqual(target.read_text(encoding="utf-8"), "same\nmiddle\nsame\n")

    def test_apply_patch_rejects_semantic_noop(self):
        target = self.workspace / "report.md"
        target.write_text("same\n", encoding="utf-8")
        result = self.registry.execute(
            "apply_patch",
            {
                "patch": (
                    "*** Begin Patch\n"
                    "*** Update File: report.md\n"
                    "@@\n"
                    "-same\n"
                    "+same\n"
                    "*** End Patch"
                )
            },
            ToolExecutionContext("default"),
        )
        self.assertFalse(result.success)
        self.assertIn("没有产生实际变化", result.error)

    def test_workspace_required_for_advanced_tools(self):
        session = self.sessions.create("no workspace")
        result = self.registry.execute(
            "create_file",
            {"path": "x.txt", "content": "x"},
            ToolExecutionContext(session.session_id),
        )
        self.assertFalse(result.success)
        self.assertIn("尚未设置 Workspace", result.error)

    def test_attachment_copy_cannot_cross_sessions(self):
        attachments = AttachmentStore(self.sessions)
        metadata = attachments.save(
            "default", "notes.txt", "text/plain", BytesIO(b"session private")
        )
        other = self.sessions.create("other")
        self.workspaces.set(other.session_id, str(self.workspace))
        denied = self.registry.execute(
            "copy_attachment_to_workspace",
            {"attachment_id": metadata["attachmentId"], "target_path": "stolen.txt"},
            ToolExecutionContext(other.session_id),
        )
        self.assertFalse(denied.success)
        allowed = self.registry.execute(
            "copy_attachment_to_workspace",
            {"attachment_id": metadata["attachmentId"], "target_path": "copied.txt"},
            ToolExecutionContext("default"),
        )
        self.assertTrue(allowed.success)
        self.assertEqual((self.workspace / "copied.txt").read_bytes(), b"session private")

    def test_copy_file_supports_workspace_files_directories_and_overwrite_guard(self):
        source = self.workspace / "reports" / "weekly.txt"
        source.parent.mkdir()
        source.write_text("v1", encoding="utf-8")
        destination_dir = self.workspace / "archive"
        destination_dir.mkdir()
        context = ToolExecutionContext("default")

        copied = self.registry.execute(
            "copy_file",
            {"source_path": "reports/weekly.txt", "target_path": "archive"},
            context,
        )
        self.assertTrue(copied.success, copied.error)
        self.assertEqual(copied.output["source"], "reports\\weekly.txt")
        self.assertEqual(copied.output["path"], "archive\\weekly.txt")
        self.assertEqual((destination_dir / "weekly.txt").read_text(encoding="utf-8"), "v1")

        duplicate = self.registry.execute(
            "copy_file",
            {"source_path": "reports/weekly.txt", "target_path": "archive"},
            context,
        )
        self.assertFalse(duplicate.success)
        self.assertIn("Destination already exists", duplicate.error)

        source.write_text("v2", encoding="utf-8")
        overwritten = self.registry.execute(
            "copy_file",
            {
                "source_path": "reports/weekly.txt",
                "target_path": "archive/weekly.txt",
                "overwrite": True,
            },
            context,
        )
        self.assertTrue(overwritten.success, overwritten.error)
        self.assertTrue(overwritten.output["overwritten"])
        self.assertEqual((destination_dir / "weekly.txt").read_text(encoding="utf-8"), "v2")

        escaped = self.registry.execute(
            "copy_file",
            {"source_path": "../outside.txt", "target_path": "archive/outside.txt"},
            context,
        )
        self.assertFalse(escaped.success)
        self.assertIn("Workspace", escaped.error)

    def test_download_only_resolves_workspace_file(self):
        (self.workspace / "report.md").write_text("report", encoding="utf-8")
        result = self.registry.execute(
            "create_download", {"path": "report.md"}, ToolExecutionContext("default")
        )
        self.assertTrue(result.success)
        item = self.downloads.resolve(result.output["downloadId"])
        self.assertEqual(Path(item["path"]).read_text(encoding="utf-8"), "report")
        escaped = self.registry.execute(
            "create_download", {"path": "../outside.md"}, ToolExecutionContext("default")
        )
        self.assertFalse(escaped.success)

    def test_persistent_shell_and_cwd_escape_termination(self):
        context = ToolExecutionContext("default")
        started = self.registry.execute("new_shell", {}, context)
        self.assertTrue(started.success)
        first = self.registry.execute(
            "run_command", {"command": "$env:SJTUCLAW_TEST='persisted'"}, context
        )
        self.assertTrue(first.success, first.error)
        second = self.registry.execute(
            "run_command", {"command": "Write-Output $env:SJTUCLAW_TEST"}, context
        )
        self.assertIn("persisted", second.output["stdout"])
        escaped = self.registry.execute("run_command", {"command": "Set-Location .."}, context)
        self.assertFalse(escaped.output["success"])
        self.assertIn("离开 Workspace", escaped.output["error"])
        after = self.registry.execute("run_command", {"command": "pwd"}, context)
        self.assertFalse(after.success)
        self.assertIn("new_shell", after.error)


class ApprovalStoreConcurrencyTests(unittest.TestCase):
    def test_only_one_worker_can_claim_the_same_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ApprovalStore(Path(directory))
            approval = store.create(
                "batch_concurrent",
                "default",
                "create_file",
                {"path": "once.txt", "content": "one side effect"},
                turn_id="turn_concurrent",
                call_id="call_concurrent",
            )
            barrier = Barrier(3)
            results = []
            errors = []

            def claim(token):
                try:
                    barrier.wait(timeout=3)
                    results.append(store.claim(approval.approval_id, token))
                except Exception as exc:  # pragma: no cover - diagnostic path
                    errors.append(exc)

            workers = [
                Thread(target=claim, args=(f"worker_{index}",))
                for index in range(2)
            ]
            for worker in workers:
                worker.start()
            barrier.wait(timeout=3)
            for worker in workers:
                worker.join(timeout=3)

            self.assertFalse(errors)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(sum(1 for _, acquired in results if acquired), 1)
            current = store.get(approval.approval_id)
            self.assertEqual(current.status, "executing")
            self.assertIn(current.executor_token, {"worker_0", "worker_1"})
            self.assertEqual(current.turn_id, "turn_concurrent")
            self.assertEqual(current.call_id, "call_concurrent")


class ApprovalTests(Step8Base):
    def test_approval_resume_keeps_successful_preapproval_evidence(self):
        runtime, model = self.make_runtime([
            json.dumps({
                "type": "tool_calls",
                "calls": [
                    {"tool": "current_time", "args": {}},
                    {
                        "tool": "create_file",
                        "args": {"path": "scheduled.txt", "content": "done"},
                    },
                ],
            }),
            json.dumps({"type": "final", "content": "done"}),
        ])
        observed = []

        def capture_evidence(content, requirements, evidence, **kwargs):
            observed.append(evidence)
            return SimpleNamespace(valid=True, missing=(), reason=None)

        with patch("runtime.validate_completion", side_effect=capture_evidence):
            turn = runtime.run("check the time and create a file", "default")
            self.assertEqual(turn.status, "approval_required")
            final = runtime.resolve_approval(
                turn.pending_approvals[0]["approvalId"],
                True,
            )

        self.assertTrue(final.reply.startswith("done"))
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(observed), 1)
        self.assertIn("current_time", observed[0].successful_tools)
        self.assertIn("create_file", observed[0].successful_tools)
        self.assertIn("search", observed[0].successful_capabilities)
        self.assertIn("write", observed[0].successful_capabilities)

    def test_mixed_read_only_and_write_batch_does_not_mark_read_result_pending(self):
        runtime, _ = self.make_runtime([
            json.dumps({
                "type": "tool_calls",
                "calls": [
                    {"tool": "current_time", "args": {}},
                    {
                        "tool": "create_file",
                        "args": {"path": "mixed.txt", "content": "yes"},
                    },
                ],
            }),
        ])

        turn = runtime.run("读取时间后创建文件", "default")

        self.assertEqual(turn.status, "approval_required")
        protocol_messages = [
            item["content"] for item in self.sessions.current.messages
            if item.get("content", "").startswith("[")
        ]
        self.assertTrue(any(item.startswith("[tool_results]") for item in protocol_messages))
        self.assertFalse(any(item.startswith("[approval_required]") for item in protocol_messages))
        observation = next(
            item for item in protocol_messages if item.startswith("[tool_results]")
        )
        self.assertIn('"tool": "current_time"', observation)
        self.assertIn('"success": true', observation)
        self.assertIn('"approvalRequired": true', observation)

    def test_copy_file_waits_for_approval_then_resumes(self):
        (self.workspace / "source.txt").write_text("approved copy", encoding="utf-8")
        runtime, model = self.make_runtime([
            '{"type":"tool_call","tool":"copy_file","args":{"source_path":"source.txt","target_path":"copies/"}}',
            '{"type":"final","content":"复制完成"}',
        ])
        turn = runtime.run("复制文件", "default")
        self.assertEqual(turn.status, "approval_required")
        self.assertFalse((self.workspace / "copies" / "source.txt").exists())
        approval_id = turn.pending_approvals[0]["approvalId"]
        final = runtime.resolve_approval(approval_id, True)
        self.assertIn("复制完成", final.reply)
        self.assertIn("[⬇ 下载 source.txt](/api/downloads/", final.reply)
        self.assertEqual(
            (self.workspace / "copies" / "source.txt").read_text(encoding="utf-8"),
            "approved copy",
        )
        self.assertEqual(len(model.calls), 2)

    def test_apply_patch_waits_for_approval_then_resumes(self):
        target = self.workspace / "draft.md"
        target.write_text("# Draft\n\nOld.\n", encoding="utf-8")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: draft.md\n"
            "@@\n"
            " # Draft\n"
            " \n"
            "-Old.\n"
            "+New.\n"
            "*** End Patch"
        )
        runtime, model = self.make_runtime([
            json.dumps({
                "type": "tool_call",
                "tool": "apply_patch",
                "args": {"patch": patch},
            }, ensure_ascii=False),
            '{"type":"final","content":"增量修改完成。"}',
        ])
        turn = runtime.run("修改草稿", "default")
        self.assertEqual(turn.status, "approval_required")
        self.assertEqual(target.read_text(encoding="utf-8"), "# Draft\n\nOld.\n")
        self.assertEqual(turn.pending_approvals[0]["tool"], "apply_patch")

        final = runtime.resolve_approval(
            turn.pending_approvals[0]["approvalId"],
            True,
        )
        self.assertIn("增量修改完成。", final.reply)
        self.assertEqual(target.read_text(encoding="utf-8"), "# Draft\n\nNew.\n")
        self.assertEqual(len(model.calls), 2)

    def test_write_is_not_executed_before_approval_then_resumes(self):
        runtime, model = self.make_runtime([
            '{"type":"tool_call","tool":"create_file","args":{"path":"approved.txt","content":"yes"}}',
            '{"type":"final","content":"文件创建完成。"}',
        ])
        turn = runtime.run("创建文件", "default")
        self.assertEqual(turn.status, "approval_required")
        self.assertFalse((self.workspace / "approved.txt").exists())
        approval_id = turn.pending_approvals[0]["approvalId"]
        final = runtime.resolve_approval(approval_id, True)
        self.assertIn("文件创建完成。", final.reply)
        self.assertIn("[⬇ 下载 approved.txt](/api/downloads/", final.reply)
        self.assertEqual((self.workspace / "approved.txt").read_text(), "yes")
        self.assertEqual(self.approvals.get(approval_id).status, "approved")
        self.assertIn("approval_result", str(self.sessions.current.messages))
        self.assertEqual(len(model.calls), 2)

    def test_existing_model_download_link_is_not_duplicated(self):
        content = "[下载 linked.txt](/api/downloads/dl_existing)"
        events = [{
            "result": {
                "success": True,
                "output": {
                    "filename": "linked.txt",
                    "downloadUrl": "/api/downloads/dl_existing",
                },
            },
        }]
        self.assertEqual(
            AgentRuntime._with_download_links(content, events),
            content,
        )

    def test_model_relative_download_link_is_normalized_to_authoritative_url(self):
        content = "[点击下载报告](downloads/dl_relative)"
        events = [{
            "result": {
                "success": True,
                "output": {
                    "filename": "report.md",
                    "downloadUrl": "/api/downloads/dl_relative",
                },
            },
        }]
        self.assertEqual(
            AgentRuntime._with_download_links(content, events),
            "[点击下载报告](/api/downloads/dl_relative)",
        )

    def test_bare_download_label_is_bound_to_authoritative_tool_url(self):
        content = "👉 [**点击下载 report.md**]（15 分钟内有效）"
        events = [{
            "result": {
                "success": True,
                "output": {
                    "filename": "report.md",
                    "downloadUrl": "/api/downloads/dl_bare",
                },
            },
        }]
        self.assertEqual(
            AgentRuntime._with_download_links(content, events),
            "👉 [**点击下载 report.md**](/api/downloads/dl_bare)（15 分钟内有效）",
        )

    def test_current_turn_download_survives_later_approval_resume(self):
        session = self.sessions.current
        session.messages.extend([
            {
                "role": "user",
                "content": "生成报告并统计大小",
                "metadata": {"source": "web"},
            },
            {
                "role": "user",
                "content": "[approval_result] " + json.dumps({
                    "approvalId": "approval_create",
                    "decision": "approved",
                    "toolResult": {
                        "tool": "create_file",
                        "success": True,
                        "output": {
                            "filename": "report.md",
                            "downloadUrl": "/api/downloads/dl_created",
                        },
                    },
                }, ensure_ascii=False),
            },
            {
                "role": "user",
                "content": "[approval_result] " + json.dumps({
                    "approvalId": "approval_shell",
                    "decision": "approved",
                    "toolResult": {
                        "tool": "run_command",
                        "success": True,
                        "output": {"exitCode": 0},
                    },
                }, ensure_ascii=False),
            },
        ])
        events = AgentRuntime._current_turn_tool_events(session, [])
        repaired = AgentRuntime._with_download_links(
            "[点击下载 report.md]（15 分钟内有效）",
            events,
        )
        self.assertEqual(
            repaired,
            "[点击下载 report.md](/api/downloads/dl_created)（15 分钟内有效）",
        )

    def test_model_fabricated_download_id_is_replaced_by_real_tool_link(self):
        content = "文件已生成：[点击下载](downloads/dl_wrong)"
        events = [{
            "result": {
                "success": True,
                "output": {
                    "filename": "answer.md",
                    "downloadUrl": "/api/downloads/dl_real",
                },
            },
        }]
        normalized = AgentRuntime._with_download_links(content, events)
        self.assertNotIn("dl_wrong", normalized)
        self.assertIn("[⬇ 下载 answer.md](/api/downloads/dl_real)", normalized)

    def test_rejection_never_executes_and_reason_reaches_model(self):
        runtime, model = self.make_runtime([
            '{"type":"tool_call","tool":"create_file","args":{"path":"denied.txt","content":"no"}}',
            '{"type":"final","content":"操作已取消。"}',
        ])
        turn = runtime.run("创建文件")
        approval_id = turn.pending_approvals[0]["approvalId"]
        final = runtime.resolve_approval(approval_id, False, "不要修改项目")
        self.assertFalse((self.workspace / "denied.txt").exists())
        self.assertEqual(final.reply, "操作已取消。")
        self.assertIn("不要修改项目", str(model.calls[1]))
        self.assertEqual(self.approvals.get(approval_id).status, "rejected")

    def test_cancel_during_approved_tool_settles_plan_and_claim(self):
        runtime, _ = self.make_runtime([
            '{"type":"tool_call","tool":"create_file","args":{"path":"cancelled.txt","content":"no"}}',
        ])
        turn = runtime.run(
            "\u5148\u521b\u5efa cancelled.txt\uff0c\u7136\u540e\u6838\u5bf9\u7ed3\u679c"
        )
        approval_id = turn.pending_approvals[0]["approvalId"]
        cancellation = Event()

        def cancel_instead_of_execute(name, args, context):
            cancellation.set()
            return ToolResult(name, True, output={"unexpected": True})

        runtime.tool_registry.execute = cancel_instead_of_execute
        with self.assertRaises(AgentCancelled):
            runtime.resolve_approval(
                approval_id,
                True,
                turn_id=turn.turn_id,
                cancellation_event=cancellation,
            )

        self.assertFalse((self.workspace / "cancelled.txt").exists())
        self.assertEqual(self.approvals.get(approval_id).status, "interrupted")
        self.assertEqual(
            self.approvals.get(approval_id).execution_status, "unknown"
        )
        goal = self.sessions.current.goal_state
        self.assertEqual(goal["status"], "cancelled")
        self.assertEqual(goal["plan"]["status"], "cancelled")
        self.assertTrue(all(
            step["status"] not in {"pending", "in_progress", "awaiting_approval"}
            for step in goal["plan"]["steps"]
        ))

    def test_duplicate_approval_is_a_noop(self):
        calls = []
        runtime, model = self.make_runtime([
            '{"type":"tool_call","tool":"create_file","args":{"path":"once.txt","content":"yes"}}',
            '{"type":"final","content":"文件创建完成。"}',
        ])
        original_execute = runtime.tool_registry.execute

        def execute_once(name, args, context):
            if name == "create_file":
                calls.append(args.copy())
            return original_execute(name, args, context)

        runtime.tool_registry.execute = execute_once
        turn = runtime.run("创建文件")
        approval_id = turn.pending_approvals[0]["approvalId"]
        first = runtime.resolve_approval(approval_id, True)
        second = runtime.resolve_approval(approval_id, True)
        self.assertIn("文件创建完成。", first.reply)
        self.assertIn("[⬇ 下载 once.txt](/api/downloads/", first.reply)
        self.assertTrue(second.already_resolved)
        self.assertIsNone(second.reply)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(model.calls), 2)

    def test_approval_resume_model_failure_is_persisted_as_recoverable_reply(self):
        # The Tool succeeds, but the provider fails on the follow-up call
        # (the real-world analogue is an SJTU API 429 after approval).
        runtime, _ = self.make_runtime([
            '{"type":"tool_call","tool":"create_file","args":{"path":"resume.txt","content":"ok"}}',
        ])
        turn = runtime.run("创建文件")
        events = []
        resumed = runtime.resolve_approval(
            turn.pending_approvals[0]["approvalId"],
            True,
            event_callback=events.append,
            turn_id="turn_resume_failure",
        )
        self.assertEqual(resumed.status, "completed")
        self.assertIn("审批已完成", resumed.reply)
        self.assertIn("工具结果已保存", resumed.reply)
        self.assertTrue((self.workspace / "resume.txt").exists())
        self.assertTrue(any(item["type"] == "assistant_final" for item in events))
        self.assertIn("审批已完成", self.sessions.current.messages[-1]["content"])

    def test_failed_approved_tool_retries_and_requests_new_approval(self):
        runtime, model = self.make_runtime([
            '{"type":"tool_call","tool":"create_file","args":{"path":"../bad.txt","content":"x"}}',
            '{"type":"final","content":"好的，我用正确的相对路径重新执行。"}',
            '{"type":"tool_call","tool":"create_file","args":{"path":"retried.txt","content":"ok"}}',
            '{"type":"final","content":"文件已经创建完成。"}',
        ])
        first = runtime.run("创建文件")
        events = []
        resumed = runtime.resolve_approval(
            first.pending_approvals[0]["approvalId"], True,
            event_callback=events.append, turn_id="turn_retry",
        )
        self.assertEqual(resumed.status, "approval_required")
        self.assertEqual(resumed.pending_approvals[0]["args"]["path"], "retried.txt")
        event_types = [item["type"] for item in events]
        self.assertIn("tool_result", event_types)
        self.assertIn("assistant_reset", event_types)
        self.assertIn("approval_required", event_types)
        final = runtime.resolve_approval(resumed.pending_approvals[0]["approvalId"], True)
        self.assertIn("文件已经创建完成。", final.reply)
        self.assertIn("[⬇ 下载 retried.txt](/api/downloads/", final.reply)
        self.assertEqual((self.workspace / "retried.txt").read_text(), "ok")

    def test_multiple_approvals_wait_until_batch_resolved(self):
        runtime, model = self.make_runtime([
            '{"type":"tool_calls","calls":['
            '{"tool":"create_file","args":{"path":"one.txt","content":"1"}},'
            '{"tool":"create_file","args":{"path":"two.txt","content":"2"}}]}',
            '{"type":"final","content":"两项已处理。"}',
        ])
        turn = runtime.run("创建两个文件")
        self.assertEqual(len(turn.pending_approvals), 2)
        first = runtime.resolve_approval(turn.pending_approvals[0]["approvalId"], True)
        self.assertEqual(first.status, "approval_required")
        self.assertEqual(len(model.calls), 1)
        second = runtime.resolve_approval(first.pending_approvals[0]["approvalId"], False, "只要一个")
        self.assertIn("两项已处理。", second.reply)
        self.assertIn("[⬇ 下载 one.txt](/api/downloads/", second.reply)
        self.assertTrue((self.workspace / "one.txt").exists())
        self.assertFalse((self.workspace / "two.txt").exists())

    def test_cli_can_review_and_approve(self):
        runtime, _ = self.make_runtime([
            '{"type":"tool_call","tool":"create_file","args":{"path":"cli.txt","content":"cli"}}',
            '{"type":"final","content":"CLI 完成。"}',
        ])
        inputs = iter(["请创建文件", "y", "/exit"])
        outputs = []
        result = run_cli(runtime, input_fn=lambda _: next(inputs), output_fn=outputs.append)
        self.assertEqual(result, 0)
        self.assertTrue((self.workspace / "cli.txt").exists())
        self.assertTrue(any("approval_required" in line for line in outputs))


class Step8ApiTests(Step8Base):
    def test_two_selected_attachments_generate_downloadable_workspace_file(self):
        runtime, model = self.make_runtime([
            '{"type":"tool_call","tool":"create_file","args":{"path":"answer.md","content":"# Answer\\ncombined"}}',
            '{"type":"final","content":"已生成 [answer.md](/api/downloads/from-tool-result)"}',
        ])
        client = TestClient(create_app(runtime, web_dir=Path(__file__).parents[1] / "web"))
        selected = []
        for filename, content in (
            ("requirements.txt", b"first attachment evidence"),
            ("template.md", b"second attachment evidence"),
        ):
            response = client.post(
                "/api/sessions/default/attachments",
                files={"file": (filename, content, "text/plain")},
            )
            self.assertEqual(response.status_code, 201)
            metadata = response.json()["attachment"]
            selected.append({
                "attachmentId": metadata["attachmentId"],
                "filename": metadata["filename"],
                "contentType": metadata["contentType"],
            })

        message = (
            "请读取两份附件，在 Workspace 生成 answer.md，并给出下载链接。\n"
            "[attached_files] " + json.dumps(selected, ensure_ascii=False)
        )
        chat = client.post("/api/chat", json={"sessionId": "default", "message": message})
        self.assertEqual(chat.json()["status"], "approval_required")
        model_context = json.dumps(model.calls[0], ensure_ascii=False)
        self.assertIn("first attachment evidence", model_context)
        self.assertIn("second attachment evidence", model_context)

        approval_id = chat.json()["pendingApprovals"][0]["approvalId"]
        decision = client.post(
            f"/api/approvals/{approval_id}/decision",
            json={"approved": True},
        )
        self.assertEqual(decision.status_code, 200)
        write_event = next(
            event
            for event in reversed(self.sessions.get("default").tool_trace)
            if event.get("tool") == "create_file"
        )
        output = write_event["result"]["output"]
        self.assertEqual(output["path"], "answer.md")
        downloaded = client.get(output["downloadUrl"])
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.text.splitlines(), ["# Answer", "combined"])
        client.close()

    def test_workspace_approval_and_download_apis(self):
        runtime, _ = self.make_runtime([
            '{"type":"tool_call","tool":"create_file","args":{"path":"api.txt","content":"api"}}',
            '{"type":"final","content":"done"}',
        ])
        client = TestClient(create_app(runtime, web_dir=Path(__file__).parents[1] / "web"))
        workspace = client.get("/api/sessions/default/workspace")
        self.assertEqual(workspace.json()["workspace"], str(self.workspace.resolve()))
        chat = client.post("/api/chat", json={"sessionId": "default", "message": "create"})
        self.assertEqual(chat.json()["status"], "approval_required")
        approval_id = chat.json()["pendingApprovals"][0]["approvalId"]
        decision = client.post(
            f"/api/approvals/{approval_id}/decision",
            json={"approved": True},
        )
        self.assertIn("done", decision.json()["reply"])
        self.assertIn("[⬇ 下载 api.txt](/api/downloads/", decision.json()["reply"])
        repeated = client.post(
            f"/api/approvals/{approval_id}/decision",
            json={"approved": True},
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.json()["alreadyResolved"])
        self.assertTrue((self.workspace / "api.txt").exists())

        write_event = next(
            event
            for event in reversed(self.sessions.get("default").tool_trace)
            if event.get("tool") == "create_file"
        )
        download = write_event["result"]["output"]
        self.assertTrue(download["downloadId"].startswith("dl_"))
        self.assertEqual(download["filename"], "api.txt")
        response = client.get(download["downloadUrl"])
        self.assertEqual(response.content, b"api")
        client.close()


if __name__ == "__main__":
    unittest.main()
