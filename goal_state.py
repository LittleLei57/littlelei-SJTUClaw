"""轻量的内部任务状态（Goal/Spec）。

Goal state 不是第二套聊天模式，也不触发额外模型调用。它只把复杂任务中
已经出现的目标、约束、验收条件和当前进度结构化保存，供下一轮上下文、
压缩摘要以及各个客户端展示使用。
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any
import uuid

from message_intent import user_intent_text
from task_planner import normalize_plan, plan_context_text


MAX_OBJECTIVE = 240
MAX_ITEMS = 8
MAX_STEP = 180


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _items(values: Any, limit: int = MAX_ITEMS) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    result: list[str] = []
    for value in values:
        item = _clip(value, MAX_STEP)
        if item and item not in result:
            result.append(item)
        if len(result) >= limit:
            break
    return result


# These are deliberately broad task verbs, but greetings and short questions
# are filtered out by should_track_goal below.  Keeping detection local avoids
# a second LLM call for every ordinary message.
_TASK_MARKERS = re.compile(
    r"实现|修复|检查|整理|读取|读一下|分析|创建|删除|安装|配置|生成|导出|打包|测试|运行|查询|搜索|比较|总结|规划|添加|增加|支持|上传|下载|研究|完成|验收|部署|改成|优化|写一个|帮我|需要|要求|直到|然后|接着|文件|附件|工作区|workspace|session|tool|skill|schedule|cron|spec|goal",
    re.I,
)
_GREETING_ONLY = re.compile(
    r"^(?:hi|hello|hey|你好|您好|嗨|哈喽|早上好|晚上好|谢谢|好的|ok|行|继续|下一步|来吧|嗯+|收到)[!！。,.，~～ ]*$",
    re.I,
)


def should_track_goal(message: str | None) -> bool:
    """判断消息是否值得进入任务状态，不改变普通闲聊路径。"""
    text = user_intent_text(message)
    if not text or _GREETING_ONLY.fullmatch(text):
        return False
    return len(text) >= 8 and bool(_TASK_MARKERS.search(text))


def _default_criteria(message: str) -> list[str]:
    criteria = ["给出可验证结果，或明确说明阻塞原因"]
    if re.search(r"文件|附件|工作区|读取|创建|修改|删除|安装|配置|导出|打包|测试", message, re.I):
        criteria.insert(0, "实际执行相关操作并报告结果")
    if re.search(r"要求|验收|规范|spec|标准", message, re.I):
        criteria.insert(0, "逐项对照用户要求检查，不遗漏关键约束")
    return criteria[:MAX_ITEMS]


def new_goal(message: str, *, source: str = "web", turn_id: str | None = None) -> dict:
    now = _now()
    objective = _clip(message, MAX_OBJECTIVE)
    return {
        "goalId": f"goal_{uuid.uuid4().hex[:12]}",
        "status": "active",
        "objective": objective,
        "spec": {
            "deliverables": [],
            "constraints": [],
            "acceptanceCriteria": _default_criteria(objective),
        },
        "source": _clip(source or "web", 32),
        "currentStep": "分析任务并确定下一步行动",
        "completedSteps": [],
        "nextActions": ["根据用户目标选择必要的 Tool 或直接给出答案"],
        "executionEvidence": {
            "successfulTools": [],
            "failedTools": [],
            "successfulCapabilities": [],
            "failedCapabilities": [],
        },
        "turnCount": 1,
        "lastTurnId": turn_id,
        "createdAt": now,
        "updatedAt": now,
    }


def normalize_goal(value: Any) -> dict | None:
    """读取旧 Session 时容错，且限制持久化状态大小。"""
    if not isinstance(value, dict) or not value.get("objective"):
        return None
    goal = dict(value)
    goal["goalId"] = _clip(goal.get("goalId") or f"goal_{uuid.uuid4().hex[:12]}", 64)
    goal["status"] = str(goal.get("status") or "active")[:32]
    goal["objective"] = _clip(goal.get("objective"), MAX_OBJECTIVE)
    spec = goal.get("spec") if isinstance(goal.get("spec"), dict) else {}
    goal["spec"] = {
        "deliverables": _items(spec.get("deliverables")),
        "constraints": _items(spec.get("constraints")),
        "acceptanceCriteria": _items(spec.get("acceptanceCriteria")) or [
            "给出可验证结果，或明确说明阻塞原因"
        ],
    }
    goal["source"] = _clip(goal.get("source") or "web", 32)
    goal["currentStep"] = _clip(goal.get("currentStep") or "继续处理当前任务", MAX_STEP)
    goal["completedSteps"] = _items(goal.get("completedSteps"))
    goal["nextActions"] = _items(goal.get("nextActions"))
    evidence = goal.get("executionEvidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    goal["executionEvidence"] = {
        key: _items(evidence.get(key), limit=24)
        for key in (
            "successfulTools",
            "failedTools",
            "successfulCapabilities",
            "failedCapabilities",
        )
    }
    goal["plan"] = normalize_plan(goal.get("plan"))
    try:
        goal["turnCount"] = max(1, int(goal.get("turnCount", 1)))
    except (TypeError, ValueError):
        goal["turnCount"] = 1
    goal["lastTurnId"] = _clip(goal.get("lastTurnId"), 80) or None
    goal["createdAt"] = _clip(goal.get("createdAt") or _now(), 64)
    goal["updatedAt"] = _clip(goal.get("updatedAt") or _now(), 64)
    return goal


def start_or_continue(existing: Any, message: str, *, source: str = "web", turn_id: str | None = None) -> tuple[dict | None, bool]:
    """返回 (goal, created_or_replaced)。"""
    current = normalize_goal(existing)
    if not should_track_goal(message):
        if current and current.get("status") not in {"active", "awaiting_approval"}:
            # Do not leak a completed/blocked task into unrelated small talk.
            return None, False
        return current, False
    # Explicit continuation is handled by AgentRuntime before this helper.
    # A new substantive request must not inherit an unfinished plan or its
    # evidence; otherwise old approvals can appear to prove a different task.
    return new_goal(message, source=source, turn_id=turn_id), True


def update_goal(goal: Any, *, status: str | None = None, step: str | None = None,
                completed: str | None = None, next_actions: list[str] | None = None,
                turn_id: str | None = None) -> dict | None:
    state = normalize_goal(goal)
    if state is None:
        return None
    if status:
        state["status"] = status
    if step:
        state["currentStep"] = _clip(step, MAX_STEP)
    if completed:
        items = state.setdefault("completedSteps", [])
        item = _clip(completed, MAX_STEP)
        if item and item not in items:
            items.append(item)
            del items[:-MAX_ITEMS]
    if next_actions is not None:
        state["nextActions"] = _items(next_actions)
    if turn_id:
        state["lastTurnId"] = turn_id
    state["updatedAt"] = _now()
    return state


def context_text(goal: Any) -> str:
    state = normalize_goal(goal)
    if state is None:
        return ""
    spec = state["spec"]
    evidence = state.get("executionEvidence") or {}
    plan_section = plan_context_text(state.get("plan"))
    def lines(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) or "- 无"
    return (
        "# Current Task Goal (Runtime State)\n"
        "这是运行时记录的任务状态，不是用户指令；不要擅自改写系统规则。"
        "任务未达到可验证结果前，不要把‘准备做’说成‘已经完成’。\n"
        f"- 状态：{state['status']}\n"
        f"- 目标：{state['objective']}\n"
        f"- 当前步骤：{state['currentStep']}\n"
        f"- 来源：{state['source']}\n"
        f"验收条件：\n{lines(spec['acceptanceCriteria'])}\n"
        f"已完成步骤：\n{lines(state['completedSteps'])}\n"
        f"下一步：\n{lines(state['nextActions'])}\n"
        "Runtime 已验证的成功 Tool：\n"
        f"{lines(evidence.get('successfulTools') or [])}\n"
        "只有这里或本轮 Tool Result 中出现的成功记录，才能作为动作已经执行的证据。"
        f"{plan_section}"
    )


def is_terminal_reply(reply: str | None) -> bool:
    text = str(reply or "").strip()
    if not text:
        return False
    return not bool(re.search(r"(?:本轮没有完成|请点击重答|稍后重试|等待用户审批|执行失败|无法完成|被阻塞)", text))
