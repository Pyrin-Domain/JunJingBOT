"""OneBot v11 后端档案（NapCat / SnowLuma）。

项目本身只依赖 OneBot v11 协议；不同后端（NapCat、SnowLuma）在**历史游标**、
默认端口和提示文案上有差异。把这些差异集中在这个注册表里，``config`` 和
``transport`` 都只查表，不写 ``if backend == ...``。

约定：

- ``history_anchor`` 是历史接口的游标字段名。
  - NapCat：``message_seq``（服务端消息序号）
  - SnowLuma：``message_id``（有符号 int32 哈希，可能为负）
- 新增后端只需要在 ``BACKENDS`` 里加一项，再补一个别名即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_BACKEND = "napcat"


@dataclass(frozen=True)
class BackendProfile:
    """一个 OneBot 后端的静态描述。"""

    id: str
    label: str
    protocol: str = "onebot_v11"
    description: str = ""
    aliases: tuple[str, ...] = ()

    # 连接默认值。reverse 端口是 Bot 自己监听的端口，通常不需要跟着后端变。
    default_ws_host: str = "127.0.0.1"
    default_ws_port: int = 3001
    default_reverse_host: str = "127.0.0.1"
    default_reverse_port: int = 8085
    default_sender_port: int = 3000

    # 历史接口游标字段：message_seq / message_id
    history_anchor: str = "message_seq"

    supports_forward: bool = True
    supports_reverse: bool = True
    # 反向连接时 token 的携带方式，仅用于面板提示。
    token_mode: str = "query_or_bearer"
    notes: tuple[str, ...] = ()

    @property
    def history_anchor_param(self) -> str:
        return self.history_anchor

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "protocol": self.protocol,
            "description": self.description,
            "history_anchor": self.history_anchor,
            "supports_forward": self.supports_forward,
            "supports_reverse": self.supports_reverse,
            "token_mode": self.token_mode,
            "defaults": {
                "ws_host": self.default_ws_host,
                "ws_port": self.default_ws_port,
                "ws_reverse_host": self.default_reverse_host,
                "ws_reverse_port": self.default_reverse_port,
                "sender_port": self.default_sender_port,
            },
            "notes": list(self.notes),
        }


NAPCAT = BackendProfile(
    id="napcat",
    label="NapCat",
    description="NapCatQQ 提供的 OneBot v11 实现；历史游标使用 message_seq。",
    aliases=("napcatqq", "napcat-qq", "napcat_qq", "nc"),
    history_anchor="message_seq",
    notes=(
        "历史接口用 message_seq 翻页；message_id 只用于 get_msg / delete_msg。",
        "WebSocket 服务端默认 3001，HTTP 服务端默认 3000。",
    ),
)

SNOWLUMA = BackendProfile(
    id="snowluma",
    label="SnowLuma",
    description="SnowLuma 提供的 OneBot v11 实现；历史游标使用 message_id。",
    # 用户口头/文档里常写成 snowluna，一并接受。
    aliases=("snowluna", "snow-luma", "snow_luma", "snow", "sl", "snowluma-onebot"),
    history_anchor="message_id",
    notes=(
        "历史接口用 message_id（有符号 int32，可能为负）翻页，不认 message_seq。",
        "WebSocket 服务端默认 3001，HTTP 服务端默认 3000。",
        "Bot 自己发出的消息事件是 message_sent，监听器只处理 message。",
    ),
)

BACKENDS: dict[str, BackendProfile] = {
    NAPCAT.id: NAPCAT,
    SNOWLUMA.id: SNOWLUMA,
}

_ALIASES: dict[str, str] = {}
for _profile in BACKENDS.values():
    _ALIASES[_profile.id] = _profile.id
    for _alias in _profile.aliases:
        _ALIASES[_alias] = _profile.id


def normalize_backend(value: Any, *, default: str = DEFAULT_BACKEND) -> str:
    """把后端名 / 别名统一成注册表里的 id。

    ``default=""`` 时，无法识别会返回空串（供面板校验使用），不会静默改成 NapCat。
    """
    if isinstance(value, BackendProfile):
        return value.id
    text = str(value or "").strip().lower()
    if not text:
        return default
    key = text.replace(" ", "").replace("_", "-")
    key = key.replace("snowluna", "snowluma") if key.startswith("snowluna") else key
    if key in BACKENDS:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    # 允许 "snowluma-onebot" 这类带协议后缀的写法
    head = key.split("-", 1)[0]
    if head in BACKENDS:
        return head
    if default:
        print(
            f"[配置] 无法识别的 backend={value!r}，按 {default} 处理"
            f"（可用：{', '.join(BACKENDS)}）"
        )
    return default


def get_backend(value: Any = None) -> BackendProfile:
    """取后端档案；未知值回落到 NapCat。"""
    return BACKENDS[normalize_backend(value, default=DEFAULT_BACKEND)]


def backend_label(value: Any = None) -> str:
    return get_backend(value).label


def backend_options() -> list[dict[str, Any]]:
    """给网页面板用的后端列表（顺序稳定：NapCat 在前）。"""
    return [profile.to_dict() for profile in BACKENDS.values()]


__all__ = [
    "BACKENDS",
    "DEFAULT_BACKEND",
    "BackendProfile",
    "NAPCAT",
    "SNOWLUMA",
    "backend_label",
    "backend_options",
    "get_backend",
    "normalize_backend",
]
