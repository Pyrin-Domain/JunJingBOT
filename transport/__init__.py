"""OneBot 连接层：只懂 OneBot / WebSocket 协议，不懂业务。

支持 NapCat / SnowLuma；后端差异在 ``config.backends``，这里只做协议。

公开入口：

    from transport import ConnectionSettings, OneBotConnection, OneBotAPI, listen
    from transport import NapCatConnection, NapCatAPI          # 旧名字别名
    from transport import load_connection_config, DEFAULT_CALL_TIMEOUT

正向 ``ws`` / 反向 ``ws_re`` 两种模式对外接口一致；事件监听见 :mod:`transport.listener`。
"""

from .api import NapCatAPI, NapCatAPIInterface, OneBotAPI
from .config import (
    DEFAULT_BACKEND,
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_MODE,
    DEFAULT_RETRY_SECONDS,
    MODE_FORWARD,
    MODE_REVERSE,
    PING_INTERVAL,
    PING_TIMEOUT,
    BackendProfile,
    ConnectionConfig,
    ConnectionSettings,
    backend_label,
    backend_options,
    coerce_connection_settings,
    connection_dict,
    get_backend,
    load_connection_config,
    mode_summary,
    normalize_backend,
    normalize_mode,
    settings_from_any,
)
from .connection import NapCatConnection, NapCatReverseWS, OneBotConnection
from .listener import listen, listen_msg

__all__ = [
    "OneBotAPI",
    "OneBotConnection",
    "NapCatAPI",
    "NapCatAPIInterface",
    "NapCatConnection",
    "NapCatReverseWS",
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
    "listen",
    "listen_msg",
    "load_connection_config",
    "mode_summary",
    "normalize_backend",
    "normalize_mode",
    "settings_from_any",
]
