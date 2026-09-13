"""Feishu event and reply adapter using official Open Platform HTTP APIs."""

from __future__ import annotations

import hmac
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
from threading import Lock
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

try:
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover - optional until webhook encryption is enabled
    AES = None

from channels.base import InboundMessage, OutboundEvent, format_notification_text, notification_title
from channels.rich_text import prepare_channel_markdown


FEISHU_BASE = "https://open.feishu.cn/open-apis"
MAX_FEISHU_DOCUMENT_BYTES = 10 * 1024 * 1024
logger = logging.getLogger(__name__)


class FeishuAdapter:
    name = "feishu"

    def __init__(
        self, app_id: str | None = None, app_secret: str | None = None,
        verification_token: str | None = None, opener: Callable[..., Any] = urlopen,
        clock: Callable[[], float] = time.monotonic,
        card_version: str | None = None,
        encrypt_key: str | None = None,
        wall_clock: Callable[[], float] = time.time,
        max_event_age_seconds: float | None = None,
    ):
        self.app_id = app_id if app_id is not None else os.getenv("FEISHU_APP_ID", "")
        self.app_secret = app_secret if app_secret is not None else os.getenv("FEISHU_APP_SECRET", "")
        self.verification_token = (
            verification_token if verification_token is not None
            else os.getenv("FEISHU_VERIFICATION_TOKEN", "")
        )
        self.encrypt_key = (
            encrypt_key if encrypt_key is not None
            else os.getenv("FEISHU_ENCRYPT_KEY", "")
        ).strip()
        self._opener = opener
        self._clock = clock
        self._wall_clock = wall_clock
        configured_age = (
            max_event_age_seconds if max_event_age_seconds is not None
            else os.getenv("FEISHU_EVENT_MAX_AGE_SECONDS", "300")
        )
        try:
            self.max_event_age_seconds = max(1.0, float(configured_age))
        except (TypeError, ValueError):
            raise ValueError("FEISHU_EVENT_MAX_AGE_SECONDS 必须是正数。") from None
        # JSON 2.0 cards preserve GFM tables as real Feishu table-like
        # Markdown on current clients.  Keep an explicit legacy switch for
        # older Feishu clients or installations that still use the v1 card
        # renderer: FEISHU_CARD_VERSION=legacy (or 1.0).
        configured_card_version = (
            card_version if card_version is not None
            else os.getenv("FEISHU_CARD_VERSION", "2.0")
        )
        self.card_version = str(configured_card_version or "2.0").strip().lower()
        self._token: tuple[str, float] | None = None
        self._token_lock = Lock()

    def validate_security_config(self, require_webhook: bool = False) -> None:
        """Validate the minimum authentication material for HTTP callbacks.

        Long-connection mode does not need these values because the official
        SDK authenticates the connection itself.  A public webhook must have
        either the Verification Token or Encrypt Key configured; an Encrypt
        Key additionally enables request-signature verification and payload
        decryption.
        """
        if require_webhook and not (self.verification_token or self.encrypt_key):
            raise ValueError(
                "飞书 Webhook 至少需要配置 FEISHU_VERIFICATION_TOKEN 或 FEISHU_ENCRYPT_KEY。"
            )
        if self.encrypt_key and len(self.encrypt_key) < 16:
            raise ValueError("FEISHU_ENCRYPT_KEY 长度过短，请填写开放平台生成的 Encrypt Key。")

    @staticmethod
    def _header(headers: Any, name: str) -> str:
        if headers is None:
            return ""
        try:
            value = headers.get(name)
            if value is None:
                value = headers.get(name.lower())
        except AttributeError:
            value = None
        return str(value or "").strip()

    def verify_http_request(self, body: bytes, headers: Any = None) -> None:
        """Verify Feishu webhook signature and timestamp before JSON parsing.

        Feishu signs ``timestamp + nonce + encrypt_key + raw_body`` with
        SHA-256 (the body must be the exact bytes received from HTTP).  Token
        only installations remain compatible; once an Encrypt Key is set,
        missing or invalid signature headers are rejected instead of silently
        falling back to token-only authentication.
        """
        if not self.encrypt_key:
            return
        timestamp = self._header(headers, "X-Lark-Request-Timestamp")
        nonce = self._header(headers, "X-Lark-Request-Nonce")
        received = self._header(headers, "X-Lark-Signature")
        if not timestamp or not nonce or not received:
            raise PermissionError("飞书 Webhook 缺少签名请求头。")
        try:
            timestamp_value = float(timestamp)
        except ValueError:
            raise PermissionError("飞书 Webhook 时间戳无效。") from None
        age = self._wall_clock() - timestamp_value
        if abs(age) > self.max_event_age_seconds:
            raise PermissionError("飞书 Webhook 事件已过期或时间偏差过大。")
        expected = hashlib.sha256(
            timestamp.encode("utf-8") + nonce.encode("utf-8")
            + self.encrypt_key.encode("utf-8") + body
        ).hexdigest()
        if not hmac.compare_digest(received.lower(), expected.lower()):
            raise PermissionError("飞书 Webhook 签名校验失败。")

    def _decrypt_payload(self, payload: dict) -> dict:
        encrypted = payload.get("encrypt")
        if not encrypted:
            return payload
        if not self.encrypt_key:
            raise PermissionError(
                "飞书事件已加密，但未配置 FEISHU_ENCRYPT_KEY。"
            )
        if AES is None:
            raise RuntimeError(
                "飞书事件解密需要 pycryptodome，请运行：pip install pycryptodome"
            )
        try:
            blob = base64.b64decode(str(encrypted), validate=True)
            if len(blob) < AES.block_size * 2 or len(blob[16:]) % AES.block_size:
                raise ValueError("密文长度无效")
            key = hashlib.sha256(self.encrypt_key.encode("utf-8")).digest()
            plain = AES.new(key, AES.MODE_CBC, blob[:AES.block_size]).decrypt(blob[AES.block_size:])
            padding = plain[-1]
            if not 1 <= padding <= AES.block_size or plain[-padding:] != bytes([padding]) * padding:
                raise ValueError("PKCS7 填充无效")
            decoded = json.loads(plain[:-padding].decode("utf-8"))
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PermissionError("飞书事件解密失败，请检查 FEISHU_ENCRYPT_KEY。") from exc
        if not isinstance(decoded, dict):
            raise ValueError("飞书解密事件必须是 JSON object。")
        return decoded

    def _validate_event_age(self, payload: dict) -> None:
        header = payload.get("header") or {}
        event = payload.get("event") or {}
        raw = header.get("create_time") or event.get("create_time") or payload.get("create_time")
        if raw in (None, ""):
            return
        try:
            timestamp = float(raw)
        except (TypeError, ValueError):
            raise PermissionError("飞书事件时间戳无效。") from None
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        if abs(self._wall_clock() - timestamp) > self.max_event_age_seconds:
            raise PermissionError("飞书事件已过期或时间偏差过大。")

    def verify_payload(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            raise ValueError("飞书事件必须是 JSON object。")
        self._validate_event_age(payload)
        received = str(
            (payload.get("header") or {}).get("token")
            or payload.get("token")
            or (payload.get("event") or {}).get("token")
            or ""
        )
        if self.verification_token and not hmac.compare_digest(received, self.verification_token):
            raise PermissionError("飞书 Verification Token 校验失败。")

    def parse_event(self, payload: dict, verify: bool = True) -> InboundMessage | None:
        payload = self._decrypt_payload(payload)
        if verify:
            self.verify_payload(payload)
        header = payload.get("header") or {}
        event_type = str(header.get("event_type") or payload.get("type") or "")
        if event_type in {"card.action.trigger", "card.action.trigger_v1"}:
            return self._parse_card_action(payload, header)
        if event_type != "im.message.receive_v1":
            return None
        event = payload.get("event") or {}
        message = event.get("message") or {}
        message_type = str(message.get("message_type") or "")
        try:
            content = json.loads(message.get("content") or "{}")
        except json.JSONDecodeError:
            return None
        text = self._message_text(message_type, content)
        sender_ids = ((event.get("sender") or {}).get("sender_id") or {})
        user_id = sender_ids.get("open_id") or sender_ids.get("union_id") or sender_ids.get("user_id")
        chat_id = message.get("chat_id")
        message_id = message.get("message_id")
        event_id = header.get("event_id") or message_id
        if not all(isinstance(value, str) and value for value in [user_id, chat_id, message_id, event_id]):
            raise ValueError("飞书消息事件缺少用户、会话或消息标识。")
        attachments = ()
        if message_type in {"file", "image", "audio", "media"}:
            resource_key = content.get("file_key") or content.get("image_key")
            if isinstance(resource_key, str) and resource_key:
                filename = str(content.get("file_name") or f"feishu-{message_type}-{message_id}")
                if message_type == "image" and "." not in Path(filename).name:
                    filename += ".png"
                mime = {
                    "image": "image/png", "audio": "audio/ogg", "media": "video/mp4",
                }.get(message_type, "application/octet-stream")
                attachments = ({
                    "filename": filename,
                    "contentType": mime,
                    "loader": lambda mid=message_id, key=resource_key, kind=message_type: self._download_resource(mid, key, kind),
                },)
                text = text or f"请读取并分析飞书{message_type}附件。"
        # A shared Feishu/Wiki document is not an IM file resource.  Feishu
        # sends it as a normal text URL, so turn recognized cloud-document
        # links into lazy text attachments.  ChannelService will download
        # and persist them before the Agent turn, just like a local upload.
        document_urls = self._document_urls(text)
        # Shared-document cards/forwarded messages may keep the URL in a
        # nested field rather than in ``content.text``.
        if not document_urls:
            document_urls = self._document_urls(json.dumps(content, ensure_ascii=False))
        for document_url in document_urls:
            token = document_url.rstrip("/").rsplit("/", 1)[-1]
            attachments += ({
                "filename": f"feishu-document-{token}.txt",
                "contentType": "text/plain; charset=utf-8",
                "loader": lambda url=document_url: self._download_cloud_document(url),
            },)
        if attachments and not text:
            text = "请读取并分析飞书文档或附件。"
        if not text and not attachments:
            return None
        return InboundMessage(
            channel=self.name, event_id=event_id, external_user_id=user_id,
            conversation_id=chat_id, text=text, reply_token=message_id,
            attachments=attachments, metadata={"tenantKey": header.get("tenant_key")},
        )

    @classmethod
    def _parse_card_action(cls, payload: dict, header: dict) -> InboundMessage:
        """Turn an interactive-card callback into the normal approval command."""
        event = payload.get("event") or {}
        action = event.get("action") or payload.get("action") or {}
        value = action.get("value") if isinstance(action, dict) else None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        if not isinstance(value, dict):
            value = {}
        action_name = str(value.get("action") or value.get("decision") or "").strip().lower()
        if action_name in {"approved", "approve", "allow"}:
            command = "approve"
        elif action_name in {"rejected", "reject", "deny"}:
            command = "reject"
        else:
            raise ValueError("飞书审批卡片动作无效")
        approval_id = str(value.get("approvalId") or value.get("approval_id") or "").strip()
        if not re.fullmatch(r"approval_[A-Za-z0-9]+", approval_id):
            raise ValueError("飞书审批卡片缺少有效 approvalId")
        operator = event.get("operator") or payload.get("operator") or {}
        operator_id = str(
            operator.get("open_id") or operator.get("union_id") or operator.get("user_id") or ""
        )
        context = event.get("context") or payload.get("context") or {}
        chat_id = str(
            context.get("open_chat_id") or context.get("chat_id")
            or event.get("open_chat_id") or event.get("chat_id") or ""
        )
        event_id = str(
            header.get("event_id") or event.get("event_id") or action.get("token")
            or f"card-{approval_id}-{operator_id}"
        )
        if not operator_id or not chat_id:
            raise ValueError("飞书审批卡片事件缺少操作人或会话标识")
        return InboundMessage(
            channel=cls.name,
            event_id=event_id,
            external_user_id=operator_id,
            conversation_id=chat_id,
            text=f"/{command} {approval_id}",
            reply_token=None,
            metadata={
                "tenantKey": header.get("tenant_key"),
                "cardAction": True,
                "approvalId": approval_id,
                "action": command,
            },
        )

    def _download_resource(self, message_id: str, resource_key: str, resource_type: str) -> bytes:
        query = urlencode({"type": resource_type})
        request = Request(
            f"{FEISHU_BASE}/im/v1/messages/{message_id}/resources/{resource_key}?{query}",
            headers={"Authorization": f"Bearer {self._tenant_token()}", "User-Agent": "SJTUClaw/1.0"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=30) as response:
                data = response.read()
        except (HTTPError, URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError("下载飞书消息附件失败。") from exc
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("飞书附件超过 SJTUClaw 10MB 限制。")
        return data

    @staticmethod
    def _message_text(message_type: str, content: dict) -> str:
        """Extract text from plain and rich-text Feishu message payloads."""
        if message_type == "text":
            return str(content.get("text") or "").strip()
        if message_type == "post":
            parts: list[str] = []

            def visit(value: Any) -> None:
                if isinstance(value, dict):
                    if isinstance(value.get("text"), str):
                        parts.append(value["text"])
                    for key, child in value.items():
                        if key != "text":
                            visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)

            visit(content)
            return " ".join(part.strip() for part in parts if part.strip()).strip()
        return str(content.get("text") or content.get("title") or "").strip()

    @classmethod
    def _document_urls(cls, text: str) -> list[str]:
        """Return Feishu cloud-document URLs embedded in a message."""
        urls: list[str] = []
        for raw in re.findall(r"https?://[^\s<>]+", text or ""):
            candidate = raw.rstrip(".,，。；;:：!?！？)]}>")
            parsed = urlparse(candidate)
            host = parsed.netloc.lower()
            parts = [part for part in parsed.path.split("/") if part]
            if not parts or not any(
                host == domain or host.endswith("." + domain)
                for domain in ("feishu.cn", "larksuite.com", "larkoffice.com")
            ):
                continue
            if parts[0].lower() in {"wiki", "docx", "docs", "doc"} and len(parts) >= 2:
                if candidate not in urls:
                    urls.append(candidate)
        return urls[:3]

    def _download_cloud_document(self, document_url: str) -> bytes:
        """Resolve a Feishu/Wiki URL and fetch its plain-text document body."""
        parsed = urlparse(document_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise ValueError("飞书文档链接缺少文档标识。")
        kind, token = parts[0].lower(), parts[1]
        if kind == "wiki":
            node_payload = self._get_json(
                f"{FEISHU_BASE}/wiki/v2/spaces/get_node?{urlencode({'token': token})}"
            )
            node = ((node_payload.get("data") or {}).get("node") or {})
            kind = str(node.get("obj_type") or "").lower()
            token = str(node.get("obj_token") or "")
            if not token:
                raise RuntimeError("飞书 Wiki 节点没有返回可读取的文档资源。")
        if kind == "docx":
            payload = self._get_json(f"{FEISHU_BASE}/docx/v1/documents/{token}/raw_content")
            content = ((payload.get("data") or {}).get("content"))
        elif kind in {"docs", "doc"}:
            payload = self._get_json(f"{FEISHU_BASE}/doc/v2/{token}/content")
            data = payload.get("data") or {}
            content = data.get("content") if isinstance(data, dict) else None
        else:
            raise ValueError(
                f"暂不支持直接读取飞书 {kind or '未知类型'} 资源；请把文档导出为 DOCX/PDF 后发送。"
            )
        if not isinstance(content, str):
            raise RuntimeError("飞书文档接口未返回可读取的纯文本内容。")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_FEISHU_DOCUMENT_BYTES:
            raise ValueError("飞书云文档正文超过 SJTUClaw 10MB 限制。")
        return encoded

    def _get_json(self, url: str) -> dict:
        request = Request(
            url,
            headers={"Authorization": f"Bearer {self._tenant_token()}", "User-Agent": "SJTUClaw/1.0"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"飞书文档 API 调用失败（HTTP {exc.code}）。") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError("飞书文档 API 网络连接失败或超时。") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("飞书文档 API 返回了无法解析的响应。") from exc
        if not isinstance(payload, dict) or payload.get("code", 0) != 0:
            raise RuntimeError(
                f"飞书文档 API 返回错误：{payload.get('msg') or payload.get('message') or '未知错误'}"
            )
        return payload

    def send(self, message: InboundMessage, event: OutboundEvent) -> None:
        if event.event_type not in {"final", "assistant_note", "approval_required", "error"}:
            return
        text = prepare_channel_markdown(format_notification_text(event))
        if len(text) > 28_000:
            text = text[:28_000] + "\n\n（回复过长，已截断；完整记录请在 Web 端查看。）"
        title = notification_title(event)
        approvals = event.data.get("approvals") if isinstance(event.data, dict) else None
        if event.event_type == "approval_required" and isinstance(approvals, list) and approvals:
            card = self._approval_card(text, approvals)
            body = {
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            }
        elif self.card_version in {"2.0", "v2", "json2", "json-2.0"} and self._contains_table(text):
            # Feishu JSON 2.0 supports standard Markdown tables in the
            # markdown element.  Do not rewrite the source table into a
            # bullet list: keeping the original GFM is what lets modern
            # Feishu clients render the aligned grid/table presentation.
            card = {
                "schema": "2.0",
                "body": {"elements": [{"tag": "markdown", "content": text}]},
            }
            if title:
                status = (event.data.get("notification") or {}).get("status")
                card["header"] = {
                    "title": {"tag": "plain_text", "content": title},
                    "template": {"completed": "green", "failed": "red", "approval_required": "orange"}.get(status, "blue"),
                }
            body = {
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            }
        elif self._contains_markdown(text) or title:
            # Feishu's plain-text message type deliberately does not parse
            # Markdown.  Use the official interactive-card markdown element
            # for formatted answers; it renders on mobile and desktop without
            # requiring a client plugin.  Keep short plain replies as text so
            # simple acknowledgements remain lightweight.
            card = {
                "elements": [{"tag": "markdown", "content": self._to_feishu_markdown(text)}],
            }
            if title:
                status = (event.data.get("notification") or {}).get("status")
                card["header"] = {
                    "title": {"tag": "plain_text", "content": title},
                    "template": {"completed": "green", "failed": "red", "approval_required": "orange"}.get(status, "blue"),
                }
            body = {
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            }
        else:
            body = {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
        if event.data.get("proactive") or not message.reply_token:
            body["receive_id"] = message.conversation_id
            url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type=chat_id"
        else:
            url = f"{FEISHU_BASE}/im/v1/messages/{message.reply_token}/reply"
        self._post_json(url, body, authorization=f"Bearer {self._tenant_token()}")

    def begin_processing(self, message: InboundMessage):
        """Add Feishu's native Typing reaction to a fresh inbound message."""
        if not message.reply_token:
            return None
        try:
            payload = self._post_json(
                f"{FEISHU_BASE}/im/v1/messages/{message.reply_token}/reactions",
                {"reaction_type": {"emoji_type": "Typing"}},
                authorization=f"Bearer {self._tenant_token()}",
            )
            reaction_id = (payload.get("data") or {}).get("reaction_id")
            if reaction_id:
                return {"messageId": message.reply_token, "reactionId": str(reaction_id)}
        except Exception as exc:
            # Missing reaction permission must not prevent the reply itself.
            logger.debug("飞书输入状态添加失败：%s", exc)
        return None

    def end_processing(self, message: InboundMessage, handle) -> None:
        if not isinstance(handle, dict) or not handle.get("reactionId"):
            return
        try:
            self._delete_json(
                f"{FEISHU_BASE}/im/v1/messages/{handle['messageId']}"
                f"/reactions/{handle['reactionId']}",
                authorization=f"Bearer {self._tenant_token()}",
            )
        except Exception as exc:
            logger.debug("飞书输入状态移除失败：%s", exc)

    @classmethod
    def _approval_card(cls, text: str, approvals: list[dict[str, Any]]) -> dict[str, Any]:
        """Build a native Feishu card while retaining text-command fallback."""
        actions: list[dict[str, Any]] = []
        for item in approvals[:5]:
            approval_id = str(item.get("approvalId") or "").strip()
            if not approval_id:
                continue
            actions.extend([
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "批准执行"},
                    "type": "primary",
                    "value": {"action": "approve", "approvalId": approval_id},
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "拒绝"},
                    "type": "danger",
                    "value": {"action": "reject", "approvalId": approval_id},
                },
            ])
        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": cls._to_feishu_markdown(text)},
        ]
        if actions:
            elements.append({"tag": "action", "actions": actions})
        elements.append({
            "tag": "note",
            "elements": [{
                "tag": "plain_text",
                "content": "也可以发送 /approve <approvalId> 或 /reject <approvalId> [原因]",
            }],
        })
        return {
            "header": {
                "title": {"tag": "plain_text", "content": "需要审批"},
                "template": "orange",
            },
            "elements": elements,
        }

    @classmethod
    def _contains_markdown(cls, text: str) -> bool:
        """Return whether a reply benefits from Feishu's Markdown card."""
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return bool(re.search(
            r"(?:^|\n)\s{0,3}(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s)|"
            r"\*\*[^\n]+\*\*|__[^\n]+__|`[^`\n]+`|"
            r"\[[^\]]+\]\(https?://[^)]+\)|(?:^|\n)\s*\|[^\n]+\|",
            text,
            re.MULTILINE,
        ) or any(cls._is_table_header(lines, index) for index in range(len(lines))))

    @classmethod
    def _contains_table(cls, text: str) -> bool:
        """Return whether *text* contains a GFM-style table.

        This is intentionally narrower than ``_contains_markdown``: only
        table messages opt into JSON 2.0, while headings/lists continue to
        use the long-established compatible v1 card representation.
        """
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return any(cls._is_table_header(lines, index) for index in range(len(lines)))

    @classmethod
    def _to_feishu_markdown(cls, text: str) -> str:
        """Adapt common Markdown to syntax stable in Feishu mobile cards."""
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        output: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            if cls._is_table_header(lines, index):
                header = cls._table_cells(line)
                index += 2
                rows: list[list[str]] = []
                while index < len(lines) and cls._looks_like_table_row(lines[index]):
                    rows.append(cls._table_cells(lines[index]))
                    index += 1
                if header:
                    output.append("**" + " / ".join(header) + "**")
                for row in rows:
                    fields = []
                    for position, value in enumerate(row):
                        label = header[position] if position < len(header) else f"字段{position + 1}"
                        fields.append(f"**{label}**：{value}")
                    output.append("• " + "；".join(fields))
                if rows:
                    output.append("")
                continue
            if re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", line):
                output.append("")
                index += 1
                continue
            heading = re.match(r"^\s{0,3}#{1,6}\s+(.*)\s*$", line)
            if heading:
                output.append(f"**{heading.group(1).strip()}**")
            elif re.match(r"^\s{0,3}>\s?", line):
                quoted = re.sub(r"^\s{0,3}>\s?", "", line).strip()
                output.append(f"“{quoted}”" if quoted else "")
            elif re.match(r"^\s{0,3}[-*+]\s+", line):
                output.append("• " + re.sub(r"^\s{0,3}[-*+]\s+", "", line).strip())
            elif re.match(r"^\s{0,3}\d+[.)]\s+", line):
                match = re.match(r"^\s{0,3}(\d+)[.)]\s+(.*)$", line)
                output.append(f"{match.group(1)}) {match.group(2).strip()}")
            else:
                output.append(line)
            index += 1
        return "\n".join(output)

    @staticmethod
    def _looks_like_table_row(line: str) -> bool:
        return len(FeishuAdapter._table_cells(line)) >= 2

    @staticmethod
    def _table_cells(line: str) -> list[str]:
        stripped = line.strip()
        if "|" not in stripped:
            return []
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        cells: list[str] = []
        current: list[str] = []
        escaped = False
        for char in stripped:
            if char == "|" and not escaped:
                cells.append("".join(current).strip())
                current = []
            else:
                current.append(char)
            escaped = char == "\\" and not escaped
        cells.append("".join(current).strip())
        return [cell.replace("\\|", "|") for cell in cells]

    @classmethod
    def _is_table_header(cls, lines: list[str], index: int) -> bool:
        if index + 1 >= len(lines):
            return False
        header = cls._table_cells(lines[index])
        delimiter = cls._table_cells(lines[index + 1])
        if len(header) < 2 or len(delimiter) != len(header):
            return False
        return all(bool(re.fullmatch(r":?-{3,}:?", cell.replace(" ", ""))) for cell in delimiter)

    def _tenant_token(self) -> str:
        with self._token_lock:
            if self._token and self._clock() < self._token[1] - 60:
                return self._token[0]
            if not self.app_id or not self.app_secret:
                raise RuntimeError("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET。")
            payload = self._post_json(
                f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                {"app_id": self.app_id, "app_secret": self.app_secret},
            )
            token = payload.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("飞书未返回 tenant_access_token。")
            expire = int(payload.get("expire") or 7200)
            self._token = (token, self._clock() + expire)
            return token

    def _post_json(self, url: str, body: dict, authorization: str | None = None) -> dict:
        headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "SJTUClaw/1.0"}
        if authorization:
            headers["Authorization"] = authorization
        request = Request(
            url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with self._opener(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"飞书 API 调用失败（HTTP {exc.code}）。") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError("飞书 API 网络连接失败或超时。") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("飞书 API 返回了无法解析的响应。") from exc
        if not isinstance(payload, dict) or payload.get("code", 0) != 0:
            raise RuntimeError(f"飞书 API 返回错误：{payload.get('msg') or payload.get('message') or '未知错误'}")
        return payload

    def _delete_json(self, url: str, authorization: str | None = None) -> dict:
        headers = {"User-Agent": "SJTUClaw/1.0"}
        if authorization:
            headers["Authorization"] = authorization
        request = Request(url, headers=headers, method="DELETE")
        try:
            with self._opener(request, timeout=20) as response:
                raw = response.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {"code": 0}
        except HTTPError as exc:
            raise RuntimeError(f"飞书 API 调用失败（HTTP {exc.code}）。") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError("飞书 API 网络连接失败或超时。") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("飞书 API 返回了无法解析的响应。") from exc
        if not isinstance(payload, dict) or payload.get("code", 0) != 0:
            raise RuntimeError(f"飞书 API 返回错误：{payload.get('msg') or payload.get('message') or '未知错误'}")
        return payload
