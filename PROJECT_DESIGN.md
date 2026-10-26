# 君景 BOT · 重写项目设计

> 目标：把 `D:\Code\JunJingBOT` 的**全部现有行为**迁移到 `D:\Code\JungJingNew`，
> 但用一套能让人读懂、能自己改的模块结构。
>
> 本文件是总设计。先读它，再读 `docs/CONFIG.md`、`docs/DEVELOPMENT.md`。
> 旧项目只作为行为参考，不修改。

---

## 0. 这次重写到底改什么

**不改**：

- NapCat / OneBot 协议、`ws` / `ws_re` 两种连接方式
- 消息处理顺序：断点 → 搬运去重 → AI（被 @ / 敏感期回复）→ 关键词
- 去重数据库结构、指纹算法、断点 JSON 语义
- `bot_config.json` 的配置键名、网页面板的 API 形状
- Agent 的输出协议（`<status>` / `<<message>` / `<next>`）和 13 个工具
- 现有 208 个测试锁住的行为

**改**：

- 目录按职责拆分，不再有 `Minitor/` 这种“什么都往里放”的目录
- 去掉所有 `sys.path.insert` 和双 import 兼容 shim
- 下载逻辑集中到 `download_system/`，带真正的下载池
- 图片路径集中到 `images/`，一处处理 `backend_env` / `bot_env` / `path_profile`
- 前后端分离：`web/` 只提供 API，`frontend/` 是独立静态站点
- 每个模块有 `__init__.py` 和公开 API，模块之间只通过明确接口依赖

> **迁移方式**：以旧代码为行为基准，逐模块“搬运 + 拆解 + 改名”，
> 不重新发明算法。这样既保证行为一致，又能得到清晰结构。

---

## 1. 运行方式

```text
python run.py                  # 启动 Bot + 网页面板
python -m web.standalone       # 只启动网页面板（不连 NapCat）
pytest                         # 跑测试
```

- Python 3.11+
- 依赖见 `requirements.txt`
- 默认面板：`http://127.0.0.1:8390/`
- 默认连接：`config/config.json` 的 `type`（`ws_re` 反向 / `ws` 正向）

---

## 2. 设计原则

1. **一层只做一件事**
   - `transport/` 只说 NapCat 协议，不懂业务。
   - `messaging/` 只处理消息数据、历史、转发展开，不懂 AI。
   - `agent/` 只处理 LLM、提示词、工具，不懂连接。
   - `bot/` 只做编排：按顺序调用上面几层。
   - `web/` 只做 HTTP/JSON，不写业务规则。

2. **依赖单向**
   ```text
   core  ←  config  ←  images
     ↑         ↑         ↑
   transport  messaging  media  download_system
     ↑         ↑         ↑        ↑
     └──────── bot ───────┴────────┘
                 ↑
               agent
                 ↑
                web
   ```
   下层永远不 import 上层。`bot` 是唯一允许同时认识所有子系统的层。

3. **数据结构与渲染分离**
   - `messaging/models.py` 只定义 `ChatMessage` / `ChatContext` 等结构。
   - `ChatContext.render()` 是纯函数，可重复调用，不产生副作用。
   - 图片 URL 不进提示词；只留 `[图片]`，OCR 时按 `source + index` 从结构体解析。

4. **路径只在一个地方转换**
   - 业务代码永远拿到“本机可用路径”。
   - 要发给协议端时，调用 `images.to_backend(path)`。
   - 不在任何业务模块里手写 `/mnt/d/...` 或 `D:\...`。

5. **配置只有一份真相**
   - `config/settings.py` 的 `BotSettings` 是唯一内存配置对象。
   - 所有服务共享同一个实例；面板保存后 `reload()`，下一条消息生效。
   - 启动参数（`web_panel.*`、连接方式）单独标记为“需要重启”。

6. **可测试**
   - 纯逻辑（指纹、上下文渲染、输出解析、路径转换、配置合并）不依赖网络。
   - 外部副作用（NapCat API、下载、OCR）通过小接口注入，测试用 fake。

---

## 3. 目录总览

