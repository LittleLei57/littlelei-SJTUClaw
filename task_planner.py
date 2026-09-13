"""Deterministic structured plans for complex Agent tasks.

The planner exposes *what* the Runtime intends to do and which observable
evidence will prove each step.  It deliberately does not store chain-of-
thought or let model prose advance progress: only Runtime events may mutate a
step from pending to completed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Iterable
import uuid

from execution_evidence import CAPABILITY_LABELS, TOOL_CAPABILITY
from message_intent import user_intent_text


MAX_PLAN_STEPS = 8
MAX_STEP_EVIDENCE = 8
_VALID_STATUSES = {
    "pending", "in_progress", "awaiting_approval",
    "completed", "failed", "blocked", "cancelled",
}
_CAPABILITY_ORDER = ("read", "search", "install", "write", "execute", "schedule")
_EXPECTED_EVIDENCE = {
    "read": "至少一个读取或目录检查 Tool 返回成功结果",
    "search": "至少一个联网查询、天气或时间 Tool 返回成功结果",
    "write": "写入类 Tool 返回成功并给出目标文件或资源",
    "execute": "命令、编译或测试 Tool 返回成功结果",
    "install": "install_skill 返回成功并登记 Skill",
    "schedule": "调度 Tool 返回成功并给出任务标识与触发规则",
}
_COMPLEX_MARKERS = re.compile(
    r"(?:先.+(?:再|然后|接着|最后)|然后|接着|逐项|分别|依次|"
    r"直到|全部|完整|一并|并且|同时|步骤|阶段|验收|实现.+测试|"
    r"每天|每周|每月|每隔|定时|周期)",
    re.I | re.S,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip(value: Any, limit: int = 180) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def should_create_plan(message: str | None, capabilities: Iterable[str]) -> bool:
    """Only complex requests get a visible plan; ordinary chat stays quiet."""
    text = user_intent_text(message)
    caps = {str(item) for item in capabilities if item}
    return (
        len(caps) >= 2
        or (bool(caps) and bool(_COMPLEX_MARKERS.search(text)))
        or (len(text) >= 70 and bool(caps))
    )


def _step(
    title: str,
    phase: str,
    *,
    status: str = "pending",
    capability: str | None = None,
    description: str = "",
    expected: list[str] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "stepId": f"step_{uuid.uuid4().hex[:10]}",
        "phase": phase,
        "title": _clip(title, 120),
        "description": _clip(description),
        "status": status,
        "capability": capability,
        "expectedEvidence": [_clip(item) for item in (expected or []) if _clip(item)][:4],
        "evidence": list(evidence or [])[:MAX_STEP_EVIDENCE],
        "updatedAt": _now(),
    }


def create_plan(message: str, capabilities: Iterable[str]) -> dict[str, Any]:
    caps = {str(item) for item in capabilities if item}
    steps = [
        _step(
            "理解目标与约束",
            "plan",
            status="completed",
            description="从用户请求中确定交付目标、顺序和边界。",
            expected=["Runtime 已建立结构化任务规格"],
            evidence=[{
                "type": "runtime",
                "label": "已建立任务目标与验收条件",
                "success": True,
                "timestamp": _now(),
            }],
        )
    ]
    for capability in _CAPABILITY_ORDER:
        if capability not in caps:
            continue
        label = CAPABILITY_LABELS.get(capability, capability)
        steps.append(_step(
            label,
            "execute",
            capability=capability,
            description=f"通过受控 Tool 完成“{label}”，模型口头描述不计为完成。",
            expected=[_EXPECTED_EVIDENCE.get(capability, "对应 Tool 返回成功结果")],
        ))
    if len(steps) == 1:
        steps.append(_step(
            "形成任务交付结果",
            "execute",
            description="依据目标与约束生成可以直接使用的答复或产物。",
            expected=["产生非空最终回答，并覆盖任务验收条件"],
        ))
    steps.append(_step(
        "核对证据并交付",
        "verify",
        description="检查执行步骤、失败记录和最终回答是否满足验收条件。",
        expected=["所有必要执行步骤均有成功证据", "最终回答不虚报未执行动作"],
    ))
    now = _now()
    return {
        "planId": f"plan_{uuid.uuid4().hex[:12]}",
        "version": 1,
        "status": "active",
        "summary": _clip(message, 220),
        "steps": steps[:MAX_PLAN_STEPS],
        "createdAt": now,
        "updatedAt": now,
    }


def _normalize_evidence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        normalized = {
            "type": _clip(item.get("type") or "tool_result", 32),
            "label": _clip(item.get("label"), 180),
            "tool": _clip(item.get("tool"), 80) or None,
            "callId": _clip(item.get("callId"), 100) or None,
            "success": bool(item.get("success")),
            "error": _clip(item.get("error"), 180) or None,
            "timestamp": _clip(item.get("timestamp") or _now(), 64),
        }
        key = (
            normalized["type"], normalized["tool"],
            normalized["callId"], normalized["success"], normalized["label"],
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
        if len(result) >= MAX_STEP_EVIDENCE:
            break
    return result


def normalize_plan(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("steps"), list):
        return None
    steps = []
    for raw in value["steps"][:MAX_PLAN_STEPS]:
        if not isinstance(raw, dict) or not raw.get("title"):
            continue
        status = str(raw.get("status") or "pending")
        if status not in _VALID_STATUSES:
            status = "pending"
        phase = str(raw.get("phase") or "execute")
        if phase not in {"plan", "execute", "verify"}:
            phase = "execute"
        expected = raw.get("expectedEvidence")
        if not isinstance(expected, list):
            expected = []
        steps.append({
            "stepId": _clip(raw.get("stepId") or f"step_{uuid.uuid4().hex[:10]}", 64),
            "phase": phase,
            "title": _clip(raw.get("title"), 120),
            "description": _clip(raw.get("description")),
            "status": status,
            "capability": _clip(raw.get("capability"), 32) or None,
            "expectedEvidence": [_clip(item) for item in expected if _clip(item)][:4],
            "evidence": _normalize_evidence(raw.get("evidence")),
            "updatedAt": _clip(raw.get("updatedAt") or _now(), 64),
        })
    if not steps:
        return None
    return {
        "planId": _clip(value.get("planId") or f"plan_{uuid.uuid4().hex[:12]}", 64),
        "version": max(1, int(value.get("version") or 1)),
        "status": (
            str(value.get("status"))
            if str(value.get("status")) in {
                "active", "awaiting_approval", "completed", "blocked", "cancelled"
            }
            else "active"
        ),
        "summary": _clip(value.get("summary"), 220),
        "steps": steps,
        "createdAt": _clip(value.get("createdAt") or _now(), 64),
        "updatedAt": _clip(value.get("updatedAt") or _now(), 64),
    }


def ensure_plan(goal: dict[str, Any], message: str,
                capabilities: Iterable[str]) -> tuple[dict[str, Any], bool]:
    state = dict(goal)
    existing = normalize_plan(state.get("plan"))
    if existing is not None:
        state["plan"] = existing
        return state, False
    if not should_create_plan(message, capabilities):
        return state, False
    state["plan"] = create_plan(message, capabilities)
    first = next(
        (item for item in state["plan"]["steps"] if item["status"] == "pending"),
        None,
    )
    if first:
        state["currentStep"] = first["title"]
        state["nextActions"] = [first["title"]]
    return state, True


def _find_capability_step(plan: dict[str, Any], capability: str) -> dict[str, Any] | None:
    return next(
        (
            item for item in plan["steps"]
            if item.get("phase") == "execute"
            and item.get("capability") == capability
        ),
        None,
    )


def mark_tool(
    goal: dict[str, Any],
    tool: str,
    *,
    state: str,
    call_id: str | None = None,
    error: str | None = None,
    timestamp: str | None = None,
) -> tuple[dict[str, Any], bool]:
    result = dict(goal)
    plan = normalize_plan(result.get("plan"))
    capability = TOOL_CAPABILITY.get(str(tool))
    if plan is None or not capability:
        return result, False
    step = _find_capability_step(plan, capability)
    if step is None:
        verify_index = next(
            (index for index, item in enumerate(plan["steps"]) if item["phase"] == "verify"),
            len(plan["steps"]),
        )
        step = _step(
            f"补充：{CAPABILITY_LABELS.get(capability, capability)}",
            "execute",
            capability=capability,
            description="模型根据当前结果增加的必要 Tool 步骤。",
            expected=[_EXPECTED_EVIDENCE.get(capability, "对应 Tool 返回成功结果")],
        )
        plan["steps"].insert(verify_index, step)
        plan["steps"] = plan["steps"][:MAX_PLAN_STEPS]
    changed = False
    now = timestamp or _now()
    if state == "call" and step["status"] not in {"completed", "in_progress"}:
        step["status"] = "in_progress"
        changed = True
    elif state == "approval" and step["status"] != "completed":
        step["status"] = "awaiting_approval"
        plan["status"] = "awaiting_approval"
        changed = True
    elif state in {"success", "failed"}:
        success = state == "success"
        evidence = {
            "type": "tool_result",
            "label": f"{tool} {'调用成功' if success else '调用失败'}",
            "tool": tool,
            "callId": call_id,
            "success": success,
            "error": _clip(error, 180) or None,
            "timestamp": now,
        }
        before = len(step["evidence"])
        step["evidence"] = _normalize_evidence([*step["evidence"], evidence])
        changed = changed or len(step["evidence"]) != before
        if success:
            if step["status"] != "completed":
                step["status"] = "completed"
                changed = True
            if plan["status"] == "awaiting_approval":
                plan["status"] = "active"
        elif not any(item.get("success") for item in step["evidence"]):
            if step["status"] != "failed":
                step["status"] = "failed"
                changed = True
    if changed:
        step["updatedAt"] = now
        plan["updatedAt"] = now
        result["plan"] = plan
        result["currentStep"] = step["title"]
        result["nextActions"] = (
            [step["title"]]
            if step["status"] not in {"completed"}
            else [
                item["title"] for item in plan["steps"]
                if item["status"] in {"pending", "in_progress", "failed"}
            ][:3]
        )
    return result, changed


def sync_tool_events(
    goal: dict[str, Any], events: Iterable[dict[str, Any]]
) -> tuple[dict[str, Any], bool]:
    state = dict(goal)
    changed = False
    for event in events or []:
        if not isinstance(event, dict) or not isinstance(event.get("result"), dict):
            continue
        result = event["result"]
        state, item_changed = mark_tool(
            state,
            str(event.get("tool") or result.get("tool") or ""),
            state="success" if result.get("success") is True else "failed",
            call_id=str(event.get("callId") or "") or None,
            error=result.get("error"),
            timestamp=event.get("timestamp"),
        )
        changed = changed or item_changed
    return state, changed


def finalize_plan(
    goal: dict[str, Any],
    *,
    success: bool,
    missing_capabilities: Iterable[str] = (),
    final_label: str = "最终回答已生成并通过 Runtime 校验",
) -> dict[str, Any]:
    state = dict(goal)
    plan = normalize_plan(state.get("plan"))
    if plan is None:
        return state
    now = _now()
    missing = {str(item) for item in missing_capabilities}
    generic = next(
        (
            item for item in plan["steps"]
            if item["phase"] == "execute" and not item.get("capability")
        ),
        None,
    )
    if generic is not None and success:
        generic["status"] = "completed"
        generic["evidence"] = _normalize_evidence([
            *generic["evidence"],
            {
                "type": "final",
                "label": "已生成非空最终回答",
                "success": True,
                "timestamp": now,
            },
        ])
        generic["updatedAt"] = now

    # A capability-backed execute step can only be complete after Runtime has
    # recorded successful tool evidence.  A fluent final answer is not proof.
    unverified = {
        str(item.get("capability"))
        for item in plan["steps"]
        if item.get("phase") == "execute"
        and item.get("capability")
        and item.get("status") != "completed"
    }
    missing.update(unverified)
    success = bool(success and not missing)

    for item in plan["steps"]:
        if item.get("capability") in missing and item["status"] != "completed":
            item["status"] = "blocked"
            item["updatedAt"] = now
    verify = next((item for item in plan["steps"] if item["phase"] == "verify"), None)
    if verify is not None:
        verify["status"] = "completed" if success else "blocked"
        verify["evidence"] = _normalize_evidence([
            *verify["evidence"],
            {
                "type": "verification",
                "label": final_label if success else "仍有必要步骤缺少成功证据",
                "success": success,
                "timestamp": now,
            },
        ])
        verify["updatedAt"] = now
    plan["status"] = "completed" if success else "blocked"
    plan["updatedAt"] = now
    state["plan"] = plan
    return state


def plan_context_text(plan_value: Any) -> str:
    plan = normalize_plan(plan_value)
    if plan is None:
        return ""
    status_labels = {
        "pending": "待执行",
        "in_progress": "执行中",
        "awaiting_approval": "等待审批",
        "completed": "已验证",
        "failed": "执行失败",
        "blocked": "被阻塞",
        "cancelled": "cancelled",
    }
    lines = []
    for index, item in enumerate(plan["steps"], 1):
        expected = "；".join(item["expectedEvidence"]) or "无额外证据要求"
        lines.append(
            f"{index}. [{status_labels[item['status']]}] {item['title']} "
            f"(预期证据：{expected})"
        )
    return (
        "\n结构化执行计划（由 Runtime 维护）：\n"
        + "\n".join(lines)
        + "\n只能依据 Runtime Tool Result 推进执行步骤；"
        "不要自行宣称、跳过或改写已验证状态。"
    )
