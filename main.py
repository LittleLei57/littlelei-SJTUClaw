"""SJTUClaw CLI 入口。"""

import argparse
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import difflib
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import sys

from bootstrap import build_runtime
from attachment_store import AttachmentStore
from compaction import CompactionError, CompactionResult
from conversation_view import is_internal_message, visible_message_count
from daily_quotes import quote_for_day
from config import SJTU_API_MODELS, normalize_sjtu_model
from goal_state import context_text, normalize_goal
from memory_store import MemoryStore
from runtime import AgentRuntime
from session_store import utc_now


EXIT_COMMANDS = {"/exit", "/quit"}
BEIJING_TIMEZONE = timezone(timedelta(hours=8), "北京时间")
DEFAULT_ONCE_PROMPT = "你好，请用一句话介绍你自己。"
SESSION_HELP = (
    "Session 命令：\n"
    "  /session new [标题]\n"
    "  /session list\n"
    "  /session show [sessionId]\n"
    "  /session switch <sessionId>\n"
    "  /session rename <sessionId> <标题>\n"
    "  /session delete <sessionId>"
)
MEMORY_HELP = (
    "Memory 命令：\n"
    "  /memory add <长期信息>\n"
    "  /memory list\n"
    "  /memory find <关键词>\n"
    "  /memory update <memoryId> <新内容>\n"
    "  /memory delete <memoryId|记忆描述>"
)
WORKSPACE_HELP = "Workspace 命令：\n  /workspace show\n  /workspace set <目录>\n  /workspace migrate  （旧路径失效时迁移到当前项目目录）"
SKILL_HELP = (
    "Skill 命令：\n  /skill list\n  /skill show <skill-name>\n"
    "  /skill <skill-name> <task>\n  /skill usage"
)
ATTACHMENT_HELP = (
    "Attachment 命令：\n"
    "  /attachment list\n"
    "  /attachment show <attachmentId>\n"
    "  /attachments  （等同于 /attachment list）"
)
MODEL_HELP = (
    "模型命令：\n"
    "  /model                 查看当前模型与可选模型\n"
    "  /model list            列出当前 API 已配置的模型\n"
    "  /model use <model-id>  切换模型并持久化\n"
    "示例：/model use deepseek-reasoner"
)
CLI_START_HINT = (
    "输入 /help 查看命令，使用 /compact 整理长对话；"
    "模型生成中按 Ctrl+C 停止；"
    "输入 /exit 退出。"
)
CLI_HELP = """SJTUClaw CLI 帮助

模型
  /model                查看或切换当前 API 已配置的模型

会话与回答
  /session              管理会话；输入后查看 new/list/switch 等子命令
  /history [条数]       查看当前会话最近的可见消息，默认 12 条
  /context              查看当前会话、消息数量、摘要和 Workspace
  /retry                重新回答当前会话的最后一个问题

资料与能力
  /attachment           查看本会话已上传的附件
  /workspace            查看或设置允许 Agent 操作的工作目录
  /memory               查看和管理经过确认的长期记忆
  /skill                查看 Skill，或指定 Skill 执行任务

任务与整理
  /goal                 查看复杂任务的目标、步骤和完成情况
  /compact              立即整理较早对话，并展示 Summary 预览
  /export               将当前会话导出为 Markdown；也支持 JSON

输入与界面
  /paste                进入多行输入；单独输入一行 . 后发送
  /clear                清理当前终端画面，不删除会话记录
  /help [分类]          查看帮助；分类可用 session/memory/skill 等
  /exit                 退出 CLI；/quit 与它相同

使用提示
  - 模型生成或 Tool 执行时，按 Ctrl+C 可停止本轮。
  - Slash 命令只在本地执行，不会作为聊天内容发给模型。
  - 示例：/history 20    /export json    /session list"""

GOAL_HELP = "Goal 命令：\n  /goal 查看当前任务目标、验收条件和进度"

CLI_COMMANDS = (
    "/attachment", "/attachments", "/cancel", "/clear", "/compact",
    "/context", "/exit", "/export", "/goal", "/help", "/history",
    "/memory", "/model", "/paste", "/quit", "/regenerate", "/retry", "/session",
    "/skill", "/spec", "/status", "/stop", "/workspace",
)


_CONDA_SHELL_INJECTION = re.compile(
    r"^(?:[A-Za-z]:[^\r\n]*[\\/])?conda(?:\.exe)?\s+activate\s+\S+$",
    re.I,
)


def _is_shell_environment_injection(value: str) -> bool:
    """Recognize commands VS Code may echo while switching interpreters.

    A running ``input()`` loop can consume the Python extension's
    ``conda.EXE activate ...`` line as if it were a user message.  Such a
    command cannot change the parent CLI process anyway, so it must not be
    forwarded to the Agent as a Shell request.
    """
    return bool(_CONDA_SHELL_INJECTION.fullmatch((value or "").strip()))


def _configure_cli_readline(input_fn: Callable[[str], str]) -> None:
    """Enable best-effort command history and slash-command completion.

    The standard ``readline`` module is optional on Windows.  Nothing is
    written to disk: when unavailable (or when tests inject ``input_fn``),
    the CLI keeps its existing plain-input behavior.
    """
    if input_fn is not input or not getattr(sys.stdin, "isatty", lambda: False)():
        return
    if os.name == "nt":
        # pyreadline/pyreadline3 redraws ``input("")`` with an empty prompt in
        # VS Code terminals.  That erases the ``User>`` text which
        # ``_read_cli_input`` has already flushed, and it may also leave a
        # partially completed slash command on the next line.  Plain Windows
        # console input is more predictable; completion remains available on
        # platforms backed by GNU readline.
        return
    try:
        import readline  # type: ignore[import-not-found]
    except ImportError:
        return

    def complete(text: str, state: int):
        matches = [item for item in CLI_COMMANDS if item.startswith(text)]
        return matches[state] if state < len(matches) else None

    try:
        readline.set_completer(complete)
        readline.parse_and_bind("tab: complete")
    except (AttributeError, OSError):
        return


def _read_cli_input(input_fn: Callable[[str], str], prompt: str) -> str:
    """Display an interactive prompt before blocking for keyboard input.

    On Windows, ``readline`` implementations used by VS Code terminals may
    defer painting the prompt passed to ``input(prompt)`` until the first
    keypress.  Writing and flushing it ourselves keeps ``User>`` and approval
    prompts visible immediately.  Injected input functions retain the normal
    ``input_fn(prompt)`` contract used by tests and adapters.
    """
    if input_fn is not input:
        return input_fn(prompt)
    sys.stdout.write(prompt)
    sys.stdout.flush()
    return input_fn("")


def _configure_cli_output_encoding(
    output_fn: Callable[[str], None],
) -> None:
    """Prefer UTF-8 for the interactive Windows terminal.

    Some Conda/VS Code combinations still expose stdout as GBK.  Ordinary
    Chinese text happens to survive, while emoji and box-drawing characters
    are replaced after readline redraws the prompt.  Reconfiguring only the
    real interactive streams keeps injected test/adaptor outputs untouched.
    """
    if output_fn is not print or os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower()
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure) and "utf" not in encoding and "65001" not in encoding:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (LookupError, OSError, ValueError):
                pass


def handle_help_command(command: str) -> str:
    """Return either the full guide or focused help for one command group."""
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"命令格式错误：{exc}") from exc
    if len(parts) == 1:
        return CLI_HELP
    if len(parts) != 2:
        raise ValueError("用法：/help [session|memory|workspace|skill|attachment|goal|input]")
    topic = parts[1].lower().lstrip("/")
    focused = {
        "session": SESSION_HELP,
        "history": "History 命令：\n  /history [条数]  查看最近的可见消息，默认 12 条、最多 100 条",
        "memory": MEMORY_HELP,
        "model": MODEL_HELP,
        "workspace": WORKSPACE_HELP,
        "skill": SKILL_HELP,
        "attachment": ATTACHMENT_HELP,
        "attachments": ATTACHMENT_HELP,
        "goal": GOAL_HELP,
        "spec": GOAL_HELP,
        "input": (
            "输入帮助：\n"
            "  /paste  进入多行输入模式；单独输入一行 . 后发送\n"
            "  /clear  清理终端画面，但不删除会话记录\n"
            "  Ctrl+C  生成中停止本轮；输入时取消当前输入"
        ),
        "export": (
            "Export 命令：\n"
            "  /export                 导出当前会话为 Markdown\n"
            "  /export json            导出为 JSON\n"
            "  /export markdown <文件>  指定 Workspace 内的导出文件名"
        ),
        "compact": (
            "Compaction 命令：\n"
            "  /compact  立即整理当前会话的较早消息，并显示 Summary 预览"
        ),
    }
    if topic not in focused:
        raise ValueError(f"未知帮助分类：{parts[1]}。输入 /help 查看全部命令。")
    return focused[topic]


