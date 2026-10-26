"""LLM 接入配置（base_url / api_key / model / temperature / max_tokens）。

从旧 ``Agent/llm_config.py`` 原样搬运：

    resolve_llm_config(agent.*)                 → 实际交给 ChatOpenAI 的配置
    resolve_model_entry(agent.models 里的一条)   → 分级 / 备用模型配置

默认值与旧版一致；不要在这里初始化任何客户端。
"""

from __future__ import annotations

import os

# api_key 缺省读 api_key_env 指定的环境变量；max_tokens 缺省读 LLM_MAX_TOKENS
LLM_CONFIG = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
    "api_key_env": "DEEPSEEK_API_KEY",
    "model": "deepseek-v4-flash",
    "temperature": 0.7,
    "max_tokens": None,
}

# agent.models 里一条配置能覆盖的字段（没写的沿用主模型）
MODEL_ENTRY_KEYS = ("base_url", "api_key", "api_key_env", "model", "temperature", "max_tokens")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def resolve_llm_config(overrides=None) -> dict:
    """模块默认值 ← bot_config.json 的 agent.*（网页面板保存后热加载）。

    只认 LLM_CONFIG 里已有的键；None / 空白字符串视为「没配」，
    继续用默认值（api_key、max_tokens 因此还能走环境变量）。
    """
    config = dict(LLM_CONFIG)
    overrides = overrides or {}

    # 先确定环境变量名，支持 DeepSeek、百度千帆等不同服务商。
    api_key_env = str(overrides.get("api_key_env") or LLM_CONFIG["api_key_env"]).strip()
    if api_key_env:
        config["api_key_env"] = api_key_env
    config["api_key"] = os.environ.get(config["api_key_env"], "")

    explicit_api_key = str(overrides.get("api_key") or "").strip()
    if explicit_api_key:
        config["api_key"] = explicit_api_key

    if config["max_tokens"] is None:
        config["max_tokens"] = _env_int("LLM_MAX_TOKENS", 10240)

    for key, value in overrides.items():
        if key in ("api_key", "api_key_env") or key not in LLM_CONFIG:
            continue
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        try:
            if key == "temperature":
                value = float(value)
            elif key == "max_tokens":
                value = int(value)
                if value <= 0:
                    continue
        except (TypeError, ValueError):
            continue
        config[key] = value
    return config


def resolve_model_entry(entry, base_config=None) -> dict:
    """把 `agent.models` 里的一条配置解析成完整的 ChatOpenAI 参数。

    没写的字段沿用 `base_config`（通常就是主模型那套 temperature / max_tokens）。
    密钥优先级：这条自己的 api_key > 自己的 api_key_env > 主模型密钥（仅当没换 base_url）。
    """
    base = dict(base_config or resolve_llm_config())
    entry = entry if isinstance(entry, dict) else {}
    config = {key: base.get(key) for key in ("base_url", "api_key", "model", "temperature", "max_tokens")}
    config["api_key_env"] = base.get("api_key_env", "")

    entry_base_url = str(entry.get("base_url") or "").strip()
    if entry_base_url:
        config["base_url"] = entry_base_url

    # 密钥优先级：这条自己的 api_key > 这条自己的 api_key_env > 主模型（仅同一服务地址时）
    api_key_env = str(entry.get("api_key_env") or "").strip()
    if api_key_env:
        config["api_key_env"] = api_key_env
        config["api_key"] = os.environ.get(api_key_env, "")

    explicit_api_key = str(entry.get("api_key") or "").strip()
    if explicit_api_key:
        config["api_key"] = explicit_api_key
    elif not api_key_env:
        if entry_base_url:
            # 换了服务地址却没配密钥：留空让 ChatOpenAI 直接报错，
            # 别把主模型的密钥发给另一家服务商
            config["api_key"] = ""
        else:
            # 只换模型名、还是同一家：沿用主模型的密钥
            config["api_key"] = base.get("api_key", "")

    for key in ("model",):
        value = str(entry.get(key) or "").strip()
        if value:
            config[key] = value

    try:
        if entry.get("temperature") is not None and not isinstance(entry.get("temperature"), bool):
            config["temperature"] = float(entry["temperature"])
    except (TypeError, ValueError):
        pass
    try:
        if entry.get("max_tokens") is not None:
            max_tokens = int(entry["max_tokens"])
            if max_tokens > 0:
                config["max_tokens"] = max_tokens
    except (TypeError, ValueError):
        pass
    return config


def model_label(entry) -> str:
    """模型配置的显示名：备注名 > 模型名 > 服务地址 > 未知。"""
    entry = entry if isinstance(entry, dict) else {}
    for key in ("name", "model", "base_url"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return "未命名模型"


def find_model_entry(models, model_id) -> dict | None:
    """按 id 从 `agent.models` 里找一条；找不到返回 None。"""
    if not model_id:
        return None
    for entry in models or []:
        if isinstance(entry, dict) and str(entry.get("id") or "") == str(model_id):
            return entry
    return None


__all__ = [
    "LLM_CONFIG",
    "MODEL_ENTRY_KEYS",
    "find_model_entry",
    "model_label",
    "resolve_llm_config",
    "resolve_model_entry",
]
