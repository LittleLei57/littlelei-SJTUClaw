"""Bounded PDF text extraction with local OCR fallback for scanned pages."""

from __future__ import annotations

from pathlib import Path
import math

from PIL import Image
from pypdf import PdfReader

from local_ocr import LocalOCR


MAX_PAGES = 100
MAX_OCR_PAGES = 20
# Keep one PDF Tool Result within the active-turn context budget.  Returning a
# larger chunk only to truncate it before the next model call silently loses
# evidence; page-level continuation is safer.
MAX_OUTPUT_CHARS = 60_000
MIN_NATIVE_TEXT = 20


def extract_pdf(
    path: Path,
    filename: str,
    ocr: LocalOCR,
    *,
    pages: str | None = None,
    start_page: int | None = None,
    end_page: int | None = None,
) -> dict:
    try:
        reader = PdfReader(str(path), strict=False)
    except Exception as exc:
        raise ValueError(f"PDF 文件损坏或无法解析：{filename}") from exc
    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise ValueError("PDF 已加密，请先解密后重新上传。")
        except Exception as exc:
            raise ValueError("PDF 已加密，请先解密后重新上传。") from exc

    total_pages = len(reader.pages)
    page_indexes = _select_page_indexes(total_pages, pages, start_page, end_page)
    processed_page_indexes = page_indexes[:MAX_PAGES]
    native_texts: list[str] = []
    scanned_indexes: list[int] = []
    for index in processed_page_indexes:
        page = reader.pages[index]
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        native_texts.append(text)
        if len(text) < MIN_NATIVE_TEXT:
            scanned_indexes.append(index)

    ocr_texts: dict[int, str] = {}
    ocr_errors: list[dict] = []
    if scanned_indexes:
        try:
            import fitz

            document = fitz.open(str(path))
            try:
                for index in scanned_indexes[:MAX_OCR_PAGES]:
                    try:
                        page = document.load_page(index)
                        width, height = max(page.rect.width, 1), max(page.rect.height, 1)
                        scale = min(2.0, 4096 / width, 4096 / height, math.sqrt(40_000_000 / (width * height)))
                        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                        result = ocr.extract_image(image, f"{filename} 第 {index + 1} 页")
                        ocr_text = result["text"].strip()
                        native_position = page_indexes.index(index)
                        if len(ocr_text) > len(native_texts[native_position]):
                            ocr_texts[index] = ocr_text
                    except Exception as exc:
                        ocr_errors.append({"pages": [index + 1], "error": str(exc)})
            finally:
                document.close()
        except Exception as exc:
            ocr_errors.append({"pages": [item + 1 for item in scanned_indexes[:MAX_OCR_PAGES]], "error": str(exc)})

    # OCR is bounded per call. Do not silently mark later scanned pages as
    # processed with empty text; stop before the first deferred OCR page so
    # nextPage can resume on it without losing content.
    if len(scanned_indexes) > MAX_OCR_PAGES:
        first_deferred_scan = scanned_indexes[MAX_OCR_PAGES]
        deferred_position = processed_page_indexes.index(first_deferred_scan)
        processed_page_indexes = processed_page_indexes[:deferred_position]
        native_texts = native_texts[:deferred_position]

    sections = []
    page_details = []
    for position, native in enumerate(native_texts):
        index = processed_page_indexes[position]
        text = native if len(native) >= MIN_NATIVE_TEXT else ocr_texts.get(index, native)
        method = "local_ocr" if index in ocr_texts else ("native_text" if native else "empty")
        sections.append(f"## 第 {index + 1} 页\n{text or '（未提取到文字）'}")
        page_details.append({"page": index + 1, "method": method, "characters": len(text)})

    included_sections, content_limited = _fit_sections(sections, MAX_OUTPUT_CHARS)
    included_details = page_details[:len(included_sections)]
    included_pages = [detail["page"] for detail in included_details]
    content = "\n\n".join(included_sections)
    selected_pages = [index + 1 for index in page_indexes]
    next_page = None
    if len(included_pages) < len(selected_pages):
        next_page = selected_pages[len(included_pages)]
    elif page_indexes and page_indexes[-1] + 1 < total_pages and not pages and not start_page and not end_page:
        next_page = page_indexes[-1] + 2
    truncated = (
        len(page_indexes) > MAX_PAGES
        or len(included_pages) < len(selected_pages)
        or content_limited
        or len(scanned_indexes) > MAX_OCR_PAGES
    )
    return {
        "filename": filename,
        "format": "pdf",
        "content": content,
        "pages": total_pages,
        "selectedPages": selected_pages,
        "includedPages": included_pages,
        "processedPages": len(processed_page_indexes),
        "ocrPages": [index + 1 for index in ocr_texts],
        "pageDetails": included_details,
        "ocrErrors": ocr_errors,
        "truncated": truncated,
        "nextPage": next_page,
        "continueHint": (
            f"结果被截断；请继续调用 read_attachment，参数使用 start_page={next_page}。"
            if next_page else (
                "当前单页提取文本超过单次输出上限，页内内容已截断；"
                "请明确告知用户该页未能完整返回。"
                if content_limited else ""
            )
        ),
        "citations": [
            {
                "label": f"[P{detail['page']}]",
                "kind": "pdf_page",
                "filename": filename,
                "page": detail["page"],
                "method": detail["method"],
            }
            for detail in included_details
        ],
    }


def _select_page_indexes(
    total_pages: int,
    pages: str | None,
    start_page: int | None,
    end_page: int | None,
) -> list[int]:
    if total_pages <= 0:
        return []
    if pages:
        selected: list[int] = []
        for part in pages.replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                left, right = [item.strip() for item in part.split("-", 1)]
                if not left.isdigit() or not right.isdigit():
                    raise ValueError(f"页码范围无效：{part}")
                start, end = int(left), int(right)
            else:
                if not part.isdigit():
                    raise ValueError(f"页码无效：{part}")
                start = end = int(part)
            if start < 1 or end < start:
                raise ValueError(f"页码范围无效：{part}")
            selected.extend(range(start - 1, min(end, total_pages)))
        if not selected:
            raise ValueError("pages 未指定有效页码。")
        return sorted(dict.fromkeys(selected))

    start = start_page or 1
    end = end_page or total_pages
    if start < 1:
        raise ValueError("start_page 必须大于等于 1。")
    if end < start:
        raise ValueError("end_page 必须大于等于 start_page。")
    return list(range(start - 1, min(end, total_pages)))


def _fit_sections(sections: list[str], limit: int) -> tuple[list[str], bool]:
    included: list[str] = []
    current = 0
    for section in sections:
        extra = len(section) + (2 if included else 0)
        if current + extra > limit:
            if not included:
                return [section[:limit] + "\n\n[本页内容过长，已在页内截断。]"], True
            return included, True
        included.append(section)
        current += extra
    return included, False
