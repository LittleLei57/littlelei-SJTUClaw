"""Versioned schema for durable Agent Turn checkpoints and events."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from sqlite_migrations import MigrationResult, SQLiteMigration, run_sqlite_migrations


TURN_SCHEMA_VERSION = 3


def _base_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS turns (
            turn_id TEXT PRIMARY KEY,
            run_id TEXT,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            phase TEXT,
            message TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_seq INTEGER NOT NULL DEFAULT 0,
            error TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS turn_events (
            turn_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_name TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (turn_id, seq),
            FOREIGN KEY (turn_id) REFERENCES turns(turn_id) ON DELETE CASCADE
        )
        """
    )


def _trace_columns(connection: sqlite3.Connection) -> None:
    turn_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(turns)").fetchall()
    }
    if "run_id" not in turn_columns:
        connection.execute("ALTER TABLE turns ADD COLUMN run_id TEXT")
    event_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(turn_events)").fetchall()
    }
    for name, definition in (
        ("parent_seq", "INTEGER"),
        ("trace_id", "TEXT"),
        ("trace_type", "TEXT"),
    ):
        if name not in event_columns:
            connection.execute(
                f"ALTER TABLE turn_events ADD COLUMN {name} {definition}"
            )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_turns_session_updated "
        "ON turns(session_id, updated_at DESC)"
    )


def _lifecycle_columns(connection: sqlite3.Connection) -> None:
    """Add durable orchestration state without invalidating old journals."""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(turns)").fetchall()
    }
    for name, definition in (
        ("active_approval_id", "TEXT"),
        ("cancel_requested_at", "TEXT"),
        ("resume_count", "INTEGER NOT NULL DEFAULT 0"),
        ("version", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE turns ADD COLUMN {name} {definition}")
    connection.execute(
        "UPDATE turns SET status = 'awaiting_approval', "
        "phase = 'awaiting_approval' WHERE status = 'approval_required'"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_turns_status_updated "
        "ON turns(status, updated_at DESC)"
    )


TURN_MIGRATIONS = (
    SQLiteMigration(1, "base_turn_journal", _base_tables),
    SQLiteMigration(2, "trace_tree", _trace_columns),
    SQLiteMigration(3, "durable_turn_lifecycle", _lifecycle_columns),
)


def migrate_turn_database(path: str | Path) -> MigrationResult:
    return run_sqlite_migrations(path, TURN_MIGRATIONS)

