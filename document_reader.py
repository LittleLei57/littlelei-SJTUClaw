"""Bounded text extraction for OOXML office documents."""

from __future__ import annotations

from pathlib import Path
import re
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


MAX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_CHARS = 120_000
MAX_MEMBERS = 2_000


def extract_document(
    path: Path,
    filename: str | None = None,
    *,
    start_char: int = 0,
    max_chars: int = MAX_OUTPUT_CHARS,
) -> dict:
    """Extract one bounded, resumable chunk from a DOCX or PPTX document.

    ``start_char`` is an offset into the normalized extracted text, rather
    than an OOXML byte offset.  This keeps continuation stable across calls
    and lets callers concatenate chunks without gaps or overlaps.
    """
    if not isinstance(start_char, int) or start_char < 0:
        raise ValueError("start_char 必须是大于等于 0 的整数。")
    if not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError("max_chars 必须是大于等于 1 的整数。")
    max_chars = min(max_chars, MAX_OUTPUT_CHARS)
    display_name = filename or path.name
    suffix = Path(display_name).suffix.lower()
    if suffix not in {".docx", ".pptx"}:
        raise ValueError("当前文档解析器支持 .docx 和 .pptx。")
    try:
        with ZipFile(path) as archive:
            _validate_archive(archive)
            sections = _extract_docx(archive) if suffix == ".docx" else _extract_pptx(archive)
    except BadZipFile as exc:
        raise ValueError(f"文件不是有效的 {suffix} 文档：{display_name}") from exc
    content = "\n\n".join(sections).strip()
    total_characters = len(content)
    start = min(start_char, total_characters)
    end = min(start + max_chars, total_characters)
    truncated = end < total_characters
    return {
        "filename": display_name,
        "format": suffix[1:],
        "content": content[start:end],
        "sections": len(sections),
        "truncated": truncated,
        "startChar": start,
        "endChar": end,
        "totalCharacters": total_characters,
        "nextOffset": end if truncated else None,
        "continueHint": (
            f"结果被截断；请继续调用 read_attachment，参数使用 start_char={end}。"
            if truncated else ""
        ),
        "citations": _citations(display_name, suffix, sections),
    }


def _validate_archive(archive: ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ValueError("Office 文档内部文件数量异常，已拒绝解析。")
    if sum(item.file_size for item in infos) > MAX_EXPANDED_BYTES:
        raise ValueError("Office 文档解压后体积过大，已拒绝解析。")
    if any(item.flag_bits & 0x1 for item in infos):
        raise ValueError("暂不支持加密的 Office 文档。")


def _read_xml(archive: ZipFile, name: str):
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_MEMBER_BYTES:
        raise ValueError(f"Office 文档内部 XML 过大：{name}")
    try:
        raw = archive.read(info)
        lowered = raw.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise ValueError("Office 文档包含不安全的 XML 实体声明，已拒绝解析。")
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Office 文档 XML 损坏：{name}") from exc


def _extract_docx(archive: ZipFile) -> list[str]:
    names = ["word/document.xml"] + sorted(
        name for name in archive.namelist()
        if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
    )
    sections = []
    word_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    for name in names:
        root = _read_xml(archive, name)
        if root is None:
            continue
        paragraphs = []
        for paragraph in root.iter(word_ns + "p"):
            text = "".join(node.text or "" for node in paragraph.iter(word_ns + "t")).strip()
            if text:
                paragraphs.append(text)
        if paragraphs:
            label = "正文" if name == "word/document.xml" else Path(name).stem
            sections.append(f"## {label}\n" + "\n".join(paragraphs))
    return sections


def _extract_pptx(archive: ZipFile) -> list[str]:
    slide_names = sorted(
        (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
        key=_numeric_suffix,
    )
    sections = []
    drawing_text = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
    for index, name in enumerate(slide_names, 1):
        root = _read_xml(archive, name)
        texts = [node.text.strip() for node in root.iter(drawing_text) if node.text and node.text.strip()]
        notes_name = f"ppt/notesSlides/notesSlide{index}.xml"
        notes_root = _read_xml(archive, notes_name)
        notes = [] if notes_root is None else [
            node.text.strip() for node in notes_root.iter(drawing_text)
            if node.text and node.text.strip()
        ]
        body = "\n".join(texts) or "（无可提取文本）"
        if notes:
            body += "\n\n讲者备注：\n" + "\n".join(notes)
        sections.append(f"## 第 {index} 页\n{body}")
    if not sections:
        raise ValueError("PPTX 中没有找到幻灯片。")
    return sections


def _numeric_suffix(name: str) -> int:
    match = re.search(r"(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def _citations(filename: str, suffix: str, sections: list[str]) -> list[dict]:
    if suffix == ".pptx":
        citations = []
        for index, section in enumerate(sections, start=1):
            match = re.match(r"## 第 (\d+) 页", section)
            page = int(match.group(1)) if match else index
            citations.append({
                "label": f"[S{page}]",
                "kind": "slide",
                "filename": filename,
                "slide": page,
            })
        return citations
    return [
        {
            "label": f"[D{index}]",
            "kind": "document_section",
            "filename": filename,
            "section": index,
        }
        for index, _ in enumerate(sections, start=1)
    ]
