"""Step 4：当前 Session 的安全上下文压缩。

Compaction 只处理旧的语义 user/assistant 消息，不压缩 Stable Context，也不把
Tool 协议噪声占作近期消息。超长历史会先分块摘要再合并；成功后保留最近消息并
更新 ``session.summary``，失败则回滚原消息。手动与自动压缩共用同一流程并返回
可展示的统计和 Summary 预览。
"""

from dataclasses import dataclass
import json
from pathlib import Path
import re
from threading import local
from typing import Protocol

from llm_client import Message
from session_store import Session, SessionStore, preserve_message_citations, utc_now
from conversation_view import semantic_context
from goal_state import normalize_goal


class ChatModel(Protocol):
    """摘要器依赖的最小模型接口。"""

    def complete(self, messages: list[Message]) -> str: ...


class CompactionError(RuntimeError):
    """压缩失败且原始 Session 已被保留。"""

    pass


@dataclass(frozen=True)
class CompactionResult:
    """一次成功压缩的消息数量、分块数和 Summary 元数据。"""

    session_id: str
    old_messages: int
    recent_messages: int
    summary: str
    chunks: int = 1
    old_tokens: int = 0
    recent_tokens: int = 0
    summary_version: int = 1
    covered_message_start: int = 1
    covered_message_end: int = 0
    quality_warnings: tuple[str, ...] = ()

    @property
    def preview(self) -> str:
        return self.summary if len(self.summary) <= 500 else self.summary[:500] + "…"


