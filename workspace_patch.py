"""Workspace-scoped, transactional text patch engine.

The accepted format intentionally mirrors the ``*** Begin Patch`` protocol
used by coding agents such as Codex.  It is small enough for an LLM to produce
reliably, but stricter than invoking an external ``patch`` command:

* every path is resolved through :class:`WorkspaceManager`;
* all files and hunks are validated before the first disk mutation;
* update context must match exactly and unambiguously;
* writes use a temporary sibling plus ``os.replace``;
* a failed multi-file commit restores the original files.

Only UTF-8 text is supported.  Binary artifacts continue to use the existing
document/download tools rather than being modified through a textual diff.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Literal

from workspace import WorkspaceManager


MAX_PATCH_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_OPERATIONS = 50

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_OP_RE = re.compile(r"^\*\*\* (Add|Delete|Update) File: (.+)$")
_BARE_OP_RE = re.compile(r"^\*\*\* (Add|Delete|Update) File:?\s*$")
_FILE_RE = re.compile(r"^\*\*\* File: (.+)$")
_MOVE_RE = re.compile(r"^\*\*\* Move to: (.+)$")
_NUMBERED_HUNK_RE = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@(?P<section>.*)$"
)


class PatchError(ValueError):
    """Raised when a patch cannot be validated or safely applied."""


@dataclass(frozen=True)
class Hunk:
    header: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Operation:
    kind: Literal["add", "delete", "update"]
    path: str
    body: tuple[str, ...]
    move_to: str | None = None


@dataclass(frozen=True)
class PlannedChange:
    source: Path
    destination: Path | None
    content: bytes | None
    kind: str
    hunks: int
    lines_added: int
    lines_removed: int


class WorkspacePatchEngine:
    """Parse and atomically apply agent patches inside one Session Workspace."""

    def __init__(self, workspaces: WorkspaceManager):
        self.workspaces = workspaces

    def apply(
        self,
        session_id: str,
        patch: str,
        default_path: str | None = None,
    ) -> dict:
        """Validate an entire patch, then commit it as one recoverable change."""

        operations = parse_patch(patch, default_path=default_path)
        changes = self._plan(session_id, operations)
        self._commit(changes)
        files = []
        for change in changes:
            display_path = change.destination or change.source
            item = {
                "path": self.workspaces.relative(session_id, display_path),
                "operation": change.kind,
                "hunks": change.hunks,
                "linesAdded": change.lines_added,
                "linesRemoved": change.lines_removed,
            }
            if change.destination is not None and change.destination != change.source:
                item["from"] = self.workspaces.relative(session_id, change.source)
            files.append(item)
        return {
            "success": True,
            "tool": "apply_patch",
            "files": files,
            "filesChanged": len(files),
            "linesAdded": sum(item.lines_added for item in changes),
            "linesRemoved": sum(item.lines_removed for item in changes),
            "message": f"Patch 已安全应用到 {len(files)} 个文件。",
        }

    def _plan(self, session_id: str, operations: list[Operation]) -> list[PlannedChange]:
        seen_sources: set[Path] = set()
        seen_destinations: set[Path] = set()
        changes: list[PlannedChange] = []
        for operation in operations:
            source = self._resolve_relative(session_id, operation.path)
            if source in seen_sources:
                raise PatchError(f"同一 Patch 不能重复操作文件：{operation.path}")
            seen_sources.add(source)

            if operation.kind == "add":
                if source.exists():
                    raise PatchError(f"Add File 目标已存在：{operation.path}")
                content, added = _build_added_file(operation)
                destination = source
                change = PlannedChange(
                    source, destination, content, "added", 0, added, 0
                )
            elif operation.kind == "delete":
                self._require_regular_file(source, operation.path)
                _read_utf8(source, operation.path)
                change = PlannedChange(
                    source, None, None, "deleted", 0, 0,
                    _line_count(source.read_bytes()),
                )
            else:
                self._require_regular_file(source, operation.path)
                original, encoding, newline, final_newline = _read_utf8(
                    source, operation.path
                )
                updated, hunk_count, added, removed = _apply_hunks(
                    original, operation, final_newline
                )
                if updated == original:
                    raise PatchError(
                        f"Patch 没有产生实际变化：{operation.path}。"
                        "文件可能已是目标状态；请先 read_file 验证，不要重复提交。"
                    )
                destination = (
                    self._resolve_relative(session_id, operation.move_to)
                    if operation.move_to else source
                )
                if destination != source and destination.exists():
                    raise PatchError(f"Move to 目标已存在：{operation.move_to}")
                content = _encode_text(updated, encoding, newline)
                change = PlannedChange(
                    source,
                    destination,
                    content,
                    "moved" if destination != source else "updated",
                    hunk_count,
                    added,
                    removed,
                )

            if change.destination is not None:
                if change.destination in seen_destinations:
                    raise PatchError(
                        "多个 Patch 操作不能写入同一个目标："
                        f"{self.workspaces.relative(session_id, change.destination)}"
                    )
                seen_destinations.add(change.destination)
            changes.append(change)
        return changes

    def _resolve_relative(self, session_id: str, value: str | None) -> Path:
        path_text = str(value or "").strip()
        if not path_text:
            raise PatchError("Patch 文件路径不能为空。")
        path = Path(path_text)
        if path.is_absolute():
            raise PatchError("apply_patch 只接受相对于 Workspace 的路径。")
        if any(part == ".." for part in path.parts):
            raise PatchError("Patch 路径不能包含 ..。")
        return self.workspaces.resolve(session_id, path_text)

    @staticmethod
    def _require_regular_file(path: Path, display: str) -> None:
        if not path.exists():
            raise PatchError(f"Patch 文件不存在：{display}")
        if not path.is_file():
            raise PatchError(f"Patch 目标不是普通文件：{display}")

    @staticmethod
    def _commit(changes: list[PlannedChange]) -> None:
        """Commit staged files and roll back every path if any write fails."""

        originals: dict[Path, bytes | None] = {}
        modes: dict[Path, int | None] = {}
        touched: set[Path] = set()
        staged: list[tuple[Path, Path, int | None]] = []
        try:
            for change in changes:
                paths = {change.source}
                if change.destination is not None:
                    paths.add(change.destination)
                for path in paths:
                    if path not in originals:
                        originals[path] = path.read_bytes() if path.exists() else None
                        modes[path] = (
                            stat.S_IMODE(path.stat().st_mode) if path.exists() else None
                        )
                if change.content is not None and change.destination is not None:
                    destination = change.destination
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    fd, temp_name = tempfile.mkstemp(
                        prefix=f".{destination.name}.",
                        suffix=".sjtuclaw-patch.tmp",
                        dir=destination.parent,
                    )
                    temp_path = Path(temp_name)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(change.content)
                            stream.flush()
                            os.fsync(stream.fileno())
                        mode = modes.get(change.source)
                        if mode is not None:
                            os.chmod(temp_path, mode)
                        staged.append((temp_path, destination, mode))
                    except Exception:
                        temp_path.unlink(missing_ok=True)
                        raise

            staged_by_destination = {
                destination: temp_path
                for temp_path, destination, _ in staged
            }
            for change in changes:
                if change.destination is not None:
                    temp_path = staged_by_destination[change.destination]
                    os.replace(temp_path, change.destination)
                    touched.add(change.destination)
                if (
                    change.kind in {"deleted", "moved"}
                    and change.source != change.destination
                    and change.source.exists()
                ):
                    change.source.unlink()
                    touched.add(change.source)
        except Exception:
            for temp_path, _, _ in staged:
                temp_path.unlink(missing_ok=True)
            for path in touched | set(originals):
                original = originals[path]
                if original is None:
                    if path.exists() and path.is_file():
                        path.unlink()
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(original)
                if modes[path] is not None:
                    os.chmod(path, modes[path])
            raise
        finally:
            for temp_path, _, _ in staged:
                temp_path.unlink(missing_ok=True)


def parse_patch(
    patch: str,
    default_path: str | None = None,
) -> list[Operation]:
    """Parse the bounded ``*** Begin Patch`` protocol."""

    if not isinstance(patch, str):
        raise PatchError("patch 必须是字符串。")
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise PatchError(f"Patch 超过 {MAX_PATCH_BYTES // (1024 * 1024)} MiB 上限。")
    text = patch.strip().lstrip("\ufeff")
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    # Some compatible models wrap the protocol in one explanatory sentence.
    # Recover only one complete, unambiguous envelope; never guess a truncated
    # patch or silently combine multiple envelopes.
    if not text.startswith(_BEGIN):
        begin_count = text.count(_BEGIN)
        end_count = text.count(_END)
        if begin_count == 1 and end_count == 1:
            begin = text.index(_BEGIN)
            end = text.index(_END, begin) + len(_END)
            text = text[begin:end]
    lines = text.splitlines()
    if not lines or lines[0].strip() != _BEGIN:
        raise PatchError("Patch 必须以 *** Begin Patch 开始。")
    if lines[-1].strip() != _END:
        raise PatchError("Patch 必须以 *** End Patch 结束。")

    operations: list[Operation] = []
    index = 1
    while index < len(lines) - 1:
        directive = lines[index]
        match = _OP_RE.match(directive)
        if match:
            kind = match.group(1).lower()
            path = _clean_patch_path(match.group(2))
            index += 1
        else:
            # Some compatible models split the canonical
            # ``*** Update File: path`` directive into two lines.  Accept that
            # harmless formatting variation, while still requiring a concrete
            # path from either the patch or the guarded single-file fallback.
            bare_match = _BARE_OP_RE.match(directive)
            if not bare_match:
                raise PatchError(f"无法识别 Patch 操作：{lines[index]}")
            kind = bare_match.group(1).lower()
            index += 1
            file_match = (
                _FILE_RE.match(lines[index])
                if index < len(lines) - 1 else None
            )
            if file_match:
                path = _clean_patch_path(file_match.group(1))
                index += 1
            elif (
                index < len(lines) - 1
                and lines[index].strip()
                and not lines[index].lstrip().startswith(("@@", "***"))
                and not lines[index].startswith((" ", "+", "-"))
            ):
                # Common two-line form. The next line cannot be hunk content
                # here because an Update body must begin with @@.
                path = _clean_patch_path(lines[index])
                index += 1
            elif default_path and not operations:
                path = _clean_patch_path(str(default_path))
            else:
                raise PatchError(
                    f"{bare_match.group(1)} File 缺少相对于 Workspace 的文件路径；"
                    "请使用 *** Update File: path，或传入 path 参数。"
                )
        move_to = None
        if kind == "update" and index < len(lines) - 1:
            move_match = _MOVE_RE.match(lines[index])
            if move_match:
                move_to = _clean_patch_path(move_match.group(1))
                index += 1
        body: list[str] = []
        while (
            index < len(lines) - 1
            and not _OP_RE.match(lines[index])
            and not _BARE_OP_RE.match(lines[index])
        ):
            body.append(lines[index])
            index += 1
        operations.append(Operation(kind, path, tuple(body), move_to))
        if len(operations) > MAX_OPERATIONS:
            raise PatchError(f"单次 Patch 最多操作 {MAX_OPERATIONS} 个文件。")
    if not operations:
        raise PatchError("Patch 中没有文件操作。")
    return operations


def _clean_patch_path(value: str) -> str:
    """Remove harmless Markdown quoting around one protocol path."""

    path = str(value or "").strip()
    if len(path) >= 2 and (
        (path[0] == path[-1] and path[0] in {'"', "'", "`"})
        or (path[0], path[-1]) in {("<", ">"), ("[", "]")}
    ):
        path = path[1:-1].strip()
    return path


def _build_added_file(operation: Operation) -> tuple[bytes, int]:
    content: list[str] = []
    for line in operation.body:
        if not line.startswith("+"):
            raise PatchError(
                f"Add File 的每一行必须以 + 开始：{operation.path}"
            )
        content.append(line[1:])
    text = "\n".join(content)
    if content:
        text += "\n"
    return text.encode("utf-8"), len(content)


def _read_utf8(path: Path, display: str) -> tuple[str, str, str, bool]:
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise PatchError(
            f"文本文件超过 {MAX_FILE_BYTES // (1024 * 1024)} MiB 上限：{display}"
        )
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise PatchError(f"apply_patch 只支持 UTF-8 文本：{display}") from exc
    newline = "\r\n" if b"\r\n" in raw else "\n"
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized, encoding, newline, normalized.endswith("\n")


def _encode_text(text: str, encoding: str, newline: str) -> bytes:
    if newline != "\n":
        text = text.replace("\n", newline)
    return text.encode(encoding)


def _line_count(raw: bytes) -> int:
    if not raw:
        return 0
    return len(raw.splitlines())


def _parse_hunks(operation: Operation) -> list[Hunk]:
    hunks: list[Hunk] = []
    header: str | None = None
    body: list[str] = []
    for line in operation.body:
        if line.startswith("@@"):
            if header is not None:
                if not body:
                    raise PatchError(f"空 Patch hunk：{operation.path}")
                hunks.append(Hunk(header, tuple(body)))
            header = line
            body = []
            continue
        if line == "*** End of File":
            continue
        if header is None:
            raise PatchError(
                f"Update File 必须包含以 @@ 开始的 hunk：{operation.path}"
            )
        if not line.startswith((" ", "+", "-")):
            raise PatchError(
                f"Patch hunk 行必须以空格、+ 或 - 开始：{operation.path}"
            )
        body.append(line)
    if header is not None:
        if not body:
            raise PatchError(f"空 Patch hunk：{operation.path}")
        hunks.append(Hunk(header, tuple(body)))
    if not hunks:
        raise PatchError(f"Update File 没有可应用的 hunk：{operation.path}")
    return hunks


def _apply_hunks(
    original: str,
    operation: Operation,
    final_newline: bool,
) -> tuple[str, int, int, int]:
    lines = original.splitlines()
    hunks = _parse_hunks(operation)
    cursor = 0
    added_total = 0
    removed_total = 0
    for hunk_number, hunk in enumerate(hunks, start=1):
        if not any(line.startswith(("+", "-")) for line in hunk.lines):
            implicit = _implicit_full_file_replacement(lines, hunk, cursor)
            if implicit is None:
                raise PatchError(
                    f"Patch hunk 没有任何增删内容：{operation.path}，hunk #{hunk_number}。"
                    "若要替换整份文件，请让编号 hunk 的旧范围覆盖当前全部行，"
                    "并让正文行数等于新范围；局部修改必须显式使用 - 和 +。"
                )
            lines, added, removed, cursor = implicit
            added_total += added
            removed_total += removed
            continue
        old_lines = [line[1:] for line in hunk.lines if not line.startswith("+")]
        new_lines = [line[1:] for line in hunk.lines if not line.startswith("-")]
        if not old_lines:
            raise PatchError(
                f"纯新增 hunk 必须提供至少一行上下文：{operation.path}"
            )
        positions = _match_positions(lines, old_lines, cursor)
        if not positions:
            # Models frequently lose Markdown's two trailing spaces or other
            # harmless end-of-line whitespace while copying read_file output.
            # Accept that difference only when the relaxed match is unique;
            # indentation and all non-whitespace characters remain exact.
            relaxed_positions = _match_positions(
                [line.rstrip() for line in lines],
                [line.rstrip() for line in old_lines],
                cursor,
            )
            if len(relaxed_positions) == 1:
                positions = relaxed_positions
        expected = _expected_old_index(hunk.header)
        section = _hunk_section(hunk.header)
        # Text after ``@@`` is a helpful locator, not part of the actual diff.
        # Models sometimes paraphrase it. Exact hunk context remains the
        # source of truth, so a stale locator must not invalidate a good hunk.
        if section and positions:
            section_positions = [
                index for index in range(cursor, len(lines))
                if section in lines[index]
            ]
            if section_positions:
                lower_bound = section_positions[0]
                located = [
                    position for position in positions if position >= lower_bound
                ]
                if located:
                    positions = located
        if not positions:
            changed_only = _unique_changed_block(lines, hunk, cursor)
            if changed_only is not None:
                position, removed_lines, added_lines = changed_only
                lines[position:position + len(removed_lines)] = added_lines
                cursor = position + len(added_lines)
                added_total += sum(
                    1 for line in hunk.lines if line.startswith("+")
                )
                removed_total += sum(
                    1 for line in hunk.lines if line.startswith("-")
                )
                continue
            raise PatchError(_context_mismatch_message(
                operation.path,
                hunk_number,
                hunk.header,
                old_lines,
                lines,
                expected,
                cursor,
            ))
        if expected is not None:
            position = min(positions, key=lambda item: abs(item - expected))
        elif len(positions) == 1:
            position = positions[0]
        else:
            raise PatchError(
                f"Patch 上下文出现 {len(positions)} 次，无法安全确定位置："
                f"{operation.path}"
            )
        lines[position:position + len(old_lines)] = new_lines
        cursor = position + len(new_lines)
        added_total += sum(1 for line in hunk.lines if line.startswith("+"))
        removed_total += sum(1 for line in hunk.lines if line.startswith("-"))
    updated = "\n".join(lines)
    if final_newline:
        updated += "\n"
    return updated, len(hunks), added_total, removed_total


def _unique_changed_block(
    current_lines: list[str],
    hunk: Hunk,
    cursor: int,
) -> tuple[int, list[str], list[str]] | None:
    """Recover a hunk whose unchanged context drifted but edit block is unique.

    This is intentionally narrower than general fuzzy matching: the hunk must
    contain one contiguous +/- block, must delete at least one line, and those
    deleted lines must occur exactly once after the current cursor. A stale
    heading or nearby comment can therefore be ignored without risking an edit
    to an arbitrary duplicate.
    """

    changed_indexes = [
        index
        for index, line in enumerate(hunk.lines)
        if line.startswith(("+", "-"))
    ]
    if not changed_indexes:
        return None
    first, last = changed_indexes[0], changed_indexes[-1]
    if any(
        not hunk.lines[index].startswith(("+", "-"))
        for index in range(first, last + 1)
    ):
        return None
    changed_lines = hunk.lines[first:last + 1]
    removed = [line[1:] for line in changed_lines if line.startswith("-")]
    added = [line[1:] for line in changed_lines if line.startswith("+")]
    if not removed or removed == added:
        return None
    positions = _match_positions(current_lines, removed, cursor)
    if not positions:
        positions = _match_positions(
            [line.rstrip() for line in current_lines],
            [line.rstrip() for line in removed],
            cursor,
        )
    if len(positions) != 1:
        return None
    return positions[0], removed, added


def _implicit_full_file_replacement(
    current_lines: list[str],
    hunk: Hunk,
    cursor: int,
) -> tuple[list[str], int, int, int] | None:
    """Repair one common model formatting mistake without guessing location.

    Some models emit a numbered whole-file replacement whose body contains the
    desired new file as context lines, but forget every ``+``/``-`` prefix.
    It is safe to recover only when the numbered old range proves that the hunk
    covers the complete current file.  Local no-prefix hunks remain rejected:
    their intended deletion cannot be inferred safely.
    """

    match = _NUMBERED_HUNK_RE.match(hunk.header)
    if match is None or cursor != 0:
        return None
    old_start = int(match.group("old"))
    old_count = int(match.group("old_count") or "1")
    new_start = int(match.group("new"))
    new_count = int(match.group("new_count") or "1")
    desired = [line[1:] for line in hunk.lines]
    if (
        old_start != 1
        or new_start != 1
        or old_count != len(current_lines)
        or len(desired) != new_count
        or desired == current_lines
    ):
        return None
    return desired, new_count, old_count, new_count


def _context_mismatch_message(
    path: str,
    hunk_number: int,
    header: str,
    old_lines: list[str],
    actual_lines: list[str],
    expected: int | None,
    cursor: int,
) -> str:
    """Return a bounded, actionable mismatch report for the next model turn."""

    anchor = expected if expected is not None else cursor
    anchor = min(max(anchor, 0), max(len(actual_lines) - 1, 0))
    start = max(anchor - 3, 0)
    end = min(anchor + max(len(old_lines), 1) + 3, len(actual_lines))
    nearby = "\n".join(
        f"{index + 1:>6}: {actual_lines[index]}"
        for index in range(start, end)
    )
    expected_preview = "\n".join(old_lines[:8])
    if len(old_lines) > 8:
        expected_preview += "\n..."
    return (
        f"Patch 上下文不匹配：{path}，hunk #{hunk_number} ({header})。\n"
        "请使用 read_file 的 start_line/end_line 读取该位置后重新生成；"
        "不要原样重试。\n"
        f'retry_read: {{"path": "{path}", "start_line": {start + 1}, '
        f'"end_line": {max(end, start + 1)}}}\n'
        f"expected_context:\n{expected_preview}\n"
        f"actual_context:\n{nearby or '[文件为空]'}"
    )


def _match_positions(
    haystack: list[str],
    needle: list[str],
    start: int,
) -> list[int]:
    if len(needle) > len(haystack):
        return []
    return [
        index
        for index in range(start, len(haystack) - len(needle) + 1)
        if haystack[index:index + len(needle)] == needle
    ]


def _expected_old_index(header: str) -> int | None:
    match = _NUMBERED_HUNK_RE.match(header)
    return max(int(match.group("old")) - 1, 0) if match else None


def _hunk_section(header: str) -> str:
    match = _NUMBERED_HUNK_RE.match(header)
    if match:
        return match.group("section").strip()
    return header[2:].strip()
