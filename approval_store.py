"""Durable and idempotent approval state for high-risk tools.

An approval is bound to the exact Session, Tool arguments, logical Turn and
Tool call. The durable executing claim is written before a side effect starts,
so duplicate clicks, refresh retries or workers cannot execute it twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid

from session_store import utc_now
from state_database import StateDatabase


@dataclass
class Approval:
    """One pending, executing, or resolved Tool authorization."""

    approval_id: str
    batch_id: str
    session_id: str
    tool: str
    args: dict
    status: str = "pending"
    reason: str | None = None
    result: dict | None = None
    created_at: str = ""
    decided_at: str | None = None
    retry_of: str | None = None
    attempt: int = 1
    turn_id: str | None = None
    call_id: str | None = None
    claimed_at: str | None = None
    executor_token: str | None = None
    decision: str | None = None
    execution_status: str = "not_started"

    def to_dict(self) -> dict:
        return {
            "approvalId": self.approval_id,
            "batchId": self.batch_id,
            "sessionId": self.session_id,
            "tool": self.tool,
            "args": self.args,
            "status": self.status,
            "reason": self.reason,
            "result": self.result,
            "createdAt": self.created_at,
            "decidedAt": self.decided_at,
            "retryOf": self.retry_of,
            "attempt": self.attempt,
            "turnId": self.turn_id,
            "callId": self.call_id,
            "claimedAt": self.claimed_at,
            "executorToken": self.executor_token,
            "decision": self.decision,
            "executionStatus": self.execution_status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Approval":
        status = data.get("status", "pending")
        result = data.get("result")
        decision = data.get("decision")
        if decision is None:
            if status in {"approved", "failed", "interrupted", "executing"}:
                decision = "approved"
            elif status in {"rejected", "cancelled"}:
                decision = status
        execution_status = data.get("executionStatus")
        if not execution_status:
            if status == "executing":
                execution_status = "running"
            elif status == "interrupted":
                execution_status = "unknown"
            elif status == "approved":
                execution_status = "succeeded" if not isinstance(result, dict) or result.get("success") is not False else "failed"
            elif status == "failed":
                execution_status = "failed"
            elif status == "cancelled":
                execution_status = "cancelled"
            else:
                execution_status = "not_started"
        return cls(
            approval_id=data["approvalId"],
            batch_id=data["batchId"],
            session_id=data["sessionId"],
            tool=data["tool"],
            args=data["args"],
            status=status,
            reason=data.get("reason"),
            result=data.get("result"),
            created_at=data.get("createdAt", ""),
            decided_at=data.get("decidedAt"),
            retry_of=data.get("retryOf"),
            attempt=max(1, int(data.get("attempt", 1) or 1)),
            turn_id=data.get("turnId"),
            call_id=data.get("callId"),
            claimed_at=data.get("claimedAt"),
            executor_token=data.get("executorToken"),
            decision=decision,
            execution_status=execution_status,
        )


class ApprovalStore:
    """SQLite-backed approval store with atomic claim/resolve transitions."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "approvals.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.database = StateDatabase(self.path.parent)
        self.database.ensure_imported("approvals", self.path, [])

    def _read(self) -> list[dict]:
        data = self.database.read("approvals", [])
        if not isinstance(data, list):
            raise ValueError("Approval \u6570\u636e\u683c\u5f0f\u9519\u8bef")
        return data

    def create(
        self,
        batch_id: str,
        session_id: str,
        tool: str,
        args: dict,
        *,
        retry_of: str | None = None,
        attempt: int = 1,
        turn_id: str | None = None,
        call_id: str | None = None,
    ) -> Approval:
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("Approval attempt \u5fc5\u987b\u662f\u6b63\u6574\u6570")
        approval = Approval(
            approval_id=f"approval_{uuid.uuid4().hex[:12]}",
            batch_id=batch_id,
            session_id=session_id,
            tool=tool,
            args=args,
            created_at=utc_now(),
            retry_of=retry_of,
            attempt=attempt,
            turn_id=turn_id,
            call_id=call_id,
        )

        def append(data):
            if not isinstance(data, list):
                raise ValueError("Approval \u6570\u636e\u683c\u5f0f\u9519\u8bef")
            rows = list(data)
            rows.append(approval.to_dict())
            return rows, approval

        return self.database.mutate("approvals", [], append)

    def list(
        self, session_id: str | None = None, status: str | None = None
    ) -> list[Approval]:
        items = [Approval.from_dict(row) for row in self._read()]
        if session_id:
            items = [item for item in items if item.session_id == session_id]
        if status:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def get(self, approval_id: str) -> Approval:
        item = next(
            (row for row in self.list() if row.approval_id == approval_id), None
        )
        if item is None:
            raise KeyError(f"Approval \u4e0d\u5b58\u5728\uff1a{approval_id}")
        return item

    def claim(
        self, approval_id: str, executor_token: str
    ) -> tuple[Approval, bool]:
        """Atomically claim a pending approval before executing its Tool."""
        if not executor_token:
            raise ValueError("executor_token \u4e0d\u80fd\u4e3a\u7a7a")

        def claim_row(data):
            if not isinstance(data, list):
                raise ValueError("Approval \u6570\u636e\u683c\u5f0f\u9519\u8bef")
            rows = list(data)
            for index, original in enumerate(rows):
                if original.get("approvalId") != approval_id:
                    continue
                row = dict(original)
                if row.get("status", "pending") == "pending":
                    row["status"] = "executing"
                    row["decision"] = "approved"
                    row["executionStatus"] = "running"
                    row["claimedAt"] = utc_now()
                    row["executorToken"] = executor_token
                    rows[index] = row
                    return rows, (Approval.from_dict(row), True)
                return rows, (Approval.from_dict(row), False)
            raise KeyError(f"Approval \u4e0d\u5b58\u5728\uff1a{approval_id}")

        return self.database.mutate("approvals", [], claim_row)

    def resolve(
        self,
        approval_id: str,
        approved: bool,
        reason: str | None,
        result: dict,
    ) -> Approval:
        """Durably complete an approval; repeated completions are no-ops."""

        def resolve_row(data):
            if not isinstance(data, list):
                raise ValueError("Approval \u6570\u636e\u683c\u5f0f\u9519\u8bef")
            rows = list(data)
            for index, original in enumerate(rows):
                if original.get("approvalId") != approval_id:
                    continue
                row = dict(original)
                status = row.get("status", "pending")
                if status in {"approved", "rejected", "failed", "cancelled", "interrupted"}:
                    return rows, Approval.from_dict(row)
                row["status"] = "approved" if approved else "rejected"
                row["decision"] = "approved" if approved else "rejected"
                row["executionStatus"] = (
                    "succeeded" if approved and result.get("success") is not False
                    else "failed" if approved else "not_started"
                )
                row["reason"] = reason
                row["result"] = result
                row["decidedAt"] = utc_now()
                rows[index] = row
                return rows, Approval.from_dict(row)
            raise KeyError(f"Approval \u4e0d\u5b58\u5728\uff1a{approval_id}")

        return self.database.mutate("approvals", [], resolve_row)

    def interrupt_claim(
        self, approval_id: str, executor_token: str, reason: str
    ) -> Approval:
        """Close an executing claim whose side-effect outcome is unknown."""
        def interrupt_row(data):
            if not isinstance(data, list):
                raise ValueError("Approval \u6570\u636e\u683c\u5f0f\u9519\u8bef")
            rows = list(data)
            for index, original in enumerate(rows):
                if original.get("approvalId") != approval_id:
                    continue
                row = dict(original)
                if (row.get("status") == "executing"
                        and row.get("executorToken") == executor_token):
                    row["status"] = "interrupted"
                    row["decision"] = "approved"
                    row["executionStatus"] = "unknown"
                    row["reason"] = reason
                    row["result"] = {
                        "success": False, "error": reason,
                        "errorCode": "execution_outcome_unknown", "retryable": False,
                    }
                    row["decidedAt"] = utc_now()
                    rows[index] = row
                return rows, Approval.from_dict(row)
            raise KeyError(f"Approval \u4e0d\u5b58\u5728\uff1a{approval_id}")
        return self.database.mutate("approvals", [], interrupt_row)

    def recover_executing(self) -> int:
        """Mark claims left by a crashed Gateway as outcome-unknown."""
        def recover(data):
            if not isinstance(data, list):
                raise ValueError("Approval \u6570\u636e\u683c\u5f0f\u9519\u8bef")
            rows = list(data)
            changed = 0
            for index, original in enumerate(rows):
                if original.get("status") != "executing":
                    continue
                row = dict(original)
                message = "Gateway \u91cd\u542f\u65f6\u8be5\u64cd\u4f5c\u4ecd\u5728\u6267\u884c\uff1b\u7ed3\u679c\u672a\u77e5\uff0c\u4e0d\u4f1a\u81ea\u52a8\u91cd\u8bd5\u3002"
                row.update({
                    "status": "interrupted", "reason": message,
                    "decision": "approved", "executionStatus": "unknown",
                    "result": {"success": False, "error": message,
                               "errorCode": "execution_outcome_unknown",
                               "retryable": False},
                    "decidedAt": utc_now(),
                })
                rows[index] = row
                changed += 1
            return rows, changed
        return self.database.mutate("approvals", [], recover)

    def cancel_pending_for_turn(self, turn_id: str, reason: str) -> int:
        """Reject approvals that have not crossed the execution boundary."""
        def cancel(data):
            if not isinstance(data, list):
                raise ValueError("Approval \u6570\u636e\u683c\u5f0f\u9519\u8bef")
            rows = list(data)
            changed = 0
            for index, original in enumerate(rows):
                if original.get("turnId") != turn_id or original.get("status") != "pending":
                    continue
                row = dict(original)
                row.update({
                    "status": "cancelled", "reason": reason,
                    "decision": "cancelled", "executionStatus": "cancelled",
                    "result": {"success": False, "error": reason,
                               "errorCode": "cancelled"},
                    "decidedAt": utc_now(),
                })
                rows[index] = row
                changed += 1
            return rows, changed
        return self.database.mutate("approvals", [], cancel)

    def pending_batch(self, batch_id: str) -> list[Approval]:
        return [
            item
            for item in self.list()
            if item.batch_id == batch_id
            and item.status in {"pending", "executing"}
        ]
