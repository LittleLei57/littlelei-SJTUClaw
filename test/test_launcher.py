"""验证启动器的环境检查、命令构造和故障提示。"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sjtuclaw_launcher as launcher


class LauncherTests(unittest.TestCase):
    def test_env_reader_ignores_comments_and_unquotes_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# comment\nLLM_API_KEY='secret'\nEMPTY=\n", encoding="utf-8")
            self.assertEqual(launcher._read_env(path)["LLM_API_KEY"], "secret")

    def test_health_url_uses_loopback_for_wildcard_bind(self):
        self.assertEqual(launcher.health_url("0.0.0.0", 8123), "http://127.0.0.1:8123")
        self.assertEqual(launcher.health_url("127.0.0.1", 8000), "http://127.0.0.1:8000")

    def test_start_stops_before_spawning_when_required_check_fails(self):
        failed = [launcher.Check("dependency", False, "missing")]
        with patch.object(launcher, "collect_checks", return_value=failed), \
             patch.object(launcher.subprocess, "Popen") as popen:
            self.assertEqual(launcher.start("127.0.0.1", 8000, False, False), 2)
            popen.assert_not_called()

    def test_parser_exposes_doctor_install_and_start(self):
        parser = launcher.build_parser()
        self.assertEqual(parser.parse_args(["doctor"]).command, "doctor")
        self.assertEqual(parser.parse_args(["install", "--no-pet"]).command, "install")
        args = parser.parse_args(["start", "--pet", "--port", "9000"])
        self.assertTrue(args.pet)
        self.assertEqual(args.port, 9000)

    def test_install_without_pet_only_installs_python_dependencies(self):
        with patch.object(launcher.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(launcher.install(include_pet=False), 0)
        run.assert_called_once()
        self.assertIn("requirements.txt", str(run.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()
