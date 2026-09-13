"""Durable journal for Gateway Agent turns.

The in-memory ``active_turns`` map is intentionally kept as the fast path for
SSE subscribers.  This module provides the durable companion: one row per turn
and an append-only event journal.  It lets the Gateway distinguish a turn that
is genuinely running from one that was interrupted by a process restart.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
from typing import Any

from session_store import utc_now
from turn_schema import migrate_turn_database


TERMINAL_STATUSES = {
    "completed",
    "cancelled",
    "error",
    # Public docs use ``failed``; ``error`` remains for compatibility.
    "failed",
}
RUNNING_STATUSES = {
    "starting",
    "running",
    "tool_call",
    "tool_result",
    "assistant_delta",
    "cancelling",
    # Older versions persisted the detailed phase as ``status``. Keep these
    # values so restart recovery also handles historical in-flight rows.
    "analyzing",
    "model_call",
    "model_stream",
    "protocol_retry",
    "empty_response_retry",
    "protocol_repair",
    "assistant_reset",
    "compaction_started",
    "compaction",
    "compaction_failed",
    "attachment_read",
    "time_grounding",
    "tool_observation",
}
SUSPENDED_STATUSES = {"awaiting_approval"}
ACTIVE_STATUSES = RUNNING_STATUSES | SUSPENDED_STATUSES

ALLOWED_STATUS_TRANSITIONS = {
    "starting": {"running", "awaiting_approval", "cancelling", "cancelled", "failed", "error", "interrupted"},
    "running": {"running", "awaiting_approval", "cancelling", "cancelled", "completed", "failed", "error", "interrupted"},
    "awaiting_approval": {"running", "cancelling", "cancelled", "failed", "error", "interrupted"},
    "cancelling": {"cancelled", "failed", "error", "interrupted"},
}


class TurnStore:
    """Small SQLite-backed turn/checkpoint store.

    Every method opens its own connection so the store is safe to share with
    Gateway worker threads, channel adapters, and the scheduler.  Events are
    keyed by ``(turn_id, seq)``; a repeated write of the same sequence is
    idempotent, which is useful when a client reconnects around a terminal
    event.
    """

    def __init__(self, data_dir: str | Path):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "turns.sqlite3"
        self.migration = migrate_turn_database(self.path)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(row)

    def start(
        self,
        turn_id: str,
        session_id: str,
        kind: str,
        started_at: str | None = None,
        phase: str = "starting",
        message: str = "正在启动",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        timestamp = started_at or utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT status FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if existing is not None:
                raise ValueError(f"Turn 已存在：{turn_id}（{existing['status']}）")
            connection.execute(
                """
                INSERT INTO turns(
                    turn_id, run_id, session_id, kind, status, phase, message,
                    started_at, updated_at, last_seq, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                """,
                (
                    turn_id, run_id, session_id, kind, phase, phase, message,
                    timestamp, timestamp,
                ),
            )
            connection.commit()
        return self.get(turn_id) or {}

    def update(
        self,
        turn_id: str,
        *,
        phase: str | None = None,
        message: str | None = None,
        status: str | None = None,
        updated_at: str | None = None,
        expected_run_id: str | None = None,
    ) -> bool:
        fields: list[str] = []
        values: list[Any] = []
        if phase is not None:
            fields.append("phase = ?")
            values.append(phase)
        if message is not None:
            fields.append("message = ?")
            values.append(message)
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        fields.append("updated_at = ?")
        values.append(updated_at or utc_now())
        values.append(turn_id)
        where = "turn_id = ?"
        if expected_run_id is not None:
            where += " AND run_id = ?"
            values.append(expected_run_id)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE turns SET {', '.join(fields)} WHERE {where}", values
            )
            connection.commit()
            return cursor.rowcount == 1

    def transition(
        self,
        turn_id: str,
        status: str,
        *,
        phase: str | None = None,
        message: str | None = None,
        active_approval_id: str | None = None,
        run_id: str | None = None,
        expected_run_id: str | None = None,
        expected_statuses: set[str] | None = None,
        increment_resume: bool = False,
    ) -> dict[str, Any]:
        """Atomically move a turn between coarse lifecycle states."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Turn \u4e0d\u5b58\u5728\uff1a{turn_id}")
            current = str(row["status"])
            if expected_run_id is not None and row["run_id"] != expected_run_id:
                connection.rollback()
                return dict(row)
            if expected_statuses is not None and current not in expected_statuses:
                connection.rollback()
                return dict(row)
            if current in TERMINAL_STATUSES | {"interrupted"}:
                connection.rollback()
                return dict(row)
            allowed = ALLOWED_STATUS_TRANSITIONS.get(current)
            if allowed is not None and status not in allowed:
                raise ValueError(f"\u975e\u6cd5 Turn \u72b6\u6001\u8fc1\u79fb\uff1a{current} -> {status}")
            connection.execute(
                """
                UPDATE turns SET
                    status = ?, phase = ?, message = COALESCE(?, message),
                    active_approval_id = ?, run_id = COALESCE(?, run_id),
                    resume_count = resume_count + ?, version = version + 1,
                    updated_at = ?
                WHERE turn_id = ? AND version = ?
                """,
                (
                    status, phase or status, message, active_approval_id, run_id,
                    1 if increment_resume else 0, utc_now(), turn_id, row["version"],
                ),
            )
            connection.commit()
        return self.get(turn_id) or {}

    def request_cancel(
        self, turn_id: str, message: str = "\u6b63\u5728\u53d6\u6d88"
    ) -> dict[str, Any]:
        """Persist cancellation before signalling the in-memory worker."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Turn \u4e0d\u5b58\u5728\uff1a{turn_id}")
            if row["status"] not in TERMINAL_STATUSES | {"interrupted"}:
                timestamp = utc_now()
                connection.execute(
                    """
                    UPDATE turns SET status = 'cancelling', phase = 'cancelling',
                        message = ?, cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        version = version + 1, updated_at = ?
                    WHERE turn_id = ?
                    """,
                    (message, timestamp, timestamp, turn_id),
                )
            connection.commit()
        return self.get(turn_id) or {}

    def is_cancel_requested(self, turn_id: str) -> bool:
        row = self.get(turn_id)
        return bool(row and row.get("cancel_requested_at"))

    def append_event(
        self,
        turn_id: str,
        seq: int,
        event_name: str,
        payload: dict[str, Any],
        created_at: str | None = None,
        parent_seq: int | None = None,
        trace_id: str | None = None,
        trace_type: str | None = None,
        expected_run_id: str | None = None,
    ) -> bool:
        if seq < 1:
            raise ValueError("Turn event sequence must be positive")
        if parent_seq is not None:
            parent_seq = int(parent_seq)
            if parent_seq < 1 or parent_seq >= seq:
                raise ValueError("Turn event parent sequence must precede the event")
        timestamp = created_at or utc_now()
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if expected_run_id is not None:
                owner = connection.execute(
                    "SELECT run_id FROM turns WHERE turn_id = ?", (turn_id,)
                ).fetchone()
                if owner is None or owner["run_id"] != expected_run_id:
                    connection.rollback()
                    return False
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO turn_events(
                    turn_id, seq, event_name, payload, created_at,
                    parent_seq, trace_id, trace_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id, seq, event_name, encoded, timestamp,
                    parent_seq, trace_id, trace_type,
                ),
            )
            connection.execute(
                """
                UPDATE turns
                SET last_seq = CASE WHEN last_seq < ? THEN ? ELSE last_seq END,
                    updated_at = ?
                WHERE turn_id = ?
                """,
                (seq, seq, timestamp, turn_id),
            )
            connection.commit()
            return inserted.rowcount == 1

    def finish(
        self,
        turn_id: str,
        status: str,
        *,
        error: str | None = None,
        message: str | None = None,
        expected_run_id: str | None = None,
    ) -> bool:
        if status not in TERMINAL_STATUSES | {"interrupted"}:
            raise ValueError(f"未知的 Turn 结束状态：{status}")
        placeholders = ", ".join("?" for _ in TERMINAL_STATUSES | {"interrupted"})
        run_clause = ""
        values: list[Any] = [
            status, status, message, error, utc_now(), turn_id,
            *sorted(TERMINAL_STATUSES | {"interrupted"}),
        ]
        if expected_run_id is not None:
            run_clause = " AND run_id = ?"
            values.append(expected_run_id)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE turns
                SET status = ?, phase = ?, message = COALESCE(?, message),
                    error = ?, active_approval_id = NULL,
                    version = version + 1, updated_at = ?
                WHERE turn_id = ?
                  AND status NOT IN ({placeholders})
                  {run_clause}
                """,
                values,
            )
            connection.commit()
            return cursor.rowcount == 1

    def mark_interrupted(self, message: str = "Gateway 重启，Turn 未完成") -> int:
        recoverable = RUNNING_STATUSES | {"cancelling"}
        placeholders = ", ".join("?" for _ in recoverable)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE turns
                SET status = 'interrupted', phase = 'interrupted',
                    message = ?, updated_at = ?
                WHERE status IN ({placeholders})
                """,
                (message, utc_now(), *sorted(recoverable)),
            )
            connection.commit()
            return cursor.rowcount

    def get(self, turn_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        return self._row_to_dict(row)

    def list_recent(
        self, session_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        with self._connect() as connection:
            if session_id:
                rows = connection.execute(
                    """
                    SELECT * FROM turns
                    WHERE session_id = ?
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (session_id, bounded),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM turns ORDER BY updated_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
        return [dict(row) for row in rows]

    def list_active(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return durable running or suspended turns, including after refresh."""
        statuses = sorted(ACTIVE_STATUSES)
        placeholders = ", ".join("?" for _ in statuses)
        query = f"SELECT * FROM turns WHERE status IN ({placeholders})"
        values: list[Any] = list(statuses)
        if session_id:
            query += " AND session_id = ?"
            values.append(session_id)
        query += " ORDER BY updated_at ASC"
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [dict(row) for row in rows]

    def events_after(self, turn_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT seq, event_name, payload, created_at,
                       parent_seq, trace_id, trace_type
                FROM turn_events
                WHERE turn_id = ? AND seq > ?
                ORDER BY seq ASC
                """,
                (turn_id, max(0, int(after))),
            ).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = {"raw": row["payload"]}
            result.append(
                {
                    "seq": row["seq"],
                    "eventName": row["event_name"],
                    "payload": payload,
                    "createdAt": row["created_at"],
                    "parentSeq": row["parent_seq"],
                    "traceId": row["trace_id"],
                    "traceType": row["trace_type"],
                }
            )
        return result

    def trace_tree(self, turn_id: str) -> dict[str, Any]:
        """Return the durable parent-child trace for one Agent turn.

        The flat event journal remains the source of truth.  This view is
        intentionally derived at read time so old clients can continue to
        consume ``/events`` while diagnostics and a future timeline UI can
        render model -> tool -> result/approval branches.
        """
        events = self.events_after(turn_id)
        by_seq = {item["seq"]: item for item in events}
        children: dict[int, list[dict[str, Any]]] = {}
        roots: list[dict[str, Any]] = []
        for event in events:
            parent = event.get("parentSeq")
            if parent is not None and parent in by_seq and parent != event["seq"]:
                children.setdefault(parent, []).append(event)
            else:
                roots.append(event)

        def node(event: dict[str, Any], path: set[int] | None = None) -> dict[str, Any]:
            path = set(path or ())
            seq = event["seq"]
            # A corrupt/malicious journal must not make the diagnostic endpoint
            # recurse forever.  Keep the event visible and stop that branch.
            if seq in path:
                return {**event, "children": []}
            path.add(seq)
            return {
                **event,
                "children": [node(child, path) for child in children.get(seq, [])],
            }

        return {
            "turnId": turn_id,
            "eventCount": len(events),
            "roots": [node(event) for event in roots],
            # The flat list makes the endpoint useful to clients that want to
            # build a custom visualization without walking the tree.
            "events": events,
        }
