"""验证 QQ、飞书和微信渠道的消息收发适配。"""

import json
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from threading import Lock, Thread
import time
import unittest
from io import BytesIO
from unittest.mock import patch

from channels.base import (
    InboundMessage, OutboundEvent, SPEECH_TO_TEXT_ERROR_CODE,
    speech_to_text_error,
)
from channels.dedup import EventDeduplicator
from channels.feishu import FeishuAdapter
from channels.feishu_ws import FeishuLongConnection
from channels.qq import QQBotAdapter, QQBotConnection, format_qq_text
from channels.rich_text import prepare_channel_markdown, readable_latex
from channels.weixin import (
    WeixinAdapter, WeixinConnection, WeixinCredentialStore, WeixinLoginManager,
    _reconnect_delay,
)
from channels.service import MAX_CHANNEL_MESSAGE_CHARS, ChannelNotifier, ChannelService
from channels.session_map import ChannelSessionMap
from approval_store import ApprovalStore
from attachment_store import AttachmentStore
from context_builder import ContextBuilder
from runtime import AgentRuntime
from session_store import SessionStore
from tools import Tool, ToolRegistry


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class _RawResponse(_Response):
    def __init__(self, payload):
        self.payload = payload


class _Adapter:
    name = "test"

    def __init__(self):
        self.events = []

    def send(self, message, event):
        self.events.append((message, event))