```text
JungJingNew/
├─ run.py                       # 唯一启动入口
├─ requirements.txt
├─ pyproject.toml               # pytest / ruff / 包路径
├─ README.md                    # 五分钟上手
├─ PROJECT_DESIGN.md            # 本文件
│
├─ config/                      # 配置层
│  ├─ __init__.py               # 公开：load_settings / BotSettings / EnvConfig / PathBridge
│  ├─ defaults.py               # bot_config 默认值（与旧版一致）
│  ├─ backends.py               # NapCat / SnowLuma 差异注册表
│  ├─ environment.py            # bot_env / backend_env / path_profile / 路径绑定
│  ├─ path_profiles.py          # 路径翻译预设（same / windows_to_wsl / ...）
│  ├─ paths.py                  # AppPaths：root/config/data/frontend/imgs/downloads
│  ├─ settings.py               # BotSettings：读/写/合并/热加载/typed 访问器
│  └─ connection.py             # ConnectionSettings：ws / ws_re
│
├─ core/                        # 无业务依赖的小工具
│  ├─ __init__.py
│  ├─ logging.py                # 统一日志前缀 / 文件日志
│  ├─ jsonstore.py              # 原子写 JSON、安全读
│  ├─ pathmap.py                # 纯路径翻译引擎（最长前缀 / 边界 / 双向）
│  ├─ anchors.py                # 历史游标（message_seq / message_id）
│  └─ async_utils.py            # 后台任务、超时、重试
│
├─ AbstractionLayer/            # NapCat / SnowLuma 协议差异抽象（纯标准库）
│  ├─ __init__.py               # 公开：OneBotKernel / HistoryPager / ForwardExpander
│  ├─ kernal.py                 # 能力档案 + 统一发包 + 返回归一化 + 运行时探测
│  ├─ cursor.py                 # MessageCursor：跨后端可比的全局序号
│  ├─ history.py                # 归一化翻页（锚点剔除 / 方向自检 / count 夹取）
│  ├─ forward.py                # 统一转发展开（内联优先，否则按 id 再查）
│  └─ cli.py                    # 调试器：probe / history / recover / forward / diff / sweep
│
├─ transport/                   # OneBot 连接层（只懂协议）
│  ├─ __init__.py               # 公开：OneBotConnection / OneBotAPI（NapCat* 为旧别名）
│  ├─ config.py                 # ws / ws_re 配置解析
│  ├─ connection.py             # WebSocket 连接、重连、pending API futures
│  ├─ api.py                    # OneBot API 封装
│  └─ listener.py               # 事件监听、按消息并发分发
│
├─ messaging/                   # 消息数据层（不懂 AI）
│  ├─ __init__.py
│  ├─ models.py                 # Sender / Segment / Quote / ChatMessage / ChatContext
│  ├─ render.py                 # 把 ChatContext 渲染成紧凑文本（纯函数）
│  ├─ parse.py                  # NapCat event → ChatMessage
│  ├─ processor.py              # MessageProcessor：发送、@、关键词、历史翻页
│  ├─ history.py                # 历史拉取 / 长区间翻页
│  ├─ forward.py                # 合并转发展开 / 转发卡片解析
│  ├─ thread_history.py         # 每会话滚动历史（TTL / LRU / upsert）
│  └─ context_builder.py        # 补全引用 + 历史 → ChatContext
│
├─ images/                      # 图片路径与发送（用户要求的重点模块）
│  ├─ __init__.py               # 公开：ImageResolver / ImageRef / to_backend / resolve
│  ├─ models.py                 # ImageRef / ImageKind（backend_path 是 napcat_path 的别名）
│  ├─ resolver.py               # 本地/远程/base64 + 协议端路径翻译
│  ├─ store.py                  # imgs 目录、dress 子目录、文件命名
│  └─ sender.py                 # 构造 NapCat image 段 / summary
│
├─ media/                       # 媒体能力（OCR / 图片哈希）
│  ├─ __init__.py
│  ├─ ocr.py                    # Windows winrt / Linux tesseract + 缓存
│  ├─ image_hash.py             # ahash / dhash / 远程图片哈希
│  └─ service.py                # MediaService：OCR、图片转发、B站发送的粘合层
│
├─ download_system/             # 下载服务（用户要求的重点模块）
│  ├─ __init__.py               # 公开：DownloadService / DownloadPool / DownloadTask
│  ├─ task.py                   # 任务状态、结果、错误
│  ├─ pool.py                   # 下载池：队列 + 并发 + 重试 + 超时 + 取消
│  ├─ storage.py                # 临时/输出目录、清理策略
│  ├─ mux.py                    # ffmpeg 合并 / ffprobe 探测
│  ├─ service.py                # 高层门面：download_bilibili / download_music
│  └─ providers/
│     ├─ __init__.py
│     ├─ base.py                # Provider 协议
│     ├─ bilibili.py            # 视频信息、流选择、音视频合并
│     └─ music.py               # 点歌：搜索页 + 播放器页解析
│
├─ bot/                         # 业务编排层（唯一认识所有服务）
│  ├─ __init__.py
│  ├─ app.py                    # BotApp：组装、生命周期、心跳
│  ├─ pipeline.py               # process_message：严格保持旧顺序
│  ├─ keyword.py                # keyword_actions → 可执行 pattern
│  ├─ checkpoint.py             # 断点读写 / 定时保存 / 回放
│  ├─ commands/
│  │  ├─ __init__.py            # CommandHandlers 门面
│  │  ├─ registry.py            # action 名 → 处理函数
│  │  ├─ export.py              # export
│  │  ├─ recall.py              # recall
│  │  ├─ fabric.py              # fabric
│  │  ├─ music.py               # 点歌
│  │  └─ image_forward.py       # img_fd
│  └─ services/
│     ├─ __init__.py
│     ├─ forward.py             # 自动搬运 + 图片批量转发
│     ├─ dedup.py               # SQLite 去重库（schema/hash 完全兼容旧库）
│     ├─ dedup_rebuild.py       # 从日志重建
│     ├─ range_task.py          # -b / -e 跨消息区间
│     └─ interject.py           # 群聊主动插话
│
├─ agent/                       # LLM Agent
│  ├─ __init__.py               # 公开：QQBotAgent
│  ├─ runtime.py                # QQBotAgent：chat / maybe_interject
│  ├─ config.py                 # LLM_CONFIG 默认值 + resolve
│  ├─ router.py                 # 429 / 额度 / 降级
│  ├─ prompts.py                # SYSTEM_PROMPT + 人设槽位 + 各场景补充提示
│  ├─ relations.py              # 关系档案：标签/画像/状态过期/记忆/自动整理
│  ├─ personas/                 # 角色卡 + 通用群聊行为（Markdown）
│  ├─ output.py                 # 解析 <status>/<message>/<next>、续说、去复读
│  ├─ hints.py                  # build_action_hint / 女装照判定
│  ├─ logger.py                 # Agent/logs/日期.json
│  ├─ campus.py                 # CampusAssistant
│  └─ tools/
│     ├─ __init__.py            # build_tools(...)
│     ├─ registry.py            # 工具注册 + 调用缓存 + current_context
│     ├─ chat.py                # send_message / send_dress / silent_observe
│     ├─ media.py               # ocr_img / 图片相关
│     ├─ history.py             # get_message / get_history / 历史查询
│     ├─ moderation.py          # poke / emoji_like / set_ban / recall_msg
│     ├─ relations.py           # update_person / update_group
│     └─ methods.py             # 方法库读写
│
├─ web/                         # 后端 API（前后端分离的“后端”）
│  ├─ __init__.py
│  ├─ server.py                 # aiohttp app / 生命周期 / 静态托管
│  ├─ auth.py                   # token 中间件
│  ├─ validation.py             # 配置校验（旧 WebPanel 的规则）
│  ├─ standalone.py             # 只启动面板
│  └─ routes/
│     ├─ __init__.py            # register_routes(app)
│     ├─ status.py              # GET /api/status
│     ├─ config.py              # GET/POST /api/config, /api/validate, /api/reload
│     ├─ groups.py              # GET /api/groups
│     └─ ws_settings.py         # GET/POST /api/ws
│
├─ frontend/                    # 前端（独立静态站点，无构建）
│  ├─ index.html
│  ├─ README.md
│  └─ assets/
│     ├─ style.css
│     └─ js/
│        ├─ api.js              # fetch 封装 / token
│        ├─ state.js            # 全局状态 + dirty 标记
│        ├─ dom.js              # 小工具
│        ├─ app.js              # 启动 / tab 切换 / 保存
│        └─ tabs/
│           ├─ overview.js
│           ├─ connection.js
│           ├─ agent.js
│           ├─ forward.js
│           ├─ image_forward.js
│           ├─ interject.js
│           ├─ keywords.js
│           ├─ prompts.js
│           └─ advanced.js
│
├─ config/                      # 运行时配置（首次启动自动生成）
│  ├─ config.json
│  ├─ sender_config.json
│  ├─ bot_config.json
│  └─ environment.json
├─ data/                        # 运行时数据
│  ├─ forward_dedup.db
│  ├─ check_point/
│  ├─ relations/                 # users/<QQ>.json、groups/<群号>.json、self.json
│  ├─ downloads/
│  └─ tmp/
├─ imgs/                        # 回复图片（dress/ 为私密图，不提交）
├─ logs/
├─ tests/                       # 旧测试迁移 + 新单测
└─ docs/
   ├─ CONFIG.md                 # bot_env / backend_env / path_profile / 路径规则
   ├─ RELATIONS.md              # 人设文件 + 关系档案格式 / 维护 / 时间规则
   ├─ DEVELOPMENT.md            # 怎么跑、怎么加功能、怎么调试
   ├─ FLOWS.md                  # 每条业务流的时序
   └─ MIGRATION.md              # 旧文件 → 新文件对照 + 验收清单
```

