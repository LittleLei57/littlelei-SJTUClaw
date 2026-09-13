"""Step 2：多 Session 数据模型与持久化。

``Session`` 保存标题、messages、summary、Workspace、附件和 Tool 审计引用；
``SessionStore`` 提供创建、切换、重命名、删除、加载与原子保存。每个 Session
拥有稳定 ID，进程重启后仍可恢复；内部锁与临时文件替换避免并发写坏 JSON。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from threading import Lock, RLock, get_ident
import time

from llm_client import Message
from goal_state import normalize_goal


_JSON_LOCKS: dict[str, RLock] = {}
_JSON_LOCKS_GUARD = Lock()


def _json_lock(path: Path) -> RLock:
    key = str(path.absolute())
    with _JSON_LOCKS_GUARD:
        lock = _JSON_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _JSON_LOCKS[key] = lock
        return lock


def utc_now() -> str:
    """返回带时区的 UTC ISO 时间，作为持久化时间字段的统一格式。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_WEB_CITATION_RE = re.compile(r"\[W\d+\]")
_CITATION_FIELDS = (
    "label", "kind", "url", "title", "filename", "page", "slide",
    "section", "block", "attachmentId", "method",
)


def preserve_message_citations(
    messages: list[Message],
    citation_index: list[dict] | None,
    activity: list[dict] | None = None,
    tool_trace: list[dict] | None = None,
) -> None:
    """Persist exact web sources on visible assistant messages.

    Older sessions reconstructed citations from adjacent Tool protocol
    messages. Compaction intentionally removes that protocol chatter, so the
    association must be copied onto the answer before those messages vanish.
    Turn/call-id evidence is preferred; the session index is only used when a
    label has one unambiguous URL.
    """

    def clean_refs(citations) -> list[dict]:
        refs: list[dict] = []
        seen: set[str] = set()
        for citation in citations or []:
            if not isinstance(citation, dict) or not citation.get("label"):
                continue
            item = {
                key: citation[key]
                for key in _CITATION_FIELDS
                if citation.get(key) is not None
            }
            fingerprint = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            refs.append(item)
        return refs

    def citations_from_trace(event: dict | None) -> list[dict]:
        result = event.get("result") if isinstance(event, dict) else None
        output = result.get("output") if isinstance(result, dict) else None
        citations = output.get("citations") if isinstance(output, dict) else None
        return clean_refs(citations)

    trace_items = [
        {"event": event, "used": False}
        for event in (tool_trace or [])
        if isinstance(event, dict)
    ]
    refs_by_turn: dict[str, list[dict]] = {}
    for activity_event in activity or []:
        if not isinstance(activity_event, dict) or activity_event.get("type") != "tool_result":
            continue
        turn_id = str(activity_event.get("turnId") or "")
        data = activity_event.get("data") or {}
        tool = str(data.get("tool") or "")
        call_id = str(data.get("callId") or "")
        candidates = [
            item for item in trace_items
            if not item["used"] and (not tool or item["event"].get("tool") == tool)
        ]
        match = next(
            (
                item for item in candidates
                if call_id and str(item["event"].get("callId") or "") == call_id
            ),
            None,
        )
        if match is None and candidates:
            # Legacy activity entries did not persist callId. Tool events and
            # activity are append-only, so ordered matching is safer than
            # guessing from a duplicated provider label such as [W1].
            match = candidates[0]
        if match is None:
            continue
        match["used"] = True
        refs = citations_from_trace(match["event"])
        if refs and turn_id:
            refs_by_turn.setdefault(turn_id, []).extend(refs)

    finals = [
        event for event in (activity or [])
        if isinstance(event, dict)
        and event.get("type") == "assistant_final"
        and event.get("turnId")
    ]
    final_cursor = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = str(message.get("content") or "")
        metadata = dict(message.get("metadata") or {})
        if metadata.get("citationRefs"):
            continue
        match_index = -1
        for index in range(final_cursor, len(finals)):
            preview = str((finals[index].get("data") or {}).get("contentPreview") or "")
            if (
                preview
                and content
                and (
                    preview == content
                    or content.startswith(preview)
                    or preview.startswith(content)
                )
            ):
                match_index = index
                break
        if match_index < 0:
            continue
        final_cursor = match_index + 1
        turn_id = str(finals[match_index].get("turnId") or "")
        refs = clean_refs(refs_by_turn.get(turn_id, []))
        if refs:
            metadata["citationRefs"] = refs
            message["metadata"] = metadata

    # Last-resort migration for sessions whose old activity log is incomplete.
    # Resolve only labels with one unique target; ambiguous legacy [W1]
    # entries deliberately remain plain text instead of linking to the wrong
    # page.
    candidates_by_label: dict[str, dict[str, dict]] = {}
    for citation in citation_index or []:
        if not isinstance(citation, dict):
            continue
        label = str(citation.get("label") or "")
        url = str(citation.get("url") or "")
        if not label or not url:
            continue
        candidates_by_label.setdefault(label, {})[url] = citation
    unique_by_label = {
        label: next(iter(items.values()))
        for label, items in candidates_by_label.items()
        if len(items) == 1
    }
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = str(message.get("content") or "")
        labels = list(dict.fromkeys(_WEB_CITATION_RE.findall(content)))
        if not labels:
            continue
        metadata = dict(message.get("metadata") or {})
        refs = clean_refs(metadata.get("citationRefs") or [])
        existing = {str(item.get("label") or "") for item in refs}
        for label in labels:
            if label not in existing and label in unique_by_label:
                refs.extend(clean_refs([unique_by_label[label]]))
        if refs:
            metadata["citationRefs"] = refs
            message["metadata"] = metadata