class ChannelTests(unittest.TestCase):
    def test_weixin_reconnect_uses_bounded_exponential_backoff(self):
        self.assertEqual(
            [_reconnect_delay(attempt) for attempt in range(1, 8)],
            [2, 4, 8, 16, 32, 60, 60],
        )

    def test_speech_to_text_failure_has_one_transport_neutral_error_shape(self):
        event = speech_to_text_error("音频格式不受支持", channel="qqbot")
        self.assertEqual(event.event_type, "error")
        self.assertEqual(event.data["errorCode"], SPEECH_TO_TEXT_ERROR_CODE)
        self.assertTrue(event.data["retryable"])
        self.assertEqual(event.data["channel"], "qqbot")
        self.assertIn("语音转文字失败", event.text)

    def test_session_mapping_is_stable_and_does_not_change_current(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            current = store.current_id
            mapping = ChannelSessionMap(directory, store)
            first = mapping.get_or_create("feishu", "ou_1", "oc_1")
            second = mapping.get_or_create("feishu", "ou_1", "oc_1")
            self.assertEqual(first, second)
            self.assertEqual(store.current_id, current)

    def test_dedup_can_release_failed_event(self):
        with TemporaryDirectory() as directory:
            dedup = EventDeduplicator(directory)
            self.assertTrue(dedup.claim("feishu", "evt_1"))
            self.assertFalse(dedup.claim("feishu", "evt_1"))
            dedup.release("feishu", "evt_1")
            self.assertTrue(dedup.claim("feishu", "evt_1"))

    def test_channel_service_routes_final_and_releases_on_failure(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            dedup = EventDeduplicator(directory)
            adapter = _Adapter()
            inbound = InboundMessage("test", "evt", "user", "chat", "hello")

            class Runtime:
                def __init__(self):
                    self.fail = False

                def run(self, text, session_id, event_callback, make_current):
                    self.last = (text, session_id, make_current)
                    if self.fail:
                        raise RuntimeError("temporary")
                    event_callback({"type": "status", "message": "working"})
                    return SimpleNamespace(reply="done", pending_approvals=[], turn_id="turn_1")

            runtime = Runtime()
            service = ChannelService(runtime, mapping, dedup)
            self.assertTrue(service.handle(adapter, inbound))
            self.assertEqual([item[1].event_type for item in adapter.events], ["status", "final"])
            self.assertFalse(runtime.last[2])
            self.assertFalse(service.handle(adapter, inbound))

            failed = InboundMessage("test", "retry", "user", "chat", "again")
            runtime.fail = True
            with self.assertRaises(RuntimeError):
                service.handle(adapter, failed)
            runtime.fail = False
            self.assertTrue(service.handle(adapter, failed))

    def test_channel_delivers_final_before_post_turn_bookkeeping(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            order = []

            class Adapter(_Adapter):
                def send(self, message, event):
                    order.append(("send", event.event_type, event.text))
                    super().send(message, event)

            class Runtime:
                def run(self, _text, _session_id, event_callback, make_current):
                    self.make_current = make_current
                    order.append(("runtime", "answer"))
                    event_callback({
                        "type": "assistant_final",
                        "content": "answer ready",
                        "turnId": "turn-before-compact",
                    })
                    # Represents summary generation/persistence after the
                    # answer checkpoint. Channel delivery must precede it.
                    order.append(("runtime", "compaction"))
                    return SimpleNamespace(
                        reply="answer ready",
                        pending_approvals=[],
                        turn_id="turn-before-compact",
                    )

            adapter = Adapter()
            service = ChannelService(
                Runtime(), mapping, EventDeduplicator(directory),
            )
            inbound = InboundMessage(
                "test", "early-final", "user", "chat", "hello",
            )
            self.assertTrue(service.handle(adapter, inbound))
            self.assertEqual(
                order,
                [
                    ("runtime", "answer"),
                    ("send", "final", "answer ready"),
                    ("runtime", "compaction"),
                ],
            )
            self.assertEqual(
                [event.event_type for _message, event in adapter.events],
                ["final"],
            )

    def test_channel_service_wraps_agent_turn_with_processing_indicator(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            calls = []

            class Adapter(_Adapter):
                def begin_processing(self, message):
                    calls.append(("begin", message.event_id))
                    return "typing-handle"

                def end_processing(self, message, handle):
                    calls.append(("end", message.event_id, handle))

            class Runtime:
                def run(self, *_args, **_kwargs):
                    calls.append(("runtime",))
                    return SimpleNamespace(
                        reply="done", pending_approvals=[], turn_id="turn-typing",
                    )

            service = ChannelService(
                Runtime(), mapping, EventDeduplicator(directory),
            )
            inbound = InboundMessage("test", "typing-event", "user", "chat", "hello")
            service.handle(Adapter(), inbound)
            self.assertEqual(
                calls,
                [
                    ("begin", "typing-event"),
                    ("runtime",),
                    ("end", "typing-event", "typing-handle"),
                ],
            )

    def test_channel_rejects_oversized_inbound_payload_before_model_call(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            adapter = _Adapter()
            called = []

            class Runtime:
                def run(self, *_args, **_kwargs):
                    called.append(True)
                    return SimpleNamespace(reply="unexpected", pending_approvals=[], turn_id="turn")

            service = ChannelService(Runtime(), mapping, EventDeduplicator(directory))
            inbound = InboundMessage(
                "test", "huge-event", "user", "chat", "x" * (MAX_CHANNEL_MESSAGE_CHARS + 1)
            )
            self.assertTrue(service.handle(adapter, inbound))
            self.assertFalse(called)
            self.assertEqual(adapter.events[-1][1].event_type, "error")
            self.assertIn("消息过长", adapter.events[-1][1].text)

    def test_same_channel_session_is_serialized_across_concurrent_messages(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            adapter = _Adapter()
            state_lock = Lock()
            active = 0
            max_active = 0

            class Runtime:
                def run(self, *_args, **_kwargs):
                    nonlocal active, max_active
                    with state_lock:
                        active += 1
                        max_active = max(max_active, active)
                    time.sleep(0.03)
                    with state_lock:
                        active -= 1
                    return SimpleNamespace(reply="done", pending_approvals=[], turn_id="turn")

            service = ChannelService(Runtime(), mapping, EventDeduplicator(directory))
            messages = [
                InboundMessage("test", f"parallel-{index}", "user", "chat", "hello")
                for index in range(2)
            ]
            threads = [Thread(target=service.handle, args=(adapter, item)) for item in messages]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            self.assertEqual(max_active, 1)
            self.assertEqual([item[1].event_type for item in adapter.events], ["final", "final"])

    def test_channel_service_routes_assistant_progress_notes(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            dedup = EventDeduplicator(directory)
            adapter = _Adapter()
            inbound = InboundMessage("test", "evt-note", "user", "chat", "查资料")

            class Runtime:
                def run(self, text, session_id, event_callback, make_current):
                    event_callback({
                        "type": "assistant_note",
                        "content": "我先检索相关资料，再继续整理。",
                    })
                    return SimpleNamespace(reply="整理完成", pending_approvals=[], turn_id="turn_1")

            service = ChannelService(Runtime(), mapping, dedup)
            self.assertTrue(service.handle(adapter, inbound))
            self.assertEqual(
                [item[1].event_type for item in adapter.events],
                ["assistant_note", "final"],
            )
            self.assertEqual(adapter.events[0][1].text, "我先检索相关资料，再继续整理。")

    def test_channel_attachments_enter_shared_attachment_pipeline(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            adapter = _Adapter()

            class Runtime:
                def run(self, text, session_id, **_kwargs):
                    self.text = text
                    return SimpleNamespace(reply="done", pending_approvals=[], turn_id="turn")

            runtime = Runtime()
            service = ChannelService(
                runtime, mapping, EventDeduplicator(directory), AttachmentStore(store)
            )
            inbound = InboundMessage(
                "test", "file-event", "user", "chat", "分析文件",
                attachments=({"filename": "note.txt", "contentType": "text/plain", "content": b"hello"},),
            )
            self.assertTrue(service.handle(adapter, inbound))
            session_id = mapping.get_or_create("test", "user", "chat")
            self.assertEqual(store.get(session_id).attachments[0]["filename"], "note.txt")
            self.assertIn("[attached_files]", runtime.text)

    def test_channel_can_resolve_its_own_approval_by_command(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            approvals = ApprovalStore(directory)
            registry = ToolRegistry()
            registry.register(Tool(
                "confirm", "confirm", {"type": "object", "properties": {}, "additionalProperties": False},
                lambda: "executed", safety_level="approval_required",
            ))

            class Model:
                def complete(self, _messages):
                    return '{"type":"final","content":"审批后继续完成"}'

            runtime = AgentRuntime(
                Model(), store, ContextBuilder(tool_definitions=registry.definitions()),
                tool_registry=registry, approval_store=approvals,
            )
            adapter = _Adapter()
            service = ChannelService(runtime, mapping, EventDeduplicator(directory))
            session_id = mapping.get_or_create("test", "user", "chat")
            approval = approvals.create("batch", session_id, "confirm", {})
            inbound = InboundMessage(
                "test", "approve-event", "user", "chat", f"/approve {approval.approval_id}"
            )
            self.assertTrue(service.handle(adapter, inbound))
            self.assertEqual(approvals.get(approval.approval_id).status, "approved")
            self.assertEqual(adapter.events[-1][1].event_type, "final")
            self.assertIn("审批后继续完成", adapter.events[-1][1].text)

    def test_failed_approval_gets_explicit_retry_command(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            approvals = ApprovalStore(directory)
            registry = ToolRegistry()
            registry.register(Tool(
                "flaky", "flaky", {"type": "object", "properties": {}, "additionalProperties": False},
                lambda: {"success": False, "error": "temporary failure"},
                safety_level="approval_required",
            ))

            class Model:
                def complete(self, _messages):
                    return '{"type":"final","content":"执行失败"}'

            runtime = AgentRuntime(
                Model(), store, ContextBuilder(tool_definitions=registry.definitions()),
                tool_registry=registry, approval_store=approvals,
            )
            adapter = _Adapter()
            service = ChannelService(runtime, mapping, EventDeduplicator(directory))
            session_id = mapping.get_or_create("qq", "user", "chat")
            approval = approvals.create("batch", session_id, "flaky", {})
            approve = InboundMessage(
                "qq", "approve-event", "user", "chat", f"/approve {approval.approval_id}"
            )
            self.assertTrue(service.handle(adapter, approve))
            self.assertIn(f"/retry {approval.approval_id}", adapter.events[-1][1].text)
            retry = InboundMessage(
                "qq", "retry-event", "user", "chat", f"/retry {approval.approval_id}"
            )
            self.assertTrue(service.handle(adapter, retry))
            self.assertEqual(adapter.events[-1][1].event_type, "approval_required")
            self.assertEqual(len(adapter.events[-1][1].data["approvals"]), 1)
            self.assertNotEqual(
                adapter.events[-1][1].data["approvals"][0]["approvalId"],
                approval.approval_id,
            )

    def test_channel_notifier_uses_latest_route_for_scheduler_push(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            session_id = mapping.get_or_create("test", "user", "chat")
            inbound = InboundMessage(
                "test", "evt", "user", "chat", "hello", "reply",
                metadata={"route": "latest"},
            )
            mapping.remember_route(session_id, inbound)
            adapter = _Adapter()
            notifier = ChannelNotifier(mapping, {"test": adapter})
            self.assertTrue(notifier.notify(session_id, OutboundEvent("final", "scheduled")))
            sent_message, sent_event = adapter.events[-1]
            self.assertEqual(sent_message.metadata["route"], "latest")
            self.assertTrue(sent_event.data["proactive"])

    def test_channel_notifier_can_target_an_explicit_channel_route(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            session_id = mapping.get_or_create("feishu", "user", "chat")
            mapping._write({
                "feishu-route": {
                    "channel": "feishu", "externalUserId": "user", "conversationId": "chat",
                    "sessionId": session_id, "replyToken": "feishu-token",
                    "metadata": {"route": "feishu"}, "lastSeenAt": "2026-07-17T10:00:00+00:00",
                },
                "qq-route": {
                    "channel": "qqbot", "externalUserId": "user", "conversationId": "group",
                    "sessionId": session_id, "replyToken": "qq-token",
                    "metadata": {"route": "qqbot"}, "lastSeenAt": "2026-07-17T09:00:00+00:00",
                },
            })
            feishu = _Adapter()
            qqbot = _Adapter()
            notifier = ChannelNotifier(mapping, {"feishu": feishu, "qqbot": qqbot})
            event = OutboundEvent("final", "targeted", {"deliveryId": "delivery-channel", "deliveryChannel": "qqbot"})
            self.assertTrue(notifier.notify(session_id, event))
            self.assertEqual(feishu.events, [])
            self.assertEqual(len(qqbot.events), 1)
            self.assertEqual(qqbot.events[0][0].metadata["route"], "qqbot")

    def test_channel_notifier_broadcasts_with_independent_delivery_ids(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            session_id = mapping.get_or_create("feishu", "user", "chat")
            mapping._write({
                "feishu-route": {
                    "channel": "feishu", "externalUserId": "user", "conversationId": "chat",
                    "sessionId": session_id, "replyToken": "feishu-token", "metadata": {},
                    "lastSeenAt": "2026-07-17T10:00:00+00:00",
                },
                "qq-route": {
                    "channel": "qqbot", "externalUserId": "user", "conversationId": "group",
                    "sessionId": session_id, "replyToken": "qq-token", "metadata": {},
                    "lastSeenAt": "2026-07-17T09:00:00+00:00",
                },
            })
            feishu = _Adapter()
            qqbot = _Adapter()
            notifier = ChannelNotifier(mapping, {"feishu": feishu, "qqbot": qqbot}, data_dir=directory)
            event = OutboundEvent(
                "final", "broadcast",
                {"deliveryId": "delivery-broadcast", "deliveryChannels": ["feishu", "qqbot"]},
            )
            self.assertTrue(notifier.notify(session_id, event))
            self.assertEqual(len(feishu.events), 1)
            self.assertEqual(len(qqbot.events), 1)
            parent = notifier.delivery_store.get("delivery-broadcast")
            self.assertEqual(parent["deliveryKind"], "broadcast")
            self.assertEqual(parent["status"], "delivered")
            self.assertEqual(set(parent["childStatuses"].values()), {"delivered"})
            self.assertEqual(notifier.delivery_store.get("delivery-broadcast:feishu")["status"], "delivered")
            self.assertEqual(notifier.delivery_store.get("delivery-broadcast:qqbot")["status"], "delivered")

    def test_channel_notifier_delivery_id_is_idempotent(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            session_id = mapping.get_or_create("test", "user", "chat")
            mapping.remember_route(session_id, InboundMessage("test", "evt", "user", "chat", "hello"))
            adapter = _Adapter()
            notifier = ChannelNotifier(mapping, {"test": adapter}, data_dir=directory)
            event = OutboundEvent("final", "scheduled", {"deliveryId": "delivery-1"})
            self.assertTrue(notifier.notify(session_id, event))
            self.assertTrue(notifier.notify(session_id, event))
            self.assertEqual(len(adapter.events), 1)
            restored = ChannelNotifier(mapping, {"test": adapter}, data_dir=directory)
            self.assertEqual(restored.delivery_store.get("delivery-1")["status"], "delivered")

    def test_channel_notifier_restart_marks_inflight_unknown_without_resend(self):
        """A restart must not replay an adapter call with an unknown outcome."""
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            session_id = mapping.get_or_create("test", "user", "chat")
            mapping.remember_route(session_id, InboundMessage("test", "evt", "user", "chat", "hello"))
            adapter = _Adapter()
            first = ChannelNotifier(mapping, {"test": adapter}, data_dir=directory)
            event = OutboundEvent("final", "once", {"deliveryId": "delivery-crash"})
            first.delivery_store.ensure(
                "delivery-crash",
                {"sessionId": session_id, "eventType": event.event_type, "text": event.text, "data": event.data},
            )
            self.assertIsNotNone(first.delivery_store.claim_attempt("delivery-crash"))

            restored = ChannelNotifier(mapping, {"test": adapter}, data_dir=directory)
            self.assertFalse(restored.notify(session_id, event))
            item = restored.delivery_store.get("delivery-crash")
            self.assertEqual(item["status"], "unknown")
            self.assertEqual(item["deliveryOutcome"], "unknown")
            self.assertEqual(adapter.events, [])

    def test_channel_notifier_records_failed_delivery_for_timeline(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            session_id = mapping.get_or_create("test", "user", "chat")
            mapping.remember_route(session_id, InboundMessage("test", "evt", "user", "chat", "hello"))

            class Down(_Adapter):
                def send(self, message, event):
                    raise RuntimeError("channel offline")

            activity = []
            notifier = ChannelNotifier(
                mapping, {"test": Down()}, data_dir=directory,
                activity_recorder=lambda sid, data: activity.append((sid, data)),
            )
            event = OutboundEvent("final", "retry me", {"deliveryId": "delivery-failed"})
            self.assertFalse(notifier.notify(session_id, event))
            self.assertEqual(activity[-1][0], session_id)
            self.assertEqual(activity[-1][1]["status"], "pending")
            self.assertEqual(activity[-1][1]["outcome"], "retrying")
            self.assertIn("offline", activity[-1][1]["lastError"])

    def test_channel_notifier_retries_adapter_failure_without_rerunning_agent(self):
        with TemporaryDirectory() as directory:
            store = SessionStore(directory)
            mapping = ChannelSessionMap(directory, store)
            session_id = mapping.get_or_create("test", "user", "chat")
            mapping.remember_route(session_id, InboundMessage("test", "evt", "user", "chat", "hello"))

            class Flaky(_Adapter):
                def __init__(self):
                    super().__init__(); self.calls = 0
                def send(self, message, event):
                    self.calls += 1
                    if self.calls == 1:
                        raise RuntimeError("temporary channel outage")
                    super().send(message, event)

            adapter = Flaky()
            notifier = ChannelNotifier(mapping, {"test": adapter}, data_dir=directory, retry_interval=0.01)
            event = OutboundEvent("final", "scheduled", {"deliveryId": "delivery-2"})
            self.assertFalse(notifier.notify(session_id, event))
            notifier.delivery_store._update("delivery-2", nextRetryAt="2000-01-01T00:00:00+00:00")
            notifier.start()
            try:
                for _ in range(100):
                    if notifier.delivery_store.get("delivery-2")["status"] == "delivered":
                        break
                    time.sleep(0.01)
            finally:
                notifier.stop()
            self.assertEqual(adapter.calls, 2)
            self.assertEqual(len(adapter.events), 1)

    def test_feishu_parses_text_and_replies_with_cached_token(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            if "tenant_access_token" in request.full_url:
                return _Response({"code": 0, "tenant_access_token": "token", "expire": 7200})
            return _Response({"code": 0, "msg": "success"})

        adapter = FeishuAdapter("id", "secret", "verify", opener=opener, clock=lambda: 10)
        payload = {
            "header": {"token": "verify", "event_type": "im.message.receive_v1", "event_id": "evt"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {"message_id": "om_msg", "chat_id": "oc_chat", "message_type": "text",
                            "content": json.dumps({"text": "你好"}, ensure_ascii=False)},
            },
        }
        message = adapter.parse_event(payload)
        self.assertEqual(message.text, "你好")
        adapter.send(message, OutboundEvent("final", "回答"))
        adapter.send(message, OutboundEvent("final", "第二次"))
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[1][0].headers["Authorization"], "Bearer token")
        body = json.loads(requests[1][0].data.decode("utf-8"))
        self.assertEqual(json.loads(body["content"])["text"], "回答")

    def test_feishu_processing_indicator_adds_and_removes_typing_reaction(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            if "tenant_access_token" in request.full_url:
                return _Response({"code": 0, "tenant_access_token": "token", "expire": 7200})
            if request.get_method() == "POST":
                return _Response({"code": 0, "data": {"reaction_id": "reaction-1"}})
            return _Response({"code": 0})

        adapter = FeishuAdapter("id", "secret", opener=opener)
        message = InboundMessage("feishu", "evt", "user", "chat", "hello", "om_msg")
        handle = adapter.begin_processing(message)
        adapter.end_processing(message, handle)

        reaction_requests = [
            item for item in requests if "/reactions" in item.full_url
        ]
        self.assertEqual(
            [item.get_method() for item in reaction_requests],
            ["POST", "DELETE"],
        )
        body = json.loads(reaction_requests[0].data.decode("utf-8"))
        self.assertEqual(body["reaction_type"]["emoji_type"], "Typing")
        self.assertTrue(reaction_requests[1].full_url.endswith("/reactions/reaction-1"))

    def test_feishu_markdown_reply_uses_mobile_renderable_card(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            if "tenant_access_token" in request.full_url:
                return _Response({"code": 0, "tenant_access_token": "token", "expire": 7200})
            return _Response({"code": 0})

        adapter = FeishuAdapter("id", "secret", opener=opener)
        message = InboundMessage("feishu", "evt", "user", "chat", "", "om_msg")
        adapter.send(message, OutboundEvent("final", "## 结果\n\n- **已完成**\n- [来源](https://example.com)"))

        body = json.loads(requests[-1].data.decode("utf-8"))
        self.assertEqual(body["msg_type"], "interactive")
        card = json.loads(body["content"])
        self.assertEqual(card["elements"][0]["tag"], "markdown")
        self.assertIn("已完成", card["elements"][0]["content"])

    def test_feishu_card_keeps_markdown_and_degrades_latex_readably(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            if "tenant_access_token" in request.full_url:
                return _Response({"code": 0, "tenant_access_token": "token", "expire": 7200})
            return _Response({"code": 0})

        adapter = FeishuAdapter("id", "secret", opener=opener)
        message = InboundMessage("feishu", "evt", "user", "chat", "", "om_msg")
        adapter.send(
            message,
            OutboundEvent("final", r"## 解答" "\n\n" r"$x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}$"),
        )

        card = json.loads(json.loads(requests[-1].data.decode("utf-8"))["content"])
        content = card["elements"][0]["content"]
        self.assertIn("**解答**", content)
        self.assertIn("±", content)
        self.assertIn("√", content)
        self.assertNotIn(r"\frac", content)
        self.assertNotIn("$", content)

    def test_feishu_markdown_table_uses_json2_card(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            if "tenant_access_token" in request.full_url:
                return _Response({"code": 0, "tenant_access_token": "token", "expire": 7200})
            return _Response({"code": 0})

        adapter = FeishuAdapter("id", "secret", opener=opener, card_version="2.0")
        message = InboundMessage("feishu", "evt", "user", "chat", "", "om_msg")
        source = "## 结果\n\n| 名称 | 结论 |\n| --- | --- |\n| A | B |"
        adapter.send(message, OutboundEvent("final", source))

        body = json.loads(requests[-1].data.decode("utf-8"))
        self.assertEqual(body["msg_type"], "interactive")
        card = json.loads(body["content"])
        self.assertEqual(card["schema"], "2.0")
        content = card["body"]["elements"][0]["content"]
        self.assertIn("| 名称 | 结论 |", content)
        self.assertIn("| --- | --- |", content)
        self.assertNotIn("**名称**：A", content)

    def test_feishu_markdown_table_legacy_card_keeps_compatibility(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            if "tenant_access_token" in request.full_url:
                return _Response({"code": 0, "tenant_access_token": "token", "expire": 7200})
            return _Response({"code": 0})

        adapter = FeishuAdapter("id", "secret", opener=opener, card_version="legacy")
        message = InboundMessage("feishu", "evt", "user", "chat", "", "om_msg")
        source = "| 名称 | 结论 |\n| --- | --- |\n| A | B |"
        adapter.send(message, OutboundEvent("final", source))

        card = json.loads(json.loads(requests[-1].data.decode("utf-8"))["content"])
        self.assertNotIn("schema", card)
        content = card["elements"][0]["content"]
        self.assertNotIn("| --- |", content)
        self.assertIn("**名称**：A", content)

    def test_feishu_markdown_adapter_falls_back_for_headings_and_tables(self):
        source = "## 标题\n\n| 名称 | 结论 |\n| --- | --- |\n| A | B |"
        rendered = FeishuAdapter._to_feishu_markdown(source)
        self.assertNotIn("##", rendered)
        self.assertNotIn("| --- |", rendered)
        self.assertIn("**标题**", rendered)
        self.assertIn("**名称**：A", rendered)
        self.assertIn("**结论**：B", rendered)

    def test_feishu_markdown_adapter_accepts_tables_without_outer_pipes(self):
        source = "标题\n\n名称 | 结论\n--- | ---\nA | B"
        rendered = FeishuAdapter._to_feishu_markdown(source)
        self.assertNotIn("名称 | 结论", rendered)
        self.assertNotIn("--- | ---", rendered)
        self.assertIn("**名称**：A", rendered)

    def test_feishu_wiki_document_link_becomes_readable_attachment(self):
        requests = []

        def opener(request, timeout):
            requests.append(request.full_url)
            if "tenant_access_token" in request.full_url:
                return _Response({"code": 0, "tenant_access_token": "token", "expire": 7200})
            if "/wiki/v2/spaces/get_node?" in request.full_url:
                return _Response({
                    "code": 0,
                    "data": {"node": {"obj_type": "docx", "obj_token": "doxcn-document"}},
                })
            if "/docx/v1/documents/doxcn-document/raw_content" in request.full_url:
                return _Response({"code": 0, "data": {"content": "文档第一段\n文档第二段"}})
            raise AssertionError(request.full_url)

        adapter = FeishuAdapter("id", "secret", opener=opener)
        payload = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "doc-event"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_doc", "chat_id": "oc_chat", "message_type": "text",
                    "content": json.dumps({
                        "text": "请读一下 https://my.feishu.cn/wiki/wikcn-node-token",
                    }),
                },
            },
        }
        message = adapter.parse_event(payload, verify=False)
        self.assertEqual(len(message.attachments), 1)
        self.assertEqual(message.attachments[0]["contentType"], "text/plain; charset=utf-8")
        self.assertEqual(message.attachments[0]["loader"](), "文档第一段\n文档第二段".encode())
        self.assertTrue(any("/wiki/v2/spaces/get_node" in url for url in requests))

    def test_feishu_post_message_extracts_text_and_document_link(self):
        adapter = FeishuAdapter("id", "secret")
        payload = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "post-event"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_post", "chat_id": "oc_chat", "message_type": "post",
                    "content": json.dumps({"zh_cn": {"title": "标题", "content": [[
                        {"tag": "text", "text": "正文"},
                    ]]}}, ensure_ascii=False),
                },
            },
        }
        message = adapter.parse_event(payload, verify=False)
        self.assertIn("正文", message.text)

    def test_feishu_rejects_bad_token_and_ignores_non_text(self):
        adapter = FeishuAdapter(verification_token="expected")
        with self.assertRaises(PermissionError):
            adapter.verify_payload({"header": {"token": "wrong"}})
        payload = {"header": {"token": "expected", "event_type": "other"}}
        self.assertIsNone(adapter.parse_event(payload))

    def test_feishu_webhook_signature_and_replay_window(self):
        adapter = FeishuAdapter(
            verification_token="verify", encrypt_key="0123456789abcdef",
            wall_clock=lambda: 1_000.0, max_event_age_seconds=30,
        )
        body = b'{"header":{"token":"verify"}}'
        timestamp, nonce = "990", "nonce-1"
        signature = hashlib.sha256(
            timestamp.encode() + nonce.encode() + adapter.encrypt_key.encode() + body
        ).hexdigest()
        adapter.verify_http_request(body, {
            "X-Lark-Request-Timestamp": timestamp,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": signature,
        })
        with self.assertRaises(PermissionError):
            adapter.verify_http_request(body, {
                "X-Lark-Request-Timestamp": "900",
                "X-Lark-Request-Nonce": nonce,
                "X-Lark-Signature": signature,
            })
        with self.assertRaises(PermissionError):
            adapter.verify_http_request(body, {
                "X-Lark-Request-Timestamp": timestamp,
                "X-Lark-Request-Nonce": nonce,
                "X-Lark-Signature": "bad",
            })

    def test_feishu_encrypted_payload_uses_key_and_pkcs7(self):
        class FakeCipher:
            def decrypt(self, _ciphertext):
                return b'{"header":{"token":"verify"}}' + b"\x01"

        class FakeAES:
            block_size = 16
            MODE_CBC = object()

            @staticmethod
            def new(_key, _mode, _iv):
                return FakeCipher()

        adapter = FeishuAdapter(verification_token="verify", encrypt_key="0123456789abcdef")
        with patch("channels.feishu.AES", FakeAES):
            decoded = adapter._decrypt_payload({"encrypt": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="})
        self.assertEqual(decoded["header"]["token"], "verify")

    def test_feishu_file_message_downloads_through_channel_attachment_loader(self):
        requests = []

        def opener(request, timeout):
            requests.append(request.full_url)
            if "tenant_access_token" in request.full_url:
                return _Response({"code": 0, "tenant_access_token": "token", "expire": 7200})
            return _RawResponse(b"office bytes")

        adapter = FeishuAdapter("id", "secret", opener=opener)
        payload = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "file-event"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_user"}},
                "message": {
                    "message_id": "om_file", "chat_id": "oc_chat", "message_type": "file",
                    "content": json.dumps({"file_key": "file-key", "file_name": "report.docx"}),
                },
            },
        }
        message = adapter.parse_event(payload, verify=False)
        self.assertEqual(message.attachments[0]["filename"], "report.docx")
        self.assertEqual(message.attachments[0]["loader"](), b"office bytes")
        self.assertTrue(any("/resources/file-key?type=file" in url for url in requests))

    def test_feishu_proactive_event_sends_to_chat_instead_of_replying(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            if "tenant_access_token" in request.full_url:
                return _Response({"code": 0, "tenant_access_token": "token", "expire": 7200})
            return _Response({"code": 0})

        adapter = FeishuAdapter("id", "secret", opener=opener)
        inbound = InboundMessage("feishu", "event", "user", "oc_chat", "", None)
        adapter.send(inbound, OutboundEvent("final", "定时任务完成", {"proactive": True}))
        sent = requests[-1]
        self.assertIn("receive_id_type=chat_id", sent.full_url)
        self.assertEqual(json.loads(sent.data.decode())["receive_id"], "oc_chat")

    def test_feishu_scheduler_notification_uses_native_card_header(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            if "tenant_access_token" in request.full_url:
                return _Response({"code": 0, "tenant_access_token": "token", "expire": 7200})
            return _Response({"code": 0})

        adapter = FeishuAdapter("id", "secret", opener=opener)
        inbound = InboundMessage("feishu", "event", "user", "oc_chat", "", None)
        adapter.send(inbound, OutboundEvent(
            "final", "任务已经完成。",
            {"proactive": True, "notification": {"kind": "scheduler", "status": "completed"}},
        ))
        body = json.loads(requests[-1].data.decode())
        self.assertEqual(body["msg_type"], "interactive")
        card = json.loads(body["content"])
        self.assertEqual(card["header"]["template"], "green")
        self.assertIn("定时任务完成", card["header"]["title"]["content"])

    def test_feishu_approval_notification_uses_action_buttons(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            if "tenant_access_token" in request.full_url:
                return _Response({"code": 0, "tenant_access_token": "token", "expire": 7200})
            return _Response({"code": 0})

        adapter = FeishuAdapter("id", "secret", opener=opener)
        inbound = InboundMessage("feishu", "event", "user", "oc_chat", "", "om_msg")
        adapter.send(inbound, OutboundEvent(
            "approval_required", "need approval",
            {"approvals": [{"approvalId": "approval_123", "tool": "run_command"}]},
        ))
        body = json.loads(requests[-1].data.decode())
        self.assertEqual(body["msg_type"], "interactive")
        card = json.loads(body["content"])
        action = next(item for item in card["elements"] if item.get("tag") == "action")
        self.assertEqual(action["actions"][0]["value"], {"action": "approve", "approvalId": "approval_123"})
        self.assertEqual(action["actions"][1]["value"], {"action": "reject", "approvalId": "approval_123"})

    def test_feishu_card_action_parses_to_approval_command(self):
        adapter = FeishuAdapter("id", "secret", verification_token="verify")
        payload = {
            "header": {
                "token": "verify", "event_type": "card.action.trigger",
                "event_id": "card-event",
            },
            "event": {
                "operator": {"open_id": "ou_user"},
                "context": {"open_chat_id": "oc_chat"},
                "action": {"value": {"action": "approve", "approvalId": "approval_123"}},
            },
        }
        message = adapter.parse_event(payload)
        self.assertEqual(message.text, "/approve approval_123")
        self.assertIsNone(message.reply_token)
        self.assertTrue(message.metadata["cardAction"])

    def test_feishu_websocket_starts_and_accepts_event_without_verification_token(self):
        calls = []

        class Builder:
            def register_p2_im_message_receive_v1(self, callback):
                calls.append(("handler", callback))
                return self

            def build(self):
                return "dispatcher"

        class Dispatcher:
            @staticmethod
            def builder(*_args):
                return Builder()

        class Client:
            def __init__(self, app_id, app_secret, **kwargs):
                calls.append(("client", app_id, app_secret, kwargs))

            def start(self):
                calls.append(("started",))

        class JSON:
            @staticmethod
            def marshal(value):
                return json.dumps(value)

        sdk = SimpleNamespace(
            EventDispatcherHandler=Dispatcher,
            ws=SimpleNamespace(Client=Client), LogLevel=SimpleNamespace(INFO=20), JSON=JSON,
        )
        adapter = FeishuAdapter("app", "secret", "webhook-only-token")
        service = SimpleNamespace(handle=lambda *_args: calls.append(("handled",)))
        connection = FeishuLongConnection(adapter, service, sdk=sdk)
        self.assertTrue(connection.start())
        connection._thread.join(timeout=1)
        self.assertIn(("started",), calls)

        payload = {
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt-ws"},
            "event": {"sender": {"sender_id": {"open_id": "ou"}}, "message": {
                "message_id": "om", "chat_id": "oc", "message_type": "text",
                "content": json.dumps({"text": "长连接"}, ensure_ascii=False),
            }},
        }
        connection._on_message(payload)
        for _ in range(100):
            if ("handled",) in calls:
                break
            import time
            time.sleep(0.005)
        self.assertIn(("handled",), calls)

    def test_qq_adapter_forwards_only_user_visible_events(self):
        adapter = QQBotAdapter("app", "secret")
        sent = []
        adapter.connection = SimpleNamespace(send_text=lambda message, text, proactive=False: sent.append((message, text, proactive)))
        inbound = InboundMessage("qqbot", "evt", "user", "c2c:user", "hi", "msg", metadata={})
        adapter.send(inbound, OutboundEvent("status", "working"))
        adapter.send(inbound, OutboundEvent("final", r"解：$x^2=\frac{1}{2}$"))
        self.assertEqual(sent, [(inbound, r"解：$x^2=\frac{1}{2}$", False)])

    def test_qq_adapter_delegates_native_typing_lifecycle(self):
        adapter = QQBotAdapter("app", "secret")
        calls = []
        adapter.connection = SimpleNamespace(
            begin_typing=lambda message: calls.append(("begin", message.event_id)) or "handle",
            end_typing=lambda handle: calls.append(("end", handle)),
        )
        inbound = InboundMessage("qqbot", "evt-typing", "user", "c2c:user", "hi", "msg")
        handle = adapter.begin_processing(inbound)
        adapter.end_processing(inbound, handle)
        self.assertEqual(calls, [("begin", "evt-typing"), ("end", "handle")])

    def test_qq_adapter_unwraps_final_protocol_envelope(self):
        adapter = QQBotAdapter("app", "secret")
        sent = []
        adapter.connection = SimpleNamespace(
            send_text=lambda message, text, proactive=False: sent.append(text)
        )
        inbound = InboundMessage(
            "qqbot", "evt", "user", "c2c:user", "hi", "msg", metadata={}
        )
        adapter.send(
            inbound,
            OutboundEvent(
                "final",
                '{"type":"final","content":"天气速报\\n晴朗"}',
            ),
        )
        self.assertEqual(sent, ["天气速报\n晴朗"])

    def test_qq_markdown_table_becomes_labelled_mobile_text(self):
        rendered = format_qq_text(
            "## 未来预报\n\n"
            "| 日期 | 天气 | 气温 |\n"
            "|---|---|---|\n"
            "| 今天 | **晴** | 28~35℃ |\n"
            "| 明天 | 多云 | 27~34℃ |"
        )
        self.assertIn("未来预报", rendered)
        self.assertNotIn("##", rendered)
        self.assertNotIn("|---", rendered)
        self.assertIn("• 今天\n  天气：晴；气温：28~35℃", rendered)
        self.assertIn("• 明天\n  天气：多云；气温：27~34℃", rendered)

    def test_qq_native_markdown_rejection_retries_as_plain_text(self):
        calls = []

        class Api:
            async def send_text(self, scope, chat_id, text, *, reply_to, markdown):
                calls.append((scope, chat_id, text, reply_to, markdown))
                if markdown:
                    raise RuntimeError("markdown rejected")
                return {"ok": True}

        connection = QQBotConnection(
            QQBotAdapter("app", "secret"),
            SimpleNamespace(handle=lambda *_args: None),
        )
        connection._loop = object()
        connection._api = Api()
        message = InboundMessage(
            "qqbot", "evt", "user", "c2c:user", "hi", "msg",
            metadata={"chatScope": "c2c", "chatId": "user"},
        )

        def submit(coroutine, _loop):
            import asyncio
            value = asyncio.run(coroutine)
            return SimpleNamespace(result=lambda timeout: value)

        with patch("channels.qq.asyncio.run_coroutine_threadsafe", side_effect=submit):
            connection.send_text(
                message,
                "## 结果\n\n| 项目 | 数值 |\n|---|---|\n| x | **1** |",
            )

        self.assertEqual([call[-1] for call in calls], [True, False])
        self.assertIn("## 结果", calls[0][2])
        self.assertNotIn("##", calls[1][2])
        self.assertNotIn("|---", calls[1][2])
        self.assertIn("项目：x", calls[1][2])

    def test_channel_latex_fallback_preserves_markdown_structure(self):
        source = (
            r"## 一元二次方程" "\n\n"
            r"- 解：$x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}$" "\n"
            r"- 条件：$\theta^2 \le 1$"
        )
        rendered = prepare_channel_markdown(source)
        self.assertIn("## 一元二次方程", rendered)
        self.assertIn("- 解：", rendered)
        self.assertIn("±", rendered)
        self.assertIn("√", rendered)
        self.assertIn("θ² ≤ 1", rendered)
        self.assertNotIn(r"\frac", rendered)
        self.assertNotIn("$", rendered)

        plain = format_qq_text(source)
        self.assertNotIn("##", plain)
        self.assertIn("x=", plain)
        self.assertIn("±", plain)
        self.assertIn("√", plain)

    def test_weixin_preserves_markdown_with_readable_formula_fallback(self):
        with TemporaryDirectory() as directory:
            adapter = WeixinAdapter(WeixinCredentialStore(directory))
            sent = []
            adapter.connection = SimpleNamespace(
                send_text=lambda message, text: sent.append(text)
            )
            inbound = InboundMessage("weixin", "evt", "user", "chat", "hi", "msg")
            adapter.send(
                inbound,
                OutboundEvent("final", r"## 结果" "\n\n" r"- $x^2 \ge 0$"),
            )
            self.assertEqual(len(sent), 1)
            self.assertIn("## 结果", sent[0])
            self.assertIn("- x² ≥ 0", sent[0])
            self.assertNotIn("$", sent[0])

    def test_weixin_processing_hint_is_sent_before_agent_work(self):
        with TemporaryDirectory() as directory:
            adapter = WeixinAdapter(WeixinCredentialStore(directory))
            calls = []
            adapter.connection = SimpleNamespace(
                send_text=lambda message, text, timeout=20: calls.append(
                    (message.event_id, text, timeout)
                )
            )
            inbound = InboundMessage("weixin", "evt-hint", "user", "chat", "hi", "msg")

            handle = adapter.begin_processing(inbound)

            self.assertIsNone(handle)
            self.assertEqual(calls, [("evt-hint", "··· 正在处理", 3)])

    def test_readable_latex_does_not_treat_currency_as_formula(self):
        self.assertEqual(readable_latex("预算是 $5 到 $10。"), "预算是 $5 到 $10。")

    def test_qq_connection_sets_up_http_client_before_gateway_request(self):
        import asyncio
        calls = []

        class Api:
            def __init__(self, *_args, **_kwargs): self.ready = False
            def setup(self, client): self.ready = client is not None; calls.append("setup")
            async def ensure_token(self): self._check(); calls.append("token"); return "token"
            async def get_gateway_url(self): self._check(); calls.append("gateway"); return "wss://example"
            def ensure_token_sync(self): return "token"
            def get_gateway_url_sync(self): return "wss://example"
            def clear_token(self): pass
            def _check(self):
                if not self.ready: raise RuntimeError("HTTP client not initialized")

        class WebSocket:
            def __init__(self, **_kwargs): pass
            def start(self, *_args): calls.append("ws-start")
            def stop(self): calls.append("ws-stop")

        sdk = SimpleNamespace(
            QQApiClient=Api, QQWebSocket=WebSocket,
            WSCallbacks=lambda **kwargs: SimpleNamespace(**kwargs),
            EventParser=lambda: None,
        )
        adapter = QQBotAdapter("app", "secret")
        connection = QQBotConnection(adapter, SimpleNamespace(handle=lambda *_args: None), sdk=sdk)
        connection._stop.set()
        asyncio.run(connection._main())
        self.assertEqual(calls[:4], ["setup", "token", "gateway", "ws-start"])

    def test_weixin_qr_login_saves_credentials_without_returning_token(self):
        requests = []
        responses = iter([
            {"qrcode": "qr/secret+value", "qrcode_img_content": "https://example.test/qr"},
            {"status": "confirmed", "bot_token": "bot-secret", "ilink_bot_id": "bot-id",
             "ilink_user_id": "wx-user", "baseurl": "https://wx.example.test"},
        ])

        def opener(request, timeout):
            requests.append((request, timeout))
            return _Response(next(responses))

        with TemporaryDirectory() as directory:
            store = WeixinCredentialStore(directory)
            login = WeixinLoginManager(store, opener=opener)
            started = login.start()
            result = login.poll(started["sessionId"])
            self.assertEqual(result["status"], "confirmed")
            self.assertNotIn("bot_token", result)
            saved = store.load()
            self.assertEqual(saved["token"], "bot-secret")
            self.assertEqual(saved["accountId"], "bot-id")
            headers = requests[0][0].headers
            self.assertEqual(headers["Authorizationtype"], "ilink_bot_token")
            self.assertIn("X-wechat-uin", headers)
            self.assertIn("qrcode=qr%2Fsecret%2Bvalue", requests[1][0].full_url)

    def test_weixin_message_normalization_uses_shared_channel_service(self):
        with TemporaryDirectory() as directory:
            store = WeixinCredentialStore(directory)
            handled = []
            adapter = WeixinAdapter(store)
            connection = WeixinConnection(adapter, SimpleNamespace(handle=lambda _adapter, msg: handled.append(msg)))
            connection._accept({
                "message_id": 42, "from_user_id": "wx-user", "session_id": "chat",
                "message_type": 1, "context_token": "ctx",
                "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
            })
            for _ in range(100):
                if handled: break
                import time
                time.sleep(0.005)
            self.assertEqual(handled[0].text, "你好")
            self.assertEqual(handled[0].metadata["contextToken"], "ctx")


if __name__ == "__main__":
    unittest.main()