---

## 4. 依赖规则（必须遵守）

| 模块 | 允许 import | 禁止 import |
|---|---|---|
| `core` | 标准库 | 其它所有项目模块 |
| `AbstractionLayer` | 标准库 | 其它所有项目模块（可用 `CallableTransport` 反向适配 `transport`） |
| `config` | `core` | `transport` / `bot` / `agent` / `web` |
| `images` | `core`, `config` | `transport` / `bot` / `agent` |
| `transport` | `core`, `config`, `AbstractionLayer` | `messaging` / `bot` / `agent` |
| `messaging` | `core`, `config`, `images` | `transport`（只依赖 API 协议接口）, `agent`, `bot` |
| `media` | `core`, `config`, `images`, `download_system` | `bot`, `agent` |
| `download_system` | `core`, `config` | `bot`, `messaging`, `agent` |
| `agent` | `core`, `config`, `messaging`, `media` | `bot`, `web` |
| `bot` | 所有下层 | `web` |
| `web` | `core`, `config`, `bot`（只读状态/调用 reload） | 不改业务逻辑 |

`messaging` 需要发消息时，不直接 import `transport`，而是接收一个
`NapCatAPIProtocol`（`send_group_message` / `send_private_message` / ...）。
`transport/api.py` 的 `NapCatAPI` 实现这个协议；测试用 fake。

