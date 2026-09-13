"""Step 3：跨 Session 的结构化长期记忆。

Memory 与聊天历史分离，只保存用户确认过的稳定事实和偏好。``MemoryStore``
负责增删改查、简单相关性检索和 SQLite 持久化；候选记忆的审核流程位于
``memory_candidates.py``，避免模型把临时信息直接写入长期记忆。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from functools import wraps
from threading import RLock

from session_store import utc_now
from state_database import StateDatabase


MEMORY_TYPES = {"preference", "profile", "project", "course", "fact"}


def _locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


@dataclass(frozen=True)
class Memory:
    """一条带稳定 ID、创建时间和标签的长期记忆。"""

    memory_id: str
    content: str
    created_at: str
    memory_type: str = "fact"
    importance: int = 3
    source: str = "user"
    updated_at: str = ""
    expires_at: str | None = None
    status: str = "active"

    def to_dict(self) -> dict:
        return {
            "memoryId": self.memory_id,
            "content": self.content,
            "type": self.memory_type,
            "importance": self.importance,
            "source": self.source,
            "status": self.status,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at or self.created_at,
            "expiresAt": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict, source: Path) -> "Memory":
        required = {"memoryId", "content", "createdAt"}
        missing = required - data.keys()
        if missing:
            raise ValueError(f"Memory 文件缺少字段 {sorted(missing)}：{source}")
        return cls(
            memory_id=str(data["memoryId"]),
            content=str(data["content"]),
            created_at=str(data["createdAt"]),
            memory_type=str(data.get("type", "fact")),
            importance=int(data.get("importance", 3)),
            source=str(data.get("source", "user")),
            updated_at=str(data.get("updatedAt", data["createdAt"])),
            expires_at=data.get("expiresAt"),
            status=str(data.get("status", "active")),
        )

    def is_active(self) -> bool:
        if self.status != "active":
            return False
        if not self.expires_at:
            return True
        try:
            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            return expires > datetime.now(timezone.utc)
        except ValueError:
            return False


class MemoryStore:
    """线程安全的长期记忆仓库与轻量相关性检索器。"""

    def __init__(self, data_dir: str | Path = "data"):
        self.path = Path(data_dir) / "memories.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.database = StateDatabase(self.path.parent)
        self.database.ensure_imported("memories", self.path, [])

    def _read(self) -> list[dict]:
        data = self.database.read("memories", [])
        if not isinstance(data, list):
            raise ValueError(f"Memory 文件顶层必须是列表：{self.path}")
        return data

    def _write(self, data: list[dict]) -> None:
        try:
            self.database.write("memories", data)
        except (OSError, sqlite3.Error) as exc:
            raise OSError(f"保存 Memory 失败：{self.database.path}（{exc}）") from exc

    @_locked
    def list(self, active_only: bool = False) -> list[Memory]:
        memories = [Memory.from_dict(item, self.path) for item in self._read()]
        return [item for item in memories if item.is_active()] if active_only else memories

    @_locked
    def add(
        self,
        content: str,
        memory_type: str = "fact",
        importance: int = 3,
        source: str = "user",
        expires_at: str | None = None,
    ) -> Memory:
        text = content.strip()
        if not text:
            raise ValueError("Memory 内容不能为空。")
        self._validate_metadata(memory_type, importance, expires_at)
        memories = self.list()
        now = utc_now()
        memory = Memory(
            self._next_memory_id(memories), text, now, memory_type,
            importance, source.strip() or "user", now, expires_at,
        )
        self._write([item.to_dict() for item in [*memories, memory]])
        return memory

    @staticmethod
    def _next_memory_id(memories: list[Memory]) -> str:
        """Return a stable, human-friendly ID without renumbering old records."""
        numbers = []
        for item in memories:
            match = re.fullmatch(r"mem_(\d+)", item.memory_id)
            if match:
                numbers.append(int(match.group(1)))
        return f"mem_{max(numbers, default=0) + 1}"

    @_locked
    def update(
        self,
        memory_id: str,
        content: str | None = None,
        memory_type: str | None = None,
        importance: int | None = None,
        expires_at: str | None = None,
        source: str | None = None,
    ) -> Memory:
        memories = self.list()
        current = next((item for item in memories if item.memory_id == memory_id), None)
        if current is None:
            raise KeyError(f"Memory 不存在：{memory_id}")
        new_content = current.content if content is None else content.strip()
        if not new_content:
            raise ValueError("Memory 内容不能为空。")
        new_type = memory_type or current.memory_type
        new_importance = current.importance if importance is None else importance
        new_expiry = current.expires_at if expires_at is None else (expires_at or None)
        self._validate_metadata(new_type, new_importance, new_expiry)
        updated = Memory(
            current.memory_id, new_content, current.created_at, new_type,
            new_importance, source or current.source, utc_now(), new_expiry, current.status,
        )
        self._write([updated.to_dict() if item.memory_id == memory_id else item.to_dict() for item in memories])
        return updated

    @_locked
    def search(self, query: str, limit: int = 8) -> list[Memory]:
        if limit < 1:
            raise ValueError("Memory 搜索数量必须大于 0。")
        query = query.strip()
        memories = self.list(active_only=True)
        query_tokens = _tokens(query)

        def score(item: Memory) -> tuple[float, str]:
            content = item.content.casefold()
            overlap = len(query_tokens & _tokens(content))
            exact = 10 if query and (query.casefold() in content or content in query.casefold()) else 0
            pinned = 4 if item.memory_type in {"profile", "preference"} else 0
            return exact + overlap * 2 + pinned + item.importance * 0.25, item.updated_at

        return sorted(memories, key=score, reverse=True)[:limit]

    @_locked
    def delete(self, memory_id: str) -> Memory:
        memories = self.list()
        deleted = next((item for item in memories if item.memory_id == memory_id), None)
        if deleted is None:
            raise KeyError(f"Memory 不存在：{memory_id}")
        self._write([item.to_dict() for item in memories if item.memory_id != memory_id])
        return deleted

    @_locked
    def delete_by_text(self, text: str) -> Memory:
        query = text.strip().casefold()
        if not query:
            raise ValueError("Memory 删除描述不能为空。")
        matches = [
            item for item in self.list(active_only=True)
            if query in item.content.casefold() or item.content.casefold() in query
        ]
        if len(matches) == 1:
            return self.delete(matches[0].memory_id)
        candidates = self.search(text, limit=3)
        detail = "、".join(f"{item.memory_id}（{item.content[:30]}）" for item in candidates)
        if matches:
            raise ValueError(f"描述匹配到多条 Memory，请改用 ID：{detail}")
        raise ValueError(f"无法唯一确定要删除的 Memory，请从候选中选择 ID：{detail or '无'}")

    @staticmethod
    def _validate_metadata(memory_type: str, importance: int, expires_at: str | None) -> None:
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"Memory type 必须是：{', '.join(sorted(MEMORY_TYPES))}")
        if isinstance(importance, bool) or not 1 <= importance <= 5:
            raise ValueError("Memory importance 必须在 1 到 5 之间。")
        if expires_at:
            try:
                datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("expiresAt 必须是 ISO 8601 时间。") from exc


def _tokens(text: str) -> set[str]:
    folded = text.casefold()
    words = set(re.findall(r"[a-z0-9_]+", folded))
    chinese_runs = re.findall(r"[\u4e00-\u9fff]+", folded)
    for run in chinese_runs:
        words.update(run)
        words.update(run[index:index + 2] for index in range(len(run) - 1))
    return words