_LATEX_GLYPHS = {
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ϵ", r"\theta": "θ", r"\lambda": "λ",
    r"\mu": "μ", r"\pi": "π", r"\sigma": "σ", r"\phi": "φ",
    r"\varphi": "ϕ", r"\omega": "ω", r"\Gamma": "Γ", r"\Delta": "Δ",
    r"\Theta": "Θ", r"\Lambda": "Λ", r"\Pi": "Π", r"\Sigma": "Σ",
    r"\Phi": "Φ", r"\Omega": "Ω", r"\infty": "∞", r"\cdot": "·",
    r"\times": "×", r"\pm": "±", r"\leq": "≤", r"\geq": "≥",
    r"\neq": "≠", r"\approx": "≈", r"\sim": "∼", r"\to": "→",
    r"\rightarrow": "→", r"\leftarrow": "←", r"\Rightarrow": "⇒",
    r"\Leftrightarrow": "⇔", r"\in": "∈", r"\notin": "∉", r"\mid": "|", r"\vert": "|", r"\subset": "⊂",
    r"\subseteq": "⊆", r"\cup": "∪", r"\cap": "∩", r"\forall": "∀",
    r"\exists": "∃", r"\partial": "∂", r"\nabla": "∇", r"\sum": "Σ",
    r"\prod": "Π", r"\int": "∫", r"\ldots": "…", r"\cdots": "⋯",
}


