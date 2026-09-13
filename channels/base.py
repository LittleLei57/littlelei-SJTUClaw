"""Transport-neutral channel message contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from tool_protocol import unwrap_final_content


@dataclass(frozen=True)
class InboundMessage:
    channel: str
    event_id: str
    external_user_id: str
    conversation_id: str
    text: str
    reply_token: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutboundEvent:
    event_type: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


SPEECH_TO_TEXT_ERROR_CODE = "speech_to_text_failed"


def speech_to_text_error(reason: str = "", *, channel: str | None = None) -> OutboundEvent:
    """Build the transport-neutral error used by future voice adapters.

    Voice input is intentionally opt-in for now, but all channels should
    surface transcription failures in the same shape instead of silently
    dropping an audio message or feeding an empty prompt to the Agent.
    """
    detail = str(reason or "语音内容无法识别。")[:1000]
    return OutboundEvent(
        "error",
        "语音转文字失败：请重新发送语音，或直接输入文字。",
        {
            "errorCode": SPEECH_TO_TEXT_ERROR_CODE,
            "source": "speech_to_text",
            "retryable": True,
            "detail": detail,
            **({"channel": channel} if channel else {}),
        },
    )


def notification_title(event: OutboundEvent) -> str | None:
    """Return a compact native-notification title when one is requested."""
    notification = event.data.get("notification") if isinstance(event.data, dict) else None
    status = notification.get("status") if isinstance(notification, dict) else None
    return {
        "completed": "✅ 定时任务完成",
        "failed": "⚠️ 定时任务失败",
        "approval_required": "🔐 定时任务等待审批",
    }.get(str(status))


def format_notification_text(event: OutboundEvent) -> str:
    """Add a native-channel-friendly status heading without changing normal replies."""
    text = unwrap_final_content(event.text) or "SJTUClaw 已完成处理。"
    title = notification_title(event)
    if not title or text.startswith(title):
        return text
    return f"{title}\n\n{text}"


class ChannelAdapter(Protocol):
    name: str

    def send(self, message: InboundMessage, event: OutboundEvent) -> None: ...

    # Optional transport-native processing feedback.  ChannelService checks
    # these methods dynamically so lightweight/test adapters do not need to
    # implement them.
    def begin_processing(self, message: InboundMessage) -> Any: ...
    def end_processing(self, message: InboundMessage, handle: Any) -> None: ...
