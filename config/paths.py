"""项目目录约定。

所有“文件放哪儿”的问题都问这里：

    paths = AppPaths.discover()
    paths.config_file("bot_config.json")
    paths.data_file("forward_dedup.db")
    paths.ensure_dirs()

新项目只有一套布局，不再兼容旧仓库的散落文件；但读取配置时仍会
在“新位置没有、项目根目录有”的情况下回退到根目录，方便把旧配置
直接复制过来。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    root: Path
    config_dir: Path
    data_dir: Path
    frontend_dir: Path
    images_dir: Path
    downloads_dir: Path
    temp_dir: Path
    logs_dir: Path
    checkpoint_dir: Path

    # ── 构造 ──
    @classmethod
    def discover(cls, root: str | Path | None = None) -> "AppPaths":
        base = Path(root).resolve() if root is not None else Path(__file__).resolve().parent.parent
        data = base / "data"
        return cls(
            root=base,
            config_dir=base / "config",
            data_dir=data,
            frontend_dir=base / "frontend",
            images_dir=base / "imgs",
            downloads_dir=data / "downloads",
            temp_dir=data / "tmp",
            logs_dir=base / "logs",
            checkpoint_dir=data / "check_point",
        )

    def with_overrides(
        self,
        *,
        images_dir: str | Path | None = None,
        downloads_dir: str | Path | None = None,
        temp_dir: str | Path | None = None,
    ) -> "AppPaths":
        """按 environment.json 覆盖目录（相对路径按项目根解析）。"""
        def resolve(value: str | Path | None, fallback: Path) -> Path:
            if value is None or str(value).strip() == "":
                return fallback
            path = Path(str(value)).expanduser()
            if not path.is_absolute():
                path = self.root / path
            return path.resolve(strict=False)

        return replace(
            self,
            images_dir=resolve(images_dir, self.images_dir),
            downloads_dir=resolve(downloads_dir, self.downloads_dir),
            temp_dir=resolve(temp_dir, self.temp_dir),
        )

    # ── 目录 ──
    def ensure_dirs(self) -> None:
        for path in (
            self.config_dir,
            self.data_dir,
            self.downloads_dir,
            self.temp_dir,
            self.logs_dir,
            self.checkpoint_dir,
            self.images_dir,
            self.images_dir / "dress",
        ):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # pragma: no cover
                print(f"[路径] 创建 {path} 失败：{exc}")

    # ── 具体文件 ──
    def config_file(self, name: str = "config.json") -> Path:
        """配置文件：优先 config/，其次项目根目录（旧布局兼容）。"""
        path = Path(name)
        if path.is_absolute() or path.parent != Path("."):
            return path
        new = self.config_dir / path.name
        legacy = self.root / path.name
        if new.exists():
            return new
        if legacy.exists():
            return legacy
        return new

    def config_target(self, name: str = "config.json") -> Path:
        """要写入的配置路径：永远是 config/。"""
        return self.config_dir / Path(name).name

    def data_file(self, name: str) -> Path:
        path = Path(name)
        if path.is_absolute() or path.parent != Path("."):
            return path
        new = self.data_dir / path.name
        legacy = self.root / path.name
        if new.exists():
            return new
        if legacy.exists():
            return legacy
        return new

    def frontend_file(self, name: str = "index.html") -> Path:
        path = Path(name)
        if path.is_absolute():
            return path
        return self.frontend_dir / path

    def layout_report(self) -> str:
        cfg = self.config_file("config.json")
        parts = [f"配置目录 {cfg.parent}", f"数据目录 {self.data_dir}"]
        if cfg.parent != self.config_dir:
            parts.append("（正在使用项目根目录的旧配置）")
        return " · ".join(parts)


__all__ = ["AppPaths"]