def _latex_braced_argument(text: str, start: int) -> tuple[str, int] | None:
    """Read one ``{...}`` argument, including nested braces."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
    return None


def _latex_to_terminal(text: str) -> str:
    """Convert common LaTeX into readable terminal text without new deps.

    Terminals cannot reproduce KaTeX's two-dimensional layout.  This small
    normalizer deliberately preserves the mathematical meaning while making
    formulas readable in Rich and plain ANSI output (for example,
    ``\\frac{a}{b}`` becomes ``(a)/(b)`` and ``\\sqrt{x}`` becomes ``√(x)``).
    """
    value = str(text or "").strip()
    value = re.sub(r"\\(?:left|right|Bigg?|bigg?|Big|big)\\?", "", value)
    # Handle nested-free command arguments first; repeat for expressions such
    # as \\frac{\\sqrt{x}}{2} after the inner command has been normalized.
    for _ in range(8):
        changed = False
        for command, replacement in (("frac", None), ("dfrac", None), ("tfrac", None), ("sqrt", "√({0})"), ("text", "{0}"), ("mathrm", "{0}"), ("mathbf", "{0}"), ("mathbb", "{0}"), ("operatorname", "{0}")):
            marker = "\\" + command
            position = value.find(marker)
            while position >= 0:
                first = position + len(marker)
                while first < len(value) and value[first].isspace():
                    first += 1
                argument = _latex_braced_argument(value, first)
                if argument is None:
                    position = value.find(marker, first)
                    continue
                body, end = argument
                if command in {"frac", "dfrac", "tfrac"}:
                    second = end
                    while second < len(value) and value[second].isspace():
                        second += 1
                    denominator = _latex_braced_argument(value, second)
                    if denominator is None:
                        position = value.find(marker, end)
                        continue
                    lower, final_end = denominator
                    replacement_text = f"({body})/({lower})"
                else:
                    replacement_text = replacement.format(body)
                    final_end = end
                value = value[:position] + replacement_text + value[final_end:]
                changed = True
                position = value.find(marker, position + len(replacement_text))
        if not changed:
            break

    for command, glyph in _LATEX_GLYPHS.items():
        value = value.replace(command, glyph)
    value = re.sub(r"\\(?:!,?|;|:|quad|qquad|space)", "", value)
    # Remaining braces are grouping punctuation, not useful terminal syntax.
    value = value.replace("{", "(").replace("}", ")")
    value = re.sub(r"\\([A-Za-z]+)", r"\1", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _render_latex(text: str) -> str:
    """Replace display/inline math markers while leaving normal ``$`` text."""
    if not text or "\\" not in text and "$" not in text:
        return text

    def display(match: re.Match) -> str:
        return f"\n[公式] {_latex_to_terminal(match.group(1))}\n"

    rendered = re.sub(r"\$\$(.+?)\$\$", display, text, flags=re.S)
    rendered = re.sub(r"\\\[(.+?)\\\]", display, rendered, flags=re.S)

    def inline(match: re.Match) -> str:
        return f"⟦{_latex_to_terminal(match.group(1))}⟧"

    rendered = re.sub(r"\\\((.+?)\\\)", inline, rendered, flags=re.S)

    def dollar_inline(match: re.Match) -> str:
        body = match.group(1)
        # Avoid treating ordinary currency (e.g. ``$5``) as mathematics.
        if not re.search(r"\\|[_^=]|\\b(?:frac|sum|sqrt|sin|cos|log)\\b", body):
            return match.group(0)
        return f"⟦{_latex_to_terminal(body)}⟧"

    return re.sub(r"(?<!\$)\$(?!\$)([^$\n]+?)(?<!\$)\$(?!\$)", dollar_inline, rendered)


def _render_terminal_math_in_markdown(markdown: str) -> str:
    """Normalize formulas outside fenced code blocks for terminal output."""
    sections = re.split(r"(```[^\n]*\n.*?```)", markdown or "", flags=re.S)
    for index in range(0, len(sections), 2):
        sections[index] = _render_latex(sections[index])
    return "".join(sections)


def _render_inline_markdown(text: str, ansi: bool = True) -> str:
    """Small ANSI renderer for terminals without the optional Rich package."""
    text = _render_latex(text)
    text = re.sub(r"!?(?:\[([^\]]+)\])\((https?://[^)]+)\)", r"\1 <\2>", text)
    code_replacement = (lambda match: f"\033[36m{match.group(1)}\033[0m") if ansi else (lambda match: match.group(1))
    bold_replacement = (lambda match: f"\033[1m{match.group(1) or match.group(2)}\033[0m") if ansi else (lambda match: match.group(1) or match.group(2))
    italic_replacement = (lambda match: f"\033[3m{match.group(1) or match.group(2)}\033[0m") if ansi else (lambda match: match.group(1) or match.group(2))
    text = re.sub(r"`([^`]+)`", code_replacement, text)
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", bold_replacement, text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)|(?<!_)_([^_]+)_(?!_)", italic_replacement, text)
    return text


def render_terminal_markdown_fallback(markdown: str) -> str:
    """Make common Markdown readable in VS Code even when Rich is unavailable."""
    output: list[str] = []
    markdown = _render_terminal_math_in_markdown(markdown)
    lines = markdown.replace("\r\n", "\n").split("\n")
    in_code_block = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            index += 1
            continue
        if in_code_block:
            output.append(f"    {line}")
            index += 1
            continue

        # GFM table: consume a header, separator and following rows as aligned text.
        if (
            "|" in line
            and index + 1 < len(lines)
            and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1])
        ):
            rows: list[list[str]] = []
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                candidate = lines[index].strip().strip("|")
                if re.fullmatch(r"\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*", candidate):
                    index += 1
                    continue
                rows.append([_render_inline_markdown(cell.strip()) for cell in candidate.split("|")])
                index += 1
            column_count = max((len(row) for row in rows), default=0)
            widths = [max(len(re.sub(r"\033\[[0-9;]*m", "", row[col]) if col < len(row) else "") for row in rows) for col in range(column_count)]
            for row_number, row in enumerate(rows):
                cells = [row[col] if col < len(row) else "" for col in range(column_count)]
                padded = [cell + " " * max(0, widths[col] - len(re.sub(r"\033\[[0-9;]*m", "", cell))) for col, cell in enumerate(cells)]
                output.append("│ " + " │ ".join(padded) + " │")
                if row_number == 0:
                    output.append("├" + "┼".join("─" * (width + 2) for width in widths) + "┤")
            continue

        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            output.append(f"\033[1m{_render_inline_markdown(heading.group(2))}\033[0m")
        elif line.lstrip().startswith(">"):
            output.append("│ " + _render_inline_markdown(line.lstrip()[1:].lstrip()))
        else:
            output.append(_render_inline_markdown(line))
        index += 1
    return "\n".join(output).strip()


def output_assistant_reply(reply: str, output_fn: Callable[[str], None]) -> None:
    """Render Markdown interactively while preserving injectable CLI output."""
    if output_fn is not print:
        output_fn(f"Assistant> {reply}")
        return
    try:
        from rich.console import Console
        from rich.markdown import Markdown

        console = Console(file=sys.stdout)
        console.print("[bold #c6533d]Assistant>[/bold #c6533d]")
        console.print(Markdown(_render_terminal_math_in_markdown(reply)))
    except Exception:
        output_fn("Assistant>\n" + render_terminal_markdown_fallback(reply))


def _supports_keyword(callable_obj, name: str) -> bool:
    """Return whether a runtime/fake runtime accepts a keyword argument.

    The CLI is also exercised with small fake runtimes in the test suite.  A
    signature check lets the real AgentRuntime receive event callbacks while
    keeping those light-weight adapters backwards compatible.
    """
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return True
    parameters = signature.parameters.values()
    return name in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _format_cli_compaction_event(event: dict) -> str:
    event_type = str(event.get("type") or "")
    old_messages = event.get("oldMessages", 0)
    recent_messages = event.get("recentMessages", 0)
    chunks = event.get("chunks", 0)
    if event_type == "compaction_started":
        chunk_text = f" · {chunks} 个分块" if chunks else ""
        return (
            f"[system] ✦ 正在整理上下文 · 整理 {old_messages} 条旧消息，"
            f"保留最近 {recent_messages} 条{chunk_text}"
        )
    if event_type == "compaction":
        preview = str(event.get("summaryPreview") or "").strip()
        header = (
            f"[system] ✦ 上下文整理完成 · 整理 {old_messages} 条旧消息，"
            f"保留最近 {recent_messages} 条"
        )
        if chunks:
            header += f" · {chunks} 个分块"
        version = event.get("summaryVersion")
        covered_start = event.get("coveredMessageStart")
        covered_end = event.get("coveredMessageEnd")
        if version:
            header += f" · Summary v{version}"
        if covered_start and covered_end:
            header += f" · 覆盖语义消息 {covered_start}-{covered_end}"
        warnings = event.get("qualityWarnings") or []
        if warnings:
            header += f" · {len(warnings)} 项格式提醒"
        if not preview:
            return header
        return f"{header}\n[system] summary:\n{render_terminal_markdown_fallback(preview)}"
    if event_type == "compaction_failed":
        return f"[system] ⚠ 上下文整理失败：{event.get('error') or '未知错误'}"
    return ""


class CliTheme:
    """Small, dependency-free CLI theme with safe terminal fallbacks."""

    _unicode_replacements = {
        "🦞": "[SJTUClaw]",
        "✦": "*",
        "⚠": "!",
        "↻": "<-",
        "↳": "->",
        "⚙": "[tool]",
        "🔐": "[approval]",
        "╭": "+",
        "╰": "+",
        "│": "|",
        "·": "-",
        "─": "-",
        "–": "-",
        "…": "...",
        "✓": "OK",
    }

    def __init__(self, unicode: bool = True, ansi: bool = False):
        self.unicode = unicode
        self.ansi = ansi
        self.separator = "─" * 42 if unicode else "-" * 42
        self.assistant_prefix = "🦞 Assistant> " if unicode else "Assistant> "

    @classmethod
    def detect(cls, output_fn: Callable[[str], None]) -> "CliTheme":
        # Captured/injected output is deliberately left readable and
        # uncoloured; it may be consumed by tests or another application.
        if output_fn is not print:
            return cls(unicode=True, ansi=False)
        encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
        unicode_capable = "utf" in encoding or "65001" in encoding
        ansi_capable = (
            bool(getattr(sys.stdout, "isatty", lambda: False)())
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM", "") != "dumb"
        )
        return cls(unicode=unicode_capable, ansi=ansi_capable)

    def text(self, value: str) -> str:
        if self.unicode:
            return value
        for source, replacement in self._unicode_replacements.items():
            value = value.replace(source, replacement)
        return value

    def paint(self, value: str) -> str:
        value = self.text(value)
        if not self.ansi:
            return value
        if "approval_required" in value or "!" in value or "失败" in value:
            color = "33"  # amber
        elif "调用中" in value:
            color = "36"  # cyan
        elif "[system]" in value:
            color = "35"  # magenta
        else:
            color = "37"
        return f"\033[{color}m{value}\033[0m"


class CliStreamRenderer:
    """Render AgentRuntime events as a compact, terminal-friendly stream.

    ``AgentRuntime`` emits visible assistant deltas only for the final answer;
    protocol JSON used for Tool Calls never reaches this renderer as text.  In
    a real terminal chunks are written immediately.  Injected output
    functions (used by tests and embedding callers) receive one complete
    ``Assistant> ...`` line, preserving the historical API.
    """

    def __init__(
        self,
        output_fn: Callable[[str], None],
        verbose_tools: bool = False,
        show_metrics: bool = False,
    ):
        self.output_fn = output_fn
        self.verbose_tools = verbose_tools
        self.show_metrics = show_metrics
        self.theme = CliTheme.detect(output_fn)
        self.live = output_fn is print
        self.seen_event = False
        self.streamed = False
        self.final_seen = False
        self.final_content = ""
        self._line_open = False
        self._buffer: list[str] = []
        self._section_started = False
        self._metrics: list[dict] = []
        self._tool_count = 0
        self._markdown_pending = ""
        self._live_line_pending = ""
        self._live_table_lines: list[str] = []
        self._tool_group_open = False

    def _render_live_markdown(self, text: str) -> str:
        """Render complete inline Markdown pairs while preserving streaming."""
        combined = self._markdown_pending + (text or "")
        self._markdown_pending = ""
        output: list[str] = []
        cursor = 0
        delimiters = ("**", "__", "`")
        while cursor < len(combined):
            positions = [combined.find(item, cursor) for item in delimiters]
            positions = [item for item in positions if item >= 0]
            if not positions:
                tail = combined[cursor:]
                if tail.endswith(("*", "_", "`")):
                    self._markdown_pending = tail[-1]
                    tail = tail[:-1]
                output.append(_render_inline_markdown(tail, ansi=self.theme.ansi))
                break
            start = min(positions)
            output.append(_render_inline_markdown(combined[cursor:start], ansi=self.theme.ansi))
            delimiter = next(item for item in delimiters if combined.startswith(item, start))
            end = combined.find(delimiter, start + len(delimiter))
            if end < 0:
                self._markdown_pending = combined[start:]
                break
            inner = combined[start + len(delimiter):end]
            if delimiter == "`":
                output.append(f"\033[36m{inner}\033[0m" if self.theme.ansi else inner)
            else:
                output.append(f"\033[1m{inner}\033[0m" if self.theme.ansi else inner)
            cursor = end + len(delimiter)
        return "".join(output)

    def _flush_markdown_pending(self) -> None:
        if not self._markdown_pending:
            return
        pending = re.sub(r"\*\*|__|`", "", self._markdown_pending)
        self._markdown_pending = ""
        if pending:
            if self.live:
                sys.stdout.write(self.theme.paint(pending))
                sys.stdout.flush()
            else:
                self._buffer.append(pending)

    @staticmethod
    def _is_table_separator(line: str) -> bool:
        return bool(re.fullmatch(
            r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*",
            line,
        ))

    def _write_live_line(self, line: str) -> None:
        """Write one completed Markdown line, retaining inline streaming."""
        stripped = line.strip()
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            rendered = f"\033[1m{_render_inline_markdown(heading.group(2), ansi=self.theme.ansi)}\033[0m"
        elif line.lstrip().startswith(">"):
            rendered = "│ " + self._render_live_markdown(line.lstrip()[1:].lstrip())
        else:
            rendered = self._render_live_markdown(line)
        # Emphasis/code delimiters are not allowed to hide the following
        # terminal lines indefinitely.  If one completed source line contains
        # malformed or unmatched Markdown, show its text plainly and reset the
        # inline state at that line boundary.
        if self._markdown_pending:
            rendered += re.sub(r"\*\*|__|`", "", self._markdown_pending)
            self._markdown_pending = ""
        if self.live:
            sys.stdout.write(self.theme.paint(rendered + "\n"))
            sys.stdout.flush()

    def _flush_live_table(self) -> None:
        rows = self._live_table_lines
        self._live_table_lines = []
        if len(rows) < 2 or not self._is_table_separator(rows[1]):
            for row in rows:
                self._write_live_line(row)
            return
        parsed: list[list[str]] = []
        for row in rows:
            if self._is_table_separator(row):
                continue
            parsed.append([_render_inline_markdown(cell.strip(), ansi=self.theme.ansi)
                           for cell in row.strip().strip("|").split("|")])
        widths = [
            max(
                len(re.sub(r"\033\[[0-9;]*m", "", row[col]))
                if col < len(row) else 0
                for row in parsed
            )
            for col in range(max((len(row) for row in parsed), default=0))
        ]
        for index, row in enumerate(parsed):
            cells = [row[col] if col < len(row) else "" for col in range(len(widths))]
            padded = [
                cell + " " * max(0, widths[col] - len(re.sub(r"\033\[[0-9;]*m", "", cell)))
                for col, cell in enumerate(cells)
            ]
            self._write_live_line("│ " + " │ ".join(padded) + " │")
            if index == 0:
                self._write_live_line("├─" + "─┼─".join("─" * (width + 2) for width in widths) + "─┤")

    def _write_live_markdown(self, text: str) -> None:
        # A stray CR from a provider/adapter returns the terminal cursor to
        # column zero and visually overwrites list numbers or the start of the
        # current line.  Normalize all newline forms before incremental
        # rendering.
        normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        combined = self._live_line_pending + normalized
        self._live_line_pending = ""
        parts = combined.split("\n")
        if not parts:
            return
        self._live_line_pending = parts.pop()
        # Keep ordinary prose genuinely streaming. Only hold a partial line
        # when it could become a heading/quote or a GFM table row that needs a
        # look-ahead to render correctly.
        pending = self._live_line_pending
        if (
            pending
            and not self._live_table_lines
            and "|" not in pending
            and not pending.lstrip().startswith(("#", ">"))
        ):
            self._live_line_pending = ""
            sys.stdout.write(self.theme.paint(self._render_live_markdown(pending)))
            sys.stdout.flush()
        for line in parts:
            if self._live_table_lines:
                if len(self._live_table_lines) == 1 and not self._is_table_separator(line):
                    first = self._live_table_lines.pop()
                    self._write_live_line(first)
                elif "|" not in line or not line.strip():
                    self._flush_live_table()
            if self._live_table_lines:
                self._live_table_lines.append(line)
            elif "|" in line:
                self._live_table_lines.append(line)
            else:
                self._write_live_line(line)

    def _flush_live_pending(self) -> None:
        if self._live_line_pending:
            line = self._live_line_pending
            self._live_line_pending = ""
            if self._live_table_lines:
                if "|" in line and line.strip():
                    self._live_table_lines.append(line)
                else:
                    self._flush_live_table()
                    self._write_live_line(line)
            elif "|" in line:
                self._live_table_lines.append(line)
            else:
                self._write_live_line(line)
        if self._live_table_lines:
            self._flush_live_table()

    def _start_section(self) -> None:
        if self._section_started:
            return
        self._section_started = True
        self.output_fn(self.theme.paint(f"\n{self.theme.separator}"))

    def _begin_tool_group(self) -> None:
        """Start one readable activity block for the current tool phase."""
        if self._tool_group_open:
            return
        self.flush_for_status()
        if self._section_started:
            self.output_fn("")
        self._start_section()
        self.output_fn(self.theme.paint("╭─ 工具执行"))
        self._tool_group_open = True

    def _tool_line(self, text: str) -> None:
        self._begin_tool_group()
        self.output_fn(self.theme.paint(f"│ {text}"))

    def _close_tool_group(self) -> None:
        if not self._tool_group_open:
            return
        self.output_fn(self.theme.paint("╰─"))
        self._tool_group_open = False

    def _write_stream(self, text: str) -> None:
        if not text:
            return
        if self.live:
            # A Tool block belongs immediately before the assistant prose that
            # follows it. Close it at the first streamed answer chunk instead
            # of waiting until the entire turn finishes.
            self._close_tool_group()
            if not self._line_open:
                if self._tool_count:
                    sys.stdout.write("\n")
                self._start_section()
                sys.stdout.write(self.theme.paint(self.theme.assistant_prefix))
                sys.stdout.flush()
                self._line_open = True
            self._write_live_markdown(text)
        else:
            self._buffer.append(text)
        self.streamed = True

    def flush_for_status(self) -> None:
        """Close an interactive stream before a status/prompt is printed."""
        if self.live:
            self._flush_live_pending()
        self._flush_markdown_pending()
        if self.live and self._line_open:
            sys.stdout.write("\n")
            sys.stdout.flush()
        self._line_open = False
        self._close_tool_group()

    def _status(self, text: str) -> None:
        if not text:
            return
        self.flush_for_status()
        self._start_section()
        self.output_fn(self.theme.paint(text))

    def on_event(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        self.seen_event = True
        event_type = str(event.get("type") or "")
        if event_type == "assistant_delta":
            self._write_stream(str(event.get("delta") or ""))
            return
        if event_type == "assistant_final":
            self.final_seen = True
            self.final_content = str(event.get("content") or "")
            return
        if event_type == "assistant_reset":
            self.flush_for_status()
            self._buffer.clear()
            self.streamed = False
            self._status("[system] ↻ 正在重新生成本轮回答")
            return
        if event_type == "assistant_note":
            self._status(f"  ↳ {event.get('message') or event.get('content') or ''}")
            return
        if event_type == "tool_call":
            tool = str(event.get("tool") or "Tool")
            if self.verbose_tools:
                args = json.dumps(event.get("args") or {}, ensure_ascii=False)
                self._tool_line(f"⚙ {tool} · 调用中")
                self._tool_line(f"参数：{args}")
            else:
                self._tool_line(f"⚙ {tool} · 调用中")
            return
        if event_type == "tool_result":
            self._tool_count += 1
            if self.verbose_tools:
                raw_result = json.dumps(
                    event.get("result") or {}, ensure_ascii=False
                )
                if len(raw_result) > 1200:
                    raw_result = raw_result[:1200] + "…"
                self._tool_line(f"✓ {event.get('tool') or 'Tool'} · 调用完成")
                self._tool_line(f"结果：{raw_result}")
            else:
                tool = str(event.get("tool") or "Tool")
                result = event.get("result") or {}
                icon = "✓" if result.get("success", False) else "!"
                self._tool_line(f"{icon} {tool} · {_tool_result_brief(tool, result)}")
            return
        if event_type == "metrics":
            self._metrics.append(event)
            return
        if event_type == "approval_required":
            approval = event.get("approval") or {}
            approval_id = approval.get("approvalId") or event.get("approvalId") or "unknown"
            tool = approval.get("tool") or event.get("tool") or "Tool"
            self._tool_line(
                f"🔐 approval_required · {tool} · 等待审批 ({approval_id})"
            )
            return
        if event_type == "goal_state":
            goal = normalize_goal(event.get("goal"))
            if goal:
                self._status(
                    f"🎯 任务 {goal.get('status')} · {goal.get('currentStep') or goal.get('objective')}"
                )
            return
        if event_type in {"compaction_started", "compaction", "compaction_failed"}:
            self._status(_format_cli_compaction_event(event))
            return
        if event_type == "status":
            phase = str(event.get("phase") or "")
            if phase in {"protocol_retry", "truncated_final_retry", "model_output_limit_retry"}:
                self._status(f"[system] ⚠ {event.get('message') or phase}")

    def finish(self, reply: str | None) -> None:
        """Flush the final answer once, avoiding duplicate delta + final text."""
        if self.streamed:
            if self.live:
                self.flush_for_status()
            else:
                text = "".join(self._buffer).strip() or str(reply or self.final_content)
                if text:
                    self._close_tool_group()
                    if self._tool_count:
                        self.output_fn("")
                    self._start_section()
                    self.output_fn(f"Assistant> {text}")
        elif reply:
            self._close_tool_group()
            output_assistant_reply(reply, self.output_fn)
        elif self.final_content:
            self._close_tool_group()
            output_assistant_reply(self.final_content, self.output_fn)
        if self.show_metrics and self._metrics:
            class _Turn:
                metrics = self._metrics
                tool_events = [{}] * self._tool_count
            self._status(format_turn_metrics(_Turn()))

    def abort(self) -> None:
        self.flush_for_status()


def format_compaction_result(result: CompactionResult) -> str:
    text = (
        f"[system] compact session {result.session_id}: "
        f"old_messages={result.old_messages}, recent_messages={result.recent_messages}\n"
        f"[system] summary:\n{render_terminal_markdown_fallback(result.preview)}"
    )
    text += (
        f"\n[system] summary_version={result.summary_version} "
        f"covered_messages={result.covered_message_start}-{result.covered_message_end}"
    )
    if result.quality_warnings:
        text += "\n[system] quality_warnings: " + "；".join(result.quality_warnings)
    return text


def _tool_result_brief(tool: str, result: dict) -> str:
    """Return a useful one-line CLI observation without dumping provider JSON."""
    if not result.get("success", False):
        return f"失败：{result.get('error') or '未知错误'}"
    output = result.get("output")
    if not isinstance(output, dict):
        return "调用成功"
    if tool == "current_time":
        return output.get("iso") or "已获取当前时间"
    if tool == "calculate":
        expression = str(output.get("expression") or "表达式").replace("\n", " ")
        formatted = output.get("formatted", output.get("result"))
        return f"{expression[:80]} = {formatted}"
    if tool == "weather_forecast":
        location = (output.get("location") or {}).get("name") or "目标地点"
        daily = output.get("daily") or []
        forecast = daily[-1] if daily else {}
        date = forecast.get("date", "")
        text = forecast.get("weather_text", "")
        minimum = forecast.get("temperature_2m_min")
        maximum = forecast.get("temperature_2m_max")
        temperature = f" · {minimum}–{maximum}°C" if minimum is not None and maximum is not None else ""
        return f"{location} {date} · {text}{temperature}".strip(" ·")
    if tool == "web_search":
        results = output.get("results") or []
        answer = str(output.get("answer") or "").replace("\n", " ").strip()
        return (answer[:140] + ("…" if len(answer) > 140 else "")) if answer else f"找到 {len(results)} 条结果"
    if tool == "read_attachment":
        name = output.get("filename") or output.get("attachmentId") or "附件"
        pages = output.get("pages") or output.get("processedPages")
        suffix = f" · {pages} 页" if pages else ""
        if output.get("truncated"):
            suffix += "（待继续读取）"
        return f"已读取 {name}{suffix}"
    if tool == "list_dir":
        path = output.get("path") or output.get("resolvedPath")
        count = len(output.get("entries") or [])
        suffix = "（结果已截断）" if output.get("truncated") else ""
        return f"{path or '目录'} · {count} 项{suffix}"
    if tool == "read_file":
        path = output.get("path") or output.get("resolvedPath") or "文件"
        size = output.get("bytes_read")
        suffix = "（内容已截断）" if output.get("truncated") else ""
        return f"已读取 {path}" + (f" · {size} B" if size is not None else "") + suffix
    message = output.get("message")
    if message:
        return str(message).replace("\n", " ")[:160]
    if tool in {"read_document", "read_attachment", "ocr_image"}:
        name = output.get("filename") or output.get("attachmentId") or "附件"
        pages = output.get("pages") or output.get("processedPages")
        suffix = f" · {pages} 页" if pages else ""
        if output.get("truncated"):
            suffix += "（待继续读取）"
        return f"已读取 {name}{suffix}"
    if tool == "new_shell":
        return f"Shell 已启动 · {output.get('cwd') or output.get('path') or 'Workspace'}"
    if tool == "run_command":
        code = output.get("exit_code", output.get("exitCode"))
        return f"命令执行完成" + (f" · exit {code}" if code is not None else "")
    if tool == "use_skill":
        return f"已加载 {output.get('name') or output.get('skill') or 'Skill'}"
    if tool == "create_download":
        return "下载链接已创建"
    return "调用成功"


def format_tool_event(event: dict, verbose: bool = False) -> str:
    tool = str(event.get("tool") or "Tool")
    args = event.get("args") or {}
    result = event.get("result") or {}
    if verbose:
        raw_args = json.dumps(args, ensure_ascii=False)
        raw_result = json.dumps(result, ensure_ascii=False)
        if len(raw_result) > 1200:
            raw_result = raw_result[:1200] + "…"
        return f"[tool] {tool}\n  参数：{raw_args}\n  结果：{raw_result}"
    return f"[tool] {tool} · {_tool_result_brief(tool, result)}"


def replay_cli_tool_events(renderer: CliStreamRenderer, events: list[dict]) -> None:
    """Render persisted Tool events through the same activity layout.

    Older Runtime adapters may not accept ``event_callback``.  They still
    return Tool Result records on the completed Turn; replay those records
    through the renderer instead of falling back to raw ``[tool]`` lines.
    """
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type"):
            renderer.on_event(event)
        elif event.get("tool") and "result" in event:
            renderer.on_event({"type": "tool_result", **event})


def format_session_detail(session, limit: int = 20) -> str:
    """Format one session for CLI inspection without sending anything to LLM."""
    lines = [
        f"Session: {session.session_id}",
        f"Title: {session.title}",
        f"Messages: {visible_message_count(session.messages)}",
        f"Stored records: {len(session.messages)} (including internal Tool/Approval records)",
        f"Created: {_format_beijing_time(session.created_at)}",
        f"Updated: {_format_beijing_time(session.updated_at)}",
    ]
    if session.summary.strip():
        preview = session.summary.strip().replace("\n", " ")
        lines.append(f"Summary: {preview[:160]}{'…' if len(preview) > 160 else ''}")
    visible_messages = [
        item for item in session.messages
        if item.get("role") in {"user", "assistant"} and not is_internal_message(item)
    ]
    if not visible_messages:
        lines.append("History: （暂无消息）")
        return "\n".join(lines)

    messages = visible_messages[-limit:]
    # Numbering follows the same user-visible message semantics as the
    # summary count; internal Tool/Approval records must not create gaps.
    skipped = len(visible_messages) - len(messages)
    lines.append("History:")
    if skipped > 0:
        lines.append(f"  ... 已省略较早 {skipped} 条可见消息")
    for index, message in enumerate(messages, start=skipped + 1):
        role = str(message.get("role", "unknown"))
        content = str(message.get("content", "")).replace("\n", " ")
        lines.append(f"  {index}. {role}> {content[:220]}{'…' if len(content) > 220 else ''}")
    return "\n".join(lines)


def format_cli_history(session, limit: int = 12) -> str:
    """Render recent user-visible messages without exposing protocol records."""
    if limit < 1 or limit > 100:
        raise ValueError("/history 条数应在 1 到 100 之间。")
    visible_messages = [
        item for item in session.messages
        if item.get("role") in {"user", "assistant"} and not is_internal_message(item)
    ]
    if not visible_messages:
        return "当前 Session 暂无可见消息。"
    selected = visible_messages[-limit:]
    skipped = len(visible_messages) - len(selected)
    lines = [
        f"最近 {len(selected)} 条消息"
        + (f"（更早 {skipped} 条已省略）：" if skipped else "：")
    ]
    for index, message in enumerate(selected, start=skipped + 1):
        role = "你" if message.get("role") == "user" else "SJTUClaw"
        content = re.sub(r"\s+", " ", str(message.get("content") or "")).strip()
        if len(content) > 180:
            content = content[:180] + "…"
        lines.append(f"  {index:>3}. {role} · {content or '（空消息）'}")
    return "\n".join(lines)


def handle_history_command(runtime: AgentRuntime, command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"命令格式错误：{exc}") from exc
    if len(parts) > 2:
        raise ValueError("用法：/history [条数]")
    try:
        limit = int(parts[1]) if len(parts) == 2 else 12
    except ValueError as exc:
        raise ValueError("/history 条数必须是整数。") from exc
    return format_cli_history(runtime.store.current, limit)


def collect_multiline_input(
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> str:
    """Collect a multiline user message terminated by a single dot line."""
    output_fn("多行输入模式：可直接换行；单独输入一行 . 后发送，Ctrl+C 取消。")
    lines: list[str] = []
    while True:
        try:
            line = _read_cli_input(input_fn, "... ")
        except (EOFError, StopIteration):
            break
        if line == ".":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def clear_cli_screen(output_fn: Callable[[str], None]) -> None:
    """Clear an interactive ANSI terminal, with a test/redirect-safe fallback."""
    if (
        output_fn is print
        and getattr(sys.stdout, "isatty", lambda: False)()
        and not os.getenv("NO_COLOR")
    ):
        output_fn("\033[2J\033[H", end="")
        return
    output_fn("[system] 已清理当前终端视图。")


def format_cli_context_status(runtime: AgentRuntime) -> str:
    """Show a compact, user-facing snapshot of the active Session."""
    session = runtime.store.current
    visible = visible_message_count(session.messages)
    stored = len(session.messages)
    summary = "有 Summary" if session.summary.strip() else "无 Summary"
    summary_meta = getattr(session, "summary_meta", {}) or {}
    if summary_meta.get("version"):
        summary += f" v{summary_meta['version']}"
    workspace = session.workspace or "未设置"
    return (
        f"Session {session.session_id} · {session.title} · "
        f"可见消息 {visible} 条（存储 {stored} 条） · {summary}\n"
        f"Workspace: {workspace}"
    )


def format_goal_status(runtime: AgentRuntime) -> str:
    """Render the persisted Goal/Spec state without exposing raw JSON."""
    goal = normalize_goal(getattr(runtime.store.current, "goal_state", None))
    if not goal:
        return "当前 Session 暂无进行中的 Goal/Spec 任务。"
    spec = goal.get("spec") or {}
    lines = [
        f"Goal {goal.get('goalId')} · 状态：{goal.get('status')}",
        f"目标：{goal.get('objective')}",
        f"当前步骤：{goal.get('currentStep')}",
        "验收条件：",
    ]
    lines.extend(f"  - {item}" for item in spec.get("acceptanceCriteria") or ["无"])
    lines.append("已完成：")
    lines.extend(f"  - {item}" for item in goal.get("completedSteps") or ["无"])
    lines.append("下一步：")
    lines.extend(f"  - {item}" for item in goal.get("nextActions") or ["无"])
    return "\n".join(lines)


def _latest_visible_user_message(session):
    """Return ``(index, content)`` for the last real user message."""
    for index in range(len(session.messages) - 1, -1, -1):
        message = session.messages[index]
        if (
            message.get("role") == "user"
            and not is_internal_message(message)
            and str(message.get("content") or "").strip()
        ):
            return index, str(message["content"])
    return None, None


def _get_attachment_store(runtime):
    store = getattr(runtime, "attachment_store", None)
    if store is not None:
        return store
    return AttachmentStore(runtime.store)


def handle_attachment_command(runtime: AgentRuntime, command: str) -> str:
    """List or inspect attachments already uploaded to the active Session."""
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"命令格式错误：{exc}") from exc
    if len(parts) == 1:
        return ATTACHMENT_HELP
    action = parts[1].lower()
    if action not in {"list", "show"}:
        return ATTACHMENT_HELP
    store = _get_attachment_store(runtime)
    session_id = runtime.store.current_id
    if action == "list":
        if len(parts) != 2:
            raise ValueError("用法：/attachment list")
        items = store.list(session_id)
        if not items:
            return "当前 Session 没有附件。"
        lines = [f"Attachments ({len(items)}):"]
        for item in items:
            available = "可用" if item.get("available") else "磁盘文件缺失"
            size = item.get("size")
            size_text = f" · {size} B" if size is not None else ""
            lines.append(
                f"- {item.get('attachmentId', 'unknown')} · "
                f"{item.get('filename', 'attachment')} · {available}{size_text}"
            )
        return "\n".join(lines)
    if len(parts) != 3:
        raise ValueError("用法：/attachment show <attachmentId>")
    metadata, path = store.get(session_id, parts[2])
    details = {
        "attachmentId": metadata.get("attachmentId"),
        "filename": metadata.get("filename"),
        "contentType": metadata.get("contentType"),
        "size": metadata.get("size"),
        "uploadedAt": metadata.get("uploadedAt"),
        "available": path.is_file(),
    }
    return json.dumps(details, ensure_ascii=False, indent=2)


def _visible_messages_for_export(session):
    return [
        item
        for item in session.messages
        if item.get("role") in {"user", "assistant"} and not is_internal_message(item)
    ]


def _session_export_markdown(session) -> str:
    lines = [
        f"# {session.title or session.session_id}",
        "",
        f"- Session: `{session.session_id}`",
        f"- Updated: {_format_beijing_time(session.updated_at)}",
        f"- Visible messages: {visible_message_count(session.messages)}",
        f"- Workspace: `{session.workspace or '未设置'}`",
        "",
    ]
    if session.summary.strip():
        lines.extend(["## Session Summary", "", session.summary.strip(), ""])
    if session.attachments:
        lines.extend(["## Attachments", ""])
        for item in session.attachments:
            lines.append(
                f"- `{item.get('attachmentId', 'unknown')}` "
                f"{item.get('filename', 'attachment')} ({item.get('size', '?')} B)"
            )
        lines.append("")
    lines.append("## Conversation")
    lines.append("")
    for index, item in enumerate(_visible_messages_for_export(session), start=1):
        role = "User" if item.get("role") == "user" else "Assistant"
        lines.extend([f"### {index}. {role}", "", str(item.get("content") or ""), ""])
    return "\n".join(lines).rstrip() + "\n"


def _safe_export_path(runtime, requested: str | None, extension: str) -> Path:
    session = runtime.store.current
    export_root = (runtime.store.data_dir / "exports").resolve()
    export_root.mkdir(parents=True, exist_ok=True)
    if not requested:
        session_id = str(session.session_id)
        # Session IDs already use the ``session_<n>`` form.  Do not prepend
        # the label a second time when constructing the default filename.
        stem = (
            session_id
            if re.match(r"^session_", session_id, re.IGNORECASE)
            else f"session_{session_id}"
        )
        return export_root / f"{stem}.{extension}"
    candidate = Path(requested)
    if candidate.is_absolute():
        workspace = Path(session.workspace).resolve() if session.workspace else None
        resolved = candidate.resolve()
        if workspace is None:
            raise ValueError("绝对导出路径需要先设置当前 Session 的 Workspace。")
        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("导出路径必须位于当前 Workspace 内。") from exc
        return resolved
    resolved = (export_root / candidate).resolve()
    try:
        resolved.relative_to(export_root)
    except ValueError as exc:
        raise ValueError("导出路径不能跳出 data/exports。") from exc
    return resolved


def handle_export_command(runtime: AgentRuntime, command: str) -> str:
    """Export the active Session without exposing internal protocol noise in Markdown."""
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"命令格式错误：{exc}") from exc
    args = parts[1:]
    export_format = "markdown"
    if args and args[0].lower() in {"md", "markdown", "json"}:
        export_format = "json" if args.pop(0).lower() == "json" else "markdown"
    if len(args) > 1:
        raise ValueError("用法：/export [markdown|json] [相对路径]")
    extension = "json" if export_format == "json" else "md"
    path = _safe_export_path(runtime, args[0] if args else None, extension)
    session = runtime.store.current
    if export_format == "json":
        payload = {
            "exportedAt": utc_now(),
            "session": session.to_dict(),
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    else:
        content = _session_export_markdown(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"已导出 Session：{path} · {len(content.encode('utf-8'))} B"


def format_turn_metrics(turn) -> str:
    metrics = list(getattr(turn, "metrics", None) or [])
    tool_events = list(getattr(turn, "tool_events", None) or [])
    duration = sum(float(item.get("durationMs") or 0) for item in metrics)
    input_tokens = sum(int(item.get("inputTokens") or 0) for item in metrics)
    output_tokens = sum(int(item.get("outputTokens") or 0) for item in metrics)
    token_text = (
        f" · tokens {input_tokens}/{output_tokens}"
        if input_tokens or output_tokens
        else ""
    )
    return (
        f"[metrics] model calls={len(metrics)} · tools={len(tool_events)} · "
        f"duration={round(duration)} ms{token_text}"
    )


def handle_session_command(runtime: AgentRuntime, command: str) -> str:
    """处理内部 Session 命令；这些内容永远不会发送给 LLM。"""
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"命令格式错误：{exc}") from exc

    if len(parts) < 2:
        return SESSION_HELP

    action = parts[1].lower()
    store = runtime.store

    if action == "new":
        title = " ".join(parts[2:]) or None
        session = store.create(title)
        return f"已创建并切换到：{session.session_id}（{session.title}）"

    if action == "list":
        if len(parts) != 2:
            raise ValueError("用法：/session list")
        lines = ["Sessions:"]
        current_id = store.current_id
        for session in store.list_sessions():
            marker = "*" if session.session_id == current_id else " "
            lines.append(
                f"{marker} {session.session_id}  {session.title}  "
                f"messages={visible_message_count(session.messages)}  updated={_format_beijing_time(session.updated_at)}"
            )
        return "\n".join(lines)

    if action in {"show", "view"}:
        if len(parts) > 3:
            raise ValueError("用法：/session show [sessionId]")
        session = store.get(parts[2]) if len(parts) == 3 else store.current
        return format_session_detail(session)

    if action == "switch":
        if len(parts) != 3:
            raise ValueError("用法：/session switch <sessionId>")
        store.set_current_id(parts[2])
        return f"已切换到：{parts[2]}（{store.current.title}）"

    if action == "rename":
        if len(parts) < 4:
            raise ValueError("用法：/session rename <sessionId> <标题>")
        session = store.rename(parts[2], " ".join(parts[3:]))
        return f"已重命名：{session.session_id}（{session.title}）"

    if action == "delete":
        if len(parts) != 3:
            raise ValueError("用法：/session delete <sessionId>")
        deleted_id = parts[2]
        current_id = store.delete(deleted_id)
        return f"已删除：{deleted_id}；当前 Session：{current_id}"

    return SESSION_HELP


def handle_memory_command(memory_store: MemoryStore, command: str) -> str:
    """处理手动 Memory 管理；普通聊天不能修改 Memory。"""
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"命令格式错误：{exc}") from exc

    if len(parts) < 2:
        return MEMORY_HELP
    action = parts[1].lower()

    if action == "add":
        if len(parts) < 3:
            raise ValueError("用法：/memory add <长期信息>")
        memory = memory_store.add(" ".join(parts[2:]))
        return f"已添加 Memory：{memory.memory_id}"

    if action == "list":
        if len(parts) != 2:
            raise ValueError("用法：/memory list")
        memories = memory_store.list()
        if not memories:
            return "暂无长期 Memory。"
        return "Memories:\n" + "\n".join(
            f"- {item.memory_id}  [{item.memory_type}/importance={item.importance}] "
            f"{item.content}  updated={_format_beijing_time(item.updated_at)}" for item in memories
        )

    if action in {"find", "search"}:
        if len(parts) < 3:
            raise ValueError("用法：/memory find <关键词>")
        memories = memory_store.search(" ".join(parts[2:]))
        return "Memory 搜索结果:\n" + "\n".join(
            f"- {item.memory_id}  {item.content}" for item in memories
        )

    if action == "update":
        if len(parts) < 4:
            raise ValueError("用法：/memory update <memoryId> <新内容>")
        memory = memory_store.update(parts[2], content=" ".join(parts[3:]))
        return f"已更新 Memory：{memory.memory_id}"

    if action == "delete":
        if len(parts) < 3:
            raise ValueError("用法：/memory delete <memoryId|记忆描述>")
        target = " ".join(parts[2:])
        memory = memory_store.delete(target) if target.startswith("mem_") else memory_store.delete_by_text(target)
        return f"已删除 Memory：{memory.memory_id}"

    return MEMORY_HELP


def _format_beijing_time(value: str) -> str:
    """Render stored ISO timestamps consistently in Asia/Shanghai time."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S 北京时间")
    except (TypeError, ValueError):
        return value


