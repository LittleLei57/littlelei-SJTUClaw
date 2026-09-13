"""Safe, reviewable candidates for agent-managed long-term memory."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import uuid
from functools import wraps
from threading import RLock, Thread

from memory_store import MEMORY_TYPES, Memory, MemoryStore
from session_store import utc_now
from state_database import StateDatabase


_SENSITIVE = re.compile(
    r"(?:sk-|tvly-|api[ _-]?key|secret|password|密码|口令|验证码|token|身份证|银行卡)",
    re.I,
)
_TEMPORARY = re.compile(r"(?:刚刚|这次|临时|今天|明天|今晚|本次|一次性)")
_RULES = (
    # Explicit instructions about how the assistant should behave are stable
    # preferences too (for example, tool/path conventions), not merely chat.
    ("preference", re.compile(
        r"(?:请|你)?(?:以后|下次|今后).{0,160}(?:记住|使用|不要|务必|必须|优先).{0,160}"
    )),
    ("preference", re.compile(r"(?:请|帮我)?记住(?:一下)?[，,:：]\s*.{1,160}")),
    ("preference", re.compile(r"(?:我(?:比较)?(?:喜欢|偏好|习惯|更喜欢)|我(?:不再|不)(?:喜欢|偏好|习惯)|以后(?:请|都)|请一直|不要再).{1,120}")),
    ("profile", re.compile(r"(?:我叫|我是|我的专业是|我就读于|我的身份是).{1,120}")),
    ("project", re.compile(r"(?:我(?:正在|目前在)(?:做|开发|实现|研究)|我的项目是|我们的项目是|长期目标是|我的目标是).{1,160}")),
    ("course", re.compile(r"(?:我(?:正在|这学期在)(?:学|上)|我的课程是).{1,120}")),
)


def _locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


@dataclass(frozen=True)
class MemoryCandidate:
    candidate_id: str
    content: str
    memory_type: str
    confidence: float
    session_id: str
    source_text: str
    status: str
    created_at: str
    resolved_at: str | None = None
    memory_id: str | None = None
    reason: str = "用户明确表达了可能长期有效的信息"
    conflict_memory_ids: tuple[str, ...] = ()
    model_assisted: bool = False

    def to_dict(self) -> dict:
        return {
            "candidateId": self.candidate_id,
            "content": self.content,
            "type": self.memory_type,
            "confidence": self.confidence,
            "sessionId": self.session_id,
            "sourceText": self.source_text,
            "status": self.status,
            "createdAt": self.created_at,
            "resolvedAt": self.resolved_at,
            "memoryId": self.memory_id,
            "reason": self.reason,
            "conflictMemoryIds": list(self.conflict_memory_ids),
            "modelAssisted": self.model_assisted,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "MemoryCandidate":
        return cls(
            str(value["candidateId"]), str(value["content"]), str(value["type"]),
            float(value.get("confidence", 0.8)), str(value["sessionId"]),
            str(value.get("sourceText", value["content"])), str(value.get("status", "pending")),
            str(value["createdAt"]), value.get("resolvedAt"), value.get("memoryId"),
            str(value.get("reason", "用户明确表达了可能长期有效的信息")),
            tuple(str(item) for item in value.get("conflictMemoryIds", [])),
            bool(value.get("modelAssisted", False)),
        )


class MemoryCandidateStore:
    def __init__(self, data_dir: str | Path, memory_store: MemoryStore, model=None):
        self.path = Path(data_dir) / "memory_candidates.json"
        self.memory_store = memory_store
        self.model = model
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.database = StateDatabase(self.path.parent)
        self.database.ensure_imported("memory_candidates", self.path, [])

    def _read(self) -> list[MemoryCandidate]:
        data = self.database.read("memory_candidates", [])
        return [MemoryCandidate.from_dict(item) for item in data]

    def _write(self, items: list[MemoryCandidate]) -> None:
        self.database.write("memory_candidates", [item.to_dict() for item in items])

    @_locked
    def list(self, status: str | None = None, session_id: str | None = None) -> list[MemoryCandidate]:
        items = self._read()
        if status:
            items = [item for item in items if item.status == status]
        if session_id:
            items = [item for item in items if item.session_id == session_id]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    @_locked
    def propose_from_message(self, text: str, session_id: str) -> list[MemoryCandidate]:
        source = " ".join(text.strip().split())
        if not source or "?" in source or "？" in source or _SENSITIVE.search(source) or _TEMPORARY.search(source):
            return []
        existing = self.memory_store.list(active_only=True)
        candidates = self._read()
        created: list[MemoryCandidate] = []
        for memory_type, pattern in _RULES:
            match = pattern.search(source)
            if not match:
                continue
            content = match.group(0).strip("。！!，, ")
            folded = content.casefold()
            if any(
                _similar(folded, item.content.casefold())
                and _polarity(content) == _polarity(item.content)
                for item in existing
            ):
                continue
            if any(item.status == "pending" and _similar(folded, item.content.casefold()) for item in candidates):
                continue
            item = MemoryCandidate(
                f"mc_{uuid.uuid4().hex[:10]}", content, memory_type, 0.9,
                session_id, source[:500], "pending", utc_now(),
                reason="检测到用户明确表达的长期信息",
                conflict_memory_ids=tuple(self._find_conflicts(content, memory_type)),
            )
            candidates.append(item)
            created.append(item)
            break
        if created:
            self._write(candidates)
            if self.model is not None:
                for item in created:
                    Thread(
                        target=self._refine_safely,
                        args=(item.candidate_id,),
                        name=f"memory-refine-{item.candidate_id}",
                        daemon=True,
                    ).start()
        return created

    @_locked
    def update(self, candidate_id: str, content: str, memory_type: str | None = None) -> MemoryCandidate:
        text = " ".join(content.strip().split())
        if not text:
            raise ValueError("候选记忆内容不能为空。")
        items = self._read()
        current = next((item for item in items if item.candidate_id == candidate_id), None)
        if current is None:
            raise KeyError(f"候选记忆不存在：{candidate_id}")
        if current.status != "pending":
            raise ValueError(f"候选记忆已经处理：{current.status}")
        next_type = memory_type or current.memory_type
        if next_type not in MEMORY_TYPES:
            raise ValueError(f"候选记忆类型无效：{next_type}")
        updated = MemoryCandidate(
            current.candidate_id, text, next_type, current.confidence,
            current.session_id, current.source_text, current.status, current.created_at,
            current.resolved_at, current.memory_id, current.reason,
            tuple(self._find_conflicts(text, next_type)), current.model_assisted,
        )
        self._write([updated if item.candidate_id == candidate_id else item for item in items])
        return updated

    @_locked
    def resolve(self, candidate_id: str, accepted: bool) -> tuple[MemoryCandidate, Memory | None]:
        items = self._read()
        current = next((item for item in items if item.candidate_id == candidate_id), None)
        if current is None:
            raise KeyError(f"候选记忆不存在：{candidate_id}")
        if current.status != "pending":
            raise ValueError(f"候选记忆已经处理：{current.status}")
        memory = None
        if accepted:
            conflicts = [
                item for item in self.memory_store.list(active_only=True)
                if item.memory_id in current.conflict_memory_ids
            ]
            if conflicts:
                memory = self.memory_store.update(
                    conflicts[0].memory_id,
                    content=current.content,
                    memory_type=current.memory_type,
                    importance=4 if current.memory_type in {"preference", "profile"} else 3,
                    source=f"agent_candidate:{current.session_id}",
                )
                for duplicate in conflicts[1:]:
                    self.memory_store.delete(duplicate.memory_id)
            else:
                memory = self.memory_store.add(
                    current.content,
                    memory_type=current.memory_type,
                    importance=4 if current.memory_type in {"preference", "profile"} else 3,
                    source=f"agent_candidate:{current.session_id}",
                )
        resolved = MemoryCandidate(
            current.candidate_id, current.content, current.memory_type, current.confidence,
            current.session_id, current.source_text, "accepted" if accepted else "rejected",
            current.created_at, utc_now(), memory.memory_id if memory else None,
            current.reason, current.conflict_memory_ids, current.model_assisted,
        )
        self._write([resolved if item.candidate_id == candidate_id else item for item in items])
        return resolved, memory

    def _refine_safely(self, candidate_id: str) -> None:
        try:
            self._refine(candidate_id)
        except Exception:
            # Memory assistance is best-effort and must never affect chat.
            return

    @_locked
    def _refine(self, candidate_id: str) -> None:
        items = self._read()
        current = next((item for item in items if item.candidate_id == candidate_id), None)
        if current is None or current.status != "pending" or self.model is None:
            return
        prompt = (
            "请把候选长期记忆整理成简洁、稳定、第三人称可读的中文陈述。"
            "不要补充用户没说过的信息，不要保留临时信息或秘密。只输出 JSON："
            '{"content":"...","type":"preference|profile|project|course|fact",'
            '"reason":"为什么值得长期记住"}。\n'
            f"用户原话：{current.source_text}\n候选：{current.content}"
        )
        raw = self.model.complete([
            {"role": "system", "content": "你是保守的长期记忆整理器。"},
            {"role": "user", "content": prompt},
        ])
        value = _parse_json_object(raw)
        content = " ".join(str(value.get("content", current.content)).strip().split())
        memory_type = str(value.get("type", current.memory_type))
        reason = " ".join(str(value.get("reason", current.reason)).strip().split())
        if not content or memory_type not in MEMORY_TYPES or _SENSITIVE.search(content):
            return
        refined = MemoryCandidate(
            current.candidate_id, content, memory_type, current.confidence,
            current.session_id, current.source_text, current.status, current.created_at,
            current.resolved_at, current.memory_id, reason or current.reason,
            tuple(self._find_conflicts(content, memory_type)), True,
        )
        latest = self._read()
        target = next((item for item in latest if item.candidate_id == candidate_id), None)
        if target is None or target.status != "pending":
            return
        self._write([refined if item.candidate_id == candidate_id else item for item in latest])

    def _find_conflicts(self, content: str, memory_type: str) -> list[str]:
        target = _topic_tokens(content)
        if not target:
            return []
        conflicts = []
        for memory in self.memory_store.list(active_only=True):
            if memory.memory_type != memory_type:
                continue
            other = _topic_tokens(memory.content)
            overlap = len(target & other) / max(1, min(len(target), len(other)))
            if overlap >= 0.65 and content.casefold() != memory.content.casefold():
                conflicts.append(memory.memory_id)
        return conflicts


def _similar(left: str, right: str) -> bool:
    if left in right or right in left:
        return True
    left_tokens = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", left))
    right_tokens = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", right))
    return bool(left_tokens) and len(left_tokens & right_tokens) / len(left_tokens | right_tokens) >= 0.8


def _topic_tokens(text: str) -> set[str]:
    folded = re.sub(r"(?:用户|我|比较|更|不再|不|喜欢|偏好|习惯|请|一直|以后)", "", text.casefold())
    return set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", folded))


def _polarity(text: str) -> str:
    return "negative" if re.search(r"(?:不再|不|不要|讨厌|避免)", text) else "positive"


def _parse_json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("模型未返回 JSON object。")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("模型返回的候选记忆不是 object。")
    return value
