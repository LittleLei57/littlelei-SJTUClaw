"""验证共享状态数据库的事务与并发访问。"""

from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from state_database import StateDatabase
from state_schema import STATE_SCHEMA_VERSION


class StateDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_and_wal_mode(self):
        database = StateDatabase(self.root)
        database.write("example", {"items": [1, 2, 3]})
        self.assertEqual(database.read("example", None), {"items": [1, 2, 3]})
        connection = sqlite3.connect(database.path)
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(mode.lower(), "wal")
        self.assertEqual(version, STATE_SCHEMA_VERSION)

    def test_existing_database_is_backed_up_before_schema_upgrade(self):
        path = self.root / "state.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE TABLE documents("
                "name TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO documents VALUES ('kept', '{\"ok\": true}', 'now')"
            )
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        finally:
            connection.close()

        database = StateDatabase(self.root)
        self.assertEqual(database.read("kept", None), {"ok": True})
        self.assertIsNotNone(database.migration.backup_path)
        self.assertTrue(database.migration.backup_path.exists())
        backup = sqlite3.connect(database.migration.backup_path)
        try:
            version = backup.execute("PRAGMA user_version").fetchone()[0]
            names = {
                row[0]
                for row in backup.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            backup.close()
        self.assertEqual(version, 1)
        self.assertNotIn("tool_executions", names)

    def test_imports_legacy_json_once_and_keeps_backup(self):
        legacy = self.root / "example.json"
        legacy.write_text(json.dumps([{"id": 1}]), encoding="utf-8")
        database = StateDatabase(self.root)
        imported = database.ensure_imported("example", legacy, [])
        self.assertEqual(imported, [{"id": 1}])
        self.assertFalse(legacy.exists())
        self.assertTrue((self.root / "example.json.legacy.bak").exists())
        self.assertEqual(database.read("example", []), [{"id": 1}])

    def test_corrupt_legacy_json_is_not_renamed(self):
        legacy = self.root / "broken.json"
        legacy.write_text("{broken", encoding="utf-8")
        database = StateDatabase(self.root)
        with self.assertRaisesRegex(ValueError, "未执行迁移"):
            database.ensure_imported("broken", legacy, [])
        self.assertTrue(legacy.exists())


if __name__ == "__main__":
    unittest.main()
