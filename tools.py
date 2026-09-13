"""Step 5：只读 Tool、Registry 与执行边界。

``Tool`` 描述名称、JSON Schema、安全级别和处理函数；``ToolRegistry`` 负责
参数校验、超时、并行执行和统一 ``ToolResult``。本模块注册时间、目录、文件等
只读 Tool，Workspace 写入和 Shell 被明确留到 Step 8 的审批链路。
"""

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
from queue import Queue
from pathlib import Path
import re
from threading import Thread
import time
from typing import Any, Callable

from math_tools import calculate
from symbolic_math import symbolic_math


@dataclass(frozen=True)
class ToolExecutionContext:
    """一次 Tool 执行所需的 Session 上下文。"""

    session_id: str


@dataclass(frozen=True)
class ToolResult:
    """Tool 的统一成功/失败结果，可回灌模型并展示给用户。"""

    tool: str
    success: bool
    output: Any = None
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False
    attempts: int = 1
    duration_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "errorCode": self.error_code,
            "retryable": self.retryable,
            "attempts": self.attempts,
            "durationMs": self.duration_ms,
        }


@dataclass(frozen=True)
class Tool:
    """一个 Tool 的协议描述、安全属性和 Python handler。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]
    safety_level: str = "read_only"
    contextual: bool = False
    # Execution metadata is deliberately explicit. ``None`` for side_effect
    # keeps old Tool(...) declarations safe-level-aware without requiring a
    # mass migration of existing registrations.
    parallel_safe: bool = True
    side_effect: bool | None = None
    retryable: bool = False
    max_attempts: int = 1
    timeout_seconds: float | None = None
    idempotent: bool = False

    @property
    def effective_side_effect(self) -> bool:
        return self.side_effect if self.side_effect is not None else self.safety_level != "read_only"

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "safety_level": self.safety_level,
            "parallel_safe": self.parallel_safe,
            "side_effect": self.effective_side_effect,
            "retryable": self.retryable,
            "max_attempts": self.max_attempts,
            "timeout_seconds": self.timeout_seconds,
            "idempotent": self.idempotent,
        }


class ToolRegistry:
    """注册、公开 Schema，并在统一边界内执行 Tool。"""

    def __init__(self, max_execution_seconds: float | None = None):
        self._tools: dict[str, Tool] = {}
        self.max_execution_seconds = max_execution_seconds

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool 已注册：{tool.name}")
        if tool.safety_level not in {"read_only", "approval_required", "download"}:
            raise ValueError(f"未知 Tool safety level：{tool.safety_level}")
        if not isinstance(tool.parallel_safe, bool):
            raise ValueError(f"Tool {tool.name} parallel_safe 必须是 boolean。")
        if not isinstance(tool.retryable, bool) or not isinstance(tool.idempotent, bool):
            raise ValueError(f"Tool {tool.name} retryable/idempotent 必须是 boolean。")
        if isinstance(tool.max_attempts, bool) or not isinstance(tool.max_attempts, int) or not 1 <= tool.max_attempts <= 3:
            raise ValueError(f"Tool {tool.name} max_attempts 必须在 1 到 3 之间。")
        if tool.timeout_seconds is not None and tool.timeout_seconds <= 0:
            raise ValueError(f"Tool {tool.name} timeout_seconds 必须大于 0。")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition() for tool in self._tools.values()]

    def execute(
        self,
        name: str,
        args: Any,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(name, False, error=f"未知 Tool：{name}", error_code="unknown_tool")
        started = time.perf_counter()
        try:
            validated = self.validate_args(tool, args)
        except Exception as exc:
            return self._error_result(name, exc, "invalid_args", started)

        # Never blindly retry a side-effecting handler. A caller may opt into
        # retries only when it has explicitly declared the operation idempotent.
        attempts_limit = tool.max_attempts if tool.retryable and (
            not tool.effective_side_effect or tool.idempotent
        ) else 1
        last_error: tuple[Exception, str] | None = None
        for attempt in range(1, attempts_limit + 1):
            try:
                if tool.contextual:
                    if context is None:
                        raise ValueError(f"Tool {name} 缺少执行上下文。")
                    output = self._call_handler(
                        tool.handler, context, execution_timeout=tool.timeout_seconds,
                        **validated,
                    )
                else:
                    output = self._call_handler(
                        tool.handler, execution_timeout=tool.timeout_seconds,
                        **validated,
                    )
                if isinstance(output, dict) and output.get("success") is False:
                    error = str(output.get("error") or "Tool 返回失败。")
                    last_error = (RuntimeError(error), "handler_error")
                    if attempt < attempts_limit:
                        continue
                    return ToolResult(
                        name, False, output=output, error=_redact_error(error),
                        error_code=str(output.get("errorCode") or "handler_error"),
                        retryable=tool.retryable, attempts=attempt,
                        duration_ms=_elapsed_ms(started),
                    )
                return ToolResult(
                    name, True, output=output, attempts=attempt,
                    duration_ms=_elapsed_ms(started),
                )
            except TimeoutError as exc:
                last_error = (exc, "timeout")
            except Exception as exc:
                last_error = (exc, "handler_error")
            if attempt < attempts_limit:
                continue
        exc, code = last_error or (RuntimeError("Tool 执行失败。"), "handler_error")
        return self._error_result(
            name, exc, code, started, attempts=attempts_limit,
            retryable=tool.retryable,
        )

    def execute_many(
        self,
        calls: list[tuple[str, Any]],
        context: ToolExecutionContext | None = None,
        max_workers: int = 4,
    ) -> list[ToolResult]:
        """Execute a batch while preserving safety and caller order.

        Only explicitly parallel-safe, non-side-effecting Tools run together.
        Any batch containing an approval/write/download Tool falls back to the
        ordinary serial path, so this helper cannot accidentally bypass the
        Runtime's approval boundary.
        """
        if not calls:
            return []
        bounded_workers = max(1, min(int(max_workers), 5, len(calls)))
        eligible = all(
            (self.get(name) is not None)
            and self.get(name).parallel_safe
            and not self.get(name).effective_side_effect
            and self.get(name).safety_level == "read_only"
            for name, _ in calls
        )
        if not eligible or bounded_workers == 1:
            return [self.execute(name, args, context) for name, args in calls]
        results: list[ToolResult | None] = [None] * len(calls)
        with ThreadPoolExecutor(max_workers=bounded_workers, thread_name_prefix="sjtu-tool") as pool:
            futures = {
                pool.submit(self.execute, name, args, context): index
                for index, (name, args) in enumerate(calls)
            }
            for future in as_completed(futures):
                index = futures[future]
                results[index] = future.result()
        return [item for item in results if item is not None]

    def _call_handler(
        self, handler: Callable[..., Any], *args,
        execution_timeout: float | None = None, **kwargs,
    ) -> Any:
        timeout = execution_timeout if execution_timeout is not None else self.max_execution_seconds
        if timeout is None:
            return handler(*args, **kwargs)
        if timeout <= 0:
            raise TimeoutError("Tool 执行超时时间必须大于 0。")
        result_queue: Queue[tuple[bool, Any]] = Queue(maxsize=1)

        def target() -> None:
            try:
                result_queue.put((True, handler(*args, **kwargs)))
            except Exception as exc:
                result_queue.put((False, exc))

        thread = Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(f"Tool 执行超过 {timeout:g} 秒，已停止等待。")
        ok, value = result_queue.get_nowait()
        if ok:
            return value
        raise value

    @staticmethod
    def _error_result(
        name: str,
        exc: Exception,
        code: str,
        started: float,
        *,
        attempts: int = 1,
        retryable: bool = False,
    ) -> ToolResult:
        return ToolResult(
            name,
            False,
            error=_redact_error(str(exc)),
            error_code=code,
            retryable=retryable,
            attempts=attempts,
            duration_ms=_elapsed_ms(started),
        )

    @staticmethod
    def validate_args(tool: Tool, args: Any) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise ValueError("Tool args 必须是 JSON object。")
        schema = tool.input_schema
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [name for name in required if name not in args]
        if missing:
            raise ValueError(f"缺少必需参数：{missing}")
        unknown = set(args) - set(properties)
        if unknown and schema.get("additionalProperties", True) is False:
            raise ValueError(f"存在未知参数：{sorted(unknown)}")
        type_map = {"string": str, "integer": int, "boolean": bool, "number": (int, float)}
        for name, value in args.items():
            expected = properties.get(name, {}).get("type")
            python_type = type_map.get(expected)
            if expected in {"integer", "number"} and isinstance(value, bool):
                raise ValueError(f"参数 {name} 应为 {expected}。")
            if python_type is not None and not isinstance(value, python_type):
                raise ValueError(f"参数 {name} 应为 {expected}。")
        return args


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|tvly|key|token)[-_][A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~-]+"),
)


def _redact_error(value: str) -> str:
    """Bound and redact provider/credential details before they enter logs."""
    text = str(value or "Tool 执行失败。")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[已脱敏]", text)
    return text[:1_000]


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def current_time() -> dict[str, str]:
    """返回北京时间，为“当前/最新”类任务建立可靠时间基准。"""

    now = datetime.now().astimezone()
    return {"iso": now.isoformat(timespec="seconds"), "timezone": str(now.tzinfo)}


def list_dir(path: str = ".") -> dict[str, Any]:
    """列出 Workspace 内的相对目录，拒绝越界路径。"""

    target = Path(path).expanduser()
    if not target.exists():
        raise FileNotFoundError(f"目录不存在：{target}")
    if not target.is_dir():
        raise NotADirectoryError(f"不是目录：{target}")
    entries = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    limit = 200
    return {
        "path": str(target.resolve()),
        "entries": [
            {
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else None,
            }
            for item in entries[:limit]
        ],
        "truncated": len(entries) > limit,
    }


def read_file(
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> dict[str, Any]:
    """读取 Workspace 内的有界文本文件。"""

    target = Path(path).expanduser()
    if not target.exists():
        raise FileNotFoundError(f"文件不存在：{target}")
    if not target.is_file():
        raise IsADirectoryError(f"不是文件：{target}")
    if start_line < 1:
        raise ValueError("start_line 必须大于等于 1。")
    if end_line is not None and end_line < start_line:
        raise ValueError("end_line 不能小于 start_line。")
    max_bytes = 10 * 1024 * 1024
    with target.open("rb") as f:
        raw = f.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("文本文件超过 read_file 的 10 MB 上限。")
    try:
        full_content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"文件不是 UTF-8 文本：{target}") from exc
    lines = full_content.splitlines(keepends=True)
    requested_end = min(end_line or len(lines), len(lines))
    selected = "".join(lines[start_line - 1:requested_end])
    content = selected[:100_000]
    return {
        "path": str(target.resolve()),
        "content": content,
        "truncated": len(selected) > 100_000,
        "bytes_read": len(content.encode("utf-8")),
        "start_line": start_line,
        "end_line": requested_end,
        "total_lines": len(lines),
    }


def create_read_only_registry(
    advanced_service=None,
    web_search_handler=None,
    weather_handler=None,
    max_execution_seconds: float | None = None,
    github_read_handler=None,
    wolfram_handler=None,
) -> ToolRegistry:
    """创建 Step 5 基线 Registry，并按配置附加搜索、天气等只读 Tool。"""

    registry = ToolRegistry(max_execution_seconds=max_execution_seconds)
    registry.register(
        Tool(
            "current_time",
            "读取运行 SJTUClaw 的计算机当前本地时间和时区。",
            {"type": "object", "properties": {}, "additionalProperties": False},
            current_time,
            parallel_safe=True,
        )
    )
    registry.register(
        Tool(
            "calculate",
            (
                "安全科学计算器。用于可靠计算四则运算、幂和根式、三角/对数、"
                "阶乘与组合、统计函数及变量代入；支持 pi/e/tau，^ 会按幂处理。"
                "组合数可写 comb(n,k)、C(n,k)、combination(n,k) 或 n choose k；"
                "支持复数常量 i/j、2i 写法与负数开方，例如 sqrt(-3)；"
                "复数结果会返回 real、imag 和 formatted。"
                "angle_unit 可选 radian 或 degree。本工具不执行任意 Python，"
                "也不提供自然语言表达式、符号方程求解、求导或积分；这些任务应改用 symbolic_math。"
            ),
            {
                "type": "object",
                "properties": {
                    "expression": {"type": "string"},
                    "variables": {"type": "object"},
                    "precision": {"type": "integer"},
                    "angle_unit": {"type": "string"},
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
            calculate,
            safety_level="read_only",
            parallel_safe=True,
            timeout_seconds=3,
        )
    )
    registry.register(
        Tool(
            "symbolic_math",
            (
                "受限的符号数学工具。用于代数化简 simplify、展开 expand、因式分解 factor、"
                "方程求解 solve、求导 diff、积分 integrate、极限 limit，以及小型矩阵的"
                "行列式 matrix_det、求逆 matrix_inv 和线性方程组 matrix_solve。"
                "expression 支持 ^、隐式乘法（如 2x）和常用数学函数；未知变量必须通过 "
                "variables 声明。返回精确结果、数值近似和 LaTeX。普通数值计算优先使用 calculate。"
            ),
            {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "simplify", "expand", "factor", "solve", "diff",
                            "integrate", "limit", "matrix_det", "matrix_inv",
                            "matrix_solve",
                        ],
                    },
                    "expression": {"type": "string"},
                    "variable": {"type": "string"},
                    "variables": {"type": "array", "items": {"type": "string"}},
                    "order": {"type": "integer"},
                    "point": {"type": "string"},
                    "lower": {"type": "string"},
                    "upper": {"type": "string"},
                    "matrix": {
                        "type": "array",
                        "items": {"type": "array"},
                    },
                    "vector": {"type": "array"},
                    "precision": {"type": "integer"},
                },
                "required": ["operation"],
                "additionalProperties": False,
            },
            symbolic_math,
            safety_level="read_only",
            parallel_safe=True,
            timeout_seconds=8,
        )
    )
    registry.register(
        Tool(
            "list_dir",
            "列出指定目录中的文件和子目录；path 默认为当前项目目录。",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
            advanced_service.list_dir if advanced_service else list_dir,
            contextual=advanced_service is not None,
            parallel_safe=True,
        )
    )
    registry.register(
        Tool(
            "read_file",
            "读取 UTF-8 文本文件；可用 start_line/end_line 精确读取长文件片段，单次结果最多 100000 字符。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            advanced_service.read_file if advanced_service else read_file,
            contextual=advanced_service is not None,
            parallel_safe=True,
        )
    )
    if web_search_handler is not None:
        registry.register(
            Tool(
                "web_search",
                "使用 Tavily 联网搜索最新公开信息，返回答案、来源链接和摘要。",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer"},
                        "search_depth": {"type": "string"},
                        "topic": {"type": "string"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                web_search_handler,
                safety_level="read_only",
                parallel_safe=True,
                retryable=True,
                max_attempts=2,
            )
        )
    if weather_handler is not None:
        registry.register(
            Tool(
                "weather_forecast",
                "使用 Open-Meteo 查询指定地点的当前天气和未来 1-7 天预报。普通天气查询优先使用本工具；它不提供权威台风或灾害预警。",
                {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"},
                        "days": {"type": "integer"},
                        "include_hourly": {"type": "boolean"},
                    },
                    "required": ["location"],
                    "additionalProperties": False,
                },
                weather_handler,
                safety_level="read_only",
                parallel_safe=True,
                retryable=True,
                max_attempts=2,
            )
        )
    if wolfram_handler is not None:
        registry.register(
            Tool(
                "wolfram_query",
                (
                    "使用 Wolfram|Alpha 查询自然语言数学、单位换算、科学计算和"
                    "跨领域计算知识。普通数值计算优先使用 calculate，符号代数"
                    "优先使用 symbolic_math；仅在本地工具不足或用户明确要求"
                    " Wolfram|Alpha 时调用本工具。结果必须注明来自 Wolfram|Alpha。"
                ),
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_chars": {"type": "integer"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                wolfram_handler,
                safety_level="read_only",
                parallel_safe=True,
                retryable=True,
                max_attempts=2,
                timeout_seconds=25,
            )
        )
    if github_read_handler is not None:
        registry.register(
            Tool(
                "github_read",
                "读取 GitHub 公开仓库中的文本文件或目录；仅允许 github.com 官方域名，并限制文件数量与总字节数，不执行仓库代码。大型仓库必须指定具体文件 path（如 README.md），不要用空 path 反复读取整仓库；仅关闭 recursive 不能降低归档下载体积。",
                {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "path": {"type": "string"},
                        "ref": {"type": "string"},
                        "max_files": {"type": "integer"},
                        "max_bytes": {"type": "integer"},
                        "recursive": {"type": "boolean"},
                    },
                    "required": ["repo"],
                    "additionalProperties": False,
                },
                github_read_handler,
                safety_level="read_only",
                parallel_safe=True,
                # The handler already has a codeload fallback for API rate
                # limits. Retrying the same unauthenticated API request only
                # doubles latency and makes the limit more likely.
                retryable=False,
                max_attempts=1,
            )
        )
    return registry


def serialize_tool_result(result: ToolResult) -> str:
    """把 ToolResult 编码为旧式 JSON 协议所需的文本。"""

    return json.dumps(result.to_dict(), ensure_ascii=False)