def handle_workspace_command(runtime: AgentRuntime, command: str) -> str:
    manager = getattr(runtime, "workspace_manager", None)
    if manager is None:
        raise ValueError("Workspace Manager 未配置。")
    # shlex defaults to POSIX escaping and turns a Windows path such as
    # D:\SJTU\project into D:SJTUproject. Parse this command's path as an
    # opaque remainder instead, preserving backslashes and spaces.
    stripped = command.strip()
    parts = stripped.split(maxsplit=2)
    if len(parts) < 2:
        return WORKSPACE_HELP
    if parts[1] == "show" and len(parts) == 2:
        workspace = runtime.store.current.workspace
        return f"当前 Workspace：{workspace or '未设置'}"
    if parts[1] == "migrate" and len(parts) == 2:
        result = manager.migrate(runtime.store.current_id)
        return f"Workspace 已迁移：{result['previousWorkspace']} -> {result['workspace']}"
    if parts[1] == "set" and len(parts) == 3:
        path = parts[2].strip()
        if len(path) >= 2 and path[0] == path[-1] and path[0] in {'"', "'"}:
            path = path[1:-1]
        if not path:
            return WORKSPACE_HELP
        return f"已设置 Workspace：{manager.set(runtime.store.current_id, path)}"
    return WORKSPACE_HELP


