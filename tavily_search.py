"""Tavily-backed, read-only web search tool."""

from __future__ import annotations

import json
import socket
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import load_tavily_api_key


TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class TavilySearch:
    """Small Tavily HTTP client with bounded, model-friendly output."""

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 20.0,
        opener: Callable[..., Any] = urlopen,
    ):
        self._api_key = api_key
        self.timeout = timeout
        self._opener = opener

    def search(
        self,
        query: str,
        max_results: int = 5,
        search_depth: str = "basic",
        topic: str = "general",
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("搜索关键词不能为空。")
        if not 1 <= max_results <= 10:
            raise ValueError("max_results 必须在 1 到 10 之间。")
        if search_depth not in {"basic", "advanced"}:
            raise ValueError("search_depth 只支持 basic 或 advanced。")
        if topic not in {"general", "news", "finance"}:
            raise ValueError("topic 只支持 general、news 或 finance。")

        api_key = self._api_key.strip() if self._api_key else load_tavily_api_key()

        body = json.dumps(
            {
                "query": query,
                "max_results": max_results,
                "search_depth": search_depth,
                "topic": topic,
                "include_answer": True,
                "include_raw_content": False,
                "include_images": False,
            }
        ).encode("utf-8")
        request = Request(
            TAVILY_SEARCH_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "SJTUClaw/1.0",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = _http_error_detail(exc)
            suffix = f"：{detail}" if detail else ""
            raise RuntimeError(f"Tavily 搜索失败（HTTP {exc.code}）{suffix}") from exc
        except (URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError("Tavily 搜索网络连接失败或超时，请稍后重试。") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Tavily 返回了无法解析的响应。") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Tavily 返回格式异常。")
        results = payload.get("results", [])
        if not isinstance(results, list):
            results = []
        bounded_results = [
            {
                "title": _truncate(item.get("title"), 500),
                "url": item.get("url"),
                "content": _truncate(item.get("content"), 1_000),
                "score": item.get("score"),
            }
            for item in results[:max_results]
            if isinstance(item, dict)
        ]
        return {
            "query": payload.get("query", query),
            "answer": _truncate(payload.get("answer"), 2_500),
            "results": bounded_results,
            "citations": [
                {
                    "label": f"[W{index}]",
                    "kind": "web",
                    "title": item.get("title") or item.get("url") or "Web source",
                    "url": item.get("url"),
                }
                for index, item in enumerate(bounded_results, start=1)
            ],
            "response_time": payload.get("response_time"),
        }


def _truncate(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + "…"


def _http_error_detail(exc: HTTPError) -> str:
    try:
        raw = exc.read(2_001).decode("utf-8", errors="replace")
    except Exception:
        return ""
    # Tavily errors are JSON in normal operation. Keep the message bounded and
    # never include request headers (which contain the API key).
    try:
        parsed = json.loads(raw)
        detail = parsed.get("detail") or parsed.get("message") or parsed.get("error")
        return _truncate(str(detail), 500) if detail else ""
    except json.JSONDecodeError:
        return _truncate(raw.strip(), 500) or ""
