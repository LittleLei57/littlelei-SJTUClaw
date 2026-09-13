"""Safe browser preview classification and bounded text extraction."""

from __future__ import annotations

import base64
from pathlib import Path

from document_reader import extract_document


IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}
AUDIO_TYPES = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}
VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".ogv": "video/ogg"}
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".html", ".css", ".json", ".yaml", ".yml", ".xml", ".csv", ".log",
    ".ini", ".toml", ".env", ".gitignore", ".dockerignore", ".sql", ".java",
    ".c", ".h", ".cpp", ".hpp", ".go", ".rs", ".sh", ".ps1",
}
MAX_TEXT_BYTES = 400_000
MAX_TEXT_CHARS = 120_000
MAX_PDF_PREVIEW_PAGES = 5
MAX_PDF_PREVIEW_PIXELS = 2_000_000


def preview_attachment(path: Path, metadata: dict, raw_url: str, download_url: str) -> dict:
    filename = str(metadata.get("filename") or "attachment")
    suffix = Path(filename).suffix.lower()
    base = {
        "attachmentId": metadata.get("attachmentId"), "filename": filename,
        "size": metadata.get("size"), "contentType": metadata.get("contentType"),
        "downloadUrl": download_url,
    }
    if suffix in IMAGE_TYPES:
        return {**base, "kind": "image", "rawUrl": raw_url}
    if suffix == ".pdf":
        return {**base, "kind": "pdf", "rawUrl": raw_url}
    if suffix in AUDIO_TYPES:
        return {**base, "kind": "audio", "rawUrl": raw_url}
    if suffix in VIDEO_TYPES:
        return {**base, "kind": "video", "rawUrl": raw_url}
    if suffix in {".docx", ".pptx"}:
        extracted = extract_document(path, filename)
        return {
            **base, "kind": "document", "content": extracted["content"],
            "format": extracted["format"], "truncated": extracted["truncated"],
        }
    if suffix in TEXT_SUFFIXES or str(metadata.get("contentType") or "").startswith("text/"):
        raw = path.read_bytes()[:MAX_TEXT_BYTES + 1]
        if b"\x00" in raw[:4096]:
            return {**base, "kind": "unsupported"}
        for encoding in ("utf-8", "utf-8-sig", "gb18030"):
            try:
                content = raw[:MAX_TEXT_BYTES].decode(encoding)
                return {
                    **base, "kind": "text", "content": content[:MAX_TEXT_CHARS],
                    "truncated": len(raw) > MAX_TEXT_BYTES or len(content) > MAX_TEXT_CHARS,
                }
            except UnicodeDecodeError:
                continue
    return {**base, "kind": "unsupported"}


def inline_media_type(filename: str) -> str | None:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    return IMAGE_TYPES.get(suffix) or AUDIO_TYPES.get(suffix) or VIDEO_TYPES.get(suffix)


def render_pdf_preview_pages(path: Path, max_pages: int = MAX_PDF_PREVIEW_PAGES) -> dict:
    try:
        import fitz
    except ImportError as exc:
        raise ValueError("缺少 pymupdf，无法渲染 PDF 预览页。") from exc

    pages = []
    with fitz.open(path) as document:
        page_count = document.page_count
        for index in range(min(page_count, max_pages)):
            page = document.load_page(index)
            rect = page.rect
            scale = 1.35
            if rect.width * rect.height * scale * scale > MAX_PDF_PREVIEW_PIXELS:
                scale = (MAX_PDF_PREVIEW_PIXELS / max(rect.width * rect.height, 1)) ** 0.5
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            png = pixmap.tobytes("png")
            pages.append({
                "page": index + 1,
                "width": pixmap.width,
                "height": pixmap.height,
                "src": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            })
    return {
        "pageCount": page_count,
        "renderedPages": len(pages),
        "truncated": page_count > len(pages),
        "pages": pages,
    }
