"""受限的科学计算 Tool。

只解释一个很小的 Python 表达式子集，不使用 ``eval``，也不允许属性访问、
导入、下标、推导式或用户自定义函数。目标是为 Agent 提供可复现的数值结果，
而不是在本地执行任意 Python。
"""

from __future__ import annotations

import ast
import cmath
import math
import re
import statistics
from typing import Any, Callable


MAX_EXPRESSION_CHARS = 1_000
MAX_AST_NODES = 200
MAX_SEQUENCE_ITEMS = 100
MAX_INTEGER_BITS = 16_384
MAX_POWER_EXPONENT = 10_000
MAX_FACTORIAL_INPUT = 2_000

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REPLACEMENTS = str.maketrans({
    "×": "*",
    "÷": "/",
    "−": "-",
    "π": "pi",
})


class CalculatorError(ValueError):
    """用户表达式不在计算器允许的安全子集内。"""


def calculate(
    expression: str,
    variables: dict[str, int | float | complex] | None = None,
    precision: int = 12,
    angle_unit: str = "radian",
) -> dict[str, Any]:
    """安全计算一个科学计算表达式并返回结构化结果。"""

    if not isinstance(expression, str) or not expression.strip():
        raise CalculatorError("expression 不能为空。")
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise CalculatorError(f"expression 最多允许 {MAX_EXPRESSION_CHARS} 个字符。")
    if isinstance(precision, bool) or not isinstance(precision, int) or not 1 <= precision <= 15:
        raise CalculatorError("precision 必须是 1 到 15 的整数。")
    if angle_unit not in {"radian", "degree"}:
        raise CalculatorError("angle_unit 只能是 radian 或 degree。")

    normalized = expression.translate(_REPLACEMENTS).replace("^", "**").strip()
    # Accept common combinatorics notation while keeping the evaluator's
    # actual whitelist small and explicit.  These aliases are deterministic
    # rewrites, not natural-language evaluation.
    normalized = re.sub(r"\bC\s*\(", "comb(", normalized)
    normalized = re.sub(r"\bcombination\s*\(", "comb(", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"\b(\d+)\s+choose\s+(\d+)\b",
        r"comb(\1, \2)",
        normalized,
        flags=re.IGNORECASE,
    )
    # Mathematical notation commonly writes ``2i`` rather than Python's
    # ``2j``.  Insert multiplication only for a numeric coefficient; the
    # standalone constants i/j are handled by the evaluator.
    normalized = re.sub(r"(?<=\d)\s*([ij])\b", r"*\1", normalized)
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise CalculatorError(f"数学表达式语法错误：{exc.msg}。") from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise CalculatorError(f"表达式过于复杂，最多允许 {MAX_AST_NODES} 个语法节点。")

    evaluator = _SafeEvaluator(variables or {}, angle_unit)
    result = evaluator.evaluate(tree.body)
    _validate_result(result)
    return {
        "expression": expression,
        "normalizedExpression": normalized,
        "result": _serialise_result(result),
        "formatted": _format_result(result, precision),
        "angleUnit": angle_unit,
        "precision": precision,
    }


