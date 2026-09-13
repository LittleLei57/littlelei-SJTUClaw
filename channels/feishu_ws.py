"""Feishu WebSocket transport powered by the official lark-oapi SDK."""

from __future__ import annotations

import json
import logging
import os
import asyncio
from threading import Lock, Thread
from typing import Any

from channels.base import OutboundEvent
from channels.feishu import FeishuAdapter
from channels.service import ChannelService


logger = logging.getLogger(__name__)


class FeishuLongConnection:
    """Run the SDK's blocking WebSocket client in one daemon thread."""

    def __init__(
        self, adapter: FeishuAdapter, service: ChannelService,
        mode: str | None = None, sdk: Any | None = None,
    ):
        self.adapter = adapter
        self.service = service
        self.mode = (mode or os.getenv("FEISHU_CONNECTION_MODE", "websocket")).strip().lower()
        self._sdk = sdk
        self._thread: Thread | None = None
        self._lock = Lock()
        self._client = None
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.adapter.app_id and self.adapter.app_secret)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if self.mode != "websocket" or not self.configured:
            return False
        with self._lock:
            if self.running:
                return True
            self._thread = Thread(target=self._run, name="feishu-websocket", daemon=True)
            self._thread.start()
            return True

    @staticmethod
    def _load_sdk():
        try:
            import lark_oapi as lark
        except ImportError as exc:
            raise RuntimeError("飞书长连接需要安装依赖：pip install lark-oapi") from exc
        return lark

    def _run(self) -> None:
        event_loop = asyncio.new_event_loop()
        try:
            # lark-oapi captures the current event loop at import time. Importing
            # and constructing it here keeps it isolated from Uvicorn's loop.
            asyncio.set_event_loop(event_loop)
            sdk = self._sdk or self._load_sdk()
            builder = sdk.EventDispatcherHandler.builder("", "") \
                .register_p2_im_message_receive_v1(self._on_message)
            # Recent lark-oapi releases can deliver interactive-card actions
            # over the same long connection. Keep this optional so older SDKs
            # continue to support normal message events.
            for method_name in ("register_p2_card_action_trigger_v1", "register_p2_card_action_trigger"):
                register_card = getattr(builder, method_name, None)
                if callable(register_card):
                    register_card(self._on_message)
                    break
            handler = builder.build()
            self._client = sdk.ws.Client(
                self.adapter.app_id, self.adapter.app_secret,
                log_level=sdk.LogLevel.INFO, event_handler=handler, auto_reconnect=True,
            )
            self._client.start()
        except Exception as exc:
            self.last_error = str(exc)
            logger.exception("飞书长连接已停止：%s", exc)
        finally:
            asyncio.set_event_loop(None)
            if not event_loop.is_running():
                event_loop.close()

    def _on_message(self, data) -> None:
        """Acknowledge quickly; Agent processing continues outside the SDK callback."""
        try:
            sdk = self._sdk or self._load_sdk()
            payload = json.loads(sdk.JSON.marshal(data))
            inbound = self.adapter.parse_event(payload, verify=False)
        except Exception as exc:
            logger.exception("无法解析飞书长连接消息：%s", exc)
            return
        if inbound is None:
            return
        Thread(
            target=self._process, args=(inbound,),
            name=f"feishu-message-{inbound.event_id}", daemon=True,
        ).start()

    def _process(self, inbound) -> None:
        try:
            self.service.handle(self.adapter, inbound)
        except Exception as exc:
            logger.exception("处理飞书消息失败：%s", exc)
            try:
                self.adapter.send(inbound, OutboundEvent(
                    "error", f"SJTUClaw 处理失败，请查看 Gateway 日志（事件 {inbound.event_id}）。"
                ))
            except Exception:
                logger.exception("向飞书发送错误提示失败")
