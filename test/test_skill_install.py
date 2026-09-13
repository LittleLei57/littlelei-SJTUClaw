"""验证 Skill 下载、校验、安装和失败回滚。"""

import io
import base64
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from session_store import SessionStore
from skill_system import (
    SkillDownloadHTTPError,
    SkillRegistry,
    SkillService,
    register_skill_tool,
)
from tools import ToolExecutionContext, ToolRegistry


def archive(files: dict[str, str], *, symlink: str | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "target")
    return out.getvalue()


class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.registry = SkillRegistry(root / "skills")
        self.sessions = SessionStore(root / "data")
        self.service = SkillService(self.registry, self.sessions)

    def tearDown(self):
        self.temp.cleanup()

    def test_install_validates_and_refreshes_registry(self):
        callback = []
        self.service.add_refresh_callback(lambda: callback.append(True))
        payload = archive({
            "demo/SKILL.md": "---\nname: demo\ndescription: Demo skill\n---\nUse it safely.\n",
            "demo/references/guide.md": "guide",
        })
        self.service._download_source = lambda source, version: (payload, "github", "https://github.com/demo/demo")
        result = self.service.install(ToolExecutionContext("default"), "https://github.com/demo/demo", "v1")
        self.assertEqual(result["skillName"], "demo")
        self.assertTrue(callback)
        self.assertEqual(self.registry.get("demo").description, "Demo skill")

    def test_install_normalizes_github_archive_wrapper(self):
        payload = archive({
            "demo-main/SKILL.md": "---\nname: demo\ndescription: Demo skill\n---\nUse it safely.\n",
        })
        self.service._download_source = lambda source, version: (payload, "github", "https://github.com/demo/demo")
        result = self.service.install(ToolExecutionContext("default"), "https://github.com/demo/demo", "main")
        self.assertEqual(result["skillName"], "demo")
        self.assertTrue((self.registry.skills_dir / "demo" / "SKILL.md").is_file())

    def test_rejects_traversal_symlink_and_script(self):
        cases = [
            archive({"../escape.txt": "x", "demo/SKILL.md": "---\nname: demo\ndescription: x\n---\n"}),
            archive({"demo/SKILL.md": "---\nname: demo\ndescription: x\n---\n"}, symlink="demo/link"),
            archive({"demo/SKILL.md": "---\nname: demo\ndescription: x\n---\n", "demo/run.py": "print(1)"}),
        ]
        for payload in cases:
            with self.assertRaises(ValueError):
                self.service._install_archive(payload)
        self.assertFalse((self.registry.skills_dir / "demo").exists())

    def test_script_skill_requires_explicit_opt_in(self):
        payload = archive({
            "scripted/SKILL.md": (
                "---\nname: scripted\ndescription: Scripted demo\n---\n"
                "Run helpers only through an approved Tool.\n"
            ),
            "scripted/scripts/helper.py": "print('demo')\n",
        })
        with self.assertRaisesRegex(ValueError, "allow_scripts=true"):
            self.service._install_archive(payload)
        installed = self.service._install_archive(
            payload,
            allow_scripts=True,
        )
        self.assertEqual(installed, "scripted")
        self.assertTrue(
            (self.registry.skills_dir / "scripted" / "scripts" / "helper.py").is_file()
        )

    def test_accepts_canonical_clawhub_page_url(self):
        with patch.object(
            SkillService,
            "_download_clawhub",
            return_value=(b"archive", "clawhub", "https://clawhub.ai/api/v1/download"),
        ) as download:
            result = self.service._download_source(
                "https://clawhub.ai/example-owner/skills/demo-skill", "latest"
            )
        self.assertEqual(result[1], "clawhub")
        download.assert_called_once_with(
            "demo-skill", "latest", owner_handle="example-owner"
        )

    def test_accepts_compact_github_source(self):
        with patch.object(
            SkillService,
            "_download_bytes",
            classmethod(lambda _cls, url, _allowed: (b"archive", "application/zip", url)),
        ):
            result = SkillService._download_source("github:owner/repo", "main")
        self.assertEqual(result[1], "github")
        self.assertIn("codeload.github.com/owner/repo/zip/main", result[2])

    def test_downloads_only_a_github_tree_subdirectory(self):
        skill_text = "---\nname: demo\ndescription: Demo skill\n---\n"

        def download(_cls, url, _allowed_hosts):
            if "/contents/skills/demo/SKILL.md?" in url:
                payload = {
                    "type": "file",
                    "path": "skills/demo/SKILL.md",
                    "content": base64.b64encode(skill_text.encode()).decode(),
                }
            else:
                payload = [{"type": "file", "path": "skills/demo/SKILL.md"}]
            return json.dumps(payload).encode(), "application/json", url

        with patch.object(SkillService, "_download_bytes", classmethod(download)):
            archive_payload, kind, source = self.service._download_source(
                "https://github.com/owner/repo/tree/main/skills/demo", "latest"
            )
        self.assertEqual(kind, "github-tree")
        self.assertIn("/tree/main/skills/demo", source)
        with zipfile.ZipFile(io.BytesIO(archive_payload)) as archive_file:
            self.assertEqual(archive_file.namelist(), ["demo/SKILL.md"])

    def test_clawhub_409_retries_with_concrete_latest_version(self):
        calls = []

        def download(_cls, url, _allowed_hosts):
            calls.append(url)
            if "/api/v1/download?slug=demo" in url and "version=" not in url:
                raise SkillDownloadHTTPError(409, url, "version scan pending")
            if "/api/v1/skills/demo" in url:
                return json.dumps({"latestVersion": {"version": "1.2.3"}}).encode(), "application/json", url
            return archive({"demo/SKILL.md": "---\nname: demo\ndescription: Demo\n---\n"}), "application/zip", url

        with patch.object(SkillService, "_download_bytes", classmethod(download)):
            payload, kind, _ = SkillService._download_clawhub("demo", "latest")
        self.assertEqual(kind, "clawhub")
        self.assertTrue(payload)
        self.assertTrue(any("version=1.2.3" in url for url in calls))

    def test_clawhub_ambiguous_slug_resolves_owner_from_search(self):
        calls = []

        def download(_cls, url, _allowed_hosts):
            calls.append(url)
            if "/api/v1/download?slug=demo" in url and "ownerHandle=" not in url:
                raise SkillDownloadHTTPError(409, url, "multiple publishers")
            if "/api/v1/search?" in url:
                return json.dumps({"results": [{
                    "slug": "demo", "ownerHandle": "demo-owner",
                }]}).encode(), "application/json", url
            return archive({
                "demo/SKILL.md": "---\nname: demo\ndescription: Demo\n---\n",
            }), "application/zip", url

        with patch.object(SkillService, "_download_bytes", classmethod(download)):
            payload, kind, _ = SkillService._download_clawhub("demo", "latest")
        self.assertEqual(kind, "clawhub")
        self.assertTrue(payload)
        self.assertTrue(any("ownerHandle=demo-owner" in url for url in calls))

    def test_download_http_error_preserves_safe_url(self):
        error = SkillDownloadHTTPError(404, "https://github.com/example/missing", "Not Found")
        self.assertIn("HTTP 404", str(error))
        self.assertIn("https://github.com/example/missing", str(error))

    def test_transient_download_failure_is_retried_once(self):
        url = "https://github.com/example/repo/archive/main.zip"
        transient = SkillDownloadHTTPError(503, url, "temporarily unavailable")
        with patch.object(
            SkillService,
            "_download_bytes_once",
            side_effect=[transient, (b"archive", "application/zip", url)],
        ), patch("skill_system.time.sleep") as sleep:
            payload, content_type, resolved = SkillService._download_bytes(
                url, {"github.com"}
            )
        self.assertEqual((payload, content_type, resolved), (b"archive", "application/zip", url))
        sleep.assert_called_once()

    def test_clawhub_handoff_uses_exact_github_subdirectory(self):
        handoff = {
            "sourceRef": "public-github",
            "repo": "owner/repo",
            "commit": "abc123",
            "path": "skills/demo",
        }

        def download(_cls, _url, _allowed_hosts):
            return json.dumps(handoff).encode(), "application/json", "https://clawhub.ai/api/v1/download"

        tree_calls = []

        def tree(_cls, *args):
            tree_calls.append(args)
            return b"archive", "github-tree", "https://github.com/owner/repo/tree/abc123/skills/demo"

        with patch.object(SkillService, "_download_bytes", classmethod(download)), patch.object(
            SkillService, "_download_github_subdir", classmethod(tree),
        ):
            payload, kind, source = SkillService._download_clawhub("demo", "latest")
        self.assertEqual((payload, kind), (b"archive", "github-tree"))
        self.assertIn("/tree/abc123/skills/demo", source)
        self.assertEqual(tree_calls, [("owner", "repo", "abc123", "skills/demo")])

    def test_install_tool_is_approval_gated(self):
        registry = ToolRegistry()
        register_skill_tool(registry, self.service)
        tool = registry.get("install_skill")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.safety_level, "approval_required")
        self.assertTrue(tool.contextual)
        self.assertIn("source", tool.input_schema["required"])
        self.assertIn("allow_scripts", tool.input_schema["properties"])


if __name__ == "__main__":
    unittest.main()
