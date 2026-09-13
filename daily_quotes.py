"""Shared daily welcome quotes for the Web UI and CLI."""

from __future__ import annotations

from datetime import date


DAILY_QUOTES: tuple[str, ...] = (
    "每一次认真提问，都是抵达答案的开始。",
    "不必等待完美，先让此刻的灵感落地。",
    "答案并不总在远方，有时就藏在下一步里。",
    "把行动交给自己，将结果留给时间。",
    "汇集散落的灵感，拼凑出前行的轮廓。",
    "少一点猜测的内耗，多一次真实的运行。",
    "所有的豁然开朗，都来自一次次的重构与试错。",
)


def quote_for_day(day: date | None = None) -> str:
    """Return the deterministic quote for a calendar day."""
    current_day = day or date.today()
    return DAILY_QUOTES[current_day.toordinal() % len(DAILY_QUOTES)]
