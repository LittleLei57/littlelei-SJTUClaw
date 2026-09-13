"""Regression coverage for the actual public-release leak scenarios."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from scripts.github_preflight import audit, git_candidates
from scripts import check

class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        for name in ("README.md", ".env.example", "requirements.txt"):
            (self.root / name).write_text("", encoding="utf-8")
        (self.root / ".gitignore").write_text(".env\ndata/\nWorkspace/\nskills/*\n!skills/course-report/\n", encoding="utf-8")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True)

    def put(self, name, value="fixture"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def test_clean_sources_pass(self):
        self.assertEqual(audit(self.root)[0], [])

    def test_tracked_private_data_fails_even_if_ignored(self):
        self.put("data/private-chat.json")
        self.git("add", "-f", "data/private-chat.json")
        self.assertTrue(any("data/private-chat.json" in item for item in audit(self.root)[0]))

    def test_staged_secret_detected_after_working_copy_is_cleaned(self):
        self.put("config.txt", "ghp_" + "A" * 36)
        self.git("add", "config.txt")
        self.put("config.txt", "clean")
        self.assertTrue(any("Git 索引疑似凭证" in item for item in audit(self.root)[0]))

    def test_local_secret_repeated_in_source_is_detected(self):
        secret = "opaque-local-" + "1234567890123456"
        self.put(".env", "FEISHU_APP_SECRET=" + secret)
        self.put("notes.md", secret)
        self.assertTrue(any("notes.md" in item for item in audit(self.root)[0]))

    def test_forced_third_party_skill_is_rejected(self):
        self.put("skills/pptx/SKILL.md")
        self.git("add", "-f", "skills/pptx/SKILL.md")
        self.assertTrue(any("skills/pptx" in item for item in audit(self.root)[0]))

    def test_new_local_skill_is_not_a_git_candidate(self):
        self.put("skills/future-community/SKILL.md")
        self.put("skills/course-report/SKILL.md")
        names = git_candidates(self.root)
        self.assertNotIn("skills/future-community/SKILL.md", names)
        self.assertIn("skills/course-report/SKILL.md", names)

    def test_non_repository_does_not_silently_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                audit(Path(directory))

    def test_export_scan_rejects_private_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            self.assertTrue(any("data" in item for item in audit(root, exported=True)[0]))

    def test_export_accepts_builtin_skill_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("README.md", ".gitignore", ".env.example", "requirements.txt"):
                (root / name).write_text("", encoding="utf-8")
            (root / "skills/course-report").mkdir(parents=True)
            (root / "skills/course-report/SKILL.md").write_text("fixture", encoding="utf-8")
            self.assertEqual(audit(root, exported=True)[0], [])

    def test_explicit_browser_request_cannot_pass_by_skipping(self):
        with patch.object(check, "run", side_effect=[
            (True, "Ran 1 test\nOK"), (True, "Ran 0 tests\nOK (skipped=1)"),
        ]) as run, patch("builtins.print"):
            self.assertFalse(check.check_tests(full=False, browser=True))
            self.assertEqual(run.call_args.kwargs["env"]["RUN_E2E"], "1")
            self.assertEqual(run.call_args_list[0].kwargs["env"]["RUN_E2E"], "0")

if __name__ == "__main__":
    unittest.main()
