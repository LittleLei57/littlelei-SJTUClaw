"""QQ official Bot WebSocket channel using Tencent's standalone Python SDK."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from pathlib import Path
import re
from threading import Event, Thread, Timer

from channels.base import InboundMessage, OutboundEvent, format_notification_text
from channels.rich_text import prepare_channel_markdown
from channels.service import ChannelService


logger = logging.getLogger(__name__)


def _split_markdown_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_markdown_separator(line: str) -> bool:
    cells = _split_markdown_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _plain_markdown(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1（\2）", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    return text.strip()


def format_qq_text(text: str) -> str:
    """Render Markdown as compact mobile-friendly QQ plain text.

    QQ Bot text messages do not consistently support GFM headings or tables
    across clients.  Converting tables to labelled records is more readable
    than leaking pipes and separator rows on a phone.
    """
    lines = prepare_channel_markdown(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 < len(lines) and "|" in lines[index] and _is_markdown_separator(lines[index + 1]):
            headers = _split_markdown_row(lines[index])
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_split_markdown_row(lines[index]))
                index += 1
            for row in rows:
                values = row + [""] * max(0, len(headers) - len(row))
                first = _plain_markdown(values[0]) if values else ""
                if len(headers) == 2:
                    rendered.append(
                        f"• {_plain_markdown(headers[0])}：{first}\n"
                        f"  {_plain_markdown(headers[1])}：{_plain_markdown(values[1])}"
                    )
                else:
                    details = [
                        f"{_plain_markdown(header)}：{_plain_markdown(values[pos])}"
                        for pos, header in enumerate(headers[1:], start=1)
                        if pos < len(values) and values[pos].strip()
                    ]
                    rendered.append(
                        f"• {first}" + (f"\n  {'；'.join(details)}" if details else "")
                    )
            rendered.append("")
            continue
        line = lines[index]
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        if re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", line):
            line = "────────"
        line = re.sub(r"^\s*[-*+]\s+", "• ", line)
        line = re.sub(r"^\s*>\s?", "｜", line)
        if not line.strip().startswith("```"):
            rendered.append(_plain_markdown(line))
        index += 1
    result = "\n".join(rendered)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


class QQBotAdapter:
    name = "qqbot"

    def __init__(self, app_id: str | None = None, client_secret: str | None = None):
        self.app_id = app_id if app_id is not None else os.getenv("QQBOT_APP_ID", "")
        self.client_secret = client_secret if client_secret is not None else os.getenv("QQBOT_CLIENT_SECRET", "")
        self.connection: QQBotConnection | None = None

    def send(self, message: InboundMessage, event: OutboundEvent) -> None:
        if event.event_type not in {"final", "assistant_note", "approval_required", "error"}:
            return
        if not self.connection:
            raise RuntimeError("QQ Bot 长连接未启动。")
        self.connection.send_text(
            # QQ's native Markdown renderer can display TeX formulae on
            # supported clients.  Preserve the original source on the first
            # attempt; only the plain-text fallback converts LaTeX.
            message, format_notification_text(event),
            proactive=bool(event.data.get("proactive")),
        )

    def begin_processing(self, message: InboundMessage):
        if not self.connection:
            return None
        native_handle = self.connection.begin_typing(message)
        if native_handle is not None:
            return native_handle
        # QQ's input-notify API currently targets C2C messages.  Group and
        # channel chats get the same delayed visible fallback as Weixin.
        timer = Timer(1.2, lambda: self._send_processing_fallback(message))
        timer.daemon = True
        timer.start()
        return timer

    def _send_processing_fallback(self, message: InboundMessage) -> None:
        try:
            if self.connection:
                self.connection.send_text(message, "··· 正在处理")
        except Exception as exc:
            logger.debug("QQ 处理提示发送失败：%s", exc)

    def end_processing(self, message: InboundMessage, handle) -> None:
        if isinstance(handle, Timer):
            handle.cancel()
        elif self.connection:
            self.connection.end_typing(handle)


class QQBotConnection:
    def __init__(self, adapter: QQBotAdapter, service: ChannelService, sdk=None):
        self.adapter, self.service, self._sdk = adapter, service, sdk
        adapter.connection = self
        self._thread: Thread | None = None
        self._stop = Event()
        self._loop = None
        self._api = None
        self._ws = None
        self._session_id = None
        self._last_seq = None
        self.connected = False
        self.last_error: str | None = None

    @property
    def configured(self):
        return bool(self.adapter.app_id and self.adapter.client_secret)

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if not self.configured or self.running:
            return self.running
        self._stop.clear()
        self._thread = Thread(target=self._run, name="qqbot-websocket", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: None)

    def _run(self):
        try:
            asyncio.run(self._main())
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("QQ Bot 长连接已停止：%s", exc)

    async def _main(self):
        import httpx

        sdk = self._sdk
        if sdk is None:
            import qqbot_agent_sdk as sdk
        self._loop = asyncio.get_running_loop()
        self._api = sdk.QQApiClient(self.adapter.app_id, self.adapter.client_secret, log_tag="SJTUClaw")
        http_client = httpx.AsyncClient()
        self._api.setup(http_client)
        attachment_processor = None
        if hasattr(sdk, "AttachmentDownloader") and hasattr(sdk, "AttachmentProcessor"):
            cache_dir = Path(self.service.runtime.store.data_dir) / "qq-attachment-cache"
            attachment_processor = sdk.AttachmentProcessor(
                sdk.AttachmentDownloader(http_client=http_client, cache_dir=str(cache_dir), log_tag="SJTUClaw")
            )

        async def on_message(event_type, raw):
            parsed = sdk.EventParser().parse(event_type, raw)
            if not parsed:
                return
            channel_attachments = []
            if attachment_processor is not None and getattr(parsed, "attachments", None):
                for item in await attachment_processor.process(parsed.attachments):
                    local_path = Path(item.local_path) if item.local_path else None
                    if local_path and local_path.is_file():
                        channel_attachments.append({
                            "filename": local_path.name,
                            "contentType": item.content_type or "application/octet-stream",
                            "content": local_path.read_bytes(),
                        })
            content = str(parsed.content or "").strip()
            if not content and not channel_attachments:
                return
            inbound = InboundMessage(
                channel="qqbot", event_id=parsed.message_id,
                external_user_id=parsed.user_id,
                conversation_id=f"{parsed.chat_scope}:{parsed.chat_id}",
                text=content or "请读取并分析 QQ 附件。", reply_token=parsed.message_id,
                attachments=tuple(channel_attachments),
                metadata={"chatScope": parsed.chat_scope, "chatId": parsed.chat_id},
            )
            Thread(target=self._process, args=(inbound,), name=f"qqbot-{parsed.message_id}", daemon=True).start()

        callbacks = sdk.WSCallbacks(
            on_message_event=on_message,
            on_connected=self._mark_connected,
            on_disconnected=self._mark_disconnected,
            on_fatal_error=lambda code, message: setattr(self, "last_error", f"{code}: {message}"),
            get_token=self._api.ensure_token_sync,
            get_session=lambda: (self._session_id, self._last_seq),
            set_session=self._set_session,
            set_heartbeat_interval=lambda _value: None,
            clear_token=self._api.clear_token,
            fail_pending=lambda _reason: None,
            get_gateway_url=self._api.get_gateway_url_sync,
        )
        try:
            self._ws = sdk.QQWebSocket(callbacks=callbacks, log_tag="SJTUClaw")
            await self._api.ensure_token()
            gateway_url = await self._api.get_gateway_url()
            self._ws.start(gateway_url, self._loop)
            while not self._stop.is_set():
                await asyncio.sleep(0.25)
        finally:
            if self._ws:
                stopped = self._ws.stop()
                if inspect.isawaitable(stopped):
                    await stopped
            await http_client.aclose()

    def _set_session(self, session_id, last_seq):
        self._session_id, self._last_seq = session_id, last_seq

    def _mark_connected(self):
        self.connected = True
        self.last_error = None

    def _mark_disconnected(self):
        self.connected = False

    def _process(self, inbound):
        try:
            self.service.handle(self.adapter, inbound)
        except Exception as exc:
            logger.exception("处理 QQ 消息失败：%s", exc)
            try:
                self.adapter.send(inbound, OutboundEvent(
                    "error", f"SJTUClaw 处理失败，请查看 Gateway 日志（事件 {inbound.event_id}）。"
                ))
            except Exception:
                pass

    def send_text(self, message: InboundMessage, text: str, proactive: bool = False):
        if not self._loop or not self._api:
            raise RuntimeError("QQ Bot 尚未连接。")

        async def send_with_fallback():
            try:
                return await self._api.send_text(
                    message.metadata["chatScope"], message.metadata["chatId"], text,
                    reply_to=None if proactive else message.reply_token, markdown=True,
                )
            except (asyncio.TimeoutError, TimeoutError):
                raise
            except Exception as exc:
                # Native Markdown can still be rejected for an individual bot
                # or message.  Preserve delivery by retrying once as readable
                # mobile plain text.
                logger.warning("QQ Markdown 发送失败，降级为纯文本：%s", exc)
                return await self._api.send_text(
                    message.metadata["chatScope"], message.metadata["chatId"],
                    format_qq_text(text),
                    reply_to=None if proactive else message.reply_token, markdown=False,
                )

        future = asyncio.run_coroutine_threadsafe(
            send_with_fallback(), self._loop,
        )
        return future.result(timeout=30)

    def begin_typing(self, message: InboundMessage):
        """Start QQ's native C2C input-notify and refresh it while busy."""
        if (
            not self._loop or not self._api
            or str(message.metadata.get("chatScope") or "").lower() != "c2c"
            or not message.reply_token
        ):
            return None
        stop = Event()

        def keep_alive():
            while not stop.is_set():
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._api.send_typing(
                            str(message.metadata.get("chatId") or message.external_user_id),
                            message.reply_token,
                            input_seconds=60,
                        ),
                        self._loop,
                    )
                    future.result(timeout=8)
                except Exception as exc:
                    logger.debug("QQ 输入状态发送失败：%s", exc)
                if stop.wait(50):
                    break

        thread = Thread(
            target=keep_alive,
            name=f"qqbot-typing-{message.event_id}",
            daemon=True,
        )
        thread.start()
        return stop

    @staticmethod
    def end_typing(handle) -> None:
        if isinstance(handle, Event):
            handle.set()
