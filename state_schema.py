"""Shared schema for the global state and Tool execution database."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from sqlite_migrations import MigrationResult, SQLiteMigration, run_sqlite_migrations


STATE_SCHEMA_VERSION = 2


def _documents(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            name TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _tool_executions(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tool_executions (
            execution_key TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tool TEXT NOT NULL,
            args_digest TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_tool_executions_session_started "
        "ON tool_executions(session_id, started_at DESC)"
    )


STATE_MIGRATIONS = (
    SQLiteMigration(1, "documents", _documents),
    SQLiteMigration(2, "tool_executions", _tool_executions),
)


def migrate_state_database(path: str | Path) -> MigrationResult:
    return run_sqlite_migrations(path, STATE_MIGRATIONS)

