"""验证图片附件到多模态模型输入的转换。"""

from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from attachment_store import AttachmentStore
from context_builder import ContextBuilder
from runtime import AgentRuntime
from session_store import SessionStore
from tools import Tool, ToolRegistry


class VisionModel:
    supports_vision = True

    def __init__(self):
        self.messages = None

    def complete(self, messages, **_kwargs):
        self.messages = messages
        return '{"type":"final","content":"已直接看见图片。"}'


class VisionInputTests(unittest.TestCase):
    def test_selected_image_is_sent_as_multimodal_content_without_ocr_prefetch(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SessionStore(Path(temporary) / "data")
            attachments = AttachmentStore(store)
            image = BytesIO()
            Image.new("RGB", (48, 32), "#d85a43").save(image, format="PNG")
            image.seek(0)
            metadata = attachments.save(
                store.current_id, "sample.png", "image/png", image
            )
            ocr_calls = []
            registry = ToolRegistry()
            registry.register(Tool(
                "ocr_image",
                "OCR",
                {
                    "type": "object",
                    "properties": {"attachment_id": {"type": "string"}},
                    "required": ["attachment_id"],
                    "additionalProperties": False,
                },
                lambda attachment_id: ocr_calls.append(attachment_id) or {"text": "fallback"},
            ))
            model = VisionModel()
            runtime = AgentRuntime(
                model,
                store,
                ContextBuilder(tool_definitions=registry.definitions()),
                tool_registry=registry,
            )
            message = (
                "描述这张图\n\n[attached_files] "
                f'[{{"attachmentId":"{metadata["attachmentId"]}",'
                '"filename":"sample.png","contentType":"image/png"}]'
            )
            result = runtime.run(message)

            self.assertEqual(result.reply, "已直接看见图片。")
            self.assertEqual(ocr_calls, [])
            multimodal = [
                item["content"]
                for item in model.messages
                if isinstance(item.get("content"), list)
            ]
            self.assertEqual(len(multimodal), 1)
            self.assertEqual(multimodal[0][0]["type"], "text")
            self.assertIn("[native_images_ready]", multimodal[0][0]["text"])
            self.assertEqual(multimodal[0][1]["type"], "image_url")
            self.assertTrue(
                multimodal[0][1]["image_url"]["url"].startswith(
                    "data:image/jpeg;base64,"
                )
            )
            self.assertIsNone(store.current.goal_state)


if __name__ == "__main__":
    unittest.main()
