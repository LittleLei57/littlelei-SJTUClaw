"""Tests for the optional Wolfram|Alpha provider."""

import unittest
from urllib.error import HTTPError

from tools import create_read_only_registry
from wolfram_alpha import WolframAlpha


class _Response:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.body


class WolframAlphaTests(unittest.TestCase):
    def test_query_returns_model_friendly_result_and_attribution(self):
        seen = {}

        def opener(request, timeout):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return _Response(b"Result:\nx = -2 or x = 2")

        provider = WolframAlpha("TEST-APP-ID", opener=opener)
        result = provider.query("solve x^2 = 4", max_chars=800)

        self.assertEqual(result["provider"], "Wolfram|Alpha")
        self.assertIn("x = -2", result["result"])
        self.assertEqual(result["attribution"], "Computed by Wolfram|Alpha")
        self.assertIn("appid=TEST-APP-ID", seen["url"])
        self.assertIn("solve+x%5E2+%3D+4", seen["url"])

    def test_invalid_appid_response_has_actionable_error(self):
        def opener(_request, timeout):
            raise HTTPError("https://example.invalid", 403, "Forbidden", {}, None)

        with self.assertRaisesRegex(RuntimeError, "AppID"):
            WolframAlpha("bad", opener=opener).query("2+2")

    def test_registry_only_exposes_wolfram_when_configured(self):
        self.assertIsNone(create_read_only_registry().get("wolfram_query"))

        registry = create_read_only_registry(
            wolfram_handler=lambda query, max_chars=4_000: {
                "query": query,
                "max_chars": max_chars,
            }
        )
        tool = registry.get("wolfram_query")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.safety_level, "read_only")
        result = registry.execute("wolfram_query", {"query": "1 meter in feet"})
        self.assertTrue(result.success)
        self.assertEqual(result.output["query"], "1 meter in feet")


if __name__ == "__main__":
    unittest.main()
