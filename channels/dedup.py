"""Bounded persistent event idempotency store."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock

from session_store import utc_now
from state_database import StateDatabase


class EventDeduplicator:
    def __init__(self, data_dir: str | Path, limit: int = 2000):
        self.path = Path(data_dir) / "channel-events.json"
        self.limit = limit
        self._lock = Lock()
        self.database = StateDatabase(self.path.parent)
        self.database.ensure_imported("channel_events", self.path, {})

    def claim(self, channel: str, event_id: str) -> bool:
        key = f"{channel}:{event_id}"
        with self._lock:
            data = self._read()
            if key in data:
                return False
            data[key] = utc_now()
            if len(data) > self.limit:
                data = dict(list(data.items())[-self.limit:])
            self.database.write("channel_events", data)
            return True

    def release(self, channel: str, event_id: str) -> None:
        """Allow the platform to retry an event when processing failed."""
        key = f"{channel}:{event_id}"
        with self._lock:
            data = self._read()
            if data.pop(key, None) is None:
                return
            self.database.write("channel_events", data)

    def _read(self) -> dict:
        try:
            value = self.database.read("channel_events", {})
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
