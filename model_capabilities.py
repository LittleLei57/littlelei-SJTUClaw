"""Persist provider/model capability observations without storing credentials."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from urllib.parse import urlsplit

from state_database import StateDatabase


DOCUMENT_NAME = "model_capabilities"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _profile_key(base_url: str, model: str) -> str:
    return f"{base_url.rstrip('/').lower()}::{model.strip().lower()}"


class ModelCapabilityStore:
    """A small cache keyed by provider endpoint and model name.

    Only observed capability booleans are persisted. API keys, prompts and
    response content never enter this document.
    """

    def __init__(self, data_dir):
        self.database = StateDatabase(data_dir)
        self._lock = RLock()

    def get(self, base_url: str, model: str) -> dict:
        with self._lock:
            profiles = self.database.read(DOCUMENT_NAME, {})
            value = profiles.get(_profile_key(base_url, model), {})
        return dict(value) if isinstance(value, dict) else {}

    def update(self, base_url: str, model: str, **capabilities) -> dict:
        allowed = {"nativeTools", "vision", "streaming", "jsonProtocol"}
        updates = {
            key: value
            for key, value in capabilities.items()
            if key in allowed and isinstance(value, bool)
        }
        if not updates:
            return self.get(base_url, model)
        with self._lock:
            profiles = self.database.read(DOCUMENT_NAME, {})
            if not isinstance(profiles, dict):
                profiles = {}
            key = _profile_key(base_url, model)
            current = profiles.get(key, {})
            if not isinstance(current, dict):
                current = {}
            observed = dict(current.get("observed", {}))
            observed.update(updates)
            current.update(
                {
                    "model": model,
                    "provider": urlsplit(base_url).netloc or base_url,
                    "observed": observed,
                    "updatedAt": _now(),
                }
            )
            profiles[key] = current
            self.database.write(DOCUMENT_NAME, profiles)
            return dict(current)

    def reset(self, base_url: str, model: str) -> bool:
        with self._lock:
            profiles = self.database.read(DOCUMENT_NAME, {})
            if not isinstance(profiles, dict):
                return False
            removed = profiles.pop(_profile_key(base_url, model), None) is not None
            if removed:
                self.database.write(DOCUMENT_NAME, profiles)
            return removed

    def describe(self, base_url: str, model: str, *, inferred_vision: bool) -> dict:
        stored = self.get(base_url, model)
        observed = stored.get("observed", {})
        if not isinstance(observed, dict):
            observed = {}

        def item(name: str, inferred=None) -> dict:
            if isinstance(observed.get(name), bool):
                return {"supported": observed[name], "source": "observed"}
            if isinstance(inferred, bool):
                return {"supported": inferred, "source": "inferred"}
            return {"supported": None, "source": "unknown"}

        return {
            "model": model,
            "provider": urlsplit(base_url).netloc or base_url,
            "nativeTools": item("nativeTools"),
            "vision": item("vision", inferred_vision),
            "streaming": item("streaming"),
            "jsonProtocol": item("jsonProtocol"),
            "updatedAt": stored.get("updatedAt"),
        }