class Compactor:
    """只压缩 session.summary 与 session.messages，不读取 stable context。"""

    def __init__(
        self,
        model: ChatModel,
        store: SessionStore,
        prompt_file: str | Path = "prompts/compact_prompt.md",
        max_messages: int = 20,
        max_characters: int = 12_000,
        keep_recent: int = 8,
        chunk_characters: int | None = None,
        max_tokens: int | None = None,
        chunk_tokens: int | None = None,
    ):
        if keep_recent < 1:
            raise ValueError("keep_recent 必须大于 0。")
        self.model = model
        self.store = store
        self.max_messages = max_messages
        self.max_characters = max_characters
        self.keep_recent = keep_recent
        self.chunk_characters = chunk_characters or max_characters
        if max_tokens is not None and max_tokens < 1:
            raise ValueError("max_tokens 必须大于 0。")
        if chunk_tokens is not None and chunk_tokens < 1:
            raise ValueError("chunk_tokens 必须大于 0。")
        self.max_tokens = max_tokens
        self.chunk_tokens = chunk_tokens
        if self.chunk_characters < 1_000:
            raise ValueError("chunk_characters 必须至少为 1000，避免摘要分片过碎。")
        self.prompt = self._load_prompt(Path(prompt_file))
        self._prompt_local = local()

    @staticmethod
    def _load_prompt(path: Path) -> str:
        try:
            prompt = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise OSError(f"无法读取 Compaction Prompt：{path}（{exc}）") from exc
        if not prompt:
            raise ValueError(f"Compaction Prompt 不能为空：{path}")
        return prompt

    def should_compact(self, session: Session) -> bool:
        semantic = self._compactable_messages(session)
        characters = sum(len(item.get("content", "")) for item in semantic)
        tokens = sum(self._estimate_message_tokens(item) for item in semantic)
        return (
            len(semantic) > self.max_messages
            or characters > self.max_characters
            or (self.max_tokens is not None and tokens > self.max_tokens)
        )

    def estimate(self, session: Session, *, force: bool = False) -> dict:
        """Return a dry-run compaction estimate without calling the model or mutating state."""
        compactable = self._compactable_messages(session)
        characters = sum(len(item.get("content", "")) for item in compactable)
        tokens = sum(self._estimate_message_tokens(item) for item in compactable)
        should = (
            len(compactable) > self.max_messages
            or characters > self.max_characters
            or (self.max_tokens is not None and tokens > self.max_tokens)
        )
        effective_keep_recent = self._effective_keep_recent(
            compactable,
            force=force,
        )
        split_at = max(0, len(compactable) - effective_keep_recent)
        old_messages = compactable[:split_at]
        recent_messages = compactable[split_at:]
        chunks = self._chunk_messages(old_messages) if old_messages else []
        old_tokens = sum(self._estimate_message_tokens(item) for item in old_messages)
        recent_tokens = sum(self._estimate_message_tokens(item) for item in recent_messages)
        oversized = [
            {
                "index": index,
                "role": item.get("role"),
                "characters": len(item.get("content", "")),
                "tokens": self._estimate_message_tokens(item),
            }
            for index, item in enumerate(old_messages, start=1)
            if (
                len(json.dumps(item, ensure_ascii=False)) > self.chunk_characters
                or (
                    self.chunk_tokens is not None
                    and self._estimate_message_tokens(item) > self.chunk_tokens
                )
            )
        ]
        return {
            "semanticMessages": len(compactable),
            "semanticCharacters": characters,
            "semanticTokens": tokens,
            "oldTokens": old_tokens,
            "recentTokens": recent_tokens,
            "maxMessages": self.max_messages,
            "maxCharacters": self.max_characters,
            "maxTokens": self.max_tokens,
            "keepRecent": effective_keep_recent,
            "chunkCharacters": self.chunk_characters,
            "chunkTokens": self.chunk_tokens,
            "shouldCompact": should,
            "oldMessages": len(old_messages),
            "recentMessages": len(recent_messages),
            "estimatedChunks": len(chunks),
            "oversizedMessages": oversized,
        }

    def compact(self, session: Session, force: bool = False) -> CompactionResult | None:
        if not force and not self.should_compact(session):
            return None
        preserve_message_citations(
            session.messages,
            getattr(session, "citation_index", None),
            getattr(session, "activity", None),
            getattr(session, "tool_trace", None),
        )
        compactable = self._compactable_messages(session)
        # Automatic compaction keeps the configured recent tail intact.
        # A manual /compact is an explicit user request, so allow it on a
        # shorter conversation while still preserving at least two recent
        # semantic messages.  Fewer than four messages would summarize too
        # little context to justify an extra model call.
        effective_keep_recent = self._effective_keep_recent(
            compactable,
            force=force,
        )
        if len(compactable) <= effective_keep_recent:
            return None

        split_at = len(compactable) - effective_keep_recent
        old_messages = compactable[:split_at]
        recent_messages = compactable[split_at:]

        try:
            # The summary prompt also receives the bounded Goal/Spec state.
            # Keep this request-local reference so all chunk/merge rounds use
            # the same task metadata without changing the public API.
            self._prompt_local.session = session
            new_summary, chunk_count = self._summarize_old_messages(
                session.summary,
                old_messages,
            )
        except Exception as exc:
            raise CompactionError(f"摘要模型调用失败，原消息已保留：{exc}") from exc
        finally:
            self._prompt_local.session = None
        if not new_summary:
            raise CompactionError("摘要结果为空，原消息已保留。")
        if self._has_substantive_history(old_messages) and self._is_placeholder_summary(new_summary):
            raise CompactionError(
                "摘要未保留旧消息中的有效任务或事实，原消息已保留；请稍后重试压缩。"
            )

        original_summary = session.summary
        original_summary_meta = dict(getattr(session, "summary_meta", {}) or {})
        original_messages = session.messages
        original_updated_at = session.updated_at
        quality_warnings = self._summary_quality_warnings(new_summary, old_messages)
        previous_version = int(original_summary_meta.get("version") or 0)
        previous_covered = int(original_summary_meta.get("coveredMessageCount") or 0)
        covered_start = 1 if previous_covered == 0 else previous_covered + 1
        covered_end = previous_covered + len(old_messages)
        summary_version = previous_version + 1
        session.summary_meta = {
            "version": summary_version,
            "updatedAt": utc_now(),
            "coveredMessageCount": covered_end,
            "coveredMessageRange": {
                "start": covered_start,
                "end": covered_end,
            },
            "oldMessages": len(old_messages),
            "recentMessages": len(recent_messages),
            "oldTokens": sum(self._estimate_message_tokens(item) for item in old_messages),
            "recentTokens": sum(self._estimate_message_tokens(item) for item in recent_messages),
            "chunks": chunk_count,
            "preview": new_summary if len(new_summary) <= 500 else new_summary[:500] + "…",
            "qualityWarnings": quality_warnings,
        }
        session.summary = new_summary
        session.messages = recent_messages
        session.updated_at = utc_now()
        try:
            self.store.save(session)
        except Exception as exc:
            session.summary = original_summary
            session.summary_meta = original_summary_meta
            session.messages = original_messages
            session.updated_at = original_updated_at
            raise CompactionError(f"压缩结果保存失败，原消息已保留：{exc}") from exc

        return CompactionResult(
            session_id=session.session_id,
            old_messages=len(old_messages),
            recent_messages=len(recent_messages),
            summary=new_summary,
            chunks=chunk_count,
            old_tokens=sum(self._estimate_message_tokens(item) for item in old_messages),
            recent_tokens=sum(self._estimate_message_tokens(item) for item in recent_messages),
            summary_version=summary_version,
            covered_message_start=covered_start,
            covered_message_end=covered_end,
            quality_warnings=tuple(quality_warnings),
        )

    def _effective_keep_recent(
        self,
        compactable: list[Message],
        *,
        force: bool,
    ) -> int:
        """Choose a recent tail without letting one huge message block compaction.

        The ordinary path preserves ``keep_recent`` visible dialogue messages.
        A short conversation can nevertheless exceed the character/token
        budget when the user pastes one very large document. In that case a
        completed turn keeps the latest assistant answer and summarizes the
        oversized user input. Explicit manual compaction may also summarize a
        single oversized message, which is the important stress-test case.
        """
        count = len(compactable)
        effective = self.keep_recent
        if force and count >= 4:
            effective = min(self.keep_recent, max(2, count // 2))

        if self._messages_over_budget(compactable) and count <= effective:
            if count == 1:
                return 0 if force else effective
            # At the end of a normal turn this retains the latest assistant
            # answer while moving the large user payload into the summary.
            return 1
        return effective

    def _messages_over_budget(self, messages: list[Message]) -> bool:
        characters = sum(len(item.get("content", "")) for item in messages)
        tokens = sum(self._estimate_message_tokens(item) for item in messages)
        return (
            characters > self.max_characters
            or (self.max_tokens is not None and tokens > self.max_tokens)
        )

    @staticmethod
    def _summary_quality_warnings(
        summary: str,
        source_messages: list[Message] | None = None,
    ) -> list[str]:
        """Report missing required summary sections without discarding history.

        Providers occasionally return a useful but non-conforming summary. A
        warning is safer than rejecting it (which could trigger repeated
        compaction calls); the warning is persisted for UI/diagnostic review.
        """
        required = (
            "当前任务",
            "已完成",
            "用户偏好与约束",
            "关键事实与证据",
            "附件与文件来源",
            "待解决问题",
            "下一步",
            "不应记住",
        )
        sections: dict[str, str] = {}
        heading_matches = list(
            re.finditer(r"^#{2,4}\s*(.+?)\s*$", summary, re.MULTILINE)
        )
        for index, match in enumerate(heading_matches):
            heading = match.group(1).strip().rstrip("#").strip()
            end = heading_matches[index + 1].start() if index + 1 < len(heading_matches) else len(summary)
            sections[heading] = summary[match.end() : end].strip()

        missing = [
            name for name in required
            if name not in sections
        ]
        warnings = [f"缺少栏目：{name}" for name in missing]
        for name in required:
            if name not in sections:
                continue
            body = sections[name]
            if not body or re.fullmatch(r"[-*\d.\s]*无[。.!！]?", body):
                warnings.append(f"栏目为空：{name}")

        if source_messages:
            source_markers: set[str] = set()
            for message in source_messages:
                content = str(message.get("content") or "")
                source_markers.update(re.findall(r"\batt_[A-Za-z0-9_-]+\b", content))
                source_markers.update(
                    re.findall(
                        r"(?<![\w])[^\s\\/<>|]+\.(?:pdf|docx?|pptx?|xlsx?|csv|md|txt)",
                        content,
                        flags=re.IGNORECASE,
                    )
                )
            for marker in sorted(source_markers):
                if marker not in summary:
                    warnings.append(f"来源未在摘要中出现：{marker}")
        return warnings

    @staticmethod
    def _compactable_messages(session: Session) -> list[Message]:
        """Select only visible dialogue for the compaction message budget.

        ``semantic_context`` normally preserves a pending tool observation so
        the next model call can continue.  A compaction is only performed
        after a turn has completed (or explicitly by the user), and retaining
        that protocol tail here would make tool calls consume ``keep_recent``
        slots.  The full protocol trace remains in ``session.messages`` and in
        ``tool_trace`` for auditing/recovery.
        """
        return semantic_context(session.messages, preserve_active_internal=False)

    @staticmethod
    def _has_substantive_history(messages: list[Message]) -> bool:
        """Avoid replacing a meaningful history with an all-empty summary."""
        contents = [str(item.get("content", "")).strip() for item in messages]
        user_contents = [
            content for item, content in zip(messages, contents)
            if item.get("role") == "user" and len(content) >= 6
        ]
        return bool(user_contents) and sum(len(content) for content in contents) >= 240

    @staticmethod
    def _is_placeholder_summary(summary: str) -> bool:
        """Recognise the common 'all sections are empty' model failure mode."""
        body = [
            re.sub(r"^[-*\\d.\\s]+", "", line).strip().lower()
            for line in summary.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not body:
            return True
        placeholders = (
            "\u65e0", "none", "n/a", "not applicable",
            "\u65e0\u660e\u786e\u4efb\u52a1", "\u7b49\u5f85\u7528\u6237",
            "waiting for user", "\u5bd2\u6684", "\u6253\u62db\u547c",
        )
        return all(any(marker in line for marker in placeholders) for line in body)

    def _summarize_old_messages(
        self,
        existing_summary: str,
        old_messages: list[Message],
    ) -> tuple[str, int]:
        chunks = self._chunk_messages(old_messages)
        if len(chunks) == 1:
            return self._call_summary_model(
                "本次需要压缩的旧消息（JSON）：",
                existing_summary,
                json.dumps(old_messages, ensure_ascii=False),
            ), 1

        partials: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            partials.append(
                self._call_summary_model(
                    f"这是第 {index}/{len(chunks)} 个旧消息分片（JSON）。请只摘要本分片中对后续有用的信息：",
                    existing_summary,
                    json.dumps(chunk, ensure_ascii=False),
                )
            )
        return self._merge_summaries(existing_summary, partials), len(chunks)

    def _merge_summaries(self, existing_summary: str, summaries: list[str]) -> str:
        current = [item for item in summaries if item.strip()]
        if not current:
            raise CompactionError("摘要结果为空，原消息已保留。")

        round_no = 1
        while len(current) > 1 or self._text_size(current) > self.chunk_characters:
            batches = self._chunk_text_items(current)
            next_round: list[str] = []
            for index, batch in enumerate(batches, start=1):
                next_round.append(
                    self._call_summary_model(
                        (
                            f"这是第 {round_no} 轮汇总的第 {index}/{len(batches)} 组分片摘要。"
                            "请合并成更短、更去重、可继续使用的摘要："
                        ),
                        existing_summary if round_no == 1 else "",
                        "\n\n".join(batch),
                    )
                )
            if len(next_round) == len(current) and self._text_size(next_round) >= self._text_size(current):
                # 防止模型没有压缩导致死循环；保留有用信息并硬截断到安全窗口。
                joined = "\n\n".join(next_round)
                return self._call_summary_model(
                    "以下摘要仍然过长，请生成最终短摘要：",
                    existing_summary,
                    joined[: self.chunk_characters],
                )
            current = next_round
            round_no += 1
        return current[0].strip()

    def _call_summary_model(self, label: str, existing_summary: str, payload: str) -> str:
        prompt_session = getattr(self._prompt_local, "session", None)
        goal = normalize_goal(getattr(prompt_session, "goal_state", None))
        goal_text = json.dumps(goal, ensure_ascii=False, indent=2) if goal else "（暂无活动任务）"
        request: list[Message] = [
            {"role": "system", "content": self.prompt},
            {
                "role": "user",
                "content": (
                    "已有 Session Summary：\n"
                    f"{existing_summary or '（无）'}\n\n"
                    "当前 Goal/Spec 状态（必须保留仍有效的目标、约束、验收条件和阻塞）：\n"
                    f"{goal_text}\n\n"
                    f"{label}\n"
                    f"{payload}"
                ),
            },
        ]
        summary = self.model.complete(request).strip()
        if not summary:
            raise CompactionError("摘要结果为空，原消息已保留。")
        return summary

    def _chunk_messages(self, messages: list[Message]) -> list[list[Message]]:
        chunks: list[list[Message]] = []
        current: list[Message] = []
        current_size = 0
        current_tokens = 0
        for message in messages:
            pieces = self._split_message_if_needed(message)
            for piece in pieces:
                piece_size = len(json.dumps(piece, ensure_ascii=False))
                piece_tokens = self._estimate_message_tokens(piece)
                over_chars = current_size + piece_size > self.chunk_characters
                over_tokens = (
                    self.chunk_tokens is not None
                    and current_tokens + piece_tokens > self.chunk_tokens
                )
                if current and (over_chars or over_tokens):
                    chunks.append(current)
                    current = []
                    current_size = 0
                    current_tokens = 0
                current.append(piece)
                current_size += piece_size
                current_tokens += piece_tokens
        if current:
            chunks.append(current)
        return chunks or [[]]

    def _split_message_if_needed(self, message: Message) -> list[Message]:
        encoded_size = len(json.dumps(message, ensure_ascii=False))
        message_tokens = self._estimate_message_tokens(message)
        if (
            encoded_size <= self.chunk_characters
            and (self.chunk_tokens is None or message_tokens <= self.chunk_tokens)
        ):
            return [message]
        content = message.get("content", "")
        role = message.get("role", "user")
        room = max(500, self.chunk_characters - 300)
        if self.chunk_tokens is not None and message_tokens > self.chunk_tokens:
            # The estimator is intentionally conservative: CJK characters are
            # close to one token, while Latin runs average about four chars
            # per token. Use the observed ratio to choose a safe character
            # room before adding the per-part marker.
            ratio = max(1.0, len(content) / max(1, message_tokens))
            room = min(room, max(200, int((self.chunk_tokens - 80) * ratio)))
        parts = [content[index : index + room] for index in range(0, len(content), room)]
        total = len(parts)
        return [
            {
                "role": role,
                "content": f"[原始单条消息过长，分片 {index}/{total}]\n{part}",
            }
            for index, part in enumerate(parts, start=1)
        ]

    def _chunk_text_items(self, items: list[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        current: list[str] = []
        current_size = 0
        current_tokens = 0
        for item in items:
            item_size = len(item)
            item_tokens = self._estimate_tokens(item)
            over_chars = current_size + item_size > self.chunk_characters
            over_tokens = (
                self.chunk_tokens is not None
                and current_tokens + item_tokens > self.chunk_tokens
            )
            if current and (over_chars or over_tokens):
                batches.append(current)
                current = []
                current_size = 0
                current_tokens = 0
            if item_size > self.chunk_characters or (
                self.chunk_tokens is not None and item_tokens > self.chunk_tokens
            ):
                room = self.chunk_characters
                if self.chunk_tokens is not None and item_tokens > self.chunk_tokens:
                    ratio = max(1.0, item_size / max(1, item_tokens))
                    room = min(room, max(200, int((self.chunk_tokens - 80) * ratio)))
                for start in range(0, item_size, room):
                    if current:
                        batches.append(current)
                        current = []
                        current_size = 0
                        current_tokens = 0
                    piece = item[start : start + room]
                    batches.append([piece])
                continue
            current.append(item)
            current_size += item_size
            current_tokens += item_tokens
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _text_size(items: list[str]) -> int:
        return sum(len(item) for item in items)

    @classmethod
    def _estimate_message_tokens(cls, message: Message) -> int:
        return cls._estimate_tokens(json.dumps(message, ensure_ascii=False))

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Conservative dependency-free token estimate.

        CJK characters and punctuation are counted individually; Latin and
        digit runs use roughly four characters per token. It is not a model
        tokenizer, but it is stable across environments and safer than using
        raw character counts for multilingual context budgets.
        """
        value = str(text or "")
        cjk = len(re.findall(r"[\u2e80-\u9fff]", value))
        other = max(0, len(value) - cjk)
        return cjk + max(0, (other + 3) // 4)
