"""验证 Session 级模型选择与别名规范化。"""

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient

from context_builder import ContextBuilder
from gateway import create_app
from llm_client import LLMClient
from main import handle_model_command
from model_capabilities import ModelCapabilityStore
from model_selection import ModelSelectionStore
from runtime import AgentRuntime
from session_store import SessionStore


class _Client:
    def __init__(self):
        self.chat = type("Chat", (), {})()
        self.chat.completions = type("Completions", (), {})()


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.selection = ModelSelectionStore(self.root)
        self.model = LLMClient(
            model="qwen",
            client=_Client(),
            capability_store=ModelCapabilityStore(self.root),
        )
        self.runtime = AgentRuntime(
            self.model,
            SessionStore(self.root / "sessions"),
            ContextBuilder(),
        )
        self.runtime.model_selection_store = self.selection

    def tearDown(self):
        self.temp.cleanup()

    def test_selection_persists_and_rejects_external_models(self):
        self.assertEqual(self.selection.select("deepseek-chat"), "deepseek-chat")
        self.assertEqual(ModelSelectionStore(self.root).current(), "deepseek-chat")
        with self.assertRaises(ValueError):
            self.selection.select("minimax-m3-official")

    def test_switch_loads_capabilities_for_target_model(self):
        self.model.capability_store.update(
            self.model.base_url, "deepseek-reasoner", nativeTools=False
        )
        profile = self.runtime.select_model("deepseek-reasoner")
        self.assertEqual(profile["model"], "deepseek-reasoner")
        self.assertFalse(self.model.native_tools)
        self.assertEqual(self.selection.current(), "deepseek-reasoner")

    def test_gateway_lists_only_four_sjtu_models_and_switches(self):
        with TestClient(
            create_app(self.runtime, start_background_services=False)
        ) as client:
            listing = client.get("/api/model")
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(
                [item["id"] for item in listing.json()["models"]],
                ["deepseek-chat", "deepseek-reasoner", "minimax", "qwen"],
            )
            self.assertEqual(
                [item["label"] for item in listing.json()["models"]],
                [
                    "DeepSeek V4 Flash（常规模式）",
                    "DeepSeek V4 Flash（思考模式）",
                    "MiniMax-M2.7",
                    "Qwen3.6-27B",
                ],
            )
            self.assertEqual(listing.json()["models"][3]["mode"], "多模态处理")
            self.assertEqual(listing.json()["models"][3]["contextLength"], "256K")
            response = client.put(
                "/api/model", json={"model": "deepseek-chat"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["currentModel"], "deepseek-chat")
            rejected = client.put(
                "/api/model", json={"model": "external-model"}
            )
            self.assertEqual(rejected.status_code, 422)

    def test_cli_lists_and_switches(self):
        listing = handle_model_command(self.runtime, "/model")
        self.assertIn("* qwen", listing)
        switched = handle_model_command(
            self.runtime, "/model use deepseek-chat"
        )
        self.assertIn("deepseek-chat", switched)
        self.assertEqual(self.model.model, "deepseek-chat")

    def test_custom_openai_compatible_catalog_is_configuration_driven(self):
        """A private endpoint may expose arbitrary ids without leaking its Key."""
        env = os.environ.copy()
        env.update(
            {
                "LLM_BASE_URL": "https://example.invalid/v1",
                "LLM_API_KEY": "must-not-be-printed",
                "LLM_MODEL": "owner/model-a",
                "LLM_MODELS_JSON": json.dumps(
                    [
                        {
                            "id": "owner/model-a",
                            "label": "Model A (full)",
                            "shortLabel": "Model A",
                            "vision": True,
                            # Unknown fields, especially credential-like ones,
                            # must not be copied into the public model catalog.
                            "apiKey": "also-must-not-be-printed",
                        },
                        {"id": "model-b", "label": "Model B"},
                    ]
                ),
            }
        )
        command = (
            "import json, config; "
            "print(json.dumps({'models': config.MODEL_CATALOG, "
            "'selected': config.normalize_sjtu_model('OWNER/MODEL-A')}))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", command],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(
            [item["id"] for item in payload["models"]],
            ["owner/model-a", "model-b"],
        )
        self.assertEqual(payload["selected"], "owner/model-a")
        self.assertTrue(payload["models"][0]["vision"])
        self.assertNotIn("apiKey", payload["models"][0])
        self.assertNotIn("must-not-be-printed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
