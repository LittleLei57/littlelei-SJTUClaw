"""验证 Tavily 搜索参数、结果清洗与错误处理。"""

import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from tavily_search import TavilySearch
from tools import create_read_only_registry


class _Response:
    def __init__(self, payload):
        self.data = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.data


class TavilySearchTests(unittest.TestCase):
    def test_search_uses_bearer_auth_and_bounds_output(self):
        captured = {}

        def opener(request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return _Response(
                {
                    "query": "SJTU",
                    "answer": "a" * 5000,
                    "results": [{"title": "SJTU", "url": "https://sjtu.edu.cn", "content": "c" * 3000, "score": 0.9}],
                }
            )

        result = TavilySearch("secret", opener=opener).search(" SJTU ", max_results=1)
        self.assertEqual(captured["authorization"], "Bearer secret")
        self.assertFalse(captured["body"]["include_raw_content"])
        self.assertEqual(len(result["answer"]), 2501)
        self.assertEqual(len(result["results"][0]["content"]), 1001)
        self.assertEqual(result["citations"][0]["label"], "[W1]")
        self.assertEqual(result["citations"][0]["url"], "https://sjtu.edu.cn")

    def test_missing_key_is_clear(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "TAVILY_API_KEY"):
                TavilySearch().search("test")

    def test_http_error_does_not_expose_key(self):
        def opener(*_args, **_kwargs):
            raise HTTPError(
                "https://api.tavily.com/search", 401, "Unauthorized", {},
                io.BytesIO(b'{"detail":"invalid credentials"}'),
            )

        with self.assertRaises(RuntimeError) as caught:
            TavilySearch("top-secret", opener=opener).search("test")
        self.assertNotIn("top-secret", str(caught.exception))
        self.assertIn("HTTP 401", str(caught.exception))

    def test_registry_exposes_read_only_tool(self):
        registry = create_read_only_registry(web_search_handler=lambda **kwargs: kwargs)
        tool = registry.get("web_search")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.safety_level, "read_only")
        result = registry.execute("web_search", {"query": "SJTU"})
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
