"""验证 Gateway 的输入限制、路径隔离与安全响应头。"""

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from attachment_store import AttachmentStore
from context_builder import ContextBuilder
from gateway import create_app
from gateway_security import is_loopback_client, redact_sensitive, same_origin
from runtime import AgentRuntime
from session_store import SessionStore


class EchoModel:
    def complete(self, messages):
        return '{"type":"final","content":"ok"}'


class GatewaySecurityTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"FEISHU_VERIFICATION_TOKEN": ""})
        self.env.start()
        self.temp = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.temp.name) / "data")
        runtime = AgentRuntime(EchoModel(), self.store, ContextBuilder())
        self.app = create_app(
            runtime, AttachmentStore(self.store), Path(__file__).parents[1] / "web",
            start_background_services=False,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self.temp.cleanup()
        self.env.stop()

    def test_redacts_keys_bearer_tokens_and_sensitive_query_values(self):
        raw = "sk-abcdefgh123456 Bearer abcdefghijklmnop https://x/ws?access_key=hello&ticket=world"
        clean = redact_sensitive(raw)
        self.assertNotIn("abcdefgh123456", clean)
        self.assertNotIn("abcdefghijklmnop", clean)
        self.assertNotIn("hello", clean)
        self.assertNotIn("world", clean)

    def test_loopback_and_same_origin_validation(self):
        self.assertTrue(is_loopback_client("127.0.0.1"))
        self.assertTrue(is_loopback_client("::ffff:127.0.0.1"))
        self.assertFalse(is_loopback_client("192.168.1.20"))
        self.assertTrue(same_origin("http://127.0.0.1:8000", "http", "127.0.0.1:8000"))
        self.assertFalse(same_origin("https://evil.example", "http", "127.0.0.1:8000"))

    def test_cross_site_write_is_rejected_but_same_origin_is_allowed(self):
        blocked = self.client.post(
            "/api/sessions", json={"title": "bad"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(blocked.status_code, 403)
        allowed = self.client.post(
            "/api/sessions", json={"title": "good"},
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(allowed.status_code, 201)

    def test_remote_clients_are_denied_by_default(self):
        with patch("gateway.is_loopback_client", return_value=False), \
             patch("gateway.remote_access_enabled", return_value=False):
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_feishu_webhook_is_reachable_remotely_but_requires_verification_token(self):
        with patch("gateway.is_loopback_client", return_value=False), \
             patch("gateway.remote_access_enabled", return_value=False):
            # The endpoint is the sole remote exception, then performs its own
            # channel authentication instead of being accepted anonymously.
            response = self.client.post("/api/channels/feishu/events", json={})
        self.assertEqual(response.status_code, 503)

    def test_security_headers_are_present(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])


if __name__ == "__main__":
    unittest.main()