---

## 5. 核心数据流

### 5.1 启动

```text
run.py
  ├─ config.paths.AppPaths.ensure_dirs()
  ├─ config.load_settings()
  │    ├─ 读 environment.json（bot_env / backend_env / path_profile / imgs_dir / ...）
  │    ├─ 读 config.json / sender_config.json / bot_config.json
  │    └─ 建 PathBridge（WSL ↔ Windows）
  ├─ transport.ConnectionConfig.from_file(config.json)
  ├─ transport.NapCatConnection(config)      # 反向：serve；正向：connect
  ├─ if 正向: sender = ConnectionConfig(sender_config.json); api_conn = ...
  ├─ BotApp(connection=api_conn, settings, paths, images)
  ├─ web.WebServer(bot).start_if_enabled()
  ├─ transport.listen(events_conn, bot.process_message)
  ├─ bot.start()        # 取 self_id、加载断点、心跳、后台加载 Agent
  ├─ bot.recover_from_checkpoint()
  └─ await forever
```

### 5.2 一条消息

```text
NapCat event
  └─ transport.listener: post_type == "message" → asyncio.create_task(bot.process_message)

BotApp.process_message(event, is_recover=False)
  1. 群消息：checkpoint[group_id] = event
     非恢复：interject.note_message(event)
  2. 非恢复：thread_history.remember(event)
  3. emoji 收集（旧行为）
  4. auto_forward(event, is_recover)  ── 命中 → return
  5. 被 @ 或（敏感期 + 回复 Bot）且非恢复：
       a. 历史命令正则（^历史）→ history_solution
       b. context = context_builder.build(event, history_count=10)
       c. reply = agent.chat(context)      # 失败/未启用 → 关键词
       d. 发送 reply 列表；逐条 remember_sent_message
       e. return（调用过模型就不再走关键词）
  6. 关键词匹配 keyword_actions → pattern_match
```

**顺序不能变**：旧版注释记录过，AI 和关键词反过来会双发。

### 5.3 AI 一轮

```text
agent.runtime.chat(context)
  ├─ context.render()                    # 场景 / 历史 / 当前
  ├─ build_action_hint(context)          # 需要动作时点名工具
  ├─ reply_router.ainvoke(messages)      # 429 → fallback
  ├─ LangGraph agent loop（工具调用）
  ├─ parse_agent_output(text)
  ├─ 续说（next=continue，最多 2 轮）
  ├─ 去复读 / 敷衍检测 / 强制回复
  ├─ 沉默判定（回复优先；当面说话不许沉默）
  └─ return list[str]
```

### 5.4 下载

```text
命令 / B站解析
  └─ DownloadService.submit(kind, url, options)
       └─ DownloadPool
            ├─ 队列（max_queue）
            ├─ N 个 worker（max_concurrent）
            ├─ 每个任务：retries + timeout + cancel
            └─ Provider（bilibili / music）
                 ├─ 解析元数据
                 ├─ 下载到 data/tmp/<task_id>/
                 ├─ ffmpeg 合并（可选）
                 └─ 移动到 data/downloads/
       └─ DownloadResult(path, size, mime, meta)
            └─ media.send_video(...) → images.to_backend(path) → file 段
```

**约束**：下载的临时文件必须在 `finally` 清理；发送失败要回退文本；B 站发送必须用
`images.to_backend(local_path)`，不能手写 `/mnt/...`。

### 5.5 网页面板保存

```text
frontend POST /api/config {data}
  └─ web.routes.config
       ├─ validation.validate(data)      # 返回 errors / warnings
       ├─ settings.update(data)          # 只改允许的键
       ├─ settings.save()                # 原子写 config/bot_config.json
       └─ bot.reload_config()
            ├─ settings.reload()
            ├─ messaging.update_extraconfig()
            ├─ agent 变化 → 后台重建
            └─ web_panel 变化 → restart_required
```

---

## 6. 配置与路径（用户重点）

### 6.1 `config/environment.json`

