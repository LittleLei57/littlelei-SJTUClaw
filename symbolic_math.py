"""受限的符号数学 Tool。

该模块使用 SymPy 做计算机代数，但不暴露任意 Python ``eval``。输入先经过
长度、字符、标识符和复杂度检查，再交给仅包含数学构造的解析环境。矩阵通过
JSON 数组传入，避免把下标、属性访问或任意容器语法开放给表达式解析器。
"""

from __future__ import annotations

import re
from typing import Any

import sympy as sp
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)


MAX_EXPRESSION_CHARS = 600
MAX_IDENTIFIERS = 24
MAX_OPERATORS = 160
MAX_MATRIX_DIMENSION = 8
MAX_DERIVATIVE_ORDER = 8
MAX_NUMERIC_PRECISION = 30

_SAFE_CHARS = re.compile(r"^[0-9A-Za-z_+\-*/^().,= \t]+$")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_VARIABLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_TRANSFORMATIONS = standard_transformations + (
    convert_xor,
    implicit_multiplication_application,
)
_FUNCTIONS: dict[str, Any] = {
    "sin": sp.sin,
    "cos": sp.cos,
    "tan": sp.tan,
    "asin": sp.asin,
    "acos": sp.acos,
    "atan": sp.atan,
    "sinh": sp.sinh,
    "cosh": sp.cosh,
    "tanh": sp.tanh,
    "exp": sp.exp,
    "log": sp.log,
    "ln": sp.log,
    "sqrt": sp.sqrt,
    "Abs": sp.Abs,
    "abs": sp.Abs,
}
_CONSTANTS: dict[str, Any] = {
    "pi": sp.pi,
    "e": sp.E,
    "E": sp.E,
    "i": sp.I,
    "I": sp.I,
    "oo": sp.oo,
}
_GLOBAL_DICT: dict[str, Any] = {
    "__builtins__": {},
    "Integer": sp.Integer,
    "Float": sp.Float,
    "Rational": sp.Rational,
    "Symbol": sp.Symbol,
}


class SymbolicMathError(ValueError):
    """符号数学请求不合法、过于复杂或不受支持。"""


def symbolic_math(
    operation: str,
    expression: str | None = None,
    variable: str = "x",
    variables: list[str] | None = None,
    order: int = 1,
    point: str | int | float | None = None,
    lower: str | int | float | None = None,
    upper: str | int | float | None = None,
    matrix: list[list[Any]] | None = None,
    vector: list[Any] | None = None,
    precision: int = 12,
) -> dict[str, Any]:
    """执行一项白名单内的符号数学操作并返回精确、近似与 LaTeX 结果。"""

    operation = str(operation or "").strip().lower()
    supported = {
        "simplify", "expand", "factor", "solve", "diff", "integrate", "limit",
        "matrix_det", "matrix_inv", "matrix_solve",
    }
    if operation not in supported:
        raise SymbolicMathError(
            f"不支持的 operation：{operation or '(空)'}；可选值为 {', '.join(sorted(supported))}。"
        )
    if isinstance(precision, bool) or not isinstance(precision, int) or not 1 <= precision <= MAX_NUMERIC_PRECISION:
        raise SymbolicMathError(f"precision 必须是 1 到 {MAX_NUMERIC_PRECISION} 的整数。")
    symbols = _make_symbols(variable, variables)
    primary = symbols[variable]

    if operation.startswith("matrix_"):
        return _matrix_operation(operation, matrix, vector, symbols, precision)
    if not isinstance(expression, str) or not expression.strip():
        raise SymbolicMathError("expression 不能为空。")

    if operation == "solve":
        equation = _parse_equation(expression, symbols)
        result = sp.solve(equation, [symbols[name] for name in symbols], dict=True)
    else:
        parsed = _parse_expression(expression, symbols)
        if operation == "simplify":
            result = sp.simplify(parsed)
        elif operation == "expand":
            result = sp.expand(parsed)
        elif operation == "factor":
            result = sp.factor(parsed)
        elif operation == "diff":
            if isinstance(order, bool) or not isinstance(order, int) or not 1 <= order <= MAX_DERIVATIVE_ORDER:
                raise SymbolicMathError(f"order 必须是 1 到 {MAX_DERIVATIVE_ORDER} 的整数。")
            result = sp.diff(parsed, primary, order)
        elif operation == "integrate":
            if (lower is None) != (upper is None):
                raise SymbolicMathError("定积分必须同时提供 lower 和 upper。")
            if lower is None:
                result = sp.integrate(parsed, primary)
            else:
                result = sp.integrate(
                    parsed,
                    (primary, _parse_scalar(lower, symbols), _parse_scalar(upper, symbols)),
                )
        else:  # limit
            if point is None:
                raise SymbolicMathError("limit 操作必须提供 point。")
            result = sp.limit(parsed, primary, _parse_scalar(point, symbols))

    return _result_payload(operation, expression, result, precision)


def _make_symbols(variable: str, variables: list[str] | None) -> dict[str, sp.Symbol]:
    requested = [variable, *(variables or [])]
    if not requested:
        requested = ["x"]
    result: dict[str, sp.Symbol] = {}
    for name in requested:
        if not isinstance(name, str) or not _VARIABLE_NAME.fullmatch(name):
            raise SymbolicMathError(f"变量名不合法：{name!r}。")
        if name in _FUNCTIONS or name in _CONSTANTS or name.startswith("_"):
            raise SymbolicMathError(f"变量名与内置数学名称冲突：{name}。")
        result.setdefault(name, sp.Symbol(name))
    if len(result) > 8:
        raise SymbolicMathError("一次最多允许 8 个变量。")
    return result


