"""图片引用模型。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path


class ImageKind(str, Enum):
    LOCAL = "local"        # 本机文件
    REMOTE = "remote"      # http(s) URL
    BASE64 = "base64"      # base64:// 或 data:
    FILE_URL = "file_url"  # file://
    OTHER = "other"


@dataclass(frozen=True)
class ImageRef:
    """一张图片的两种视角：本机路径 + 协议端路径。

    - ``bot_path``：Bot 进程能读写的路径（远程图片为 None）
    - ``napcat_path``：发给协议端时用的字符串（本地文件已按 env 转换）；
      通用名字是 :attr:`backend_path`
    """

    raw: str
    kind: ImageKind
    napcat_path: str
    bot_path: Path | None = None
    summary: str = ""

    @property
    def backend_path(self) -> str:
        """``napcat_path`` 的通用名字（NapCat / SnowLuma 都适用）。"""
        return self.napcat_path

    @property
    def is_local(self) -> bool:
        return self.kind is ImageKind.LOCAL and self.bot_path is not None

    @property
    def is_remote(self) -> bool:
        return self.kind is ImageKind.REMOTE

    def with_summary(self, summary: str) -> "ImageRef":
        return replace(self, summary=str(summary or ""))

    def __str__(self) -> str:
        return self.napcat_path

    def __fspath__(self) -> str:
        if self.bot_path is None:
            raise TypeError("远程图片没有本机路径")
        return str(self.bot_path)


__all__ = ["ImageKind", "ImageRef"]