```json
{
  "bot_env": "windows",
  "backend_env": "wsl",
  "path_profile": "windows_to_wsl",
  "wsl_distro": "Ubuntu",
  "wsl_mount_root": "/mnt",
  "imgs_dir": "imgs",
  "downloads_dir": "data/downloads",
  "temp_dir": "data/tmp",
  "path_map": []
}
```

- `bot_env`：Bot 进程所在环境：`windows` / `wsl` / `linux`
- `backend_env`：协议端（NapCat / SnowLuma）所在环境，同上
- `path_profile`：`same` / `windows_to_wsl` / `wsl_to_windows` / `custom`
- 未写时自动探测（Windows → windows；WSL → wsl；其它 → linux）
- Windows 上 `backend_env` 缺省为 `wsl`（与旧项目行为一致；同机 Windows 部署改成 `windows`）
- 环境变量 `JJ_BOT_ENV` / `JJ_BACKEND_ENV` 优先级最高（方便部署）
- `onebot_env` / `napcat_env` 是 `backend_env` 的旧名字，只读兼容

### 6.2 翻译规则（`core/pathmap.py` + `config/path_profiles.py`）

| path_profile | 规则 |
|---|---|
| `same` | 原样 |
| `windows_to_wsl` | `D:\a\b` → `/mnt/d/a/b` |
| `wsl_to_windows` | `/mnt/d/a/b` → `D:\a\b` |
| `custom` | 只用 `path_map` |

`path_map` 叠加在预设上，最长前缀优先；额外支持：

- `\\wsl$\Ubuntu\home\a` → `/home/a`
- `\\wsl.localhost\Ubuntu\home\a` → `/home/a`
- URL（`http://` / `https://` / `base64://` / `file://`）原样返回
- 相对路径先按项目根目录解析

### 6.3 `images/` 的公开 API

业务代码只允许这样用：

```python
from images import images  # 或 from images import ImageResolver

path = images.resolve("imgs/妈妈.gif")      # → ImageRef
images.to_napcat(path)                       # → 给协议端的字符串（旧名）
images.sendable("imgs/妈妈.gif")             # → {"file": "..."} 段数据
images.expand("{imgs_dir}/妈妈.gif")         # → 本机绝对路径
```

`ImageResolver` 内部持有 `AppPaths` + `PathBridge`。任何模块都不再自己读
`paths_config.json`，也不再自己判断 WSL。

### 6.4 配置读取顺序

```text
环境变量 JJ_BOT_ENV / JJ_BACKEND_ENV（兼容 JJ_ONEBOT_ENV / JJ_NAPCAT_ENV）
  > config/environment.json
  > config/config.json 里的同名键
  > 自动探测
```

`imgs_dir` 读取顺序：

```text
environment.json 的 imgs_dir
  > config/paths_config.json 的 imgs_dir（旧配置兼容）
  > <root>/imgs
```

---

## 7. 关键接口（写代码时按这个签名）

### 7.1 配置

```python
# config/__init__.py
load_settings(root: Path | None = None) -> BotSettings
class BotSettings:
    paths: AppPaths
    env: EnvConfig
    bridge: PathBridge
    connection: ConnectionSettings
    def get(self, key_path: str, default=None): ...
    def set(self, key_path: str, value): ...
    def section(self, name: str) -> dict: ...
    def prompt(self, name: str, default="") -> str: ...
    def reply(self, name: str, default="") -> str: ...
    def save(self) -> Path: ...
    def reload(self) -> "BotSettings": ...
    # typed accessors（与旧 BotConfig 同名，便于迁移）
    @property master_qq, agent_enabled, agent_llm_config, ...
```

### 7.2 连接

```python
# transport
class ConnectionConfig:
    type: str            # "ws" | "ws_re"
    access_token: str
    host: str
    port: int
    @classmethod from_file(path) -> "ConnectionConfig"
    @property is_reverse / is_forward / endpoint / mode_label

class NapCatConnection:
    async def start(); async def stop()
    async def call_api(action: str, params: dict) -> dict
    def set_event_handler(handler)
    async def wait_for_connect(timeout=None) -> bool
    async def ensure_connected(timeout=None)
    @property is_connected: bool
```

### 7.3 消息

```python
# messaging
class MessageProcessor:
    def __init__(self, api, images, thread_history, extraconfig=None)
    def get_clean_context(event, keep_at=False) -> str
    def is_reply(event) -> int | None
    async def is_at(event, user_ids) -> list | None
    async def send_message(event, message) -> dict
    async def send_img(event, img_addr, summary="") -> dict
    async def pattern_match(event, patterns) -> bool
    async def build_chat_context(event, history=None, history_count=10, note="", note_values=None, master_ids=()) -> ChatContext
    def remember_thread_message(event, master_ids=())
    def remember_sent_message(event, text, result=None, hot_seconds=None)
    def thread_is_hot(thread_id, hot_seconds=None) -> bool
    def replies_to_bot(event) -> bool
```

