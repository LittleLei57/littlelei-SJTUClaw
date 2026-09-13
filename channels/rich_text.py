"""Cross-channel Markdown preparation and readable LaTeX fallback.

Messaging clients do not expose a dependable TeX/KaTeX component.  Keep the
Markdown that a channel can render, but turn common mathematical notation into
readable Unicode instead of leaking ``\\frac``/``$$`` source to the user.
"""

from __future__ import annotations

import re


_COMMANDS = {
    "Leftrightarrow": "⇔",
    "leftrightarrow": "↔",
    "Rightarrow": "⇒",
    "rightarrow": "→",
    "Leftarrow": "⇐",
    "leftarrow": "←",
    "varepsilon": "ε",
    "varphi": "φ",
    "partial": "∂",
    "nabla": "∇",
    "infty": "∞",
    "approx": "≈",
    "equiv": "≡",
    "propto": "∝",
    "times": "×",
    "cdot": "·",
    "pm": "±",
    "mp": "∓",
    "neq": "≠",
    "ne": "≠",
    "leq": "≤",
    "le": "≤",
    "geq": "≥",
    "ge": "≥",
    "ll": "≪",
    "gg": "≫",
    "subseteq": "⊆",
    "supseteq": "⊇",
    "subset": "⊂",
    "supset": "⊃",
    "in": "∈",
    "notin": "∉",
    "cup": "∪",
    "cap": "∩",
    "forall": "∀",
    "exists": "∃",
    "sum": "∑",
    "prod": "∏",
    "int": "∫",
    "oint": "∮",
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "tau": "τ",
    "upsilon": "υ",
    "phi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
}

_SUPERSCRIPT = str.maketrans(
    "0123456789+-=()nijk",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱʲᵏ",
)
_SUBSCRIPT = str.maketrans(
    "0123456789+-=()aeoxhklmnpstijr",
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜᵢⱼᵣ",
)


def _script(value: str, table: dict[int, str], marker: str) -> str:
    translated = value.translate(table)
    if len(translated) == len(value) and translated != value:
        return translated
    return f"{marker}{value}"


def _replace_simple_braces(text: str, command: str, replacement) -> str:
    pattern = re.compile(rf"\\{command}\s*\{{([^{{}}]*)\}}")
    for _ in range(8):
        updated = pattern.sub(lambda match: replacement(match.group(1)), text)
        if updated == text:
            break
        text = updated
    return text


def _replace_fractions(text: str) -> str:
    pattern = re.compile(r"\\(?:d?frac|tfrac)\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
    for _ in range(8):
        updated = pattern.sub(
            lambda match: f"({match.group(1)})/({match.group(2)})",
            text,
        )
        if updated == text:
            break
        text = updated
    return text


def _convert_math_segment(value: str) -> str:
    text = value.strip()
    text = re.sub(r"\\begin\{(?:aligned|align\*?|equation\*?|gathered)\}", "", text)
    text = re.sub(r"\\end\{(?:aligned|align\*?|equation\*?|gathered)\}", "", text)
    text = text.replace(r"\\", "\n")
    text = re.sub(r"\\(?:left|right)\b", "", text)
    text = _replace_simple_braces(text, "sqrt", lambda item: f"√({item})")
    text = _replace_fractions(text)
    text = _replace_simple_braces(text, "mathbb", lambda item: {"R": "ℝ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "C": "ℂ", "E": "𝔼"}.get(item, item))
    for command in ("text", "textrm", "mathrm", "mathbf", "mathit", "operatorname", "hat", "widehat", "bar", "overline"):
        text = _replace_simple_braces(text, command, lambda item: item)
    for command in sorted(_COMMANDS, key=len, reverse=True):
        text = re.sub(rf"\\{command}(?![A-Za-z])", _COMMANDS[command], text)
    text = re.sub(r"\^\{([^{}]+)\}", lambda match: _script(match.group(1), _SUPERSCRIPT, "^"), text)
    text = re.sub(r"\^([A-Za-z0-9+\-=()])", lambda match: _script(match.group(1), _SUPERSCRIPT, "^"), text)
    text = re.sub(r"_\{([^{}]+)\}", lambda match: _script(match.group(1), _SUBSCRIPT, "_"), text)
    text = re.sub(r"_([A-Za-z0-9+\-=()])", lambda match: _script(match.group(1), _SUBSCRIPT, "_"), text)
    text = re.sub(r"\\(?:quad|qquad|enspace|thinspace)\b", " ", text)
    text = re.sub(r"\\[,;:!]\s*", " ", text)
    text = text.replace(r"\{", "{").replace(r"\}", "}").replace(r"\_", "_")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _looks_like_inline_math(value: str) -> bool:
    stripped = value.strip()
    return bool(
        re.search(r"[\\^_={}+\-*/<>]", stripped)
        or re.search(r"[A-Za-z]\s*=\s*", stripped)
        or (len(stripped) <= 4 and re.fullmatch(r"[A-Za-zα-ωΑ-Ω0-9]+", stripped))
    )


def readable_latex(text: str) -> str:
    """Convert common LaTeX to readable Unicode while preserving Markdown."""
    value = str(text or "").replace("&nbsp;", "\u00a0")
    value = re.sub(
        r"\$\$(.+?)\$\$",
        lambda match: _convert_math_segment(match.group(1)),
        value,
        flags=re.DOTALL,
    )
    value = re.sub(
        r"\\\[(.+?)\\\]",
        lambda match: _convert_math_segment(match.group(1)),
        value,
        flags=re.DOTALL,
    )
    value = re.sub(
        r"\\\((.+?)\\\)",
        lambda match: _convert_math_segment(match.group(1)),
        value,
        flags=re.DOTALL,
    )

    def inline(match: re.Match[str]) -> str:
        inner = match.group(1)
        return _convert_math_segment(inner) if _looks_like_inline_math(inner) else match.group(0)

    value = re.sub(r"(?<!\\)(?<!\$)\$(?!\$)([^$\n]+?)\$(?!\$)", inline, value)

    # Models sometimes omit math delimiters.  Converting known commands is
    # still safe here; ordinary Windows paths and Markdown escapes are left
    # untouched because only recognised TeX commands are handled.
    if re.search(r"\\(?:frac|sqrt|theta|alpha|beta|sum|int|mathbb|mathbf|mathrm)\b", value):
        value = _convert_math_segment(value)
    return value


def prepare_channel_markdown(text: str) -> str:
    """Return Markdown suitable for native channel renderers."""
    return readable_latex(text)