def handle_model_command(runtime: AgentRuntime, command: str) -> str:
    """Inspect or switch among models configured on the current API."""
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"命令格式错误：{exc}") from exc
    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == "list"):
        current = getattr(runtime.model, "model", "未知")
        lines = ["当前 API 的可选模型："]
        for model_id, label in SJTU_API_MODELS.items():
            marker = "*" if model_id == current else " "
            lines.append(f"{marker} {model_id:<19} {label}")
        lines.append("使用 /model use <model-id> 切换。")
        return "\n".join(lines)
    if len(parts) == 3 and parts[1].lower() in {"use", "set", "switch"}:
        selected = normalize_sjtu_model(parts[2])
        profile = runtime.select_model(selected)
        return (
            f"已切换模型：{profile['model']}（{SJTU_API_MODELS[profile['model']]}）。\n"
            "新的模型将从下一轮对话开始生效。"
        )
    return MODEL_HELP


def handle_skill_info_command(runtime: AgentRuntime, command: str) -> str | None:
    registry = getattr(runtime, "skill_registry", None)
    if registry is None:
        raise ValueError("Skill Registry 未配置。")
    parts = shlex.split(command)
    if len(parts) < 2:
        return SKILL_HELP
    action = parts[1]
    if action == "list" and len(parts) == 2:
        return "Skills:\n" + "\n".join(
            f"- {item.name}: {item.description}" for item in registry.list()
        )
    if action == "show" and len(parts) == 3:
        info = registry.get(parts[2]).basic_info()
        return json.dumps(info, ensure_ascii=False, indent=2)
    if action == "usage" and len(parts) == 2:
        usage = runtime.store.current.skill_usage
        return "Skill Usage:\n" + (
            json.dumps(usage, ensure_ascii=False, indent=2) if usage else "暂无记录。"
        )
    if action in {"list", "show", "usage"}:
        return SKILL_HELP
    return None


