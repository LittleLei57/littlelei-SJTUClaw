"""Durable, idempotent outbound delivery records.

Scheduler/Runtime only produce an ``OutboundEvent``.  Channel adapters are
unreliable edges, so this small store keeps the event identity and retry state
separate from Agent execution.  The storage is deliberately JSON-document
compatible through ``StateDatabase`` to preserve the project's migration
model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from session_store import utc_now
from state_database import StateDatabase


class DeliveryStore:
    MAX_ATTEMPTS = 5
    # A process restart can interrupt the small window between claiming a
    # delivery and receiving the adapter's acknowledgement. Retrying that
    # window automatically could duplicate an already accepted message, so
    # restart recovery marks it as ``unknown`` instead of guessing.
    IN_FLIGHT_STATUS = "delivering"
    UNKNOWN_STATUS = "unknown"

    def __init__(self, data_dir: str | Path | None = None):
        self._lock = RLock()
        self._memory: dict[str, dict] = {}
        self.database = StateDatabase(data_dir) if data_dir is not None else None

    def _read(self) -> dict[str, dict]:
        if self.database is None:
            return self._memory
        value = self.database.read("outbound_deliveries", {})
        if not isinstance(value, dict):
            raise ValueError("outbound_deliveries 必须是 object")
        return value

    def _write(self, value: dict[str, dict]) -> None:
        if self.database is None:
            self._memory = value
        else:
            self.database.write("outbound_deliveries", value)

    def get(self, delivery_id: str) -> dict | None:
        with self._lock:
            item = self._read().get(delivery_id)
            return dict(item) if isinstance(item, dict) else None

    def ensure(self, delivery_id: str, payload: dict[str, Any]) -> dict:
        with self._lock:
            records = self._read()
            existing = records.get(delivery_id)
            if isinstance(existing, dict):
                return dict(existing)
            now = utc_now()
            item = {
                "deliveryId": delivery_id,
                **payload,
                "status": "pending",
                "attempts": 0,
                "nextRetryAt": now,
                "lastError": None,
                "deliveryOutcome": "pending",
                "deliveringAt": None,
                "createdAt": now,
                "updatedAt": now,
            }
            records[delivery_id] = item
            self._write(records)
            return dict(item)

    def claim_attempt(self, delivery_id: str) -> dict | None:
        """Claim one attempt; return ``None`` for delivered/in-flight records."""
        with self._lock:
            records = self._read()
            item = records.get(delivery_id)
            if not isinstance(item, dict) or item.get("status") == "delivered":
                return None
            if item.get("status") in {self.IN_FLIGHT_STATUS, self.UNKNOWN_STATUS}:
                return None
            now = datetime.now(timezone.utc)
            retry_at = item.get("nextRetryAt")
            if retry_at:
                try:
                    if datetime.fromisoformat(str(retry_at).replace("Z", "+00:00")) > now:
                        return None
                except ValueError:
                    pass
            if int(item.get("attempts", 0)) >= self.MAX_ATTEMPTS:
                item["status"] = "failed"
                item["updatedAt"] = utc_now()
                self._write(records)
                return None
            item["status"] = "delivering"
            item["attempts"] = int(item.get("attempts", 0)) + 1
            item["deliveryOutcome"] = "in_flight"
            item["deliveringAt"] = utc_now()
            item["updatedAt"] = utc_now()
            self._write(records)
            return dict(item)

    def delivered(self, delivery_id: str) -> None:
        self._update(
            delivery_id,
            status="delivered",
            deliveryOutcome="delivered",
            lastError=None,
            nextRetryAt=None,
            deliveringAt=None,
        )

    def failed(self, delivery_id: str, error: str) -> dict | None:
        with self._lock:
            records = self._read()
            item = records.get(delivery_id)
            if not isinstance(item, dict):
                return None
            attempts = int(item.get("attempts", 0))
            terminal = attempts >= self.MAX_ATTEMPTS
            item["status"] = "failed" if terminal else "pending"
            item["deliveryOutcome"] = "failed" if terminal else "retrying"
            item["lastError"] = str(error)[:1000]
            item["deliveringAt"] = None
            if terminal:
                item["nextRetryAt"] = None
            else:
                delay = min(60, 2 ** max(0, attempts - 1))
                item["nextRetryAt"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).isoformat(timespec="seconds")
            item["updatedAt"] = utc_now()
            self._write(records)
            return dict(item)

    def defer(self, delivery_id: str, reason: str = "目标渠道当前不可用") -> dict | None:
        """Keep an unroutable event pending without consuming retry attempts."""
        self._update(
            delivery_id,
            status="pending",
            deliveryOutcome="deferred",
            lastError=str(reason)[:1000],
            deliveringAt=None,
            nextRetryAt=(datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(timespec="seconds"),
        )
        return self.get(delivery_id)

    def recover_inflight(self, reason: str = "Gateway 在主动投递确认前重启，结果未知；为避免重复投递，未自动重试") -> list[dict]:
        """Mark pre-restart in-flight deliveries as uncertain.

        An adapter may have accepted a message immediately before the
        process stopped. Replaying it on startup would trade a rare missed
        notification for a guaranteed duplicate in that crash window. The
        result is therefore durable and visible to diagnostics, while an
        operator can explicitly re-run the scheduler task if needed.
        """
        with self._lock:
            records = self._read()
            recovered: list[dict] = []
            for item in records.values():
                if not isinstance(item, dict) or item.get("status") != self.IN_FLIGHT_STATUS:
                    continue
                item["status"] = self.UNKNOWN_STATUS
                item["deliveryOutcome"] = "unknown"
                item["lastError"] = reason[:1000]
                item["nextRetryAt"] = None
                item["deliveringAt"] = None
                item["updatedAt"] = utc_now()
                recovered.append(dict(item))
            if recovered:
                self._write(records)
            return recovered

    def pending(self) -> list[dict]:
        with self._lock:
            records = self._read()
            now = datetime.now(timezone.utc)
            result = []
            for item in records.values():
                if not isinstance(item, dict) or item.get("status") != "pending":
                    continue
                if item.get("deliveryKind") == "broadcast":
                    # Broadcast parents summarize their child records; only
                    # child deliveries are independently retried.
                    continue
                retry_at = item.get("nextRetryAt")
                if retry_at:
                    try:
                        if datetime.fromisoformat(str(retry_at).replace("Z", "+00:00")) > now:
                            continue
                    except ValueError:
                        pass
                result.append(dict(item))
            return result

    def summarize_broadcast(self, delivery_id: str) -> dict | None:
        """Refresh a broadcast parent from its independently tracked children."""
        with self._lock:
            records = self._read()
            parent = records.get(delivery_id)
            if not isinstance(parent, dict) or parent.get("deliveryKind") != "broadcast":
                return None
            child_ids = [str(item) for item in (parent.get("childDeliveryIds") or [])]
            children = [records.get(item) for item in child_ids]
            children = [item for item in children if isinstance(item, dict)]
            statuses = {str(item.get("deliveryId")): str(item.get("status") or "pending") for item in children}
            delivered_count = sum(status == "delivered" for status in statuses.values())
            if children and delivered_count == len(statuses):
                status, outcome = "delivered", "delivered"
            elif delivered_count:
                status, outcome = "partial", "partial"
            elif any(status == self.UNKNOWN_STATUS for status in statuses.values()):
                status, outcome = self.UNKNOWN_STATUS, "unknown"
            elif any(status in {"pending", self.IN_FLIGHT_STATUS} for status in statuses.values()):
                status, outcome = "pending", "retrying"
            elif any(status == "failed" for status in statuses.values()):
                status, outcome = "failed", "failed"
            else:
                status, outcome = "pending", "pending"
            parent["status"] = status
            parent["deliveryOutcome"] = outcome
            parent["childStatuses"] = statuses
            parent["attempts"] = sum(int(item.get("attempts", 0) or 0) for item in children)
            parent["lastError"] = next(
                (str(item.get("lastError")) for item in children if item.get("lastError")), None
            )
            parent["nextRetryAt"] = next(
                (item.get("nextRetryAt") for item in children if item.get("nextRetryAt")), None
            )
            parent["updatedAt"] = utc_now()
            records[delivery_id] = parent
            self._write(records)
            return dict(parent)

    def _update(self, delivery_id: str, **changes: Any) -> None:
        with self._lock:
            records = self._read()
            item = records.get(delivery_id)
            if not isinstance(item, dict):
                return
            item.update(changes)
            item["updatedAt"] = utc_now()
            self._write(records)
