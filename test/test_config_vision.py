"""验证模型视觉能力与相关配置解析。"""

import os
import unittest
from unittest.mock import patch

from config import model_supports_vision


class VisionConfigurationTests(unittest.TestCase):
    def test_minimax_m3_is_inferred_as_multimodal(self):
        with patch.dict(os.environ, {"LLM_VISION": "auto"}, clear=False):
            self.assertTrue(model_supports_vision("MiniMax-M3"))
            self.assertTrue(model_supports_vision("minimax-m3-vision"))

    def test_explicit_vision_switch_still_wins(self):
        with patch.dict(os.environ, {"LLM_VISION": "off"}, clear=False):
            self.assertFalse(model_supports_vision("MiniMax-M3"))
        with patch.dict(os.environ, {"LLM_VISION": "on"}, clear=False):
            self.assertTrue(model_supports_vision("text-only-model"))


if __name__ == "__main__":
    unittest.main()