class _SafeEvaluator:
    def __init__(self, variables: dict[str, int | float | complex], angle_unit: str):
        if not isinstance(variables, dict):
            raise CalculatorError("variables 必须是名称到数字的 JSON object。")
        self.angle_unit = angle_unit
        self.names: dict[str, int | float | complex] = {
            "pi": math.pi,
            "e": math.e,
            "tau": math.tau,
            "i": 1j,
            "j": 1j,
        }
        for name, value in variables.items():
            if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
                raise CalculatorError(f"变量名不合法：{name!r}。")
            if name in self.names or name in self.functions:
                raise CalculatorError(f"变量名与内置名称冲突：{name}。")
            self.names[name] = _number(value, f"变量 {name}")

    @property
    def functions(self) -> dict[str, Callable[..., Any]]:
        return {
            "abs": abs,
            "round": self._round,
            "sqrt": self._sqrt,
            "cbrt": math.cbrt,
            "exp": math.exp,
            "ln": math.log,
            "log": math.log,
            "log2": math.log2,
            "log10": math.log10,
            "sin": self._sin,
            "cos": self._cos,
            "tan": self._tan,
            "asin": self._asin,
            "acos": self._acos,
            "atan": self._atan,
            "sinh": math.sinh,
            "cosh": math.cosh,
            "tanh": math.tanh,
            "floor": math.floor,
            "ceil": math.ceil,
            "degrees": math.degrees,
            "radians": math.radians,
            "factorial": self._factorial,
            "gcd": self._gcd,
            "lcm": self._lcm,
            "comb": self._comb,
            "perm": self._perm,
            "min": self._min,
            "max": self._max,
            "sum": self._sum,
            "mean": self._mean,
            "median": self._median,
            "pstdev": self._pstdev,
            "stdev": self._stdev,
            "hypot": math.hypot,
        }

    def evaluate(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return _number(node.value, "常量")
        if isinstance(node, ast.Name):
            if node.id not in self.names:
                raise CalculatorError(f"未知名称：{node.id}。")
            return self.names[node.id]
        if isinstance(node, (ast.List, ast.Tuple)):
            if len(node.elts) > MAX_SEQUENCE_ITEMS:
                raise CalculatorError(f"序列最多允许 {MAX_SEQUENCE_ITEMS} 项。")
            return [self.evaluate(item) for item in node.elts]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = _scalar(self.evaluate(node.operand))
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            return self._binary(node)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise CalculatorError("只允许直接调用白名单中的数学函数。")
            if node.keywords:
                raise CalculatorError("数学函数暂不接受关键字参数。")
            function = self.functions.get(node.func.id)
            if function is None:
                raise CalculatorError(f"不支持的数学函数：{node.func.id}。")
            args = [self.evaluate(arg) for arg in node.args]
            try:
                return function(*args)
            except CalculatorError:
                raise
            except (ArithmeticError, TypeError, ValueError, statistics.StatisticsError) as exc:
                raise CalculatorError(f"{node.func.id} 参数或定义域错误：{exc}。") from exc
        raise CalculatorError(f"不支持的表达式结构：{type(node).__name__}。")

    def _binary(self, node: ast.BinOp) -> int | float | complex:
        left = _scalar(self.evaluate(node.left))
        right = _scalar(self.evaluate(node.right))
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            if isinstance(node.op, ast.Mod):
                return left % right
            if isinstance(node.op, ast.Pow):
                if abs(right) > MAX_POWER_EXPONENT:
                    raise CalculatorError(
                        f"幂指数绝对值不能超过 {MAX_POWER_EXPONENT}。"
                    )
                result = left ** right
                _validate_result(result)
                return result
        except ZeroDivisionError as exc:
            raise CalculatorError("不能除以 0。") from exc
        except OverflowError as exc:
            raise CalculatorError("计算结果超出可表示范围。") from exc
        raise CalculatorError(f"不支持的运算符：{type(node.op).__name__}。")

    @staticmethod
    def _sqrt(value: Any) -> float | complex:
        number = _scalar(value)
        if isinstance(number, complex) or number < 0:
            return cmath.sqrt(number)
        return math.sqrt(number)

    def _to_radians(self, value: Any) -> float:
        number = _scalar(value)
        if isinstance(number, complex):
            raise CalculatorError("角度制三角函数暂不接受复数参数。")
        return math.radians(number) if self.angle_unit == "degree" else number

    def _from_radians(self, value: float) -> float:
        return math.degrees(value) if self.angle_unit == "degree" else value

    def _sin(self, value: Any) -> float:
        return math.sin(self._to_radians(value))

    def _cos(self, value: Any) -> float:
        return math.cos(self._to_radians(value))

    def _tan(self, value: Any) -> float:
        return math.tan(self._to_radians(value))

    def _asin(self, value: Any) -> float:
        return self._from_radians(math.asin(_scalar(value)))

    def _acos(self, value: Any) -> float:
        return self._from_radians(math.acos(_scalar(value)))

    def _atan(self, value: Any) -> float:
        return self._from_radians(math.atan(_scalar(value)))

    @staticmethod
    def _round(value: Any, digits: Any = 0) -> int | float:
        integer_digits = _integer(digits, "round 的小数位数")
        if not -15 <= integer_digits <= 15:
            raise CalculatorError("round 的小数位数必须在 -15 到 15 之间。")
        return round(_scalar(value), integer_digits)

    @staticmethod
    def _factorial(value: Any) -> int:
        integer = _integer(value, "factorial 参数")
        if not 0 <= integer <= MAX_FACTORIAL_INPUT:
            raise CalculatorError(
                f"factorial 参数必须在 0 到 {MAX_FACTORIAL_INPUT} 之间。"
            )
        return math.factorial(integer)

    @staticmethod
    def _gcd(*values: Any) -> int:
        items = _integer_args(values, "gcd")
        return math.gcd(*items)

    @staticmethod
    def _lcm(*values: Any) -> int:
        items = _integer_args(values, "lcm")
        return math.lcm(*items)

    @staticmethod
    def _comb(n: Any, k: Any) -> int:
        return math.comb(_bounded_nonnegative_int(n, "comb n"), _bounded_nonnegative_int(k, "comb k"))

    @staticmethod
    def _perm(n: Any, k: Any | None = None) -> int:
        n_int = _bounded_nonnegative_int(n, "perm n")
        k_int = None if k is None else _bounded_nonnegative_int(k, "perm k")
        return math.perm(n_int, k_int)

    @staticmethod
    def _min(*values: Any) -> int | float:
        return min(_aggregate_values(values, "min"))

    @staticmethod
    def _max(*values: Any) -> int | float:
        return max(_aggregate_values(values, "max"))

    @staticmethod
    def _sum(*values: Any) -> int | float:
        return sum(_aggregate_values(values, "sum"))

    @staticmethod
    def _mean(*values: Any) -> float:
        return statistics.mean(_aggregate_values(values, "mean"))

    @staticmethod
    def _median(*values: Any) -> float:
        return statistics.median(_aggregate_values(values, "median"))

    @staticmethod
    def _pstdev(*values: Any) -> float:
        return statistics.pstdev(_aggregate_values(values, "pstdev"))

    @staticmethod
    def _stdev(*values: Any) -> float:
        return statistics.stdev(_aggregate_values(values, "stdev"))


def _number(value: Any, label: str) -> int | float | complex:
    if isinstance(value, bool) or not isinstance(value, (int, float, complex)):
        raise CalculatorError(f"{label} 必须是数字。")
    _validate_result(value)
    return value


def _scalar(value: Any) -> int | float | complex:
    return _number(value, "运算参数")


def _integer(value: Any, label: str) -> int:
    number = _scalar(value)
    if isinstance(number, complex):
        if number.imag:
            raise CalculatorError(f"{label} 必须是实整数。")
        number = number.real
    if isinstance(number, float) and not number.is_integer():
        raise CalculatorError(f"{label} 必须是整数。")
    return int(number)


def _bounded_nonnegative_int(value: Any, label: str) -> int:
    integer = _integer(value, label)
    if not 0 <= integer <= 100_000:
        raise CalculatorError(f"{label} 必须在 0 到 100000 之间。")
    return integer


def _integer_args(values: tuple[Any, ...], label: str) -> list[int]:
    items = _flatten(values)
    if not items:
        raise CalculatorError(f"{label} 至少需要一个参数。")
    return [_integer(value, f"{label} 参数") for value in items]


def _aggregate_values(values: tuple[Any, ...], label: str) -> list[int | float]:
    items = _flatten(values)
    if not items:
        raise CalculatorError(f"{label} 至少需要一个参数。")
    if len(items) > MAX_SEQUENCE_ITEMS:
        raise CalculatorError(f"{label} 最多允许 {MAX_SEQUENCE_ITEMS} 个数。")
    result = [_scalar(value) for value in items]
    if any(isinstance(value, complex) for value in result):
        raise CalculatorError(f"{label} 暂不接受复数参数。")
    return result


def _flatten(values: tuple[Any, ...]) -> list[Any]:
    if len(values) == 1 and isinstance(values[0], list):
        return values[0]
    if any(isinstance(value, list) for value in values):
        raise CalculatorError("序列参数必须作为唯一参数传入。")
    return list(values)


def _validate_result(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float, complex, list)):
        raise CalculatorError("计算结果不是受支持的数字或数列。")
    if isinstance(value, int):
        if value.bit_length() > MAX_INTEGER_BITS:
            raise CalculatorError("整数结果过大，已停止计算。")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CalculatorError("计算结果不是有限实数。")
        return
    if isinstance(value, complex):
        if not math.isfinite(value.real) or not math.isfinite(value.imag):
            raise CalculatorError("计算结果不是有限复数。")
        return
    if len(value) > MAX_SEQUENCE_ITEMS:
        raise CalculatorError("结果序列过长。")
    for item in value:
        _validate_result(item)


def _format_result(value: Any, precision: int) -> str:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value == 0:
            return "0"
        return format(value, f".{precision}g")
    if isinstance(value, complex):
        real = 0.0 if abs(value.real) < 10 ** (-precision) else value.real
        imag = 0.0 if abs(value.imag) < 10 ** (-precision) else value.imag
        if imag == 0:
            return format(real, f".{precision}g")
        if real == 0:
            return f"{format(imag, f'.{precision}g')}i"
        sign = "+" if imag >= 0 else "-"
        return (
            f"{format(real, f'.{precision}g')} {sign} "
            f"{format(abs(imag), f'.{precision}g')}i"
        )
    return "[" + ", ".join(_format_result(item, precision) for item in value) + "]"


def _serialise_result(value: Any) -> Any:
    """Convert complex values to JSON-safe structured data."""
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    if isinstance(value, list):
        return [_serialise_result(item) for item in value]
    return value
