"""Transactional SQLite document storage with one-time JSON migration."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any, Callable, TypeVar

from session_store import utc_now
from state_schema import migrate_state_database


T = TypeVar("T")


class StateDatabase:
    """Small transactional store for global state documents.

    Each operation uses its own SQLite connection, which makes the class safe to
    share between Gateway worker threads and channel/scheduler threads.
    """

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "state.sqlite3"
        self.migration = migrate_state_database(self.path)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
        finally:
            connection.close()

    def read(self, name: str, default: Any) -> Any:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM documents WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise ValueError(f"SQLite 状态文档损坏：{name}") from exc

    def write(self, name: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO documents(name, payload, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (name, payload, utc_now()),
            )
            connection.commit()

    def mutate(
        self,
        name: str,
        default: Any,
        mutator: Callable[[Any], tuple[Any, T]],
    ) -> T:
        """Atomically read, transform and replace one JSON document.

        The callback runs while an IMMEDIATE transaction owns the write lock.
        This is the compare-and-swap boundary used by ApprovalStore so two
        browser retries or Gateway workers cannot both claim one side effect.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM documents WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                current = default
            else:
                try:
                    current = json.loads(row[0])
                except json.JSONDecodeError as exc:
                    raise ValueError(f"SQLite 状态更新失败：{name}") from exc
            next_value, result = mutator(current)
            payload = json.dumps(next_value, ensure_ascii=False)
            connection.execute(
                """
                INSERT INTO documents(name, payload, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (name, payload, utc_now()),
            )
            connection.commit()
            return result

    def ensure_imported(self, name: str, legacy_path: Path, default: Any) -> Any:
        """Import a legacy JSON file once and preserve it as a backup."""
        marker = object()
        current = self.read(name, marker)
        if current is not marker:
            return current
        value = default
        if legacy_path.exists():
            try:
                value = json.loads(legacy_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"旧 JSON 状态文件损坏，未执行迁移：{legacy_path}") from exc
        self.write(name, value)
        if legacy_path.exists():
            backup = legacy_path.with_suffix(legacy_path.suffix + ".legacy.bak")
            if not backup.exists():
                legacy_path.replace(backup)
        return value
