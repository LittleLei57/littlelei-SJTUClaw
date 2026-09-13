"""Build transient OpenAI-compatible image blocks for multimodal models."""

from __future__ import annotations

from base64 import b64encode
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps


SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MAX_IMAGE_SIDE = 1600


def is_image_attachment(metadata: dict[str, Any]) -> bool:
    content_type = str(metadata.get("contentType") or "").lower()
    filename = str(metadata.get("filename") or "")
    return content_type.startswith("image/") or Path(filename).suffix.lower() in SUPPORTED_IMAGE_SUFFIXES


def image_content_part(path: Path, *, detail: str = "auto") -> dict[str, Any]:
    """Encode one image for an API request without changing its disk file."""
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        if getattr(image, "is_animated", False):
            image.seek(0)
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        output = BytesIO()
        image.save(output, format="JPEG", quality=88, optimize=True)
    encoded = b64encode(output.getvalue()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:image/jpeg;base64,{encoded}",
            "detail": detail,
        },
    }
