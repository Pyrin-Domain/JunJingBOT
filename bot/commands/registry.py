"""action 名 → 处理函数。

``bot_config.json`` 的 ``keyword_actions[].action`` 就用这里的名字。
加新命令：在 :mod:`bot.commands` 里实现，然后在这里登记一个名字。

**注意**：运行时的 action 表是 :meth:`bot.app.BotApp.action_registry` 里手写的那份
dict（历史原因），这里的 :func:`build_action_registry` 只是它的镜像/测试用。
**两处都要加**，漏了 app.py 那份就会报「未知 action」。

名字与旧 ``BOTMain._action_registry()`` 完全一致：

    send_message / send_img / get_long_history_test / set_tokenizer / ocr /
    export / recall / fabric / get_music / img_forward_create_task /
    try_send_bili_video / try_send_reply_bili_card / jm / sex_filter_test /
    audit_release

``mp`` 上暂时没有的方法（并行迁移期间）保留键但值为 ``None``，
``bot.keyword.build_patterns`` 会像「未知 action」一样跳过并打印。
"""

from __future__ import annotations

__all__ = ["build_action_registry", "ACTION_NAMES"]

# MessageProcessor 直接提供的 action（旧 BOTMain 里就是绑到 mp 的方法）
_MP_ACTIONS = (
    "send_message",
    "send_img",
    "get_long_history_test",
    "set_tokenizer",
    "ocr",
    "try_send_bili_video",
    "try_send_reply_bili_card",
)

# 命令门面提供的 action
_COMMAND_ACTIONS = (
    "export",
    "recall",
    "fabric",
    "get_music",
    "img_forward_create_task",
    "jm",
    "sex_filter_test",
    "audit_release",
)

# BotApp 直接持有的服务（不在 CommandHandlers 门面上）：
# 运行时以 bot.app.BotApp.action_registry 里那份为准，这里只为名字对齐。
_SERVICE_ACTIONS = ("psychology",)

ACTION_NAMES = _MP_ACTIONS + _COMMAND_ACTIONS + _SERVICE_ACTIONS


def build_action_registry(handlers, mp=None) -> dict:
    """构造 action 表；``mp`` 缺省用 ``handlers.mp``。"""
    processor = mp if mp is not None else getattr(handlers, "mp", None)
    registry: dict = {}
    for name in _MP_ACTIONS:
        registry[name] = getattr(processor, name, None) if processor is not None else None
    # export / recall / fabric / img_forward_create_task 是门面方法；
    # get_music 在门面上同时有旧名字 GetMusic，这里统一用新名字。
    for name in _COMMAND_ACTIONS:
        registry[name] = getattr(handlers, name, None)
    for name in _SERVICE_ACTIONS:
        registry[name] = getattr(handlers, name, None)
    return registry
