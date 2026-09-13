"""验证运行数据清理与维护任务。"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone

from scripts.maintenance import (
    apply_retention_cleanup,
    create_backup,
    create_diagnostic,
    integrity_report,
    retention_preview,
    restore_backup,
    verify_backup,
)


class MaintenanceTests(unittest.TestCase):
    PRIVATE_MESSAGE = "SJTUCLAW_TEST_PRIVATE_MESSAGE_5f82"
    PRIVATE_ATTACHMENT = "SJTUCLAW_TEST_PRIVATE_ATTACHMENT_9c41"
    PRIVATE_DATABASE = "SJTUCLAW_TEST_PRIVATE_DATABASE_7b16"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.backups = self.root / "backups"
        self.data.mkdir()
        connection = sqlite3.connect(self.data / "state.sqlite3")
        try:
            connection.execute("CREATE TABLE documents(name TEXT PRIMARY KEY, payload TEXT)")
            connection.execute(
                "INSERT INTO documents VALUES ('memory', ?)",
                (json.dumps({"secret": self.PRIVATE_DATABASE}),),
            )
            connection.commit()
        finally:
            connection.close()
        sessions = self.data / "sessions" / "session_1"
        sessions.mkdir(parents=True)
        (sessions / "session.json").write_text(
            json.dumps({"title": "测试会话", "messages": [{"content": self.PRIVATE_MESSAGE}]}),
            encoding="utf-8",
        )
        attachment = self.data / "attachments" / "session_1"
        attachment.mkdir(parents=True)
        (attachment / "private.txt").write_text(self.PRIVATE_ATTACHMENT, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_verify_restore_and_rolling_retention(self):
        archive = create_backup(self.data, self.backups, 2, quiet=True)
        self.assertTrue(verify_backup(archive))
        (self.data / "sessions" / "session_1" / "session.json").write_text(
            "corrupted", encoding="utf-8"
        )
        restore_backup(archive, self.data, self.backups, 2, force=True)
        restored = json.loads(
            (self.data / "sessions" / "session_1" / "session.json").read_text(encoding="utf-8")
        )
        self.assertEqual(restored["title"], "测试会话")
        self.assertTrue(integrity_report(self.data)["ok"])

    def test_diagnostic_excludes_message_attachment_and_database_payloads(self):
        output = create_diagnostic(self.data, self.root / "diagnostics")
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            combined = b"\n".join(archive.read(name) for name in names)
        self.assertIn("diagnostic.json", names)
        self.assertNotIn("session.json", names)
        self.assertNotIn("private.txt", names)
        self.assertNotIn("state.sqlite3", names)
        self.assertNotIn(self.PRIVATE_MESSAGE.encode(), combined)
        self.assertNotIn(self.PRIVATE_ATTACHMENT.encode(), combined)
        self.assertNotIn(self.PRIVATE_DATABASE.encode(), combined)

    def test_history_cleanup_is_preview_first_and_preserves_active_records(self):
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()
        turns = sqlite3.connect(self.data / "turns.sqlite3")
        try:
            turns.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE turns(
                    turn_id TEXT PRIMARY KEY, session_id TEXT, status TEXT,
                    updated_at TEXT
                );
                CREATE TABLE turn_events(
                    turn_id TEXT, seq INTEGER, payload TEXT,
                    FOREIGN KEY(turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
                );
                """
            )
            turns.executemany(
                "INSERT INTO turns VALUES (?, 's1', ?, ?)",
                [
                    ("old-completed", "completed", old),
                    ("recent-completed", "completed", recent),
                    ("old-approval", "approval_required", old),
                    ("old-running", "running", old),
                ],
            )
            turns.execute(
                "INSERT INTO turn_events VALUES ('old-completed', 1, '{}')"
            )
            turns.commit()
        finally:
            turns.close()
        state = sqlite3.connect(self.data / "state.sqlite3")
        try:
            state.execute(
                """
                CREATE TABLE tool_executions(
                    execution_key TEXT PRIMARY KEY, status TEXT,
                    completed_at TEXT
                )
                """
            )
            state.executemany(
                "INSERT INTO tool_executions VALUES (?, ?, ?)",
                [
                    ("old-done", "completed", old),
                    ("recent-done", "completed", recent),
                    ("old-executing", "executing", None),
                ],
            )
            state.commit()
        finally:
            state.close()

        preview = retention_preview(
            self.data, older_than_days=30, keep_turns_per_session=1
        )
        self.assertEqual(preview["turns"], 1)
        self.assertEqual(preview["turnEvents"], 1)
        self.assertEqual(preview["toolExecutions"], 1)
        connection = sqlite3.connect(self.data / "turns.sqlite3")
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 4
            )
        finally:
            connection.close()

        applied = apply_retention_cleanup(
            self.data,
            self.backups,
            older_than_days=30,
            keep_turns_per_session=1,
            force=True,
        )
        self.assertEqual(applied["mode"], "applied")
        self.assertTrue(Path(applied["backupDir"]).is_dir())
        connection = sqlite3.connect(self.data / "turns.sqlite3")
        try:
            remaining = {
                row[0] for row in connection.execute("SELECT turn_id FROM turns")
            }
            event_count = connection.execute(
                "SELECT COUNT(*) FROM turn_events"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(
            remaining,
            {"recent-completed", "old-approval", "old-running"},
        )
        self.assertEqual(event_count, 0)
        connection = sqlite3.connect(self.data / "state.sqlite3")
        try:
            remaining_tools = {
                row[0]
                for row in connection.execute(
                    "SELECT execution_key FROM tool_executions"
                )
            }
        finally:
            connection.close()
        self.assertEqual(remaining_tools, {"recent-done", "old-executing"})


if __name__ == "__main__":
    unittest.main()
