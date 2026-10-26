"""关键词 → 可执行 pattern。

旧 ``BOTMain.build_keyword_pattern_config()`` 的等价物，只做两件事：

1. 把 ``settings.keyword_actions`` 里的 ``action`` 名换成真正的处理函数；
2. 把 ``params`` 里的 ``{imgs_dir}`` 等占位符交给 ``images.expand`` 展开。

未知 action 跳过并打印，和旧版一致 —— 配置写错不该让整条消息处理链崩掉。
"""

from __future__ import annotations

__all__ = ["build_patterns", "match"]


def _keyword_actions(settings) -> list:
    if settings is None:
        return []
    value = getattr(settings, "keyword_actions", None)
    if value is not None:
        return list(value or [])
    if isinstance(settings, dict):
        value = settings.get("keyword_actions")
        return list(value or [])
    getter = getattr(settings, "get", None)
    if callable(getter):
        value = getter("keyword_actions", [])
        return list(value or [])
    return []


def _expander(images, settings):
    """优先用 images 层展开占位符；没注入时退回配置里的图片目录。"""
    expand = getattr(images, "expand", None)
    if callable(expand):
        return expand
    paths = getattr(settings, "paths", None)
    images_dir = getattr(paths, "images_dir", "") if paths is not None else ""

    def _expand(value: str) -> str:
        text = str(value or "")
        if images_dir:
            text = text.replace("{imgs_dir}", str(images_dir))
            text = text.replace("{images_dir}", str(images_dir))
        return text

    return _expand


def build_patterns(settings, registry, images=None) -> list[dict]:
    """把配置里的 ``keyword_actions`` 转成 ``mp.pattern_match`` 用的配置。

    :param settings: BotSettings（或行为相同的 dict）
    :param registry: action 名 → 处理函数（见 bot.commands.registry）
    :param images: images 模块 / ImageResolver（提供 ``expand``）
    :return: ``[{"solution": callable, "regex": str, "params": {...}}, ...]``
    """
    registry = registry or {}
    expand = _expander(images, settings)
    patterns: list[dict] = []
    for item in _keyword_actions(settings):
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "")
        solution = registry.get(action)
        if solution is None:
            print(f"[关键词] 未知 action={action!r}（regex={item.get('regex')!r}），已跳过")
            continue
        params = {
            key: (expand(value) if isinstance(value, str) else value)
            for key, value in (item.get("params") or {}).items()
        }
        pattern: dict = {"solution": solution, "regex": item.get("regex")}
        if params:
            pattern["params"] = params
        patterns.append(pattern)
    return patterns


async def match(mp, event, patterns) -> bool:
    """把消息交给 MessageProcessor 做关键词匹配，返回是否命中。

    ``MessageProcessor.pattern_match`` 是 ``async``，且会按参数名自动决定是否
    传 ``event``（旧行为）；这里只负责转发，不重复实现匹配规则。
    """
    if not patterns:
        return False
    return bool(await mp.pattern_match(event, patterns))
