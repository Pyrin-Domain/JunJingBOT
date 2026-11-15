# VENDOR.md · bilibilidownloadtool

上游仓库：<https://github.com/NANblogink/bilibilidownloadtool>
接入方式：源码内嵌（vendored），**裁剪为无界面版本**

| 项 | 值 |
|---|---|
| 上游版本 | `2.2.5`（`version_info.json`） |
| 接入 commit | `2a9e8d3dbf26e6365f1373ca762e8fd55daffd5f`（2026-10-29） |
| 许可证 | MIT（见 `LICENSE`） |
| 接入日期 | 2026-10-31 |

---

## 1. 为什么要裁剪

上游是给**人**用的 PyQt5 桌面程序（`main.py` / `ui.py` / 各种 `*_tab.py`），
仓库 247MB 主要是随包的 ffmpeg / mpv / Bento4 二进制。机器人只需要其中
「解析 → 下载 → 合并」三段能力，因此：

- **保留**：解析层（`parsers/bilibili_parser.py`）、配置层（`core/config.py`）、
  工具探测（`core/tool_manager.py`）、HTTP/WBI 基础设施（`core/infra/`）、
  `_pathsetup.py`（`core/config.py` 依赖它）。
- **移除**：所有 GUI（`main.py`、`ui.py`、`boot*.py`、`parsers/*_tab.py`、
  `parsers/stream_player.py`、`parsers/recording_tray.py`、`parsers/toast_winrt.py`）、
  Qt 下载引擎（`core/downloader.py`，用 `QThread` 实现，本项目的
  `download_system` 已自带并发与重试）、`core/cloud_service.py`（自带更新检查
  与日志上报，机器人不需要也不应主动外联）、PyQt 引导与部署脚本、随包二进制
  （ffmpeg / mpv / Bento4 / wheels / runtime）。
- **替换**：`core/utils.py`（顶层 `import PyQt5`）→ 本目录下的 `utils.py`
  （仅保留上游惰性调用的 `verify_and_ensure_save` / `get_unique_filename`）。

Bento4 只在下载**加密**流（付费 / 充电内容）时才需要；没有它时合并会明确报错，
不会静默产出坏文件。需要时可自行放入 `core/infra` 之外的任意路径并由上游
`tool_manager` 探测到。

## 2. 本项目新增的文件

| 文件 | 作用 |
|---|---|
| `api.py` | 唯一桥接层：`BiliClient.parse()` / `BiliClient.download()`；所有 `sys.path` 与上游差异都收口在这里 |
| `utils.py` | 上游 `core/utils.py` 的无 Qt 替代 |
| `__init__.py` | 轻量再导出，`import` 不会加载 50 万字符的解析器 |

## 3. 桥接层踩到的三个上游行为

改这里之前务必先读 `api.py` 顶部的模块文档，摘要：

1. **扁平导入** —— 上游用 `from wbi_sign import ...`，必须把 `core/`、
   `core/infra/`、`parsers/` 放进 `sys.path`。本项目用 **append**，保证项目
   自己的 `config` 包永远优先。
2. **`config` 同名** —— 上游配置模块也叫 `config.py`，桥接层用私有名
   `_bdt_upstream_config` 加载，不占用 `sys.modules["config"]`。
3. **分片下载不回填路径** —— `download_file()` 在文件 ≥2MB 且 CDN 支持
   Range 时走分片并发分支，该分支**直接 return 临时文件路径**，不移动到
   `save_path`。桥接层的 `_adopt()` 负责搬运，否则文件会堆在
   `<盘符>:\_bili_cache`。

## 4. 与项目的接口

```python
from Extension.bilibilidownloadtool import BiliClient

client = BiliClient(config_dir="data/bilibili", cookie_file=None)
info = client.parse("https://www.bilibili.com/video/BV...")   # VideoInfo
out = client.download(
    "https://www.bilibili.com/video/BV...",
    "data/downloads/xxx.mp4",
    quality="auto",      # 或 "1080p" / 720
    max_qn=80,           # 机器人默认封顶 1080P，避免 4K/8K 把群文件塞爆
    prefer_codec="h264", # H.264 在手机上兼容性最好，且可直接 copy 不转码
)
out.path, out.has_audio, out.warnings
```

真正的下载入口是 `download_system/providers/bilibili.py`；业务层不要直接
new `BiliClient`。

## 5. 升级步骤

```bash
git clone --filter=blob:none --no-checkout --depth 1 \
    https://github.com/NANblogink/bilibilidownloadtool.git /tmp/bdt
cd /tmp/bdt && git sparse-checkout init --cone
git sparse-checkout set core parsers
git checkout master
```

然后按第 1 节的「保留」清单覆盖本目录（`api.py` / `utils.py` / `__init__.py`
不要被覆盖），重跑 `pytest tests/test_bili_extension.py`，再用真实链接跑一次
`client.parse()` + `client.download()`。