@dataclass
class Session:
    """一个可持久化会话及其上下文、附件与运行状态。"""

    session_id: str
    title: str
    messages: list[Message] = field(default_factory=list)
    summary: str = ""
    # Metadata for the latest successful compaction.  It is intentionally
    # separate from the human-readable Summary so old sessions remain
    # compatible and the UI can explain exactly what was summarized.
    summary_meta: dict = field(default_factory=dict)
    tool_trace: list[dict] = field(default_factory=list)
    # Compacting replaces old visible messages, but citation targets must
    # remain available so later answers can still link to an earlier search.
    citation_index: list[dict] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    workspace: str | None = None
    # Bounded audit trail for explicit stale-Workspace migrations.
    workspace_history: list[dict] = field(default_factory=list)
    skill_usage: list[dict] = field(default_factory=list)
    active_skill: dict | None = None
    # Lightweight persisted Goal/Spec state.  Missing in old session files is
    # intentionally treated as None for backwards compatibility.
    goal_state: dict | None = None
    activity: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict:
        return {
            "sessionId": self.session_id,
            "title": self.title,
            "messages": self.messages,
            "summary": self.summary,
            "summaryMeta": self.summary_meta,
            "toolTrace": self.tool_trace,
            "citationIndex": self.citation_index,
            "attachments": self.attachments,
            "workspace": self.workspace,
            "workspaceHistory": self.workspace_history,
            "skillUsage": self.skill_usage,
            "activeSkill": self.active_skill,
            "goalState": normalize_goal(self.goal_state),
            "activity": self.activity,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict, source: Path) -> "Session":
        required = {"sessionId", "title", "messages", "createdAt", "updatedAt"}
        missing = required - data.keys()
        if missing:
            raise ValueError(f"Session 文件缺少字段 {sorted(missing)}：{source}")
        if not isinstance(data["messages"], list):
            raise ValueError(f"Session messages 必须是列表：{source}")
        messages = []
        for item in data["messages"]:
            # Migration: old versions wrote automatic protocol feedback as if it
            # were user input. Keep it available to the model, but stop showing
            # or counting it as a user message.
            if (
                isinstance(item, dict)
                and item.get("role") == "user"
                and str(item.get("content", "")).lstrip().startswith("[protocol_error]")
            ):
                migrated = dict(item)
                migrated["role"] = "system"
                migrated["metadata"] = {**dict(item.get("metadata") or {}), "internal": True, "kind": "protocol_error"}
                messages.append(migrated)
            else:
                messages.append(item)
        citation_index = data.get("citationIndex", [])
        if not isinstance(citation_index, list):
            citation_index = []
        # Migrate sessions created before citationIndex was introduced while
        # their full tool trace is still available.
        if not citation_index:
            for event in data.get("toolTrace", []) or []:
                result = event.get("result") if isinstance(event, dict) else None
                output = result.get("output") if isinstance(result, dict) else None
                for citation in (output or {}).get("citations", []) if isinstance(output, dict) else []:
                    if not isinstance(citation, dict) or not citation.get("url"):
                        continue
                    citation_index.append({
                        "label": str(citation.get("label") or ""),
                        "url": str(citation.get("url") or ""),
                        "title": str(citation.get("title") or citation.get("url") or ""),
                        "kind": str(citation.get("kind") or "web"),
                    })
            citation_index = citation_index[-500:]
        tool_trace = data.get("toolTrace", [])
        activity = data.get("activity", [])
        preserve_message_citations(
            messages,
            citation_index,
            activity if isinstance(activity, list) else [],
            tool_trace if isinstance(tool_trace, list) else [],
        )
        return cls(
            session_id=str(data["sessionId"]),
            title=str(data["title"]),
            messages=messages,
            summary=str(data.get("summary", "")),
            summary_meta=dict(data.get("summaryMeta") or {}),
            tool_trace=tool_trace,
            citation_index=citation_index,
            attachments=data.get("attachments", []),
            workspace=data.get("workspace"),
            workspace_history=data.get("workspaceHistory", []) if isinstance(data.get("workspaceHistory", []), list) else [],
            skill_usage=data.get("skillUsage", []),
            active_skill=data.get("activeSkill"),
            goal_state=normalize_goal(data.get("goalState")),
            activity=activity,
            created_at=str(data["createdAt"]),
            updated_at=str(data["updatedAt"]),
        )


