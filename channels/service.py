"""Run inbound channel messages through the shared Agent Runtime."""

from __future__ import annotations

from collections import defaultdict
from io import BytesIO
import inspect
import json
import logging
import re
from threading import Event, Lock, Thread
from pathlib import Path

from channels.base import ChannelAdapter, InboundMessage, OutboundEvent
from channels.dedup import EventDeduplicator
from channels.delivery import DeliveryStore
from channels.session_map import ChannelSessionMap
from runtime import AgentRuntime


MAX_CHANNEL_MESSAGE_CHARS = 100_000
logger = logging.getLogger(__name__)


class ChannelService:
    def __init__(
        self, runtime: AgentRuntime, session_map: ChannelSessionMap,
        deduplicator: EventDeduplicator, attachment_store=None,
    ):
        self.runtime = runtime
        self.session_map = session_map
        self.deduplicator = deduplicator
        self.attachment_store = attachment_store
        self._session_locks: defaultdict[str, Lock] = defaultdict(Lock)

    def handle(self, adapter: ChannelAdapter, message: InboundMessage) -> bool:
        if not self.deduplicator.claim(message.channel, message.event_id):
            return False
        if len(message.text or "") > MAX_CHANNEL_MESSAGE_CHARS:
            # Web requests are bounded by Pydantic, but Feishu/QQ/Weixin
            # adapters receive platform payloads directly.  Reject an
            # oversized inbound message before it reaches Context Builder or
            # the model, while keeping the event claimed so a platform does
            # not retry the same hostile payload forever.
            adapter.send(
                message,
                OutboundEvent(
                    "error",
                    f"消息过长，SJTUClaw 单条消息最多支持 {MAX_CHANNEL_MESSAGE_CHARS:,} 个字符。",
                    {"errorCode": "message_too_large", "maxChars": MAX_CHANNEL_MESSAGE_CHARS},
                ),
            )
            return True
        session_id = self.session_map.get_or_create(
            message.channel, message.external_user_id, message.conversation_id,
        )
        self.session_map.remember_route(session_id, message)

        approval_command = re.fullmatch(
            r"/(approve|reject|retry)\s+(approval_[A-Za-z0-9]+)(?:\s+(.+))?",
            message.text.strip(), re.I,
        )
        if approval_command:
            try:
                return self._handle_approval_command(adapter, message, session_id, approval_command)
            except Exception:
                self.deduplicator.release(message.channel, message.event_id)
                raise

        processing_handle = self._begin_processing(adapter, message)
        try:
            user_text = self._attach_files(message, session_id)
        except Exception as exc:
            # Channel attachments (including Feishu cloud-document links)
            # are fetched before the Agent turn.  Surface permission, link,
            # and download errors directly instead of entering the model with
            # an empty attachment and risking a fabricated answer.
            self._end_processing(adapter, message, processing_handle)
            adapter.send(message, OutboundEvent(
                "error",
                f"渠道附件读取失败：{exc}",
                {"errorCode": "channel_attachment_error", "detail": str(exc)},
            ))
            return True

        def emit(payload: dict):
            event_type = payload.get("type", "status")
            if event_type == "status":
                adapter.send(message, OutboundEvent("status", payload.get("message", ""), payload))
            elif event_type == "assistant_note":
                adapter.send(message, OutboundEvent(
                    "assistant_note", payload.get("content", ""), payload
                ))
            elif event_type in {"tool_call", "tool_result"}:
                adapter.send(message, OutboundEvent(event_type, data=payload))

        # ``assistant_final`` is emitted before Runtime starts post-turn
        # compaction. Deliver it at that checkpoint so QQ/Feishu/Weixin do
        # not wait for a potentially slow summary-model call. The result
        # fallback below remains for runtimes that do not emit this event.
        delivery_state = {"final_sent": False, "processing_ended": False}

        def end_processing_once() -> None:
            if delivery_state["processing_ended"]:
                return
            self._end_processing(adapter, message, processing_handle)
            delivery_state["processing_ended"] = True

        def emit_and_deliver(payload: dict):
            if payload.get("type") == "assistant_final" and payload.get("content"):
                end_processing_once()
                adapter.send(message, OutboundEvent(
                    "final",
                    str(payload.get("content")),
                    {**payload, "sessionId": session_id},
                ))
                delivery_state["final_sent"] = True
                return
            emit(payload)

        try:
            with self._session_locks[session_id]:
                kwargs = {
                    "event_callback": emit_and_deliver,
                    "make_current": False,
                }
                if "source" in inspect.signature(self.runtime.run).parameters:
                    kwargs["source"] = message.channel
                result = self.runtime.run(user_text, session_id, **kwargs)
        except Exception:
            end_processing_once()
            self.deduplicator.release(message.channel, message.event_id)
            raise
        end_processing_once()
        if result.pending_approvals:
            ids = [item["approvalId"] for item in result.pending_approvals]
            adapter.send(message, OutboundEvent(
                "approval_required",
                "需要审批：" + "、".join(ids)
                + "\n批准：/approve <approvalId>\n拒绝：/reject <approvalId> [原因]"
                + "\n失败后重试：/retry <approvalId>",
                {"approvals": result.pending_approvals, "sessionId": session_id},
            ))
        elif result.reply and not delivery_state["final_sent"]:
            adapter.send(message, OutboundEvent(
                "final", result.reply, {"sessionId": session_id, "turnId": result.turn_id},
            ))
        return True

    @staticmethod
    def _begin_processing(adapter: ChannelAdapter, message: InboundMessage):
        begin = getattr(adapter, "begin_processing", None)
        if not callable(begin):
            return None
        try:
            return begin(message)
        except Exception as exc:
            # A typing indicator is best-effort UX.  It must never block the
            # actual Agent turn when a platform lacks permission or is
            # temporarily rate-limited.
            logger.debug("%s 处理提示启动失败：%s", message.channel, exc)
            return None

    @staticmethod
    def _end_processing(adapter: ChannelAdapter, message: InboundMessage, handle) -> None:
        end = getattr(adapter, "end_processing", None)
        if not callable(end):
            return
        try:
            end(message, handle)
        except Exception as exc:
            logger.debug("%s 处理提示清理失败：%s", message.channel, exc)

    def _attach_files(self, message: InboundMessage, session_id: str) -> str:
        if not message.attachments or self.attachment_store is None:
            return message.text
        saved = []
        for item in message.attachments:
            content = item.get("content")
            if content is None and callable(item.get("loader")):
                content = item["loader"]()
            if not isinstance(content, (bytes, bytearray)):
                continue
            metadata = self.attachment_store.save(
                session_id,
                str(item.get("filename") or "channel-attachment"),
                str(item.get("contentType") or "application/octet-stream"),
                BytesIO(bytes(content)),
            )
            saved.append({
                "attachmentId": metadata["attachmentId"],
                "filename": metadata["filename"],
                "contentType": metadata["contentType"],
            })
        if not saved:
            return message.text
        visible = message.text.strip() or "请读取并分析这些附件。"
        return (
            f"{visible}\n\n[attached_files] {json.dumps(saved, ensure_ascii=False)}\n"
            "以上附件来自当前渠道消息，本轮必须读取后再回答。"
        )

    def _handle_approval_command(self, adapter, message, session_id, match) -> bool:
        approval_id = match.group(2)
        approval = self.runtime.approval_store.get(approval_id)
        if approval.session_id != session_id:
            raise PermissionError("不能处理其他会话的审批。")
        command = match.group(1).lower()
        if command == "retry":
            try:
                with self._session_locks[session_id]:
                    retry = self.runtime.retry_approval(approval_id)
            except (KeyError, ValueError, RuntimeError) as exc:
                adapter.send(message, OutboundEvent(
                    "error", f"无法重试该审批：{exc}",
                    {"sessionId": session_id, "approvalId": approval_id,
                     "errorCode": "approval_retry_unavailable"},
                ))
                return True
            adapter.send(message, OutboundEvent(
                "approval_required",
                f"已为失败的操作新建审批：{retry.approval_id}\n"
                f"工具：{retry.tool}（第 {retry.attempt} 次尝试）\n"
                f"批准：/approve {retry.approval_id}\n拒绝：/reject {retry.approval_id} [原因]",
                {"sessionId": session_id, "approvals": [retry.to_dict()], "retryOf": approval_id},
            ))
            return True
        approved = command == "approve"
        reason = match.group(3) or (None if approved else "用户在渠道中拒绝")
        with self._session_locks[session_id]:
            result = self.runtime.resolve_approval(approval_id, approved, reason)
        if result.pending_approvals:
            text = "该批次仍有待审批操作：" + "、".join(
                item["approvalId"] for item in result.pending_approvals
            )
            adapter.send(message, OutboundEvent(
                "approval_required", text,
                {"sessionId": session_id, "approvals": result.pending_approvals},
            ))
        else:
            text = result.reply or ("已批准并执行。" if approved else "已拒绝该操作。")
            failed_events = [
                event for event in result.tool_events
                if isinstance(event, dict)
                and isinstance(event.get("result"), dict)
                and event["result"].get("success") is False
            ]
            if approved and failed_events:
                failed_id = failed_events[-1].get("approvalId", approval_id)
                text += (
                    "\n\n工具执行失败；原审批已结束，未重复执行。"
                    f"如需再次申请审批，请发送：/retry {failed_id}"
                )
            adapter.send(message, OutboundEvent("final", text, {"sessionId": session_id}))
        return True


