"""Helpers for separating a user's request from transport metadata."""

from __future__ import annotations


ATTACHMENT_MARKER = "[attached_files]"


def user_intent_text(message: str | None) -> str:
    """Return only the human-authored part before attachment transport data."""
    text = str(message or "").strip()
    marker = text.find(ATTACHMENT_MARKER)
    if marker >= 0:
        text = text[:marker].rstrip()
    return text
