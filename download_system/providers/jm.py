"""``jm`` 下载源：返回一张本地占位 jpg，**不做任何抓取**。

用途只有一个：让 ``/jm`` 命令 → 下载池 → 私聊发文件这条链路能被完整跑通和测试，
而不用碰任何第三方站点。

刻意遵守的约束（改代码前先读）：

1. ``download()`` 不发起网络请求，也不 import 任何爬虫包；
2. 只返回仓库内的静态占位图（或 ``JM_PLACEHOLDER_FILE`` 指定的本地文件）；
3. 真要接真实来源，前提是内容合法、可分发 —— 不要在这个文件里接。

接口约定（与 :class:`~download_system.providers.base.BaseProvider` 一致）：

    task.source   → 作品标识（只做字符校验，会写进 ``meta["source"]``）
    task.options  → 预留：``output_dir`` / ``filename``，当前未使用
    return        → DownloadResult(path=占位图, mime="image/jpeg",
                                   meta={"source": ..., "placeholder": True})
"""

from __future__ import annotations

import os
from pathlib import Path

from ..storage import DownloadStorage
from ..task import DownloadResult, DownloadTask
from .base import BaseProvider
from Extension import get_jmcomic_pdf

__all__ = ["JMProvider", "JmNotConfigured", "normalize_source", "PLACEHOLDER_FILE"]

#: 仓库内的占位图（跟着包走，不依赖 config / environment.json）
PLACEHOLDER_FILE = Path(__file__).resolve().parent / "assets" / "jm_placeholder.jpg"

#: 想换成自己的图：设这个环境变量指向任意本地 jpg
PLACEHOLDER_ENV = "JM_PLACEHOLDER_FILE"


class JmNotConfigured(NotImplementedError):
    """占位图缺失 / 不可用。"""


def normalize_source(source: str) -> str:
    """校验并规范化作品标识。

    只做"是不是一个干净的短标识"这种字符级校验，不含任何站点语义。
    """
    value = str(source or "").strip()
    if not value:
        raise ValueError("jm 参数不能为空")
    if len(value) > 64:
        raise ValueError(f"jm 参数过长: {len(value)} 字符")
    if not value.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"jm 参数不合法: {value!r}")
    return value


class JMProvider(BaseProvider):
    kind = "jm"

    #: 能产出结果（占位图）→ True。缺图时 ``ensure_ready()`` 会抛。
    ready: bool = True

    @classmethod
    def placeholder_path(cls) -> Path:
        override = str(os.environ.get(PLACEHOLDER_ENV, "") or "").strip()
        return Path(override).expanduser() if override else PLACEHOLDER_FILE

    @classmethod
    def ensure_ready(cls) -> Path:
        """占位图必须真实存在，否则不进队列（避免重试把原因埋掉）。"""
        path = cls.placeholder_path()
        if not path.is_file():
            raise JmNotConfigured(
                f"jm 占位图不存在：{path}"
                f"（可用环境变量 {PLACEHOLDER_ENV} 指定别的本地图片）"
            )
        return path

    async def download(
        self, task: DownloadTask, storage: DownloadStorage
    ) -> DownloadResult:
        source = normalize_source(task.source)
        # path = self.ensure_ready()
        # 刻意不抓取：这里只把静态占位图当作“下载产物”。
        file_name = await get_jmcomic_pdf(source)
        path=Path(file_name[0])
        return DownloadResult(
            path=path,
            size=path.stat().st_size,
            mime="file/pdf",
            meta={"source": source, "placeholder": True},
        )