class ChannelNotifier:
    """Deliver proactive events with durable idempotency and retry.

    The Scheduler calls ``notify`` exactly once per logical result.  Adapter
    failures are persisted as pending deliveries and retried by this small
    worker, so a retry never re-runs the Agent turn.
    """

    def __init__(
        self,
        session_map: ChannelSessionMap,
        adapters: dict[str, ChannelAdapter],
        data_dir: str | Path | None = None,
        retry_interval: float = 1.0,
        activity_recorder=None,
    ):
        self.session_map = session_map
        self.adapters = adapters
        self.delivery_store = DeliveryStore(data_dir)
        self.retry_interval = retry_interval
        self.activity_recorder = activity_recorder
        self._stop = Event()
        self._thread: Thread | None = None
        for item in self.delivery_store.recover_inflight():
            self._record_delivery(item)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._retry_loop, name="sjtuclaw-delivery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self.retry_interval * 2))

    def notify(self, session_id: str, event: OutboundEvent) -> bool:
        requested_channels = event.data.get("deliveryChannels") if isinstance(event.data, dict) else None
        if isinstance(requested_channels, (list, tuple)):
            channels = list(dict.fromkeys(str(item).strip().lower() for item in requested_channels if str(item).strip()))
            if channels:
                if len(channels) > 1:
                    import hashlib
                    import json as _json
                    base_id = str(event.data.get("deliveryId") or "")
                    if not base_id:
                        raw = _json.dumps(
                            [session_id, event.event_type, event.text, event.data],
                            ensure_ascii=False, sort_keys=True, default=str,
                        )
                        base_id = "event:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
                    child_ids = [f"{base_id}:{channel}" for channel in channels]
                    self.delivery_store.ensure(
                        base_id,
                        {
                            "sessionId": session_id,
                            "eventType": event.event_type,
                            "text": event.text,
                            "data": event.data,
                            "deliveryKind": "broadcast",
                            "childDeliveryIds": child_ids,
                        },
                    )
                    results = []
                    for channel in channels:
                        data = {
                            **event.data,
                            "deliveryId": f"{base_id}:{channel}",
                            "deliveryChannel": channel,
                            "broadcastParentId": base_id,
                        }
                        data.pop("deliveryChannels", None)
                        results.append(self.notify(session_id, OutboundEvent(event.event_type, event.text, data)))
                    aggregate = self.delivery_store.summarize_broadcast(base_id)
                    if aggregate:
                        self._record_delivery(aggregate)
                    return all(results)
                data = {**event.data, "deliveryChannel": channels[0]}
                data.pop("deliveryChannels", None)
                event = OutboundEvent(event.event_type, event.text, data)
        delivery_id = str(event.data.get("deliveryId") or "")
        if not delivery_id:
            # Non-Scheduler callers still get deterministic idempotency.  The
            # explicit Scheduler deliveryId is preferred for recurring runs.
            import hashlib
            import json as _json
            raw = _json.dumps(
                [session_id, event.event_type, event.text, event.data],
                ensure_ascii=False, sort_keys=True, default=str,
            )
            delivery_id = "event:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        self.delivery_store.ensure(
            delivery_id,
            {
                "sessionId": session_id,
                "eventType": event.event_type,
                "text": event.text,
                "data": event.data,
            },
        )
        return self._attempt(delivery_id)

    def _attempt(self, delivery_id: str) -> bool:
        item = self.delivery_store.claim_attempt(delivery_id)
        if item is None:
            current = self.delivery_store.get(delivery_id)
            if current and current.get("status") == DeliveryStore.UNKNOWN_STATUS:
                self._record_delivery(current)
            return bool(current and current.get("status") == "delivered")
        session_id = str(item.get("sessionId"))
        event_data = item.get("data") or {}
        delivery_channel = event_data.get("deliveryChannel") if isinstance(event_data, dict) else None
        route = self.session_map.route_for_session(session_id, delivery_channel)
        adapter = self.adapters.get(str(route.get("channel"))) if route else None
        if not route or adapter is None:
            deferred = self.delivery_store.defer(delivery_id)
            if deferred:
                self._record_delivery(deferred)
            return False
        message = InboundMessage(
            channel=str(route["channel"]), event_id=f"proactive:{delivery_id}",
            external_user_id=str(route["externalUserId"]),
            conversation_id=str(route["conversationId"]), text="",
            reply_token=route.get("replyToken"), metadata=route.get("metadata") or {},
        )
        try:
            adapter.send(message, OutboundEvent(
                str(item.get("eventType") or "final"),
                str(item.get("text") or ""),
                {**(item.get("data") or {}), "proactive": True},
            ))
        except Exception as exc:
            failed = self.delivery_store.failed(delivery_id, str(exc))
            if failed:
                self._record_delivery(failed)
            return False
        self.delivery_store.delivered(delivery_id)
        delivered = self.delivery_store.get(delivery_id)
        if delivered:
            self._record_delivery(delivered)
        return True

    def _record_delivery(self, item: dict) -> None:
        """Expose durable delivery outcomes in the Session activity timeline."""
        parent_id = (item.get("data") or {}).get("broadcastParentId") if isinstance(item.get("data"), dict) else None
        if parent_id:
            previous = self.delivery_store.get(str(parent_id))
            aggregate = self.delivery_store.summarize_broadcast(str(parent_id))
            if aggregate and (
                not previous
                or previous.get("status") != aggregate.get("status")
                or previous.get("childStatuses") != aggregate.get("childStatuses")
            ):
                self._record_delivery(aggregate)
        if not callable(self.activity_recorder):
            return
        payload = {
            "deliveryId": str(item.get("deliveryId") or ""),
            "eventType": str(item.get("eventType") or "final"),
            "status": str(item.get("status") or ""),
            "outcome": str(item.get("deliveryOutcome") or ""),
            "attempts": int(item.get("attempts", 0) or 0),
            "lastError": item.get("lastError"),
            "nextRetryAt": item.get("nextRetryAt"),
            "deliveryKind": str(item.get("deliveryKind") or "single"),
            "parentDeliveryId": (item.get("data") or {}).get("broadcastParentId") if isinstance(item.get("data"), dict) else None,
            "childStatuses": item.get("childStatuses"),
        }
        try:
            self.activity_recorder(str(item.get("sessionId") or ""), payload)
        except Exception:
            # Diagnostics must never turn a successful channel delivery into
            # an application error.
            return

    def _retry_loop(self) -> None:
        while not self._stop.is_set():
            for item in self.delivery_store.pending():
                self._attempt(str(item["deliveryId"]))
            self._stop.wait(self.retry_interval)