### 7.4 Agent

```python
# agent
class QQBotAgent:
    def __init__(self, *, api, media, messaging, settings, llm_config=None)
    async def chat(self, event=None, context=None, thread_id=None, user_message=None, extra_context=None) -> list[str] | None
    async def maybe_interject(self, context, thread_id, reason="") -> list[str] | None
```

### 7.5 下载

```python
# download_system
class DownloadTask:
    id: str
    kind: str
    source: str
    options: dict
    status: DownloadStatus
    result: DownloadResult | None
    error: str | None

class DownloadPool:
    def __init__(self, *, workers=2, max_queue=32, task_timeout=600, max_retries=2, storage: DownloadStorage)
    async def start(); async def stop()
    async def submit(task: DownloadTask) -> DownloadResult
    def stats() -> dict

class DownloadService:
    async def download_bilibili(url: str, *, output_dir=None) -> DownloadResult
    async def download_music(keyword: str) -> MusicResult
    async def close()
```

### 7.6 Web

```python
# web
class WebServer:
    def __init__(self, bot=None, settings=None, *, host=None, port=None, token=None, static_dir=None)
    async def start(); async def stop(); async def start_if_enabled() -> bool
```

---

## 8. 旧文件 → 新文件对照

| 旧文件 | 新位置 | 说明 |
|---|---|---|
| `run.py` | `run.py` | 只保留启动编排 |
| `Minitor/Websockets.py` | `transport/config.py` + `connection.py` | 拆配置/连接 |
| `Minitor/NapCatAPI.py` | `transport/api.py` | 协议封装 |
| `Minitor/minitor.py` | `transport/listener.py` | 事件监听 |
| `Minitor/Paths.py` | `config/paths.py` | 加 env 转换 |
| `Minitor/BotConfig.py` | `config/defaults.py` + `settings.py` | 默认值/访问器分离 |
| `Minitor/NapCatTools.py` | `messaging/processor.py` + `history.py` + `forward.py` + `context_builder.py` + `media/` | 按职责拆 |
| `Minitor/BOTMain.py` | `bot/app.py` + `pipeline.py` + `keyword.py` | 编排与业务分开 |
| `Minitor/Commands.py` | `bot/commands/*` | 每个命令一个文件 |
| `Minitor/ForwardService.py` | `bot/services/forward.py` + `media/service.py` | 搬运/媒体分离 |
| `Minitor/forward_dedup.py` | `bot/services/dedup.py` | 算法与 DB schema 不变 |
| `Minitor/forward_dedup_rebuild.py` | `bot/services/dedup_rebuild.py` | |
| `Minitor/RangeTaskService.py` | `bot/services/range_task.py` | |
| `Minitor/InterjectService.py` | `bot/services/interject.py` | |
| `Minitor/extension.py` | `media/ocr.py` | |
| `Minitor/image_hash.py` | `media/image_hash.py` | |
| `Minitor/bili_info_processor.py` | `download_system/providers/bilibili.py` + `mux.py` | |
| `MusicServer/main.py` | `download_system/providers/music.py` | |
| `Agent/LanguageModule.py` | `agent/runtime.py` + `output.py` + `hints.py` + `prompts.py` + `tools/*` | 拆成可读模块 |
| `Agent/chat_context.py` | `messaging/models.py` + `parse.py` + `render.py` | 数据层移出 Agent |
| `Agent/thread_history.py` | `messaging/thread_history.py` | |
| `Agent/llm_config.py` | `agent/config.py` | |
| `Agent/model_router.py` | `agent/router.py` | |
| `Agent/chat_logger.py` | `agent/logger.py` | |
| `Agent/context_pocessor.py` | `agent/tools/methods.py` | |
| `Agent/CampusAssistant.py` | `agent/campus.py` | |
| `Agent/SimpleLLM.py` | — | 未启用，已删除 |
| `Minitor/WebPanel.py` | `web/server.py` + `routes/*` + `validation.py` | 后端拆分 |
| `webui/*` | `frontend/*` | 前端拆分模块 |
| `tests/*` | `tests/*` | 更新 import，行为不变 |

---

## 9. 必须保留的行为（验收清单）

迁移时逐条勾选。详细来源见 `docs/MIGRATION.md`。

> 状态（2026-10-24）：代码级迁移已完成；旧 10 个测试文件断言未改，全量
> **242 passed**；`compileall`、`run.py` 与 `web.standalone` 启动冒烟均通过。
> 下表的真人 / 真机项（NapCat 实际消息、B 站真实下载、点歌、winrt OCR）仍需
> 在有网络和 NapCat 的环境逐条核对。

