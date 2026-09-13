"""Local image OCR with bounded decoding and lazy RapidOCR initialization."""

from __future__ import annotations

from pathlib import Path
import sys
from threading import Lock
from typing import Any

MAX_IMAGE_PIXELS = 40_000_000
MAX_SIDE = 4_096
MAX_BLOCKS = 500


class LocalOCR:
    def __init__(self):
        self._engine = None
        self._lock = Lock()

    def _get_engine(self):
        if self._engine is None:
            try:
                from rapidocr import RapidOCR
            except ImportError as exc:
                raise RuntimeError(
                    "缺少本地 OCR 依赖，请执行：python -m pip install 'rapidocr>=3.4,<4'"
                ) from exc
            try:
                self._engine = RapidOCR()
            except (ImportError, ModuleNotFoundError, RuntimeError) as exc:
                missing_onnx = (
                    isinstance(exc, ModuleNotFoundError)
                    and getattr(exc, "name", None) == "onnxruntime"
                )
                if "onnxruntime" not in str(exc).lower() and not missing_onnx:
                    raise
                raise RuntimeError(
                    "本地 OCR 缺少 ONNX 推理后端。请在启动 Gateway 的同一个 Python 环境中执行："
                    f"{sys.executable} -m pip install 'onnxruntime>=1.18,<2'，然后重启 Gateway。"
                ) from exc
        return self._engine

    def extract(self, path: Path, filename: str) -> dict:
        try:
            from PIL import Image, ImageOps, UnidentifiedImageError
        except ImportError as exc:
            raise RuntimeError(
                "缺少本地 OCR 图片依赖，请执行：python -m pip install -r requirements.txt"
            ) from exc
        try:
            with Image.open(path) as source:
                width, height = source.size
                if width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片像素过大，OCR 已拒绝处理。")
                image = ImageOps.exif_transpose(source).convert("RGB")
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError(f"附件不是可识别的图片：{filename}") from exc
        return self.extract_image(image, filename, (width, height))

    def extract_image(
        self, image: Any, filename: str, original_size: tuple[int, int] | None = None
    ) -> dict:
        try:
            import numpy as np
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError(
                "缺少本地 OCR 运行依赖，请执行：python -m pip install -r requirements.txt"
            ) from exc
        image = image.convert("RGB")
        width, height = original_size or image.size
        if width * height > MAX_IMAGE_PIXELS:
            raise ValueError("图片像素过大，OCR 已拒绝处理。")
        if max(image.size) > MAX_SIDE:
            image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
        with self._lock:
            result = self._get_engine()(np.asarray(image))
        texts = list(result.txts or ())
        scores = list(result.scores or ())
        boxes = list(result.boxes) if result.boxes is not None else []
        blocks = []
        for index, text in enumerate(texts[:MAX_BLOCKS]):
            blocks.append({
                "text": text,
                "confidence": round(float(scores[index]), 4) if index < len(scores) else None,
                "box": boxes[index].astype(int).tolist() if index < len(boxes) else None,
            })
        return {
            "filename": filename,
            "text": "\n".join(texts[:MAX_BLOCKS]),
            "blocks": blocks,
            "blockCount": len(texts),
            "averageConfidence": (
                round(sum(float(score) for score in scores) / len(scores), 4) if scores else None
            ),
            "truncated": len(texts) > MAX_BLOCKS,
            "imageSize": {"width": width, "height": height},
            "processing": "local_rapidocr",
            "citations": [
                {
                    "label": f"[O{index}]",
                    "kind": "ocr_block",
                    "filename": filename,
                    "block": index,
                    "confidence": block.get("confidence"),
                }
                for index, block in enumerate(blocks, start=1)
            ],
        }
