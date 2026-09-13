"""验证 GitHub 仓库读取与错误降级服务。"""

import base64
import io
import json
import unittest
import zipfile
from urllib.error import HTTPError
from unittest.mock import patch

from github_service import GitHubReader, GitHubReadError
from tools import create_read_only_registry


class _Response:
    def __init__(self, payload, url):
        self.data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, *_args):
        return self.data

    def geturl(self):
        return self.url


class GitHubReaderTests(unittest.TestCase):
    def test_transient_github_failure_is_retried(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise HTTPError(request.full_url, 503, "temporarily unavailable", {}, None)
            return _Response({
                "type": "file", "path": "README.md", "size": 2,
                "content": base64.b64encode(b"ok").decode(),
            }, request.full_url)

        with patch("github_service.time.sleep") as sleep:
            result = GitHubReader(opener=opener).read("owner/repo", "README.md")
        self.assertEqual(result["files"][0]["content"], "ok")
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once()

    def test_rate_limited_contents_api_falls_back_to_bounded_archive(self):
        archive_buffer = io.BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as zf:
            zf.writestr("taste-skill-main/README.md", "# Taste Skill\n")
            zf.writestr("taste-skill-main/SKILL.md", "---\nname: taste\n---\n")
            zf.writestr("taste-skill-main/image.bin", b"\x00binary")
        archive = archive_buffer.getvalue()
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if "api.github.com" in request.full_url:
                raise HTTPError(request.full_url, 403, "rate limit", {}, None)
            return _Response(archive, request.full_url)

        result = GitHubReader(opener=opener).read(
            "owner/repo", recursive=True, max_files=10, max_bytes=1000
        )
        self.assertEqual(result["fileCount"], 2)
        self.assertIn("README.md", [item["path"] for item in result["files"]])
        self.assertTrue(any("codeload.github.com" in url for url in calls))
        self.assertIn("codeload.github.com", result["source"])

    def test_rate_limited_concrete_file_uses_raw_without_downloading_archive(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if "api.github.com" in request.full_url:
                raise HTTPError(request.full_url, 403, "rate limit", {}, None)
            if "raw.githubusercontent.com" in request.full_url:
                return _Response(b"# Large repository\n", request.full_url)
            self.fail(f"concrete file unexpectedly downloaded archive: {request.full_url}")

        result = GitHubReader(opener=opener).read(
            "openclaw/openclaw", path="README.md", recursive=False
        )

        self.assertEqual(result["fileCount"], 1)
        self.assertEqual(result["files"][0]["path"], "README.md")
        self.assertEqual(result["files"][0]["content"], "# Large repository\n")
        self.assertIn("raw.githubusercontent.com/openclaw/openclaw/main/README.md", result["source"])
        self.assertFalse(any("codeload.github.com" in url for url in calls))

    def test_github_token_is_sent_only_to_api_host(self):
        seen_headers = {}

        def opener(request, timeout):
            seen_headers[request.full_url] = dict(request.header_items())
            return _Response({
                "type": "file", "path": "README.md", "size": 2,
                "content": base64.b64encode(b"ok").decode(),
            }, request.full_url)

        GitHubReader(opener=opener, token="test-token").read("owner/repo", "README.md")
        api_headers = next(iter(seen_headers.values()))
        self.assertEqual(api_headers.get("Authorization"), "Bearer test-token")

    def test_reads_public_file_from_contents_api(self):
        seen = {}

        def opener(request, timeout):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return _Response({
                "type": "file", "path": "README.md", "size": 5,
                "content": base64.b64encode("你好".encode()).decode(),
            }, request.full_url)

        result = GitHubReader(timeout=3, opener=opener).read("openai/demo", "README.md")
        self.assertEqual(result["repository"], "openai/demo")
        self.assertEqual(result["files"][0]["content"], "你好")
        self.assertEqual(result["citations"][0]["label"], "[G1]")
        self.assertIn("github.com/openai/demo/blob/main/README.md", result["citations"][0]["url"])
        self.assertIn("/repos/openai/demo/contents/README.md?ref=main", seen["url"])
        self.assertEqual(seen["timeout"], 3)

    def test_reads_directory_and_honours_limits(self):
        calls = []

        def opener(request, timeout):
            calls.append(request.full_url)
            return _Response([
                {"type": "file", "path": "a.txt", "size": 3,
                 "content": base64.b64encode(b"abc").decode()},
                {"type": "file", "path": "b.txt", "size": 3,
                 "content": base64.b64encode(b"def").decode()},
                {"type": "dir", "path": "src"},
            ], request.full_url)

        result = GitHubReader(opener=opener).read("owner/repo", max_files=2, max_bytes=1000)
        self.assertEqual(result["fileCount"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["bytesRead"], 6)

    def test_raw_url_and_domain_allowlist(self):
        def opener(request, timeout):
            return _Response(b"raw text", request.full_url)

        result = GitHubReader(opener=opener).read(
            "https://raw.githubusercontent.com/owner/repo/main/docs/a.txt"
        )
        self.assertEqual(result["files"][0]["content"], "raw text")
        result = GitHubReader(opener=opener).read(
            "https://github.com/owner/repo/raw/refs/heads/main/docs/a.txt"
        )
        self.assertEqual(result["files"][0]["content"], "raw text")
        with self.assertRaises(ValueError):
            GitHubReader(opener=opener).read("https://evil.example/owner/repo/main/a.txt")

    def test_rejects_binary_raw_content(self):
        def opener(request, timeout):
            return _Response(b"header\x00binary", request.full_url)

        with self.assertRaises(GitHubReadError):
            GitHubReader(opener=opener).read(
                "https://raw.githubusercontent.com/owner/repo/main/a.bin"
            )

    def test_rejects_path_traversal_and_redacts_http_errors(self):
        with self.assertRaises(ValueError):
            GitHubReader().read("owner/repo", "../secret.txt")

        def opener(*_args, **_kwargs):
            raise HTTPError(
                "https://api.github.com/repos/owner/repo", 404, "Not Found", {},
                io.BytesIO(b"private details and token=secret"),
            )

        with self.assertRaises(GitHubReadError) as caught:
            GitHubReader(opener=opener).read("owner/repo", "missing.txt")
        self.assertIn("404", str(caught.exception))
        self.assertNotIn("private details", str(caught.exception))

    def test_registry_exposes_read_only_github_tool(self):
        registry = create_read_only_registry(github_read_handler=lambda **kwargs: kwargs)
        tool = registry.get("github_read")
        self.assertIsNotNone(tool)
        self.assertIn("仅关闭 recursive 不能降低归档下载体积", tool.description)
        self.assertEqual(tool.safety_level, "read_only")
        self.assertFalse(tool.retryable)
        self.assertEqual(tool.max_attempts, 1)
        result = registry.execute("github_read", {"repo": "owner/repo"})
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