class SessionStore:
    """每个 Session 独立保存，避免所有历史挤在同一个文件中。"""

    DEFAULT_ID = "default"

    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self.sessions_dir = self.data_dir / "sessions"
        self.state_file = self.data_dir / "state.json"
        self.sequence_file = self.data_dir / "session-sequence.json"
        self._sequence_lock = Lock()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._recover_temp_files()
        self._initialize_session_sequence()
        if not self.list_sessions():
            self.save(Session(self.DEFAULT_ID, "默认会话"))
        self._ensure_current_session()

    def _path(self, session_id: str) -> Path:
        if not session_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in session_id):
            raise ValueError("非法 sessionId。")
        return self.sessions_dir / f"{session_id}.json"

    @classmethod
    def _read_json(cls, path: Path) -> dict:
        with _json_lock(path):
            try:
                with path.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON 文件损坏，未修改原文件：{path}（{exc}）") from exc
            except OSError as exc:
                raise OSError(f"读取文件失败：{path}（{exc}）") from exc

    @classmethod
    def _write_json(cls, path: Path, data: dict) -> None:
        # Never share one ``.json.tmp`` between concurrent Web/channel saves.
        temp_path = path.with_name(f"{path.name}.{os.getpid()}.{get_ident()}.tmp")
        with _json_lock(path):
            try:
                with temp_path.open("w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                cls._replace_with_retry(temp_path, path)
            except OSError as exc:
                # Keep a valid temp snapshot for recovery after a transient
                # Windows antivirus/indexer lock instead of losing the turn.
                raise OSError(f"保存文件失败：{path}（{exc}）") from exc

    @staticmethod
    def _replace_with_retry(temp_path: Path, target: Path) -> None:
        last_error: OSError | None = None
        for attempt in range(8):
            try:
                temp_path.replace(target)
                return
            except PermissionError as exc:
                last_error = exc
                if attempt < 7:
                    time.sleep(0.05 * (2 ** attempt))
            except FileNotFoundError as exc:
                if target.exists():
                    return
                last_error = exc
                break
        if last_error is not None:
            raise last_error

    def _recover_temp_files(self) -> None:
        """Recover a newer valid snapshot left by an interrupted Windows save."""
        for temp_path in self.sessions_dir.glob("*.json*.tmp"):
            name = temp_path.name
            if ".json." in name:
                target_name = name.split(".json.", 1)[0] + ".json"
            elif name.endswith(".json.tmp"):
                target_name = name[:-4]
            else:
                continue
            target = temp_path.with_name(target_name)
            try:
                temp_data = self._read_json(temp_path)
                if not isinstance(temp_data, dict):
                    continue
                if target.exists() and temp_path.stat().st_mtime <= target.stat().st_mtime:
                    temp_path.unlink(missing_ok=True)
                    continue
                self._replace_with_retry(temp_path, target)
            except (OSError, ValueError, json.JSONDecodeError):
                # A partial snapshot must never prevent Gateway startup.
                continue

    def save(self, session: Session) -> None:
        self._write_json(self._path(session.session_id), session.to_dict())

    def get(self, session_id: str) -> Session:
        path = self._path(session_id)
        if not path.exists():
            raise KeyError(f"Session 不存在：{session_id}")
        return Session.from_dict(self._read_json(path), path)

    def list_sessions(self) -> list[Session]:
        sessions = [
            Session.from_dict(self._read_json(path), path)
            for path in self.sessions_dir.glob("*.json")
        ]
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def create(self, title: str | None = None, make_current: bool = True) -> Session:
        session_id = self._next_session_id()
        session = Session(session_id, title or "新会话")
        self.save(session)
        if make_current:
            self.set_current_id(session_id)
        return session

    def _initialize_session_sequence(self) -> None:
        """Migrate legacy eight-hex IDs once and initialize sequence v2."""
        if self.sequence_file.exists():
            sequence = self._read_json(self.sequence_file)
            if sequence.get("formatVersion") == 2:
                return

        session_files = list(self.sessions_dir.glob("*.json"))
        records = [(path, self._read_json(path)) for path in session_files]
        used_numbers = {
            int(match.group(1))
            for path, _ in records
            if (match := re.fullmatch(r"session_(\d+)", path.stem))
            and len(match.group(1)) != 8
        }
        legacy = [
            (path, data) for path, data in records
            if re.fullmatch(r"session_[0-9a-fA-F]{8}", path.stem)
        ]
        legacy.sort(key=lambda pair: str(pair[1].get("createdAt", "")))
        mapping: dict[str, str] = {}
        candidate = 1
        for path, _ in legacy:
            while candidate in used_numbers:
                candidate += 1
            mapping[path.stem] = f"session_{candidate}"
            used_numbers.add(candidate)
            candidate += 1

        for old_id, new_id in mapping.items():
            old_path = self.sessions_dir / f"{old_id}.json"
            data = self._read_json(old_path)
            data["sessionId"] = new_id
            self._write_json(self.sessions_dir / f"{new_id}.json", data)
            old_path.unlink()

        if mapping:
            self._replace_session_references(mapping)
        next_number = max(used_numbers, default=0) + 1
        self._write_json(
            self.sequence_file,
            {"formatVersion": 2, "nextSessionNumber": next_number},
        )

    def _replace_session_references(self, mapping: dict[str, str]) -> None:
        """Update exact session ID values in JSON stores such as tasks and approvals."""
        for path in self.data_dir.glob("*.json"):
            if path == self.sequence_file:
                continue
            data = self._read_json(path)
            replaced = _replace_json_values(data, mapping)
            if replaced != data:
                self._write_json(path, replaced)

    def _next_session_id(self) -> str:
        """Return a monotonic human-readable ID without reusing deleted IDs."""
        with self._sequence_lock:
            if self.sequence_file.exists():
                data = self._read_json(self.sequence_file)
                next_number = data.get("nextSessionNumber")
                if not isinstance(next_number, int) or isinstance(next_number, bool) or next_number < 1:
                    raise ValueError(f"Session 编号状态无效：{self.sequence_file}")
            else:
                existing_numbers = []
                for path in self.sessions_dir.glob("session_*.json"):
                    suffix = path.stem.removeprefix("session_")
                    if suffix.isdigit():
                        existing_numbers.append(int(suffix))
                next_number = max(existing_numbers, default=0) + 1

            while self._path(f"session_{next_number}").exists():
                next_number += 1
            session_id = f"session_{next_number}"
            self._write_json(
                self.sequence_file,
                {"formatVersion": 2, "nextSessionNumber": next_number + 1},
            )
            return session_id

    def rename(self, session_id: str, title: str) -> Session:
        if not title.strip():
            raise ValueError("Session 标题不能为空。")
        session = self.get(session_id)
        session.title = title.strip()
        session.updated_at = utc_now()
        self.save(session)
        return session

    def delete(self, session_id: str) -> str:
        path = self._path(session_id)
        if not path.exists():
            raise KeyError(f"Session 不存在：{session_id}")
        path.unlink()
        remaining = self.list_sessions()
        if not remaining:
            fallback = Session(self.DEFAULT_ID, "默认会话")
            self.save(fallback)
            remaining = [fallback]
        next_id = remaining[0].session_id
        if self.current_id == session_id:
            self.set_current_id(next_id)
        return self.current_id

    def _ensure_current_session(self) -> None:
        try:
            current_id = self.current_id
            self.get(current_id)
        except (FileNotFoundError, KeyError):
            self.set_current_id(self.list_sessions()[0].session_id)

    @property
    def current_id(self) -> str:
        if not self.state_file.exists():
            raise FileNotFoundError("当前 Session 状态文件不存在。")
        data = self._read_json(self.state_file)
        current_id = data.get("currentSessionId")
        if not isinstance(current_id, str):
            raise ValueError(f"状态文件缺少 currentSessionId：{self.state_file}")
        return current_id

    def set_current_id(self, session_id: str) -> None:
        self.get(session_id)
        self._write_json(self.state_file, {"currentSessionId": session_id})

    @property
    def current(self) -> Session:
        return self.get(self.current_id)


def _replace_json_values(value, mapping: dict[str, str]):
    if isinstance(value, dict):
        return {key: _replace_json_values(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_json_values(item, mapping) for item in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return value
