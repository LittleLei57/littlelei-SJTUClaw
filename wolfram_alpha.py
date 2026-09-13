"""Optional Wolfram|Alpha LLM API client.

The integration is deliberately a secondary provider: local ``calculate`` and
``symbolic_math`` remain available without network access, while Wolfram can
handle natural-language mathematics, units, and broader computational
knowledge when an AppID is configured.
"""

from __future__ import annotations

import socket
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


WOLFRAM_LLM_API_URL = "https://www.wolframalpha.com/api/v1/llm-api"
WOLFRAM_RESULT_URL = "https://www.wolframalpha.com/input"


class WolframAlpha:
    def __init__(
        self,
        app_id: str,
        timeout: float = 20.0,
        opener: Callable[..., Any] = urlopen,
    ):
        app_id = str(app_id or "").strip()
        if not app_id:
            raise ValueError("Wolfram|Alpha AppID 不能为空。")
        self._app_id = app_id
        self.timeout = timeout
        self._opener = opener

    def query(self, query: str, max_chars: int = 4_000) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("Wolfram|Alpha 查询内容不能为空。")
        if len(query) > 2_000:
            raise ValueError("Wolfram|Alpha 查询内容不能超过 2000 个字符。")
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            raise ValueError("max_chars 必须是整数。")
        max_chars = max(200, min(max_chars, 6_800))

        params = urlencode(
            {"input": query, "appid": self._app_id, "maxchars": max_chars}
        )
        request = Request(
            f"{WOLFRAM_LLM_API_URL}?{params}",
            headers={"Accept": "text/plain", "User-Agent": "SJTUClaw/1.0"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                result = response.read(max_chars * 4 + 1).decode(
                    "utf-8", errors="replace"
                ).strip()
        except HTTPError as exc:
            detail = _http_error_detail(exc)
            if exc.code == 403:
                raise RuntimeError(
                    "Wolfram|Alpha AppID 无效、未启用或免费额度已经用完。"
                ) from exc
            if exc.code == 501:
                raise RuntimeError(
                    f"Wolfram|Alpha 无法解释该查询。{detail}".rstrip()
                ) from exc
            raise RuntimeError(
                f"Wolfram|Alpha 查询失败（HTTP {exc.code}）。{detail}".rstrip()
            ) from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError(
                "Wolfram|Alpha 网络连接失败或超时，请稍后重试。"
            ) from exc

        if not result:
            raise RuntimeError("Wolfram|Alpha 没有返回可用结果。")
        if len(result) > max_chars:
            result = result[:max_chars].rstrip() + "…"
        return {
            "provider": "Wolfram|Alpha",
            "query": query,
            "result": result,
            "source": f"{WOLFRAM_RESULT_URL}?{urlencode({'i': query})}",
            "attribution": "Computed by Wolfram|Alpha",
        }


def _http_error_detail(exc: HTTPError) -> str:
    try:
        value = exc.read(501).decode("utf-8", errors="replace").strip()
    except Exception:
        return ""
    return value[:500]