def resolve_cli_approvals(runtime, turn, input_fn, output_fn, event_callback=None):
    """在 CLI 中逐项收集审批，并让 Runtime 从暂停点继续。"""
    events = list(turn.tool_events)
    while turn.pending_approvals:
        approval = turn.pending_approvals[0]
        if hasattr(event_callback, "flush_for_status"):
            event_callback.flush_for_status()
        # AgentRuntime already emitted an approval event to the stream
        # renderer.  Do not print a second full card; the legacy line remains
        # for adapters that do not support callbacks (and for old tests).
        if not hasattr(event_callback, "on_event"):
            output_fn(
                "\n[approval_required] 🔐 "
                f"{approval['tool']} · 等待审批 ({approval['approvalId']})\n"
                f"  参数：{json.dumps(approval['args'], ensure_ascii=False)}"
            )
        decision = _read_cli_input(input_fn, "批准执行？[y/N] ").strip().lower() in {"y", "yes"}
        reason = None
        if not decision:
            reason = _read_cli_input(input_fn, "拒绝原因（可留空）：").strip() or None
        kwargs = {}
        if event_callback is not None and _supports_keyword(runtime.resolve_approval, "event_callback"):
            callback = getattr(event_callback, "on_event", event_callback)
            kwargs["event_callback"] = callback
        turn = runtime.resolve_approval(approval["approvalId"], decision, reason, **kwargs)
        events.extend(turn.tool_events)
    return turn, events