### 连接 / 启动
- [ ] `ws_re`：Bot 起服务，NapCat 连入；同一条连接收事件 + 发 API
- [ ] `ws`：事件连接 + `sender_config.json` 独立 API 连接；断线 5 秒重连
- [ ] token 从 query / `Authorization` 读取；默认不严格校验
- [ ] 反向连接新客户端会作废旧 pending 并关闭旧连接
- [ ] 启动先起面板，再等 NapCat；断点恢复不阻塞
- [ ] 心跳 10~20 分钟 `get_login_info`

### 消息
- [ ] `process_message` 顺序：断点 → 插话计数 → 临时历史 → 搬运 → AI → 关键词
- [ ] `is_recover=True` 只做搬运，不计数、不记历史、不走 AI/关键词
- [ ] `@` 优先 AI；模型真的被调用后 return，不双发
- [ ] 敏感期内“回复 Bot”也算对 Bot 说话（不需要 @）
- [ ] `@历史` 命令走 `history_solution`（保留旧行为，即使它调用的方法不存在）
- [ ] 历史接口包含当前消息，构建上下文时必须去重
- [ ] 长历史只能按 `message_seq` 向后翻页，`message_seq != message_id`
- [ ] 关键词 `params` 里的 `{imgs_dir}` 自动替换
- [ ] 同一群同时只保留一个 `-b/-e` 任务

### 上下文 / Agent
- [ ] 一行一条：`时:分 [message_id] 昵称(QQ,主人) 提及：正文`
- [ ] 引用只留一行摘要，不递归
- [ ] 历史转发只留 `【转发 N 条】`，当前消息才展开
- [ ] 图片只留 `[图片]`，URL 不进提示词
- [ ] 预算：历史 12 条 / 2000 字，单条 300 字，当前 800 字；超了标注省略
- [ ] `ChatContext.directed_at_me` / `replies_to_me` / 提及解析
- [ ] Agent 输出解析：`<status>` / `<<message>` / `<next>`，`continue` / `end`
- [ ] 续说最多 2 轮，总消息最多 6 条
- [ ] 回复优先于沉默；当面说话空输出要强制再问一次
- [ ] 复读检测 + 场景台词池 + 轻量改写
- [ ] 13 个工具的名称/参数/副作用不变
- [ ] OCR 按 `source + index + message_id` 解析，URL 不经模型
- [ ] 模型路由：429 / 额度冷却 / fallback
- [ ] 插话判断只认 `<judge>yes|no</judge>`，拿不准当 no
- [ ] 插话门槛：frequent / idle / 敏感期 hot_frequent / hot_idle / 冷却；冷却结束补看冷却期间的新消息
- [ ] 方法库读写 `Agent/history/method.json`（新路径 `data/methods.json`，兼容读旧）

### 搬运 / 去重
- [ ] 来源群在 `groupid_list` 才搬运
- [ ] 支持目标群列表 / 任务私聊列表
- [ ] 机器人白名单 `type=all|group|private` 匹配
- [ ] 去重指纹：文本 simhash / 图片 ahash+dhash / 小程序 / 转发
- [ ] DB 表结构、字段、hash 算法与旧库兼容
- [ ] 回放命中“搬过了”不发【发过了喵】；live 命中才发
- [ ] 去重记录在发送成功后写
- [ ] 图片批量转发：只收有图无视频；节点 uin/昵称固定；目标群合并转发
- [ ] `img_fd -a` 用 `image_forward.auto_target_group_ids`

### 下载 / 媒体
- [ ] B 站短链解析；`durl` 优先，否则 dash 最高 id；分段全部拼接
- [ ] ffmpeg 合并失败回退无音频视频；ffprobe 只判断音轨
- [ ] 临时文件 `finally` 清理
- [ ] 发送本地视频前转 NapCat 路径；失败回退下载地址文本
- [ ] 点歌只发 `record` 远程 URL，不下载
- [ ] OCR：Windows 用 winrt 无置信度；Linux `chi_sim+eng`，`conf<=60` 标 `[?word]`
- [ ] OCR 缓存按 URL、只存 text、TTL 3600s、128 条 LRU；错误不入缓存
- [ ] 图片哈希 4MB 上限 / 8s 超时 / 失败降级

### 面板 / 配置
- [ ] 10 个 HTTP 端点形状不变
- [ ] `GET /` + `/static/<name>` 静态文件
- [ ] token 中间件只保护 `/api`
- [ ] 保存 = 校验 → 写盘 → 热加载
- [ ] `agent.*` 改动后台重建；`web_panel.*` / 连接方式返回 restart_required
- [ ] 配置默认值与旧版一致
- [ ] 未知 action 跳过并提示

---

## 10. 迁移阶段与子代理分工

> 每个子代理只写自己名下的文件，不碰旧仓库，不碰别人的文件。
> 所有新文件从旧实现“搬运 + 拆解”，算法不改。

