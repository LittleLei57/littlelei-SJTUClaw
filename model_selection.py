"""Persistent selection for models exposed by the configured LLM API."""

from __future__ import annotations

from threading import RLock

from config import DEFAULT_MODEL, SJTU_API_MODELS, normalize_sjtu_model
from session_store import utc_now
from state_database import StateDatabase


DOCUMENT_NAME = "model_selection"


class ModelSelectionStore:
    """Persist only the model id; credentials and endpoints remain server-side."""

    def __init__(self, data_dir):
        self.database = StateDatabase(data_dir)
        self._lock = RLock()

    def current(self) -> str:
        with self._lock:
            value = self.database.read(DOCUMENT_NAME, {})
        candidate = value.get("model") if isinstance(value, dict) else None
        try:
            return normalize_sjtu_model(candidate)
        except ValueError:
            try:
                return normalize_sjtu_model(DEFAULT_MODEL)
            except ValueError:
                return next(iter(SJTU_API_MODELS))

    def select(self, model: str) -> str:
        selected = normalize_sjtu_model(model)
        with self._lock:
            self.database.write(
                DOCUMENT_NAME,
                {"model": selected, "updatedAt": utc_now()},
            )
        return selected

    @staticmethod
    def options() -> list[dict[str, str]]:
        return [
            {"id": model_id, "label": label}
            for model_id, label in SJTU_API_MODELS.items()
        ]
