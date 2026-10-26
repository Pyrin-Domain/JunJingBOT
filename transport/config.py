"""OneBot 连接配置与连接层常量。

配置文件解析本身在 ``config.connection``（``ConnectionSettings``），这里只做两件事：

1. 把配置对象 / 字典 / 路径统一成 ``ConnectionSettings``（旧 ``NapCatBotConfig`` 的行为）；
2. 提供连接层用到的超时常量（重连间隔、API 超时、心跳）。

两种接法（与旧版一致）：

    "type": "ws_re"   反向：Bot 起 WS 服务器，NapCat / SnowLuma 主动连过来
    "type": "ws"      正向：Bot 主动连 NapCat / SnowLuma

后端差异（历史游标、默认端口）见 ``config.backends``。这部分只懂协议，
不 import 任何业务模块。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config.backends import (
    DEFAULT_BACKEND,
    BackendProfile,
    backend_label,
    backend_options,
    get_backend,
    normalize_backend,
)
from config.connection import (
    DEFAULT_MODE,
    MODE_FORWARD,
    MODE_REVERSE,
    ConnectionSettings,
    normalize_mode,
)

# 连接层常量（旧 Websockets.py 的默认值，行为不变）
DEFAULT_RETRY_SECONDS = 5.0   # 正向连接断开后的重试间隔
DEFAULT_CALL_TIMEOUT = 30.0   # call_api 等响应的超时
PING_INTERVAL = 30.0          # WS 心跳间隔
PING_TIMEOUT = 10.0           # WS 心跳超时

# 旧名字，方便从旧代码迁移过来时少改一行
ConnectionConfig = ConnectionSettings


def coerce_connection_settings(
    config: ConnectionSettings | dict | str | Path | None,
    *,
    mode: str | None = None,
) -> ConnectionSettings:
    """把「配置对象 / dict / 文件路径 / None」统一成 :class:`ConnectionSettings`。

    ``mode`` 只在配置里没有 ``type`` 时生效（与旧 ``NapCatBotConfig.from_dict`` 一致）。
    """
    if isinstance(config, ConnectionSettings):
        return config

    if isinstance(config, dict):
        raw = dict(config)
        if mode is not None and raw.get("type") is None:
            raw["type"] = mode
        return ConnectionSettings(raw)

    if isinstance(config, (str, Path)):
        loaded = ConnectionSettings.from_file(config)
        if mode is not None and loaded.raw().get("type") is None:
            raw = loaded.to_dict()
            raw["type"] = mode
            return ConnectionSettings(raw, path=loaded.path)
        return loaded

    if config is None:
        raw = {"type": mode} if mode is not None else {}
        return ConnectionSettings(raw)

    raise TypeError(f"不支持的连接配置类型：{type(config).__name__}")


def load_connection_config(
    path: str | Path, *, required: bool = False
) -> ConnectionSettings:
    """按文件读一个连接配置（config.json / sender_config.json）。"""
    return ConnectionSettings.from_file(path, required=required)


def settings_from_any(
    config: ConnectionSettings | dict | str | Path | None,
    *,
    mode: str | None = None,
) -> ConnectionSettings:
    """``coerce_connection_settings`` 的别名（语义更直白）。"""
    return coerce_connection_settings(config, mode=mode)


def mode_summary(settings: ConnectionSettings) -> str:
    """一行描述，日志与网页面板共用。"""
    return f"{settings.mode_label} {settings.endpoint}"


def connection_dict(settings: ConnectionSettings) -> dict[str, Any]:
    """给网页面板 / 日志用的字典（不含 token 明文）。"""
    data = settings.to_dict()
    if data.get("access_token"):
        data["access_token"] = "***"
    return data


__all__ = [
    "BackendProfile",
    "ConnectionConfig",
    "ConnectionSettings",
    "DEFAULT_BACKEND",
    "DEFAULT_CALL_TIMEOUT",
    "DEFAULT_MODE",
    "DEFAULT_RETRY_SECONDS",
    "MODE_FORWARD",
    "MODE_REVERSE",
    "PING_INTERVAL",
    "PING_TIMEOUT",
    "backend_label",
    "backend_options",
    "coerce_connection_settings",
    "connection_dict",
    "get_backend",
    "load_connection_config",
    "mode_summary",
    "normalize_backend",
    "normalize_mode",
    "settings_from_any",
]