### 阶段 0（主代理）
- 写本设计 + `docs/CONFIG.md` + `docs/DEVELOPMENT.md` + `docs/MIGRATION.md`
- 搭骨架：目录、`__init__.py`、`core/`、`config/`、`images/`
- `messaging/models.py` / `parse.py` / `render.py`（从 `chat_context.py` 拆）
- `download_system/pool.py` / `task.py` / `storage.py` / `service.py` 接口
- `web/server.py` 骨架、`run.py` 骨架

### 阶段 1（并行子代理）
1. **transport-messaging**
   - 文件：`transport/*`、`messaging/processor.py`、`messaging/history.py`、`messaging/forward.py`、`messaging/context_builder.py`、`messaging/thread_history.py`
   - 参考：`Websockets.py`、`NapCatAPI.py`、`minitor.py`、`NapCatTools.py`、`thread_history.py`
2. **agent**
   - 文件：`agent/*`
   - 参考：`LanguageModule.py`、`llm_config.py`、`model_router.py`、`chat_logger.py`、`context_pocessor.py`、`CampusAssistant.py`
3. **download-media**
   - 文件：`download_system/providers/*`、`download_system/mux.py`、`media/*`
   - 参考：`bili_info_processor.py`、`MusicServer/main.py`、`extension.py`、`image_hash.py`、`NapCatTools` 的 B 站方法
4. **web-frontend**
   - 文件：`web/*`、`frontend/*`
   - 参考：`WebPanel.py`、`webui/*`

### 阶段 2（依赖阶段 1 的接口）
5. **bot-services**
   - 文件：`bot/services/*`、`bot/commands/*`、`bot/keyword.py`、`bot/checkpoint.py`
   - 参考：`ForwardService.py`、`forward_dedup.py`、`RangeTaskService.py`、`InterjectService.py`、`Commands.py`
6. **tests**
   - 文件：`tests/*`
   - 参考：旧 `tests/*`，更新 import，保持断言不变

### 阶段 3（主代理）
- 写 `bot/app.py` / `bot/pipeline.py`，把所有服务接起来
- 跑 `pytest`、`py_compile`、启动冒烟
- 修集成问题，补 `docs/FLOWS.md`
- 按第 9 节逐条验收

---

## 11. 验证策略

1. **静态**：`python -m compileall .`
2. **测试**：`pytest -q`（目标：旧 208 个测试迁移后全绿）
3. **配置**：`python -m json.tool config/*.json`
4. **路径**：单测覆盖 4 种 env 组合 + URL + UNC + 相对路径
5. **启动冒烟**（无 NapCat）：
   - `python run.py` 能起面板、打印配置布局、等待连接
   - `python -m web.standalone` 能起面板
6. **真机**（用户执行）：
   - NapCat 连 `ws://127.0.0.1:8085`
   - 测试 @、关键词、搬运、`img_fd`、B 站、点歌、OCR、面板保存

---

## 12. 学习 / 修改指南（写给人看）

### 想加一个关键词命令
1. 在 `bot/commands/` 新建 `xxx.py`，写 `async def xxx(handler, event)`。
2. 在 `bot/commands/registry.py` 注册 `"xxx": ...`。
3. 在 `config/bot_config.json` 的 `keyword_actions` 加 `{"regex": "...", "action": "xxx", "params": {...}}`。

### 想加一个 NapCat API
在 `transport/api.py` 加方法，照 `delete_msg`：
```python
async def delete_msg(self, message_id: int) -> dict:
    return await self._call_api("delete_msg", {"message_id": message_id})
```

### 想加一个下载源
1. 在 `download_system/providers/` 新建 `xxx.py`，实现 `Provider` 协议。
2. 在 `download_system/service.py` 注册 `kind`。
3. 业务侧只调用 `downloads.submit(kind="xxx", ...)`。

### 想加一个 Agent 工具
1. 在 `agent/tools/` 对应文件写 `@tool` 函数。
2. 在 `agent/tools/__init__.py` 的 `build_tools()` 里加入。
3. 在 `agent/prompts.py` 说明何时使用。
4. 在 `tests/` 加解析/调用测试。

### 想改路径规则
只改 `config/environment.py` 的 `PathBridge`，然后跑路径单测。
业务代码不要动。

---

## 13. 非目标 / 暂不处理

- 不引入数据库 ORM、不换 SQLite 去重库
- 不引入前端构建链（保持无构建、离线可用）
- 不重写 Agent 的提示词内容（只拆分文件）
- 不修旧代码里已存在的 bug（除非它阻碍迁移）；在 `docs/MIGRATION.md` 标注
  - `history_solution` 调用不存在的 `get_message_history`
  - `/api/groups` 在正向模式下可能用错连接
  - `/api/status` 泄露 panel token
  - `imgs_dir` 导入期缓存
- 这些行为先保持，迁移完成后再单独开 issue 修。
