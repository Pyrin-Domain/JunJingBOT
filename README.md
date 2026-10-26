# 君景 BOT（重写版）

基于 **OneBot v11** 的 QQ 机器人，默认适配 **NapCat**，同时支持 **SnowLuma**。
功能与原 `JunJingBOT` 保持一致，但按职责重新拆分，方便阅读和自行修改。

## 当前状态

- 迁移已完成：旧 10 个测试文件全部迁入，断言未改；全量测试以
  `pytest -q` 的实际输出为准。
- 旧去重库已导入并合并：**999 条指纹 / 1578 条图片**，新项目运行期产生的
  3 条真实记录已保留。
- `python -m compileall .` 通过。
- `python run.py` 启动冒烟通过：面板 `/api/status` 返回 200，Agent 15 个工具加载。
- `python -m web.standalone` 面板独立启动通过。
- 还没在真实 NapCat / 外网环境跑过的部分：实际消息流、B 站下载与 ffmpeg
  合并、点歌、Windows `winrt` OCR。上线前按 `docs/MIGRATION.md` 第 4 节
  “验收清单”逐条真机核对。

## 快速开始

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/WSL: source .venv/bin/activate
pip install -r requirements.txt

# 配置：仓库里只有 example，复制成自己的再改
cp config/config.example.json     config/config.json
cp config/bot_config.example.json config/bot_config.json
cp config/environment.example.json config/environment.json

python run.py
```

> 仓库里的 QQ 号 / 群号全是占位符（`10001`、`20001` 这种），
> 换成自己的再跑；`config/*.json` 已在 `.gitignore` 里，不会被提交。

- 网页面板：<http://127.0.0.1:8390/>
- 默认连接方式：反向 `ws_re`，Bot 监听 `127.0.0.1:8085`，等 OneBot 后端连过来
- 默认后端：`napcat`；要换 SnowLuma，在 `config/config.json` 里写
  `"backend": "snowluma"`（详见 `docs/SNOWLUMA.md`）
- 连接配置：`config/config.json`
- 业务配置：`config/bot_config.json`
- 运行环境：`config/environment.json`（`bot_env` / `backend_env` / `path_profile`；
  `onebot_env` / `napcat_env` 作为旧名字仍然可用）

## 先读哪几份

| 文件 | 内容 |
|---|---|
| `PROJECT_DESIGN.md` | 总设计、模块树、数据流、迁移计划 |
| `docs/CONFIG.md` | 配置项 + WSL/Windows 路径转换 |
| `docs/RELATIONS.md` | 人设文件 + 每个人的关系标签 / 画像 / 近期状态 / 记忆 |
| `docs/SNOWLUMA.md` | SnowLuma 接入、端口 / token / 历史游标差异 |
| `docs/DEVELOPMENT.md` | 怎么跑、怎么加功能、怎么调试 |
| `docs/MIGRATION.md` | 旧代码 → 新代码对照、验收清单 |

## 目录速览

```text
transport/         OneBot 连接与 API（NapCat / SnowLuma，只懂协议）
messaging/         消息结构、渲染、历史、收发工具
images/            图片解析 + WSL/Windows 路径转换（业务唯一入口）
media/             OCR、图片哈希
download_system/   下载池 + B 站 / 点歌
agent/             LLM、提示词、工具、模型路由
agent/personas/    角色卡 + 通用群聊行为（Markdown）
data/relations/    每个人的关系标签 / 画像 / 近期状态 / 记忆（JSON）
bot/               业务编排：主流程、命令、搬运、去重、插话
web/               后端 API
frontend/          独立前端静态站点
config/            配置层（含 config/backends.py 后端档案）
core/              无业务依赖的小工具（含 core/anchors.py 双游标）
```

依赖只能从上往下：`bot` 认识所有子系统；`transport` / `messaging` / `agent`
互不直接依赖；路径转换只发生在 `config/environment.py`。

## 测试

```bash
pytest -q
```

## 来源与致谢

**本仓库是 [`Pyrin-Domain/JunJingBOT`](https://github.com/Pyrin-Domain/JunJingBOT)
的行为等价重写版**（原仓库保持不动）。算法、配置键名、去重库结构、网页面板
API 都沿用原项目，改的是模块边界和可读性；`docs/MIGRATION.md` 里有旧文件到
新文件的逐条对照。

**JM 下载能力来自第三方开源项目
[`hect0x7/JMComic-Crawler-Python`](https://github.com/hect0x7/JMComic-Crawler-Python)
（MIT License，Copyright © 2023 hect0x7）**：

- 该项目的源码以 vendored 形式放在 `Extension/jm_crawer/`，未做修改，
  许可证原文见 `Extension/jm_crawer/LICENSE`；
- `download_system/providers/jm.py` 通过 `Extension.get_jmcomic_pdf()` 调用它；
- 本仓库**不包含**该项目的下载产物（`Extension/jm_crawer/download/` 已忽略），
  也不对其抓取的内容做任何分发；
- 若只需官方实现，可直接 `pip install jmcomic`，删掉 `Extension/jm_crawer/`
  后改 `Extension/__init__.py` 的导入即可。

其余第三方依赖见 `requirements.txt`。

## 说明

这是对现有项目的**行为等价重写**：算法、配置键名、去重库结构、网页面板 API
都保持不变；改变的是模块边界和可读性。旧项目目录 `D:\Code\JunJingBOT` 不会被修改。
