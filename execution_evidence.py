"""Deterministic execution evidence for one Agent turn.

The language model may describe an action, but only Runtime Tool results can
prove that the action happened.  This module deliberately stays independent
from the prompt and UI so completion checks do not rely on model wording alone.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable


CAPABILITY_TOOLS: dict[str, frozenset[str]] = {
    "read": frozenset({
        "list_dir", "read_file", "read_document", "read_attachment",
        "ocr_image", "github_read", "schedule_get", "schedule_list",
        "read_skill_resource",
    }),
    "search": frozenset({
        "web_search", "tavily_search", "weather_forecast", "current_time",
    }),
    "write": frozenset({
        "create_file", "overwrite_file", "edit_file", "copy_file",
        "copy_attachment_to_workspace", "create_download",
    }),
    "execute": frozenset({"new_shell", "run_command", "run_skill_script"}),
    "install": frozenset({"install_skill"}),
    "schedule": frozenset({
        "schedule_create", "schedule_pause", "schedule_resume",
        "schedule_cancel", "schedule_run",
    }),
}

TOOL_CAPABILITY = {
    tool: capability
    for capability, tools in CAPABILITY_TOOLS.items()
    for tool in tools
}

CAPABILITY_LABELS = {
    "read": "读取或检查资料",
    "search": "查询外部信息",
    "write": "创建或修改文件",
    "execute": "执行、编译或测试",
    "install": "安装 Skill",
    "schedule": "修改定时任务",
}


_CONTINUATION_RE = re.compile(
    r"^\s*(?:好(?:的|吧)?|ok|okay|继续(?:吧|做|执行)?|接着(?:吧|做)?|"
    r"来吧|开始吧|做吧|写吧|改吧|试试吧|再试一次|再调用一次工具|"
    r"就这样|可以|行)[!！。,.，~～\s]*$",
    re.I,
)

_BLOCKED_RE = re.compile(
    r"(?:无法|不能|未能|失败|不存在|找不到|缺少|被拒绝|已取消|"
    r"需要你(?:提供|补充|选择|确认)|请(?:提供|补充|选择)|"
    r"信息不足|参数不足|权限不足|暂时不可用|没有可靠地)",
    re.I,
)

_SUCCESS_PATTERNS = {
    "read": re.compile(
        r"(?:已经|已|都|全部).{0,10}(?:读完|读取完|看完|看清|检查完|"
        r"摸清|确认完|分析完)|(?:内容|数据|源码|文件).{0,8}(?:清楚了|就位)",
        re.I,
    ),
    "search": re.compile(
        r"(?:已经|已).{0,8}(?:搜到|搜索完|查询完|查到)|"
        r"(?:最新|搜索|查询).{0,8}(?:结果|消息).{0,5}(?:如下|是)",
        re.I,
    ),
    "write": re.compile(
        r"(?:已经|已|全部).{0,10}(?:创建|写入|写好|生成|修改|覆盖|复制|"
        r"保存|替换|删除|删掉|清理|移除|改掉)"
        r"|(?:文件|报告|代码|页面|内容).{0,10}(?:创建成功|写好了|生成完成|"
        r"修改完成|替换完|清理完|删除完|没有残留)",
        re.I,
    ),
    "execute": re.compile(
        r"(?:已经|已|全部).{0,8}(?:执行|运行|编译|测试).{0,6}(?:完成|成功|通过|好了)"
        # Bare phrases such as “测试通过都给分” commonly occur in tutorials
        # and comparison tables.  Treat the short form as a completion claim
        # only when it ends like an actual result statement.
        r"|(?:编译|运行|测试).{0,6}(?:成功|通过|完成)"
        r"(?=\s*(?:了|啦|✅|[。！!；;\n]|$))",
        re.I,
    ),
    "install": re.compile(
        r"(?:已经|已).{0,8}(?:安装|装好).{0,6}(?:完成|成功|好了)?",
        re.I,
    ),
    "schedule": re.compile(
        r"(?:已经|已).{0,8}(?:创建|修改|暂停|恢复|取消|执行).{0,8}"
        r"(?:定时任务|任务|提醒)",
        re.I,
    ),
}

# A final answer can make stronger claims than the original user request.  For
# example, after an edit it may additionally say "I reread the whole file and
# confirmed there are no leftovers".  Those extra claims need their own Tool
# evidence too; otherwise fluent narration can masquerade as execution.
_VERIFICATION_READ_RE = re.compile(
    r"(?:全文|整体|重新|再).{0,10}(?:读|读取|检查|检索|搜索|匹配|确认)"
    r"|(?:确认|检查|匹配).{0,18}(?:没有|不存在|再也没有|无).{0,18}"
    r"(?:残留|匹配项|问题|错误|标记)"
    r"|(?:没有|不存在|再也没有|无).{0,18}(?:残留|匹配项).{0,8}"
    r"(?:了|啦|✅|$)",
    re.I,
)

_PROMISE_RE = re.compile(
    r"(?:我|现在|接下来|马上|这就|然后|再).{0,10}"
    r"(?:去|来|会|要|准备|开始|继续|直接).{0,8}"
    r"(?:读取|查看|检查|搜索|查询|创建|写入|修改|执行|运行|编译|测试|"
    r"安装|调用|发起)|"
    r"(?:先|再|然后).{0,8}(?:看看|读|查|搜|写|创建|运行|编译|测试)",
    re.I,
)


@dataclass(frozen=True)
class TurnEvidence:
    successful_tools: frozenset[str]
    failed_tools: frozenset[str]
    successful_capabilities: frozenset[str]
    failed_capabilities: frozenset[str]
    successful_tool_sequence: tuple[str, ...] = ()

    @classmethod
    def from_events(cls, events: Iterable[dict[str, Any]] | None) -> "TurnEvidence":
        successful_tools: set[str] = set()
        failed_tools: set[str] = set()
        successful_capabilities: set[str] = set()
        failed_capabilities: set[str] = set()
        successful_tool_sequence: list[str] = []
        for event in events or []:
            if not isinstance(event, dict):
                continue
            tool = str(event.get("tool") or "")
            result = event.get("result")
            if not tool or not isinstance(result, dict):
                continue
            capability = TOOL_CAPABILITY.get(tool)
            if result.get("success") is True:
                successful_tools.add(tool)
                successful_tool_sequence.append(tool)
                if capability:
                    successful_capabilities.add(capability)
            elif result.get("success") is False:
                failed_tools.add(tool)
                if capability:
                    failed_capabilities.add(capability)
        return cls(
            frozenset(successful_tools),
            frozenset(failed_tools),
            frozenset(successful_capabilities),
            frozenset(failed_capabilities),
            tuple(successful_tool_sequence),
        )

    @classmethod
    def from_goal(cls, goal: dict[str, Any] | None) -> "TurnEvidence":
        value = goal.get("executionEvidence") if isinstance(goal, dict) else None
        value = value if isinstance(value, dict) else {}
        return cls(
            frozenset(str(item) for item in value.get("successfulTools") or []),
            frozenset(str(item) for item in value.get("failedTools") or []),
            frozenset(str(item) for item in value.get("successfulCapabilities") or []),
            frozenset(str(item) for item in value.get("failedCapabilities") or []),
            (),
        )

    def merge(self, other: "TurnEvidence") -> "TurnEvidence":
        return TurnEvidence(
            self.successful_tools | other.successful_tools,
            self.failed_tools | other.failed_tools,
            self.successful_capabilities | other.successful_capabilities,
            self.failed_capabilities | other.failed_capabilities,
            self.successful_tool_sequence + other.successful_tool_sequence,
        )

    def to_goal_dict(self) -> dict[str, list[str]]:
        return {
            "successfulTools": sorted(self.successful_tools),
            "failedTools": sorted(self.failed_tools),
            "successfulCapabilities": sorted(self.successful_capabilities),
            "failedCapabilities": sorted(self.failed_capabilities),
        }

    def satisfies(self, capability: str) -> bool:
        return capability in self.successful_capabilities

    def has_capability_after(self, later: str, earlier: str) -> bool:
        """Whether a successful capability occurred after another one."""
        earlier_seen = False
        for tool in self.successful_tool_sequence:
            capability = TOOL_CAPABILITY.get(tool)
            if capability == earlier:
                earlier_seen = True
            elif capability == later and earlier_seen:
                return True
        return False


@dataclass(frozen=True)
class CompletionCheck:
    valid: bool
    missing: tuple[str, ...] = ()
    reason: str | None = None
    approval_claim: bool = False


def is_continuation_message(message: str | None) -> bool:
    return bool(_CONTINUATION_RE.fullmatch(str(message or "").strip()))


_INFORMATIONAL_REQUEST_RE = re.compile(
    r"^\s*(?:请)?(?:"
    r"解释(?:一下)?|介绍(?:一下)?|讲讲|说说|科普(?:一下)?|"
    r"什么是|为何|为什么|有什么区别|有哪些区别|"
    r".{1,48}(?:有什么区别|有哪些区别)|"
    r".{1,48}(?:有什么区别|有哪些区别)|"
    r"如何|怎么(?:做|实现|使用|配置|安装|创建|新建|修改|编辑|删除|读取|搜索|运行|调用)|"
    r"能不能(?:解释|介绍|讲讲|说说)|"
    r"(?:[\w.-]+|这个|该).{0,16}(?:是什么|怎么用|如何使用|能干嘛|有什么用)"
    r")",
    re.I,
)

_EXPLICIT_EXECUTION_REQUEST_RE = re.compile(
    r"(?:请|帮我|替我|给我|直接|现在|马上|立即|务必).{0,12}"
    r"(?:读取|查看|搜索|查询|创建|新建|写入|生成|修改|编辑|删除|"
    r"运行|执行|测试|安装|下载|设置|调用|发起)|"
    r"(?:读取|搜索|创建|修改|删除|运行|执行|安装|调用).{0,10}"
    r"(?:一下|一遍|一次|这个|这些|当前)",
    re.I,
)


def is_informational_request(message: str | None) -> bool:
    """Whether the user asks for an explanation rather than execution."""
    text = str(message or "").strip()
    if not text or not _INFORMATIONAL_REQUEST_RE.search(text):
        return False
    return not bool(_EXPLICIT_EXECUTION_REQUEST_RE.search(text))


def required_capabilities(message: str | None, goal: dict | None = None) -> set[str]:
    """Infer only explicit external actions; ordinary discussion stays tool-free."""
    text = str(message or "").strip()
    if is_continuation_message(text) and isinstance(goal, dict):
        text = " ".join([
            str(goal.get("objective") or ""),
            str(goal.get("currentStep") or ""),
            " ".join(str(item) for item in goal.get("nextActions") or []),
        ]).strip()
    if not text:
        return set()
    if is_informational_request(text):
        return set()

    requirements: set[str] = set()
    file_target = (
        r"(?:文件|附件|源码|代码|目录|工作区|workspace|项目|文档|"
        r"pdf|pptx|docx|html|报告|页面|数据|"
        r"[\w.-]+\.(?:c|cc|cpp|cxx|h|hpp|py|js|ts|java|md|txt|json|csv))"
    )
    write_target = (
        r"(?:文件|代码文件|源码文件|路径|工作区|workspace|报告|文档|页面|内容|"
        r"pdf|pptx|docx|html|markdown|md|"
        r"[\w.-]+\.(?:c|cc|cpp|cxx|h|hpp|py|js|ts|java|md|txt|json|csv|html))"
    )
    if re.search(
        rf"(?:读取|读一下|读完|查看|看看|检查|分析|打开|列出).{{0,18}}{file_target}|"
        rf"{file_target}.{{0,18}}(?:读取|读一下|查看|检查|分析|内容)",
        text,
        re.I,
    ):
        requirements.add("read")
    if re.search(
        r"(?:联网|网页|网上|web).{0,8}(?:搜|查)|"
        r"(?:搜索|搜一下|查询|查一下|查找).{0,18}(?:最新|新闻|资讯|资料|网页|网站)|"
        r"(?:查询|查一下|看看|告诉我).{0,18}(?:天气|气温|降雨|台风|空气质量)|"
        r"(?:天气|气温|降雨|台风|空气质量).{0,18}(?:查询|预报|情况|怎么样)|"
        r"\b(?:web_search|tavily_search)\b",
        text,
        re.I,
    ):
        requirements.add("search")
    if re.search(
        rf"(?:创建|新建|写入|写一个|生成|修改|编辑|覆盖|复制|保存|替换|"
        rf"删除|删掉|清理|移除|改掉).{{0,18}}{write_target}|"
        rf"{write_target}.{{0,18}}(?:创建|新建|写入|生成|修改|编辑|覆盖|复制|"
        rf"保存|替换|删除|删掉|清理|移除|改掉)",
        text,
        re.I,
    ):
        requirements.add("write")
    if re.search(
        r"(?:编译|运行|执行|跑一下|测试|启动).{0,18}(?:程序|代码|项目|命令|"
        r"脚本|测试|cpp|python|npm|gateway)?|\b(?:run_command|new_shell)\b",
        text,
        re.I,
    ):
        requirements.add("execute")
    if re.search(r"(?:安装|下载|装一下).{0,16}(?:skill|技能)|\binstall_skill\b", text, re.I):
        requirements.add("install")
    if re.search(
        r"(?:创建|设置|修改|暂停|恢复|取消|执行).{0,12}(?:定时任务|提醒|scheduler|cron)|"
        r"(?:每天|每日|每周|每月|每隔.{0,8}|工作日).{0,30}"
        r"(?:提醒|通知|查询|发送|推送|执行)",
        text,
        re.I,
    ):
        requirements.add("schedule")
    return requirements


def validate_completion(
    content: str | None,
    requirements: set[str],
    evidence: TurnEvidence,
    *,
    has_pending_approval: bool = False,
) -> CompletionCheck:
    text = str(content or "").strip()

    # Claims made by the answer itself are evidence obligations even when the
    # original request did not spell out every sub-step.  This is the
    # Runtime-level equivalent of a Stop hook: the model cannot turn an
    # imagined edit/read/test into truth merely by phrasing it confidently.
    action_context = bool(
        requirements
        or evidence.successful_tools
        or evidence.failed_tools
    )
    claimed = ({
        capability
        for capability, pattern in _SUCCESS_PATTERNS.items()
        if pattern.search(text)
    } if action_context else set())
    if action_context and _VERIFICATION_READ_RE.search(text):
        claimed.add("read")
    required = set(requirements) | claimed
    missing_set = {
        capability
        for capability in required
        if not evidence.satisfies(capability)
    }
    # A claim such as "I reread the whole file and confirmed no leftovers"
    # specifically promises a post-mutation check.  A read from before the
    # edit is not valid verification evidence.
    if (
        "write" in requirements
        and _VERIFICATION_READ_RE.search(text)
        and not evidence.has_capability_after("read", "write")
    ):
        missing_set.add("read")
    missing = tuple(sorted(missing_set))
    if not missing:
        return CompletionCheck(True)

    # A truthful failure or clarification is a valid final answer. It must not
    # be converted into an infinite Tool retry.
    if _BLOCKED_RE.search(text):
        return CompletionCheck(True, missing)

    unsupported_success = any(
        _SUCCESS_PATTERNS[capability].search(text)
        for capability in missing
        if capability in _SUCCESS_PATTERNS
    )
    promised_action = bool(_PROMISE_RE.search(text))
    if unsupported_success:
        reason = "assistant claimed completion without matching successful Tool evidence"
    elif promised_action:
        reason = "assistant promised an external action without emitting a Tool call"
    else:
        reason = "requested external action has no matching Tool evidence"
    return CompletionCheck(False, missing, reason)


def repair_hint(check: CompletionCheck) -> str:
    labels = "、".join(CAPABILITY_LABELS.get(item, item) for item in check.missing)
    return (
        f"当前仍缺少可验证的执行证据：{labels or '外部操作'}。"
        "不要用自然语言宣布已经完成，也不要描述稍后会做什么；"
        "请直接发起完成下一步所需的结构化 Tool Call。"
    )