def _validate_expression(text: str, symbols: dict[str, sp.Symbol]) -> str:
    value = text.strip()
    if not value or len(value) > MAX_EXPRESSION_CHARS:
        raise SymbolicMathError(f"表达式不能为空且最多 {MAX_EXPRESSION_CHARS} 个字符。")
    if "__" in value or not _SAFE_CHARS.fullmatch(value):
        raise SymbolicMathError("表达式含有不允许的字符或属性访问。")
    if sum(value.count(token) for token in "+-*/^") > MAX_OPERATORS:
        raise SymbolicMathError("表达式过于复杂。")
    identifiers = _IDENTIFIER.findall(value)
    if len(identifiers) > MAX_IDENTIFIERS:
        raise SymbolicMathError("表达式中的标识符过多。")
    allowed = set(symbols) | set(_FUNCTIONS) | set(_CONSTANTS)
    unknown = sorted(set(identifiers) - allowed)
    if unknown:
        raise SymbolicMathError(
            f"存在未声明的变量或函数：{', '.join(unknown)}；请通过 variables 声明变量。"
        )
    return value


def _parse_expression(text: str, symbols: dict[str, sp.Symbol]) -> sp.Expr:
    value = _validate_expression(text, symbols)
    local_dict = {**_FUNCTIONS, **_CONSTANTS, **symbols}
    try:
        parsed = parse_expr(
            value,
            local_dict=local_dict,
            global_dict=_GLOBAL_DICT,
            transformations=_TRANSFORMATIONS,
            evaluate=True,
        )
    except Exception as exc:
        raise SymbolicMathError(f"无法解析数学表达式：{exc}") from exc
    if not isinstance(parsed, sp.Expr):
        raise SymbolicMathError("表达式没有解析为受支持的数学对象。")
    if sp.count_ops(parsed, visual=False) > MAX_OPERATORS:
        raise SymbolicMathError("解析后的表达式过于复杂。")
    return parsed


def _parse_equation(text: str, symbols: dict[str, sp.Symbol]) -> sp.Expr:
    if text.count("=") > 1:
        raise SymbolicMathError("一次只支持一个等号；方程组请用多个变量和等价表达式分步求解。")
    if "=" not in text:
        return _parse_expression(text, symbols)
    left, right = text.split("=", 1)
    return _parse_expression(left, symbols) - _parse_expression(right, symbols)


def _parse_scalar(value: Any, symbols: dict[str, sp.Symbol]) -> sp.Expr:
    if isinstance(value, bool):
        raise SymbolicMathError("边界或极限点必须是数字或数学表达式。")
    return _parse_expression(str(value), symbols)


def _matrix_operation(
    operation: str,
    matrix: list[list[Any]] | None,
    vector: list[Any] | None,
    symbols: dict[str, sp.Symbol],
    precision: int,
) -> dict[str, Any]:
    if not isinstance(matrix, list) or not matrix or not all(isinstance(row, list) for row in matrix):
        raise SymbolicMathError("matrix 必须是非空二维 JSON 数组。")
    rows = len(matrix)
    columns = len(matrix[0]) if matrix[0] else 0
    if not columns or rows > MAX_MATRIX_DIMENSION or columns > MAX_MATRIX_DIMENSION:
        raise SymbolicMathError(f"矩阵行列数必须在 1 到 {MAX_MATRIX_DIMENSION} 之间。")
    if any(len(row) != columns for row in matrix):
        raise SymbolicMathError("矩阵每一行的列数必须相同。")
    parsed_rows = [[_parse_scalar(item, symbols) for item in row] for row in matrix]
    value = sp.Matrix(parsed_rows)

    if operation == "matrix_det":
        if rows != columns:
            raise SymbolicMathError("行列式只适用于方阵。")
        result: Any = value.det()
    elif operation == "matrix_inv":
        if rows != columns:
            raise SymbolicMathError("逆矩阵只适用于方阵。")
        try:
            result = value.inv()
        except Exception as exc:
            raise SymbolicMathError(f"矩阵不可逆：{exc}") from exc
    else:
        if not isinstance(vector, list) or len(vector) != rows:
            raise SymbolicMathError("matrix_solve 的 vector 长度必须等于矩阵行数。")
        rhs = sp.Matrix([_parse_scalar(item, symbols) for item in vector])
        try:
            result = value.gauss_jordan_solve(rhs)[0]
        except Exception as exc:
            raise SymbolicMathError(f"无法求解该线性方程组：{exc}") from exc
    return _result_payload(operation, None, result, precision)


def _result_payload(
    operation: str,
    expression: str | None,
    result: Any,
    precision: int,
) -> dict[str, Any]:
    return {
        "operation": operation,
        "expression": expression,
        "exact": _serialise(result),
        "approximate": _serialise(_numeric(result, precision)),
        "latex": sp.latex(result),
    }


def _numeric(value: Any, precision: int) -> Any:
    if isinstance(value, list):
        return [_numeric(item, precision) for item in value]
    if isinstance(value, dict):
        return {str(key): _numeric(item, precision) for key, item in value.items()}
    if isinstance(value, sp.MatrixBase):
        return value.applyfunc(lambda item: sp.N(item, precision))
    if isinstance(value, sp.Basic):
        return sp.N(value, precision)
    return value


def _serialise(value: Any) -> Any:
    if isinstance(value, list):
        return [_serialise(item) for item in value]
    if isinstance(value, tuple):
        return [_serialise(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialise(item) for key, item in value.items()}
    if isinstance(value, sp.MatrixBase):
        return [[_serialise(item) for item in row] for row in value.tolist()]
    if isinstance(value, sp.Integer):
        return int(value)
    if isinstance(value, sp.Float):
        return float(value)
    if isinstance(value, sp.Rational):
        return str(value)
    if isinstance(value, sp.Basic):
        return str(value)
    return value
