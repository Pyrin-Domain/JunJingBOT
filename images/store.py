"""图片目录管理：imgs/、imgs/dress/。"""

from __future__ import annotations

from pathlib import Path

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class ImageStore:
    def __init__(self, images_dir: str | Path):
        self.root = Path(images_dir)

    @property
    def dress_dir(self) -> Path:
        return self.root / "dress"

    def ensure(self) -> None:
        self.dress_dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str | Path, *, base: Path | None = None) -> Path:
        path = Path(str(name)).expanduser()
        if not path.is_absolute():
            path = (base or self.root) / path
        return path.resolve(strict=False)

    def dress(self, name: str | Path) -> Path:
        return self.path(name, base=self.dress_dir)

    def exists(self, name: str | Path) -> bool:
        return self.path(name).is_file()

    def iter_images(self, *, include_dress: bool = False) -> list[Path]:
        if not self.root.is_dir():
            return []
        result = [
            item
            for item in sorted(self.root.iterdir())
            if item.is_file() and item.suffix.lower() in _IMAGE_SUFFIXES
        ]
        if include_dress and self.dress_dir.is_dir():
            result.extend(
                item
                for item in sorted(self.dress_dir.iterdir())
                if item.is_file() and item.suffix.lower() in _IMAGE_SUFFIXES
            )
        return result


__all__ = ["ImageStore"]
