"""Step 8：Workspace 范围内的 Update、Shell、文件处理与 Download Tools。

本模块提供创建/更新文件、复制附件或普通文件、启动 Shell、执行命令、读取
Office/PDF/图片以及生成下载链接等高级能力。所有路径先交给
``WorkspaceManager`` 校验；会改变文件或进程状态的 Tool 标记为需要 Approval，
只读提取仍受大小、超时和输出长度限制。这样 Agent Loop 无需了解具体 I/O，
只处理标准 ``ToolResult``。
"""

from pathlib import Path
import json
import shutil

from download_store import DownloadStore
from session_store import SessionStore
from shell_manager import ShellManager
from tools import Tool, ToolExecutionContext, ToolRegistry
from workspace import WorkspaceManager
from document_reader import extract_document
from local_ocr import LocalOCR
from attachment_store import resolve_attachment_path
from workspace_patch import WorkspacePatchEngine


ATTACHMENT_CHUNK_CHARS = 60_000


class AdvancedTools:
    """实现高级 Tool handler，并复用 Session、Workspace、Shell 和下载服务。"""

    def __init__(
        self,
        sessions: SessionStore,
        workspaces: WorkspaceManager,
        shells: ShellManager,
        downloads: DownloadStore,
    ):
        self.sessions = sessions
        self.workspaces = workspaces
        self.shells = shells
        self.downloads = downloads
        self.local_ocr = LocalOCR()
        self.patch_engine = WorkspacePatchEngine(workspaces)

    def list_dir(self, context: ToolExecutionContext, path: str = ".") -> dict:
        target = self.workspaces.resolve(context.session_id, path, must_exist=True)
        if not target.is_dir():
            raise NotADirectoryError(f"不是目录：{path}")
        entries = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        return {
            "path": self.workspaces.relative(context.session_id, target) or ".",
            "entries": [
                {
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None,
                }
                for item in entries[:200]
            ],
            "truncated": len(entries) > 200,
        }

    def read_file(
        self,
        context: ToolExecutionContext,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict:
        target = self.workspaces.resolve(context.session_id, path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(f"不是文件：{path}")
        if start_line < 1:
            raise ValueError("start_line 必须大于等于 1。")
        if end_line is not None and end_line < start_line:
            raise ValueError("end_line 不能小于 start_line。")
        with target.open("rb") as f:
            raw = f.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError("文本文件超过 read_file 的 10 MB 上限。")
        try:
            full_content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"文件不是 UTF-8 文本：{path}") from exc
        lines = full_content.splitlines(keepends=True)
        requested_end = min(end_line or len(lines), len(lines))
        selected = "".join(lines[start_line - 1:requested_end])
        content = selected[:100_000]
        return {
            "path": self.workspaces.relative(context.session_id, target),
            "content": content,
            "truncated": len(selected) > 100_000,
            "bytesRead": len(content.encode("utf-8")),
            "startLine": start_line,
            "endLine": requested_end,
            "totalLines": len(lines),
        }

    def create_file(self, context: ToolExecutionContext, path: str, content: str) -> dict:
        target = self.workspaces.resolve(context.session_id, path)
        if target.exists():
            raise FileExistsError(f"文件已存在：{path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return self._update_result("create_file", context, target, "文件已创建。")

    def overwrite_file(self, context: ToolExecutionContext, path: str, content: str) -> dict:
        target = self.workspaces.resolve(context.session_id, path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(f"不是文件：{path}")
        target.write_text(content, encoding="utf-8")
        return self._update_result("overwrite_file", context, target, "文件已覆盖。")

    def edit_file(
        self,
        context: ToolExecutionContext,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> dict:
        target = self.workspaces.resolve(context.session_id, path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(f"不是文件：{path}")
        content = target.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            raise ValueError("old_text 在文件中不存在。")
        if count > 1 and not replace_all:
            raise ValueError(f"old_text 出现 {count} 次；请提供更精确文本或设置 replace_all=true。")
        updated = content.replace(old_text, new_text, -1 if replace_all else 1)
        target.write_text(updated, encoding="utf-8")
        return self._update_result("edit_file", context, target, f"已替换 {count if replace_all else 1} 处。")

    def apply_patch(
        self,
        context: ToolExecutionContext,
        patch: str,
        path: str | None = None,
    ) -> dict:
        """Apply one validated multi-file text patch inside the Workspace.

        Parsing, context matching and all-file validation happen before the
        first mutation.  The engine uses temporary sibling files and restores
        originals if a later commit fails, so this is safer than asking the
        model to overwrite a long file in full.
        """

        fallback_path = path or self._latest_read_file_path(context.session_id)
        return self.patch_engine.apply(
            context.session_id,
            patch,
            default_path=fallback_path,
        )

    def _latest_read_file_path(self, session_id: str) -> str | None:
        """Find the latest successful read target for one-file patch repair.

        Some compatible models omit the file path from ``*** Update File``
        after reading that file. Reusing the exact ``read_file`` result is
        deterministic, and the patch engine still validates every hunk before
        it writes anything.
        """

        session = self.sessions.get(session_id)
        prefix = "[tool_results] "
        for message in reversed(session.messages):
            if message.get("role") != "user":
                continue
            content = str(message.get("content") or "")
            if not content.startswith(prefix):
                continue
            try:
                results = json.loads(content[len(prefix):])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for result in reversed(results if isinstance(results, list) else []):
                output = result.get("output") if isinstance(result, dict) else None
                if (
                    isinstance(result, dict)
                    and result.get("tool") == "read_file"
                    and result.get("success") is True
                    and isinstance(output, dict)
                    and output.get("path")
                ):
                    return str(output["path"])
        return None

    def copy_attachment(
        self,
        context: ToolExecutionContext,
        attachment_id: str,
        target_path: str,
    ) -> dict:
        session = self.sessions.get(context.session_id)
        metadata = next(
            (item for item in session.attachments if item.get("attachmentId") == attachment_id),
            None,
        )
        if metadata is None:
            raise KeyError(f"当前 Session 没有附件：{attachment_id}")
        source = resolve_attachment_path(self.sessions, context.session_id, metadata)
        if not source.exists():
            raise FileNotFoundError("附件内容已不存在。")
        target = self.workspaces.resolve(context.session_id, target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return self._update_result(
            "copy_attachment_to_workspace", context, target, f"已从附件 {metadata['filename']} 拷贝。"
        )

    def copy_file(
        self,
        context: ToolExecutionContext,
        source_path: str,
        target_path: str,
        overwrite: bool = False,
    ) -> dict:
        """Copy one Workspace file to another Workspace path."""
        source = self.workspaces.resolve(context.session_id, source_path, must_exist=True)
        if not source.is_file():
            raise IsADirectoryError(f"Source is not a file: {source_path}")

        target_hint = self.workspaces.resolve(context.session_id, target_path)
        target_is_directory = target_hint.is_dir() or str(target_path).endswith(("/", "\\"))
        target = target_hint / source.name if target_is_directory else target_hint
        # Re-resolve after appending the filename to reject symlink escapes.
        target = self.workspaces.resolve(context.session_id, str(target))

        if target == source:
            raise ValueError("Source and destination must be different")
        existed = target.exists()
        if existed and not overwrite:
            raise FileExistsError(
                f"Destination already exists: {self.workspaces.relative(context.session_id, target)}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return {
            **self._update_result("copy_file", context, target, "File copied successfully"),
            "source": self.workspaces.relative(context.session_id, source),
            "overwritten": bool(existed and overwrite),
        }

    def read_document(
        self,
        context: ToolExecutionContext,
        attachment_id: str | None = None,
        path: str | None = None,
    ) -> dict:
        if bool(attachment_id) == bool(path):
            raise ValueError("attachment_id 和 path 必须且只能提供一个。")
        if attachment_id:
            session = self.sessions.get(context.session_id)
            metadata = next(
                (item for item in session.attachments if item.get("attachmentId") == attachment_id),
                None,
            )
            if metadata is None:
                raise KeyError(f"当前 Session 没有附件：{attachment_id}")
            source = resolve_attachment_path(self.sessions, context.session_id, metadata)
            if not source.exists():
                raise FileNotFoundError("附件内容已不存在。")
            result = extract_document(source, metadata["filename"])
            result["source"] = "attachment"
            result["attachmentId"] = attachment_id
            self._bind_attachment_citations(
                result,
                attachment_id=attachment_id,
                filename=metadata["filename"],
            )
            return result
        target = self.workspaces.resolve(context.session_id, path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(f"不是文件：{path}")
        result = extract_document(target)
        result["source"] = "workspace"
        result["path"] = self.workspaces.relative(context.session_id, target)
        return result

    def read_attachment(
        self,
        context: ToolExecutionContext,
        attachment_id: str,
        pages: str | None = None,
        start_page: int | None = None,
        end_page: int | None = None,
        start_char: int | None = None,
        max_chars: int | None = None,
    ) -> dict:
        session = self.sessions.get(context.session_id)
        metadata = next(
            (item for item in session.attachments if item.get("attachmentId") == attachment_id), None
        )
        if metadata is None:
            raise KeyError(f"当前 Session 没有附件：{attachment_id}")
        source = resolve_attachment_path(self.sessions, context.session_id, metadata)
        if not source.is_file():
            raise FileNotFoundError("附件内容不存在。")
        suffix = Path(metadata["filename"]).suffix.lower()
        if suffix == ".pdf":
            # PDF extraction already enforces its own bounded output size.
            # Models sometimes include the text-only ``max_chars`` argument;
            # accepting and ignoring it is safer than failing a valid read
            # and entering a repair loop. ``start_char`` remains invalid
            # because PDF continuation must preserve page boundaries.
            if start_char is not None:
                raise ValueError("PDF 请使用 pages 或 start_page/end_page 续读，不使用 start_char。")
            try:
                from pdf_reader import extract_pdf
            except ImportError as exc:
                raise RuntimeError(
                    "缺少 PDF 解析依赖，请执行：python -m pip install -r requirements.txt"
                ) from exc
            result = extract_pdf(
                source,
                metadata["filename"],
                self.local_ocr,
                pages=pages,
                start_page=start_page,
                end_page=end_page,
            )
            result.update({"source": "attachment", "attachmentId": attachment_id})
            self._bind_attachment_citations(
                result,
                attachment_id=attachment_id,
                filename=metadata["filename"],
            )
            return result
        if suffix in {".docx", ".pptx"}:
            result = extract_document(
                source,
                metadata["filename"],
                start_char=start_char or 0,
                max_chars=min(max_chars or ATTACHMENT_CHUNK_CHARS, ATTACHMENT_CHUNK_CHARS),
            )
            result.update({"source": "attachment", "attachmentId": attachment_id})
            self._bind_attachment_citations(
                result,
                attachment_id=attachment_id,
                filename=metadata["filename"],
            )
            return result
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
            raise ValueError("图片附件请使用 ocr_image 工具提取文字。")
        if pages is not None or start_page is not None or end_page is not None:
            raise ValueError("文本、DOCX、PPTX 请使用 start_char 续读，不使用页码参数。")
        if start_char is not None and (not isinstance(start_char, int) or start_char < 0):
            raise ValueError("start_char 必须是大于等于 0 的整数。")
        if max_chars is not None and (not isinstance(max_chars, int) or max_chars < 1):
            raise ValueError("max_chars 必须是大于等于 1 的整数。")
        raw = source.read_bytes()
        if b"\x00" in raw[:4096]:
            raise ValueError("该附件是二进制文件，当前文本读取器无法解析。")
        for encoding in ("utf-8", "utf-8-sig", "gb18030"):
            try:
                content = raw.decode(encoding)
                start = min(start_char or 0, len(content))
                chunk_size = min(
                    max_chars or ATTACHMENT_CHUNK_CHARS,
                    ATTACHMENT_CHUNK_CHARS,
                )
                end = min(start + chunk_size, len(content))
                truncated = end < len(content)
                return {
                    "source": "attachment", "attachmentId": attachment_id,
                    "filename": metadata["filename"], "format": suffix.lstrip(".") or "text",
                    "content": content[start:end],
                    "truncated": truncated,
                    "startChar": start,
                    "endChar": end,
                    "totalCharacters": len(content),
                    "nextOffset": end if truncated else None,
                    "continueHint": (
                        f"结果被截断；请继续调用 read_attachment，参数使用 start_char={end}。"
                        if truncated else ""
                    ),
                    "citations": [
                        {
                            "label": "[A1]",
                            "kind": "attachment",
                            "filename": metadata["filename"],
                            "attachmentId": attachment_id,
                        }
                    ],
                }
            except UnicodeDecodeError:
                continue
        raise ValueError("无法识别附件文本编码。")

    @staticmethod
    def _bind_attachment_citations(
        result: dict,
        *,
        attachment_id: str,
        filename: str,
    ) -> None:
        """Connect page/slide/section refs to the previewable attachment.

        Extractors own document coordinates while the attachment store owns
        the stable id used by preview/download routes. Persisting both keeps
        ``[P4]`` and ``[P4-P5]`` clickable after reload or compaction.
        """
        citations = result.get("citations")
        if not isinstance(citations, list):
            return
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            citation.setdefault("attachmentId", attachment_id)
            citation.setdefault("filename", filename)

    def ocr_image(
        self,
        context: ToolExecutionContext,
        attachment_id: str | None = None,
        path: str | None = None,
    ) -> dict:
        """OCR either a Session attachment or an image inside its Workspace."""
        if bool(attachment_id) == bool(path):
            raise ValueError("attachment_id 和 path 必须且只能提供一个。")

        metadata = None
        if attachment_id:
            session = self.sessions.get(context.session_id)
            metadata = next(
                (
                    item
                    for item in session.attachments
                    if item.get("attachmentId") == attachment_id
                ),
                None,
            )
            if metadata is None:
                raise KeyError(f"当前 Session 没有附件：{attachment_id}")
            source = resolve_attachment_path(self.sessions, context.session_id, metadata)
            filename = metadata["filename"]
        else:
            source = self.workspaces.resolve(context.session_id, path, must_exist=True)
            if not source.is_file():
                raise IsADirectoryError(f"不是文件：{path}")
            filename = source.name

        suffix = Path(filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
            raise ValueError("ocr_image 仅支持 PNG/JPEG/WEBP/GIF/BMP 图片。")
        if not source.is_file():
            raise FileNotFoundError("附件内容不存在。")
        result = self.local_ocr.extract(source, filename)
        citations = result.get("citations")
        if attachment_id:
            result.update({"source": "attachment", "attachmentId": attachment_id})
            if isinstance(citations, list):
                for citation in citations:
                    if isinstance(citation, dict):
                        citation["attachmentId"] = attachment_id
        else:
            relative_path = self.workspaces.relative(context.session_id, source)
            result.update({"source": "workspace", "path": relative_path})
            if isinstance(citations, list):
                for citation in citations:
                    if isinstance(citation, dict):
                        citation.update(
                            {
                                "kind": "workspace_ocr",
                                "path": relative_path,
                            }
                        )
        return result

    def new_shell(self, context: ToolExecutionContext, cwd: str = ".") -> dict:
        return self.shells.new_shell(context.session_id, cwd)

    def run_command(
        self,
        context: ToolExecutionContext,
        command: str,
        timeout_seconds: int = 30,
    ) -> dict:
        return self.shells.run_command(context.session_id, command, timeout_seconds)

    def create_download(self, context: ToolExecutionContext, path: str) -> dict:
        target = self.workspaces.resolve(context.session_id, path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(f"不是文件：{path}")
        return self.downloads.create(context.session_id, target, target.name)

    def _update_result(self, tool: str, context, target: Path, message: str) -> dict:
        # A successful Workspace write immediately gets a short-lived
        # Gateway download URL. This removes a fragile second model decision
        # ("remember to call create_download") from the delivery path.
        download = self.downloads.create(context.session_id, target, target.name)
        return {
            "success": True,
            "tool": tool,
            "path": self.workspaces.relative(context.session_id, target),
            "message": message,
            **download,
        }


def register_advanced_tools(registry: ToolRegistry, service: AdvancedTools) -> None:
    """把 Step 8 的 Tool schema、风险等级和 handler 注册到共享 Registry。"""

    string_path = {"type": "string"}
    no_extra = False
    registry.register(Tool(
        "create_file", "在当前 Workspace 内创建 UTF-8 文本文件；执行前必须由用户审批。",
        {"type": "object", "properties": {"path": string_path, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": no_extra},
        service.create_file, "approval_required", True,
    ))
    registry.register(Tool(
        "overwrite_file", "覆盖当前 Workspace 内已有 UTF-8 文本文件；执行前必须审批。",
        {"type": "object", "properties": {"path": string_path, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": no_extra},
        service.overwrite_file, "approval_required", True,
    ))
    registry.register(Tool(
        "edit_file", "用精确 old_text 替换 Workspace 文件内容；执行前必须审批。",
        {"type": "object", "properties": {"path": string_path, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["path", "old_text", "new_text"], "additionalProperties": no_extra},
        service.edit_file, "approval_required", True,
    ))
    registry.register(Tool(
        "apply_patch",
        (
            "在当前 Workspace 内以事务方式增量修改一个或多个 UTF-8 文本文件；"
            "适合现有长文件的局部编辑，执行前必须审批。patch 必须使用 "
            "*** Begin Patch / *** End Patch；支持 *** Update File、"
            "*** Add File、*** Delete File，以及 Update 后可选的 *** Move to。"
            "Update 必须包含 @@ hunk，原文行以空格开头、删除行以 - 开头、"
            "新增行以 + 开头；Add File 每行均以 + 开头。路径必须相对于 Workspace。"
            "不要把期望的新文件全文全部写成空格开头的上下文行；局部替换必须同时给出 "
            "-旧内容和 +新内容。仅当编号 hunk 明确覆盖当前整份文件时，引擎才会安全修复"
            "模型漏写增删前缀的整文件替换。"
            "若修改范围超过文件的大半或要整体更换结构，应改用 overwrite_file，"
            "不要用超长 Patch 重写整份文件。"
            "上下文不匹配时会整体失败且不修改任何文件，应先 read_file 后重新生成 Patch。"
            "失败信息会给出 hunk 编号和 actual_context；应使用 read_file 的 "
            "start_line/end_line 精确重读后修正，禁止提交无增删的空 Patch。"
        ),
        {
            "type": "object",
            "properties": {
                "patch": {"type": "string"},
                "path": {
                    "type": "string",
                    "description": (
                        "单文件 Patch 的 Workspace 相对路径。推荐始终提供；"
                        "Patch 头漏写路径时将用作安全回退。"
                    ),
                },
            },
            "required": ["patch"],
            "additionalProperties": no_extra,
        },
        service.apply_patch,
        "approval_required",
        True,
        parallel_safe=False,
        side_effect=True,
        retryable=False,
        max_attempts=1,
        idempotent=False,
    ))
    registry.register(Tool(
        "copy_attachment_to_workspace", "把当前 Session 的附件拷贝到 Workspace；执行前必须审批。",
        {"type": "object", "properties": {"attachment_id": string_path, "target_path": string_path}, "required": ["attachment_id", "target_path"], "additionalProperties": no_extra},
        service.copy_attachment, "approval_required", True,
    ))
    registry.register(Tool(
        "copy_file", "将当前 Workspace 内的任意文件复制到 Workspace 内的指定文件或目录；目标为已有目录或以 / 结尾时保留原文件名，默认不覆盖；执行前必须审批。",
        {
            "type": "object",
            "properties": {
                "source_path": string_path,
                "target_path": string_path,
                "overwrite": {"type": "boolean"},
            },
            "required": ["source_path", "target_path"],
            "additionalProperties": no_extra,
        },
        service.copy_file, "approval_required", True,
    ))
    registry.register(Tool(
        "read_document", "读取当前 Session 附件或 Workspace 中的 DOCX/PPTX，提取段落、表格文本、幻灯片和讲者备注。",
        {"type": "object", "properties": {"attachment_id": string_path, "path": string_path}, "additionalProperties": no_extra},
        service.read_document, "read_only", True,
    ))
    registry.register(Tool(
        "read_attachment", "直接读取当前 Session 的文本、代码、Markdown、CSV、JSON、DOCX、PPTX 或 PDF 附件；PDF 使用 pages 或 start_page/end_page 按页续读，扫描页自动本地 OCR；TXT/DOCX/PPTX 使用 start_char（取上轮 nextOffset）分段续读。若返回 truncated=true，必须继续读取缺失内容。",
        {
            "type": "object",
            "properties": {
                "attachment_id": string_path,
                "pages": string_path,
                "start_page": {"type": "integer"},
                "end_page": {"type": "integer"},
                "start_char": {"type": "integer"},
                "max_chars": {"type": "integer"},
            },
            "required": ["attachment_id"],
            "additionalProperties": no_extra,
        },
        service.read_attachment, "read_only", True,
    ))
    registry.register(Tool(
        "ocr_image", "完全在本地提取图片中的文字。必须且只能提供 attachment_id（当前 Session 图片附件）或 path（当前 Workspace 内图片的相对路径）之一。OCR 不能理解无文字的图形、人物或场景，不得据此猜测。",
        {
            "type": "object",
            "properties": {
                "attachment_id": string_path,
                "path": string_path,
            },
            "additionalProperties": no_extra,
        },
        service.ocr_image, "read_only", True,
    ))
    registry.register(Tool(
        "new_shell", "启动或替换当前 Session 的持久 PowerShell，cwd 优先使用相对 Workspace 的路径；位于当前 Workspace 内的绝对路径也会安全归一化。执行前必须审批。",
        {"type": "object", "properties": {"cwd": string_path}, "additionalProperties": no_extra},
        service.new_shell, "approval_required", True,
    ))
    registry.register(Tool(
        "run_command", "在已启动的持久 Shell 中运行命令；执行前必须审批。",
        {"type": "object", "properties": {"command": string_path, "timeout_seconds": {"type": "integer"}}, "required": ["command"], "additionalProperties": no_extra},
        service.run_command, "approval_required", True,
    ))
    registry.register(Tool(
        "create_download", "为 Workspace 内已有文件创建 15 分钟有效的 Gateway 下载入口。",
        {"type": "object", "properties": {"path": string_path}, "required": ["path"], "additionalProperties": no_extra},
        service.create_download, "download", True,
    ))
