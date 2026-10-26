"""图片模块：统一解析本地/远程图片，并处理 WSL / Windows 路径转换。

业务代码只需要：

    from images import resolve, to_backend
    resolve("{imgs_dir}/妈妈.gif")   # -> ImageRef
    to_backend(local_path)            # -> 发给协议端的路径

``to_napcat`` 是 ``to_backend`` 的旧名字，仍然可用。

启动时由 ``bot.app`` 调用 ``images.configure(resolver)``；
如果没配置就调用，会得到明确的报错，而不是悄悄用错路径。
"""

from __future__ import annotations

from typing import Any

from config.paths import AppPaths
from config.settings import BotSettings

from .models import ImageKind, ImageRef
from .resolver import ImageResolver
from .sender import image_message, image_segment
from .store import ImageStore

_resolver: ImageResolver | None = None


def configure(resolver: ImageResolver) -> ImageResolver:
    """安装全局解析器（启动时调用一次）。"""
    global _resolver
    _resolver = resolver
    return resolver


def configure_from_settings(settings: BotSettings) -> ImageResolver:
    return configure(ImageResolver(settings.paths, settings.bridge))


def get_resolver() -> ImageResolver:
    if _resolver is None:
        raise RuntimeError(
            "images 尚未初始化：启动时请调用 images.configure_from_settings(settings)，"
            "或手动 images.configure(ImageResolver(paths, bridge))。"
        )
    return _resolver


def resolve(value: str | Any, **kwargs: Any) -> ImageRef:
    return get_resolver().resolve(value, **kwargs)


def to_backend(value: str | Any) -> str:
    return get_resolver().to_backend(value)


def to_napcat(value: str | Any) -> str:
    """``to_backend`` 的旧名字。"""
    return to_backend(value)


def local(value: str | Any):
    return get_resolver().local(value)


def expand(value: str) -> str:
    return get_resolver().expand(value)


def dress(name: str):
    return get_resolver().dress(name)


__all__ = [
    "ImageKind",
    "ImageRef",
    "ImageResolver",
    "ImageStore",
    "configure",
    "configure_from_settings",
    "get_resolver",
    "resolve",
    "to_backend",
    "to_napcat",
    "local",
    "expand",
    "dress",
    "image_segment",
    "image_message",
]
