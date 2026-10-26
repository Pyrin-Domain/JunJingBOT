"""把 URL / 本地路径 / base64 统一解析成可发送的 ImageRef。

这是业务层唯一需要认识的图片入口：

    ref = resolver.resolve("{imgs_dir}/妈妈.gif")
    ref.napcat_path      # 发给 NapCat 的字符串
    ref.bot_path         # 本机路径（如果需要读文件）

路径转换（Windows ↔ WSL）由 PathBridge 负责，本模块不判断环境。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config.environment import PathBridge
from config.paths import AppPaths

from .models import ImageKind, ImageRef
from .store import ImageStore


class ImageResolver:
    def __init__(
        self,
        paths: AppPaths,
        bridge: PathBridge,
        *,
        images_dir: str | Path | None = None,
    ):
        self.paths = paths
        self.bridge = bridge
        target = Path(images_dir) if images_dir is not None else paths.images_dir
        if not target.is_absolute():
            target = paths.root / target
        self.store = ImageStore(target)

    # ── 占位符 ──
    @property
    def images_dir(self) -> Path:
        return self.store.root

    def expand(self, value: Any) -> str:
        """替换配置里的 ``{imgs_dir}`` / ``{root}`` / ``{downloads_dir}`` / ``{temp_dir}``。"""
        text = str(value or "")
        replacements = {
            "{imgs_dir}": str(self.store.root),
            "{images_dir}": str(self.store.root),
            "{root}": str(self.paths.root),
            "{downloads_dir}": str(self.paths.downloads_dir),
            "{temp_dir}": str(self.paths.temp_dir),
            "{data_dir}": str(self.paths.data_dir),
        }
        for key, replacement in replacements.items():
            text = text.replace(key, replacement)
        return text

    # ── 解析 ──
    def resolve(
        self,
        value: str | Path | ImageRef,
        *,
        base: str | Path | None = None,
        summary: str = "",
    ) -> ImageRef:
        if isinstance(value, ImageRef):
            return value.with_summary(summary) if summary else value

        text = self.expand(value).strip()
        if not text:
            return ImageRef(raw="", kind=ImageKind.OTHER, napcat_path="", summary=summary)

        lowered = text.lower()
        if lowered.startswith(("http://", "https://")):
            return ImageRef(text, ImageKind.REMOTE, text, summary=summary)
        if lowered.startswith(("base64://", "data:")):
            return ImageRef(text, ImageKind.BASE64, text, summary=summary)
        if lowered.startswith("file://"):
            return ImageRef(text, ImageKind.FILE_URL, text, summary=summary)

        path = self.bridge.expand(text, base=base or self.paths.root)
        return ImageRef(
            raw=text,
            kind=ImageKind.LOCAL,
            napcat_path=self.bridge.to_backend(path),
            bot_path=path,
            summary=summary,
        )

    # ── 便捷方法 ──
    def to_backend(self, value: str | Path | ImageRef) -> str:
        """发给协议端的路径（``to_napcat`` 的通用名字）。"""
        return self.resolve(value).napcat_path

    def to_napcat(self, value: str | Path | ImageRef) -> str:
        return self.to_backend(value)

    def local(self, value: str | Path | ImageRef) -> Path:
        ref = self.resolve(value)
        if ref.bot_path is None:
            raise ValueError(f"不是本地图片：{ref.raw}")
        return ref.bot_path

    def exists(self, value: str | Path | ImageRef) -> bool:
        ref = self.resolve(value)
        return bool(ref.bot_path and ref.bot_path.is_file())

    def dress(self, name: str | Path) -> ImageRef:
        path = self.store.dress(name)
        return self.resolve(path)

    def list_images(self, *, include_dress: bool = False) -> list[Path]:
        return self.store.iter_images(include_dress=include_dress)


__all__ = ["ImageResolver"]
