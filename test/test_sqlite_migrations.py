"""验证 SQLite 旧数据向当前 schema 的迁移。"""

from pathlib import Path
import sqlite3
import tempfile
import unittest

from sqlite_migrations import SQLiteMigration, run_sqlite_migrations


class SQLiteMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "sample.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_failed_batch_rolls_back_schema_and_version(self):
        connection = sqlite3.connect(self.path)
        connection.execute("CREATE TABLE original(value TEXT)")
        connection.execute("INSERT INTO original VALUES ('kept')")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()

        def first(connection):
            connection.execute("CREATE TABLE added(value TEXT)")

        def broken(connection):
            connection.execute("CREATE TABLE broken(")

        migrations = (
            SQLiteMigration(1, "original", lambda connection: None),
            SQLiteMigration(2, "added", first),
            SQLiteMigration(3, "broken", broken),
        )
        with self.assertRaises(sqlite3.OperationalError):
            run_sqlite_migrations(self.path, migrations)

        connection = sqlite3.connect(self.path)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            value = connection.execute("SELECT value FROM original").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(version, 1)
        self.assertEqual(value, "kept")
        self.assertNotIn("added", tables)
        self.assertTrue(
            (self.root / "sample.sqlite3.pre-migration-v1-to-v3.bak").exists()
        )

    def test_rejects_database_newer_than_code(self):
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA user_version=9")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(RuntimeError, "高于当前程序"):
            run_sqlite_migrations(
                self.path,
                (SQLiteMigration(1, "one", lambda connection: None),),
            )


if __name__ == "__main__":
    unittest.main()
