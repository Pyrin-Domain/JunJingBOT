"""
Paths —— 项目目录约定（配置 / 数据 / 前端 各一个文件夹）

新布局：

    config/   所有配置：config.json、sender_config.json、bot_config.json、paths_config.json
    data/     所有运行时数据：forward_dedup.db(+wal/shm)、forward_dedup_log.jsonl、check_point/
    webui/    网页参数面板的前端（index.html / app.js / style.css）

兼容旧布局：老位置（根目录的 config.json、forward_dedup.db、check_point/，
以及 Minitor/web_panel/）只要还在、新位置又没有对应文件，就继续用老的，
所以直接升级不会因为找不到文件而崩。两个位置都有时**优先新布局**。

统一从这里取路径，别在各模块里写死：

    from Paths import Paths
    Paths.config_file("sender_config.json")
    Paths.data_file("forward_dedup.db")
    Paths.check_point_dir()
    Paths.webui_dir()
"""
from pathlib import Path

# 仓库根目录（Minitor/ 的上一层）
ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
WEBUI_DIR = ROOT / "webui"

# 旧位置（只读兼容，不再往里写新东西）
_LEGACY_WEBUI_DIR = ROOT / "Minitor" / "web_panel"
_LEGACY_CHECK_POINT_DIR = ROOT / "check_point"


def _pick(new: Path, legacy: Path) -> Path:
    """新位置优先；新位置没有、老位置有 → 用老位置（兼容旧部署）。"""
    if new.exists():
        return new
    if legacy.exists():
        return legacy
    return new


class Paths:
    """项目里所有“文件放哪儿”的问题都问这里。"""

    # ── 目录 ──
    @staticmethod
    def root() -> Path:
        return ROOT

    @staticmethod
    def config_dir() -> Path:
        return CONFIG_DIR

    @staticmethod
    def data_dir() -> Path:
        """运行时数据目录（新：data/；老文件还散在根目录时返回根目录）。"""
        if DATA_DIR.exists():
            return DATA_DIR
        if (ROOT / "forward_dedup.db").exists() or _LEGACY_CHECK_POINT_DIR.exists():
            return ROOT
        return DATA_DIR

    @staticmethod
    def webui_dir() -> Path:
        """前端页面目录（新：webui/，旧：Minitor/web_panel/）。"""
        return _pick(WEBUI_DIR, _LEGACY_WEBUI_DIR)

    @staticmethod
    def check_point_dir() -> Path:
        """断点目录（新：data/check_point/，旧：check_point/）。"""
        return _pick(DATA_DIR / "check_point", _LEGACY_CHECK_POINT_DIR)

    # ── 具体文件 ──
    @staticmethod
    def config_file(name: str = "config.json") -> Path:
        """config/ 下的配置文件；老根目录的同名文件仍然认。"""
        path = Path(name)
        if path.is_absolute() or path.parent != Path("."):
            return path  # 调用方明确给了路径，原样使用
        return _pick(CONFIG_DIR / path.name, ROOT / path.name)

    @staticmethod
    def data_file(name: str) -> Path:
        """data/ 下的运行时文件（数据库、日志、断点…）。"""
        path = Path(name)
        if path.is_absolute() or path.parent != Path("."):
            return path
        return _pick(DATA_DIR / path.name, ROOT / path.name)

    @staticmethod
    def config_target(name: str = "config.json") -> Path:
        """要**写入**的配置路径：永远写新布局（config/），不往老位置写。"""
        return CONFIG_DIR / Path(name).name

    # ── 初始化 / 自检 ──
    @staticmethod
    def ensure_dirs() -> None:
        """建好三个目录（已经在用老布局也不会被搬动）。"""
        for path in (CONFIG_DIR, DATA_DIR, WEBUI_DIR, DATA_DIR / "check_point"):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # pragma: no cover
                print(f"[路径] 创建 {path} 失败: {exc}")

    @staticmethod
    def layout_report() -> str:
        """启动时打印一行，说明配置/数据到底读的哪个位置。"""
        cfg = Paths.config_file("config.json")
        parts = [f"配置目录 {cfg.parent}", f"数据目录 {Paths.data_dir()}"]
        if cfg.parent != CONFIG_DIR:
            parts.append("（还在用根目录老布局）")
        return " · ".join(parts)
