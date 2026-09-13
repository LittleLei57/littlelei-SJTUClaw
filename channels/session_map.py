"""Persistent mapping from external conversations to SJTUClaw Sessions."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock

from session_store import SessionStore, utc_now
from state_database import StateDatabase
from channels.base import InboundMessage


class ChannelSessionMap:
    def __init__(self, data_dir: str | Path, sessions: SessionStore):
        self.path = Path(data_dir) / "channel-sessions.json"
        self.sessions = sessions
        self._lock = Lock()
        self.database = StateDatabase(self.path.parent)
        self.database.ensure_imported("channel_sessions", self.path, {})

    def get_or_create(
        self, channel: str, external_user_id: str, conversation_id: str,
        title: str | None = None,
    ) -> str:
        key = self._key(channel, external_user_id, conversation_id)
        with self._lock:
            data = self._read()
            item = data.get(key)
            if isinstance(item, dict):
                session_id = item.get("sessionId")
                try:
                    self.sessions.get(session_id)
                    item["lastSeenAt"] = utc_now()
                    self._write(data)
                    return session_id
                except (KeyError, ValueError):
                    pass
            session = self.sessions.create(
                title or f"{channel} · {conversation_id[-8:]}", make_current=False
            )
            data[key] = {
                "channel": channel, "externalUserId": external_user_id,
                "conversationId": conversation_id, "sessionId": session.session_id,
                "createdAt": utc_now(), "lastSeenAt": utc_now(),
            }
            self._write(data)
            return session.session_id

    def remember_route(self, session_id: str, message: InboundMessage) -> None:
        """Persist the latest reply destination for proactive notifications."""
        with self._lock:
            data = self._read()
            for item in data.values():
                if not isinstance(item, dict) or item.get("sessionId") != session_id:
                    continue
                item["replyToken"] = message.reply_token
                item["metadata"] = message.metadata
                item["lastSeenAt"] = utc_now()
                self._write(data)
                return

    def route_for_session(self, session_id: str, channel: str | None = None) -> dict | None:
        """Return the latest route, optionally restricted to one channel."""
        requested_channel = str(channel or "").strip().lower() or None
        with self._lock:
            matches = [
                item for item in self._read().values()
                if (
                    isinstance(item, dict)
                    and item.get("sessionId") == session_id
                    and (requested_channel is None or item.get("channel") == requested_channel)
                )
            ]
        return max(matches, key=lambda item: item.get("lastSeenAt", ""), default=None)

    @staticmethod
    def _key(channel: str, external_user_id: str, conversation_id: str) -> str:
        return json.dumps([channel, external_user_id, conversation_id], ensure_ascii=False)

    def _read(self) -> dict:
        data = self.database.read("channel_sessions", {})
        if not isinstance(data, dict):
            raise ValueError(f"Channel Session 映射必须是 object：{self.path}")
        return data

    def _write(self, data: dict) -> None:
        self.database.write("channel_sessions", data)