def _run_cli_runtime_turn(runtime, message, renderer, source="cli", session_id=None):
    kwargs = {"source": source}
    if session_id is not None and _supports_keyword(runtime.run, "session_id"):
        kwargs["session_id"] = session_id
    if _supports_keyword(runtime.run, "event_callback"):
        kwargs["event_callback"] = renderer.on_event
    return runtime.run(message, **kwargs)


def _run_cli_replay(runtime, renderer):
    session = runtime.store.current
    message_index, message = _latest_visible_user_message(session)
    if message_index is None:
        raise ValueError("当前 Session 没有可重试的用户消息。")
    if not hasattr(runtime, "replay"):
        raise RuntimeError("当前 Runtime 不支持重试。")
    kwargs = {"source": "cli"}
    if _supports_keyword(runtime.replay, "event_callback"):
        kwargs["event_callback"] = renderer.on_event
    return runtime.replay(session.session_id, message_index, **kwargs)


def run_cli(
    runtime: AgentRuntime,
    memory_store: MemoryStore | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    verbose_tools: bool = False,
    show_metrics: bool = False,
) -> int:
    # Start in a local draft.  The persisted current Session is deliberately
    # not reused for ordinary input; the first user message below creates a
    # fresh Session and switches to it.  This avoids producing empty Sessions
    # merely because the CLI was opened.
    active_session_id = None
    _configure_cli_output_encoding(output_fn)
    _configure_cli_readline(input_fn)
    theme = CliTheme.detect(output_fn)
    output_fn("SJTUClaw started. 新对话（发送第一条消息后创建 Session）")
    output_fn("")
    output_fn(theme.text(f"🦞 {quote_for_day()}"))
    output_fn("")
    memory_store = memory_store or runtime.context_builder.memory_store
    output_fn(CLI_START_HINT)
    output_fn("")
    last_turn = None

    while True:
        try:
            user_input = _read_cli_input(input_fn, "\nUser> ").strip()
        except (EOFError, StopIteration):
            output_fn("\nbye.")
            return 0
        except KeyboardInterrupt:
            output_fn("\n输入已取消；可继续输入，或使用 /exit 退出。")
            continue

        if user_input.lower() in EXIT_COMMANDS:
            output_fn("bye.")
            return 0
        if not user_input:
            continue

        if _is_shell_environment_injection(user_input):
            output_fn("[system] 已忽略 VS Code/Conda 注入的环境切换命令；请在 CLI 外部终端切换环境。")
            continue

        if user_input.lower() == "/?" or user_input.split(maxsplit=1)[0].lower() == "/help":
            try:
                output_fn(handle_help_command("/help" if user_input.lower() == "/?" else user_input))
            except ValueError as exc:
                output_fn(f"Help 查询失败：{exc}")
            continue

        if user_input.lower() == "/clear":
            clear_cli_screen(output_fn)
            continue

        if user_input.lower().startswith("/history"):
            try:
                output_fn(handle_history_command(runtime, user_input))
            except (KeyError, ValueError, OSError) as exc:
                output_fn(f"History 查询失败：{exc}")
            continue

        if user_input.lower() == "/paste":
            try:
                user_input = collect_multiline_input(input_fn, output_fn)
            except KeyboardInterrupt:
                output_fn("\n多行输入已取消；没有发送消息。")
                continue
            if not user_input:
                output_fn("[system] 多行输入为空，没有发送消息。")
                continue

        if user_input.lower().startswith("/session"):
            try:
                output_fn(handle_session_command(runtime, user_input))
                # Explicit Session commands leave the draft state.  A switch
                # or an explicit /session new is a user decision, so the next
                # ordinary message should continue in that Session instead
                # of creating another one.
                session_parts = user_input.split()
                if len(session_parts) >= 2 and session_parts[1].lower() in {"switch", "new"}:
                    active_session_id = runtime.store.current_id
                elif len(session_parts) >= 2 and session_parts[1].lower() == "delete":
                    active_session_id = runtime.store.current_id
            except (KeyError, ValueError, OSError) as exc:
                output_fn(f"Session 操作失败：{exc}")
            continue

        if user_input.lower().startswith("/memory"):
            if memory_store is None:
                output_fn("Memory 操作失败：MemoryStore 未配置。")
                continue
            try:
                output_fn(handle_memory_command(memory_store, user_input))
            except (KeyError, ValueError, OSError) as exc:
                output_fn(f"Memory 操作失败：{exc}")
            continue

        if user_input.lower().startswith("/workspace"):
            try:
                output_fn(handle_workspace_command(runtime, user_input))
            except (KeyError, ValueError, OSError) as exc:
                output_fn(f"Workspace 操作失败：{exc}")
            continue

        if user_input.lower().startswith("/model"):
            try:
                output_fn(handle_model_command(runtime, user_input))
            except (KeyError, ValueError, OSError, RuntimeError) as exc:
                output_fn(f"模型切换失败：{exc}")
            continue

        if user_input.lower().startswith("/skill"):
            renderer = CliStreamRenderer(output_fn, verbose_tools, show_metrics)
            try:
                info = handle_skill_info_command(runtime, user_input)
                if info is not None:
                    output_fn(info)
                    continue
                parts = shlex.split(user_input)
                if len(parts) < 3:
                    output_fn(SKILL_HELP)
                    continue
                skill_kwargs = {"source": "cli"}
                if _supports_keyword(runtime.run_skill, "event_callback"):
                    skill_kwargs["event_callback"] = renderer.on_event
                turn = runtime.run_skill(parts[1], " ".join(parts[2:]), **skill_kwargs)
                turn, turn_events = resolve_cli_approvals(
                    runtime, turn, input_fn, output_fn, renderer
                )
                last_turn = turn
                if not renderer.seen_event:
                    replay_cli_tool_events(renderer, turn_events)
                renderer.finish(turn.reply)
            except (KeyError, ValueError, OSError, RuntimeError) as exc:
                output_fn(f"Skill 操作失败：{exc}")
            continue

        if user_input.lower() == "/compact":
            try:
                output_fn("[system] 正在整理上下文并生成 Summary，请稍候…")
                if output_fn is print:
                    sys.stdout.flush()
                result = runtime.compact_current()
                if result is None:
                    output_fn("无需压缩：当前 Session 没有足够的旧消息。")
                else:
                    output_fn(format_compaction_result(result))
            except CompactionError as exc:
                output_fn(f"Compaction 失败：{exc}")
            continue

        if user_input.lower() in {"/goal", "/spec"}:
            output_fn(format_goal_status(runtime))
            continue

        if user_input.lower() in {"/attachment", "/attachments"} or user_input.lower().startswith("/attachment "):
            command = "/attachment list" if user_input.lower() == "/attachments" else user_input
            try:
                output_fn(handle_attachment_command(runtime, command))
            except (KeyError, ValueError, OSError) as exc:
                output_fn(f"Attachment 操作失败：{exc}")
            continue

        if user_input.lower().startswith("/export"):
            try:
                output_fn(handle_export_command(runtime, user_input))
            except (KeyError, ValueError, OSError) as exc:
                output_fn(f"Export 操作失败：{exc}")
            continue

        if user_input.lower() == "/metrics":
            output_fn(format_turn_metrics(last_turn) if last_turn is not None else "[metrics] 当前还没有完成的 Turn。")
            continue

        if user_input.lower() in {"/context", "/status"}:
            try:
                output_fn(format_cli_context_status(runtime))
            except (KeyError, ValueError, OSError) as exc:
                output_fn(f"Context 查询失败：{exc}")
            continue

        if user_input.lower() in {"/stop", "/cancel"}:
            output_fn(
                "当前 CLI 没有后台并行任务；如果模型正在生成，请按 Ctrl+C 停止本轮。"
            )
            continue

        if user_input.lower() in {"/retry", "/regenerate"}:
            renderer = CliStreamRenderer(output_fn, verbose_tools, show_metrics)
            try:
                turn = _run_cli_replay(runtime, renderer)
                turn, turn_events = resolve_cli_approvals(
                    runtime, turn, input_fn, output_fn, renderer
                )
                last_turn = turn
            except KeyboardInterrupt:
                renderer.abort()
                output_fn("\n本次重试已中断，可以继续输入。")
            except Exception as exc:
                renderer.abort()
                output_fn(f"重试失败：{exc}")
            else:
                if not renderer.seen_event:
                    replay_cli_tool_events(renderer, turn_events)
                renderer.finish(turn.reply)
            continue

        if user_input.startswith("/"):
            command = user_input.split(maxsplit=1)[0]
            matches = difflib.get_close_matches(command.lower(), CLI_COMMANDS, n=1, cutoff=0.62)
            suggestion = f" 你是不是想输入 {matches[0]}？" if matches else ""
            output_fn(
                f"未知命令：{command}。{suggestion} 输入 /help 查看可用命令；"
                "本条未发送给模型。"
            )
            continue

        renderer = CliStreamRenderer(output_fn, verbose_tools, show_metrics)
        try:
            if active_session_id is None:
                if hasattr(runtime.store, "create"):
                    previous = runtime.store.current
                    session = runtime.store.create("新会话", make_current=True)
                    if getattr(previous, "workspace", None):
                        session.workspace = previous.workspace
                        runtime.store.save(session)
                    active_session_id = session.session_id
                    output_fn(f"[system] 已创建 Session：{active_session_id}")
                else:
                    # Lightweight test/embedded stores may not expose the
                    # persistence API.  Preserve their legacy current-session
                    # behavior rather than making the CLI unusable.
                    active_session_id = getattr(runtime.store, "current_id", None)
            turn = _run_cli_runtime_turn(runtime, user_input, renderer, session_id=active_session_id)
            turn, turn_events = resolve_cli_approvals(
                runtime, turn, input_fn, output_fn, renderer
            )
            last_turn = turn
        except KeyboardInterrupt:
            renderer.abort()
            output_fn("\n本次请求已中断，可以继续输入。")
        except Exception as exc:
            renderer.abort()
            output_fn(f"调用失败：{exc}")
        else:
            if not renderer.seen_event:
                replay_cli_tool_events(renderer, turn_events)
            renderer.finish(turn.reply)
            if turn.compaction is not None and not renderer.seen_event:
                output_fn(format_compaction_result(turn.compaction))
            elif turn.compaction_error and not renderer.seen_event:
                output_fn(f"[system] Compaction 失败：{turn.compaction_error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SJTUClaw CLI")
    parser.add_argument(
        "--once",
        nargs="?",
        const=DEFAULT_ONCE_PROMPT,
        help="发送一条消息并打印 assistant 回复；不进入交互式 CLI。",
    )
    parser.add_argument(
        "--verbose-tools",
        action="store_true",
        help="打印 Tool 的完整原始 JSON 结果，适用于调试；默认仅显示简洁摘要。",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help="在每轮回答后显示模型调用、Tool 和 Token 指标。",
    )
    return parser


