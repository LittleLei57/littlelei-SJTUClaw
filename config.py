"""Step 0：环境与 LLM API 配置。

配置只从项目根目录的 ``.env`` 或系统环境变量读取，源码中不保存真实
API Key。这里同时集中维护 OpenAI-compatible API 的地址、模型目录、视觉
能力和运行时上限，使 CLI、Gateway 与测试入口获得一致配置。
"""

import json
import os
import re


def _load_local_env() -> None:
    """加载项目根目录下简单的 KEY=VALUE 配置。"""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_file):
        return
    with open(env_file, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


_load_local_env()

# API 基础地址。模型切换只在同一个 OpenAI-compatible 接口内进行，Key 始终
# 留在 Gateway 进程中，不会通过 Web API 发送给浏览器。
DEFAULT_SJTU_API_BASE_URL = "https://models.sjtu.edu.cn/api/v1"
API_BASE_URL = os.getenv("LLM_BASE_URL", DEFAULT_SJTU_API_BASE_URL).strip()
DEFAULT_MODEL = os.getenv("LLM_MODEL", "qwen").strip()

_DEFAULT_SJTU_MODELS = {
    "deepseek-chat": "DeepSeek V4 Flash（常规模式）",
    "deepseek-reasoner": "DeepSeek V4 Flash（思考模式）",
    "minimax": "MiniMax-M2.7",
    "qwen": "Qwen3.6-27B",
}

_DEFAULT_SJTU_MODEL_DETAILS = {
    "deepseek-chat": {
        "shortLabel": "DS-V4 常规",
        "mode": "非思考模式",
        "strength": "通用文本处理",
        "contextLength": "256K",
    },
    "deepseek-reasoner": {
        "shortLabel": "DS-V4 思考",
        "mode": "深度思考模式",
        "strength": "复杂逻辑深度推理",
        "contextLength": "256K",
    },
    "minimax": {
        "shortLabel": "MiniMax M2.7",
        "mode": "文本生成",
        "strength": "智能体任务",
        "contextLength": "192K",
    },
    "qwen": {
        "shortLabel": "Qwen 3.6",
        "mode": "多模态处理",
        "strength": "视觉与文本理解",
        "contextLength": "256K",
    },
}


def _custom_model_entry(value) -> dict | None:
    """Normalize one item from ``LLM_MODELS_JSON`` without exposing secrets."""
    if isinstance(value, str):
        model_id = value.strip()
        return {"id": model_id, "label": model_id, "shortLabel": model_id} if model_id else None
    if not isinstance(value, dict):
        return None
    model_id = str(value.get("id") or value.get("model") or "").strip()
    if not model_id:
        return None
    label = str(value.get("label") or model_id).strip()
    short_label = str(value.get("shortLabel") or label).strip()
    entry = {"id": model_id, "label": label, "shortLabel": short_label}
    for key in ("mode", "strength", "contextLength"):
        text = str(value.get(key) or "").strip()
        if text:
            entry[key] = text
    if isinstance(value.get("vision"), bool):
        entry["vision"] = value["vision"]
    return entry


def _load_model_catalog() -> list[dict]:
    """Load the models selectable on the configured OpenAI-compatible API.

    ``LLM_MODELS_JSON`` is the most expressive form.  ``LLM_MODELS`` is a
    convenient comma-separated fallback.  With the stock SJTU endpoint and no
    explicit list, the four course-provided presets remain available.  A
    custom endpoint defaults to only ``LLM_MODEL`` so arbitrary providers do
    not accidentally inherit SJTU-only model ids.
    """
    raw_json = os.getenv("LLM_MODELS_JSON", "").strip()
    entries: list[dict] = []
    if raw_json:
        try:
            decoded = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM_MODELS_JSON 不是有效 JSON：{exc}") from exc
        if not isinstance(decoded, list):
            raise ValueError("LLM_MODELS_JSON 必须是 JSON 数组。")
        entries = [
            entry for entry in (_custom_model_entry(item) for item in decoded)
            if entry is not None
        ]
    else:
        raw_models = os.getenv("LLM_MODELS", "").strip()
        if raw_models:
            entries = [
                entry
                for entry in (
                    _custom_model_entry(item)
                    for item in raw_models.split(",")
                )
                if entry is not None
            ]
        elif API_BASE_URL.rstrip("/") == DEFAULT_SJTU_API_BASE_URL:
            entries = [
                {
                    "id": model_id,
                    "label": label,
                    **_DEFAULT_SJTU_MODEL_DETAILS.get(model_id, {}),
                }
                for model_id, label in _DEFAULT_SJTU_MODELS.items()
            ]
        else:
            label = os.getenv("LLM_MODEL_LABEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
            entries = [{"id": DEFAULT_MODEL, "label": label, "shortLabel": label}]

    if not entries:
        entries = [{"id": DEFAULT_MODEL, "label": DEFAULT_MODEL, "shortLabel": DEFAULT_MODEL}]
    known = {entry["id"].lower() for entry in entries}
    if DEFAULT_MODEL.lower() not in known:
        # The startup model must always remain selectable, even if the optional
        # list was incomplete.
        entries.insert(
            0,
            {"id": DEFAULT_MODEL, "label": DEFAULT_MODEL, "shortLabel": DEFAULT_MODEL},
        )
    return entries


MODEL_CATALOG = _load_model_catalog()
SUPPORTED_MODELS = {entry["id"]: entry["label"] for entry in MODEL_CATALOG}
MODEL_DETAILS = {
    entry["id"]: {
        key: value
        for key, value in entry.items()
        if key not in {"id", "label"}
    }
    for entry in MODEL_CATALOG
}

# Backwards-compatible names retained for existing integrations.  Their
# contents are now configuration-driven and are no longer limited to SJTU.
SJTU_API_MODELS = SUPPORTED_MODELS
SJTU_MODEL_DETAILS = MODEL_DETAILS


def normalize_sjtu_model(model: str | None) -> str:
    """Validate a model exposed by the configured API model catalog."""
    value = str(model or "").strip()
    selected = next(
        (item for item in SUPPORTED_MODELS if item.lower() == value.lower()),
        None,
    )
    if selected is None:
        choices = "、".join(SUPPORTED_MODELS)
        raise ValueError(f"未配置模型：{model or '空'}。可选：{choices}")
    return selected


def model_supports_vision(model: str | None = None) -> bool:
    """Return whether selected images should use native multimodal input."""
    raw = os.getenv("LLM_VISION", "auto").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw not in {"", "auto"}:
        raise ValueError("LLM_VISION 必须是 auto、on 或 off。")
    selected = next(
        (item for item in MODEL_CATALOG if item["id"].lower() == str(model or DEFAULT_MODEL).lower()),
        None,
    )
    if selected is not None and isinstance(selected.get("vision"), bool):
        return selected["vision"]
    name = (model or DEFAULT_MODEL).strip().lower()
    compact_name = re.sub(r"[^a-z0-9]+", "", name)
    return (
        name == "qwen"
        or "qwen-vl" in name
        or "qwen3-vl" in name
        # MiniMax-M3 exposes OpenAI-compatible multimodal input. Without
        # this inference the Runtime pre-runs OCR and never sends the pixels.
        or compact_name.startswith("minimaxm3")
    )


def load_api_key() -> str:
    """从 .env 或系统环境变量加载 API Key。"""
    key = os.getenv("LLM_API_KEY", "").strip()
    if not key:
        raise ValueError("缺少 LLM_API_KEY，请在 .env 或系统环境变量中配置。")
    return key


def load_tavily_api_key() -> str:
    """从 .env 或系统环境变量加载 Tavily API Key。"""
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        raise ValueError("缺少 TAVILY_API_KEY，请在 .env 或系统环境变量中配置。")
    return key


def wolfram_app_id() -> str | None:
    """Return the optional Wolfram|Alpha developer AppID."""
    value = os.getenv("WOLFRAM_APP_ID", "").strip()
    return value or None


def wolfram_app_id() -> str | None:
    """Return the optional Wolfram|Alpha developer AppID."""
    value = os.getenv("WOLFRAM_APP_ID", "").strip()
    return value or None


def scheduler_webhook_url() -> str | None:
    """Optional outbound URL for tasks using ``deliveryMode=webhook``."""
    value = os.getenv("SJTUCLAW_SCHEDULER_WEBHOOK_URL", "").strip()
    return value or None


def tool_timeout_seconds() -> float | None:
    """读取单个 Tool 的最长等待时间；设为 0/none/off 可关闭 Registry 层超时。"""
    raw = os.getenv("SJTUCLAW_TOOL_TIMEOUT_SECONDS", "60").strip().lower()
    if raw in {"", "0", "none", "off", "false"}:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("SJTUCLAW_TOOL_TIMEOUT_SECONDS 必须是数字、0、none 或 off。") from exc
    if value <= 0:
        return None
    return value


def agent_max_steps() -> int:
    """读取单个 Agent Turn 允许的最多模型/工具循环轮数。

    正常任务通常只需要几轮；上限用于防止模型重复调用 Tool 时无限占用
    请求、线程和上下文。设为较大的正整数即可放宽，不能设为 0。
    """
    raw = os.getenv("SJTUCLAW_MAX_AGENT_STEPS", "24").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("SJTUCLAW_MAX_AGENT_STEPS 必须是正整数。") from exc
    if value < 1:
        raise ValueError("SJTUCLAW_MAX_AGENT_STEPS 必须是正整数。")
    return value


def compaction_max_tokens() -> int | None:
    """Maximum estimated semantic tokens before automatic compaction.

    The character threshold remains as a conservative fallback, while this
    budget catches CJK-heavy conversations whose token count is much larger
    than their character count. Set the variable to ``0``, ``none`` or
    ``off`` to disable the token leg and keep legacy character-only behavior.
    """
    raw = os.getenv("SJTUCLAW_COMPACT_MAX_TOKENS", "8000").strip().lower()
    if raw in {"", "0", "none", "off", "false"}:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("SJTUCLAW_COMPACT_MAX_TOKENS 必须是正整数、0、none 或 off。") from exc
    if value < 1:
        raise ValueError("SJTUCLAW_COMPACT_MAX_TOKENS 必须是正整数、0、none 或 off。")
    return value


def compaction_chunk_tokens() -> int | None:
    """Maximum estimated tokens in one summary request payload."""
    raw = os.getenv("SJTUCLAW_COMPACT_CHUNK_TOKENS", "6000").strip().lower()
    if raw in {"", "0", "none", "off", "false"}:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("SJTUCLAW_COMPACT_CHUNK_TOKENS 必须是正整数、0、none 或 off。") from exc
    if value < 1:
        raise ValueError("SJTUCLAW_COMPACT_CHUNK_TOKENS 必须是正整数、0、none 或 off。")
    return value
