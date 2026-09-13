"""验证模型能力表及 Tool、视觉能力判断。"""

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from context_builder import ContextBuilder
from gateway import create_app
from llm_client import LLMClient
from model_capabilities import ModelCapabilityStore
from runtime import AgentRuntime
from session_store import SessionStore


class _Completions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Client:
    def __init__(self, outcomes):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions(outcomes)


class _Response:
    def __init__(self, content='{"type":"final","content":"ok"}'):
        message = type("Message", (), {"content": content, "tool_calls": None})()
        self.choices = [
            type("Choice", (), {"message": message, "finish_reason": "stop"})()
        ]
        self.usage = None


class _StatusError(RuntimeError):
    status_code = 400


class ModelCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ModelCapabilityStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_is_scoped_by_provider_and_model(self):
        self.store.update(
            "https://example.test/v1", "model-a",
            nativeTools=True, streaming=True,
        )
        profile = self.store.describe(
            "https://example.test/v1", "model-a", inferred_vision=False
        )
        self.assertEqual(profile["provider"], "example.test")
        self.assertEqual(profile["nativeTools"], {
            "supported": True, "source": "observed"
        })
        self.assertEqual(profile["vision"], {
            "supported": False, "source": "inferred"
        })
        other = self.store.describe(
            "https://example.test/v1", "model-b", inferred_vision=True
        )
        self.assertIsNone(other["nativeTools"]["supported"])
        self.assertTrue(other["vision"]["supported"])

    def test_llm_reuses_observed_native_tool_rejection_after_restart(self):
        first_client = _Client([
            _StatusError("tools are not supported"),
            _Response(),
        ])
        first = LLMClient(
            model="model-a",
            base_url="https://example.test/v1",
            client=first_client,
            capability_store=self.store,
        )
        first.complete(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "clock"}}],
        )
        self.assertFalse(first.native_tools)

        second_client = _Client([_Response()])
        second = LLMClient(
            model="model-a",
            base_url="https://example.test/v1",
            client=second_client,
            capability_store=self.store,
        )
        second.complete(
            [{"role": "user", "content": "again"}],
            tools=[{"type": "function", "function": {"name": "clock"}}],
        )
        self.assertFalse(second.native_tools)
        self.assertNotIn("tools", second_client.chat.completions.calls[0])

    def test_gateway_exposes_and_resets_current_profile(self):
        client_impl = _Client([_Response()])
        model = LLMClient(
            model="model-a",
            base_url="https://example.test/v1",
            client=client_impl,
            capability_store=self.store,
        )
        self.store.update(
            model.base_url, model.model, nativeTools=True, streaming=True
        )
        runtime = AgentRuntime(
            model, SessionStore(self.root / "sessions"), ContextBuilder()
        )
        client = TestClient(
            create_app(runtime, start_background_services=False)
        )
        try:
            health = client.get("/api/health").json()
            self.assertTrue(
                health["modelCapabilities"]["nativeTools"]["supported"]
            )
            response = client.post("/api/model/capabilities/reset")
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(
                response.json()["capabilities"]["nativeTools"]["supported"]
            )
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