def run_once(
    runtime: AgentRuntime,
    message: str,
    output_fn: Callable[[str], None] = print,
    verbose_tools: bool = False,
    show_metrics: bool = False,
) -> int:
    renderer = CliStreamRenderer(output_fn, verbose_tools, show_metrics)
    session_id = None
    if hasattr(runtime.store, "create"):
        previous = runtime.store.current
        session = runtime.store.create("新会话", make_current=True)
        if getattr(previous, "workspace", None):
            session.workspace = previous.workspace
            runtime.store.save(session)
        session_id = session.session_id
        output_fn(f"[system] 已创建 Session：{session_id}")
    else:
        session_id = getattr(runtime.store, "current_id", None)
    turn = _run_cli_runtime_turn(runtime, message, renderer, session_id=session_id)
    renderer.finish(turn.reply)
    if not renderer.seen_event and not turn.reply:
        output_fn("Assistant 回复为空。")
    # ``--once`` is also a CLI surface: do not hide the compaction result
    # behind the interactive loop when an automatic compaction happened.
    if turn.compaction is not None and not renderer.seen_event:
        output_fn(format_compaction_result(turn.compaction))
    elif turn.compaction_error and not renderer.seen_event:
        output_fn(f"[system] Compaction 失败：{turn.compaction_error}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runtime, memory_store = build_runtime()
    except Exception as exc:
        print(f"启动失败：{exc}")
        return 1
    if args.once is not None:
        try:
            return run_once(
                runtime,
                args.once,
                verbose_tools=args.verbose_tools,
                show_metrics=args.metrics,
            )
        except Exception as exc:
            print(f"调用失败：{exc}")
            return 1
    return run_cli(
        runtime,
        memory_store,
        verbose_tools=args.verbose_tools,
        show_metrics=args.metrics,
    )


if __name__ == "__main__":
    raise SystemExit(main())
