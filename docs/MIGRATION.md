# 迁移手册（旧 → 新）

旧仓库：`D:\Code\JunJingBOT`（只读参考，绝不修改）
新仓库：`D:\Code\JungJingNew`

原则：**行为等价，结构重写**。算法、配置键、数据库 schema、HTTP 接口形状都不变；
只改目录、模块边界、import 和命名。每搬一个模块，先对照旧代码，再写新代码。

---

## 1. 文件对照

| 旧文件 | 新文件 | 备注 |
|---|---|---|
| `run.py` | `run.py` | 只保留启动编排 |
| `Minitor/Websockets.py` | `transport/config.py` + `transport/connection.py` | 配置与连接拆开 |
| `Minitor/NapCatAPI.py` | `transport/api.py` | 方法名保持不变 |
| `Minitor/minitor.py` | `transport/listener.py` | 事件监听 |
| `Minitor/Paths.py` | `config/paths.py` | 新布局 + 旧布局只读回退 |
| `Minitor/BotConfig.py` | `config/defaults.py` + `config/settings.py` | 默认值与访问器分离 |
| `Minitor/NapCatTools.py` | `messaging/processor.py`、`history.py`、`forward.py`、`context_builder.py`、`media/` | 按职责拆 |
| `Minitor/BOTMain.py` | `bot/app.py` + `bot/pipeline.py` + `bot/keyword.py` | 编排与业务分开 |
| `Minitor/Commands.py` | `bot/commands/*` | 一个命令一个文件 |
| `Minitor/ForwardService.py` | `bot/services/forward.py` + `media/service.py` | 搬运与媒体分离 |
| `Minitor/forward_dedup.py` | `bot/services/dedup.py` | **算法与 schema 不变** |
| `Minitor/forward_dedup_rebuild.py` | `bot/services/dedup_rebuild.py` | |
| `Minitor/RangeTaskService.py` | `bot/services/range_task.py` | |
| `Minitor/InterjectService.py` | `bot/services/interject.py` | |
| `Minitor/extension.py` | `media/ocr.py` | |
| `Minitor/image_hash.py` | `media/image_hash.py` | |
| `Minitor/bili_info_processor.py` | `download_system/providers/bilibili.py` + `download_system/mux.py` | |
| `MusicServer/main.py` | `download_system/providers/music.py` | |
| `Agent/LanguageModule.py` | `agent/runtime.py` + `output.py` + `hints.py` + `prompts.py` + `tools/*` | 拆成可读模块 |
| `Agent/chat_context.py` | `messaging/models.py`（已复制） + `parse.py` + `render.py` | 数据层移出 Agent |
| `Agent/thread_history.py` | `messaging/thread_history.py`（已复制） | |
| `Agent/llm_config.py` | `agent/config.py` | |
| `Agent/model_router.py` | `agent/router.py` | |
| `Agent/chat_logger.py` | `agent/logger.py` | |
| `Agent/context_pocessor.py` | `agent/tools/methods.py` | |
| `Agent/CampusAssistant.py` | `agent/campus.py` | |
| `Minitor/WebPanel.py` | `web/server.py` + `web/routes/*` + `web/validation.py` | |
| `webui/*` | `frontend/*` | 拆成 ES module |
| `tests/*` | `tests/*` | 更新 import，断言不变 |

---

## 2. 各模块搬运要点

### 2.1 transport

- `NapCatBotConfig` → `transport.config.ConnectionSettings`（已在 `config/connection.py` 实现）。
  新代码直接复用，不要在 transport 里重复定义。
- `NapCatConnection`：逐行搬运为 `OneBotConnection`（`NapCatConnection` /
  `NapCatReverseWS` 保留为别名），保留：
  - 反向 `websockets.serve`；新客户端连入时关闭旧连接、清理 pending
  - 正向 `_connect_loop`，断线 5 秒重连
  - token 从 query / `Authorization` 读取
  - `call_api` 用 `echo` 匹配 future，超时 30 秒
  - ping 30s / timeout 10s
- `NapCatAPIInterface` → `OneBotAPI`（`NapCatAPI` / `NapCatAPIInterface` 保留为
  别名），方法名、参数、返回 dict 不变；历史接口新增 `message_id=` 关键字，
  由 `config/backends.py` 按后端选择真正发出去的游标。
- `listen_msg` → `transport.listener.listen`：只处理 `post_type=="message"`，
  每条 `asyncio.create_task(process_message(event))`，不阻塞。
- 连接层**不要** import 业务模块。

### 2.2 messaging

- `MessageProcessor` 的收发、`get_clean_context`、`is_AT`、`is_reply`、
  `pattern_match`、历史翻页、断点方法都保留。
- `collect_message`、`collect_history`、`build_chat_context` 拆到
  `messaging/context_builder.py`，因为它们是“补全网络数据”的层。
- `thread_history` 直接复用已复制文件；`MessageProcessor` 组合它，不再动态 import。
- 图片发送统一走 `images.ImageResolver`；`send_img` 的参数 `img_addr`
  支持 `{imgs_dir}`、本地路径、URL。
- `get_clean_context(event, keepAt=False)` 的行为保持不变（CQ 码清洗、@ 处理）。
- 历史接口“包含当前消息”的去重逻辑不能丢。

### 2.3 agent

`LanguageModule.py` 很大，按以下边界拆：

| 新文件 | 从旧文件搬什么 |
|---|---|
| `agent/prompts.py` | `SYSTEM_PROMPT`、`_INTERJECT_JUDGE_SYSTEM`、`_FORCE_REPLY_HINT`、`_REPLY_CHAIN_HINT`、`_INTERJECT_REPLY_HINT`、`_CONTINUE_HINT` |
| `agent/output.py` | `parse_agent_output`、`split_sendable_messages`、`_normalize_next`、`_render_message_body`、`looks_like_repeat`、`collect_template_lines`、`pick_scene_line`、`_looks_like_dodge`、`_parse_interject_judge` |
| `agent/hints.py` | `build_action_hint`、`_wants_dress_photo`、`_dress_is_just_talk`、`_at_targets`、`_image_urls`、`_normalize_image_source`、`_urls_for_source`、`_replied_message_id` |
| `agent/runtime.py` | `QQBotAgent`：`__init__`、`_build_llm`、`_setup_model_routes`、`chat`、`maybe_interject`、`_force_reply`、`_de_repeat`、`_rephrase`、`_extract_ai_response`、`_record_tool_call`、`_build_tools` |
| `agent/tools/*` | 13 个工具函数 |
| `agent/config.py` | `llm_config.py` 全部 |
| `agent/router.py` | `model_router.py` 全部 |
| `agent/logger.py` | `chat_logger.py` 全部 |
| `agent/campus.py` | `CampusAssistant.py` 全部 |

> `Agent/SimpleLLM.py` 没有进入主流程，迁移草稿 `agent/simple.py` 已删除。
> `core/events.py`、`core/text.py` 也没有任何引用，已一并删除。

必须保留：
- 输出协议 `<status>` / `<<message>` / `<next>`，`continue` / `end`
- 续说最多 2 轮、总消息最多 6 条
- 回复优先于沉默；`directed_at_me` 空输出强制再问一次
- 复读检测、场景台词池、轻量改写
- 13 个工具的名称/参数/副作用
- `ocr_img` 按 `source + index + message_id` 从 `ChatContext.image_sources()` 解析
- 模型路由 429 / 额度冷却 / fallback
- 插话判断只认 `<judge>yes|no</judge>`
- 方法库路径改为 `data/methods.json`，但要能读旧的 `Agent/history/method.json`

### 2.4 download / media

- `BiliInfoProcessor` 的解析逻辑原样搬进 `BilibiliProvider`：
  `extract_video_urls`、`resolve_video_url`、`get_video_info`、`get_video_stream_info`、
  `get_video_download_url`、`download_video_to_file`。
- 下载走 `DownloadPool`：provider 不自己建线程/信号量。
- ffmpeg 合并失败回退无音频视频；ffprobe 只判断音轨。
- 临时文件清理放在 provider 的 `finally`，pool 也会兜底清理任务目录。
- `Extension` → `media/ocr.py`，Windows winrt / Linux tesseract 双实现、缓存语义不变。
- `image_hash.py` 原样搬。
- `MusicServer` → `MusicProvider`，只解析远程 `music_url`，不下载。

### 2.5 bot

- `BOTMain.process_message` 的顺序**逐行对照**搬到 `bot/pipeline.py`。
- `ForwardService.auto_forward` 的参数名保持（`groupid_list` / `target_groupid` /
  `task_userid` / `dedup` / `auto_img_forward_from_bot`）。
- `forward_dedup.py` 整文件搬成 `bot/services/dedup.py`：
  - 表结构、字段、`build_fingerprint`、simhash、图片哈希、分组锁定逻辑不变
  - DB 路径用 `AppPaths.data_file("forward_dedup.db")`
  - 旧库可直接复用（本次已把旧库 998 条导入新库，见 3.1）
- `Commands.py` 按命令拆文件；`CommandHandlers` 作为门面保留，
  这样 `bot/commands/registry.py` 的 action 表不需要知道实现细节。
- `RangeTaskService` / `InterjectService` 原样搬，只改 import 和类型注释。
- `checkpoint` 用 `data/check_point/<user_id>.json`，JSON 语义不变
  （`{group_id: event}`，每 10 秒落盘）。

### 2.6 web / frontend

- 后端 aiohttp，保留 10 个端点：
  `GET /`、`GET /static/{name}`、`GET /api/status`、`GET /api/groups`、
  `GET/POST /api/ws`、`GET/POST /api/config`、`POST /api/validate`、`POST /api/reload`。
- token 中间件只保护 `/api`；`GET /api/status` **不要**返回 token 明文
  （旧版泄露是已知 bug；迁移时顺手修掉，但接口字段保留）。
- 前端无构建，拆成 ES module；`index.html` 用 `<script type="module">`。
- 静态资源路径：`/static/<name>` 兼容旧前端写死的引用。
- 后端单独启动：`python -m web.standalone`。

### 2.7 tests

- 旧 `tests/` 的断言是行为规范，**不要改断言**，只改 import 和 fake 的构造。
- 新增测试优先覆盖：路径转换、配置合并、下载池、图片解析。
- 运行：`pytest -q`。

---

## 3. 已知旧 bug（迁移时先保留或明确修）

| 位置 | 现象 | 处理 |
|---|---|---|
| `BOTMain.history_solution` | 调用不存在的 `get_message_history` | 先保留；在 `bot/pipeline.py` 标注 TODO |
| `WebPanel /api/groups` | 正向模式可能用错连接 | 迁移时用 `events_connection` 或 `bot.conn` 明确 |
| `WebPanel /api/status` | 返回 panel token | 迁移时移除 token 字段 |
| `NapCatTools._IMGS_DIR` | 导入期缓存，改配置不生效 | 新代码用 `settings.paths.images_dir`，彻底修掉 |
| `get_long_history_only_processor` | `message_seq` 与 `message_id` 混用 | 保持旧行为，注释说明 |

---

## 3.1 迁移完成状态（2026-10-24）

### 已验证

- 旧 `tests/` 的 10 个测试文件全部迁入 `tests/`，断言未改，只调整 import 和
  `BotSettings` / `BotApp` / `WebServer` 的构造方式；**242 passed**。
- 新增架构测试：
  - `test_environment.py`：Windows / WSL 路径桥、URL 直通、`path_map`
  - `test_images_resolver.py`：`{imgs_dir}`、远程 / base64 / 本地路径
  - `test_settings.py`：默认值、覆盖、保存 / 重载、环境目录
  - `test_download_pool.py`：成功、重试、失败、超时、取消、临时目录
  - `test_web_validation.py`：核心校验、正则、action、面板 / WS
  - `test_media_bili_music.py`：B 站文本 / 小程序转发、点歌 record 形状
  - `test_forward_dedup_store.py`：图片感知哈希差 1 位时的模糊兜底，以及阈值拒绝
- `python -m compileall -q .` 通过。
- `python run.py` 启动冒烟：面板 `/api/status` 返回 200；反向 WS 监听
  `127.0.0.1:8085`；Agent 15 个工具（原有 13 + 关系档案 2）、CampusAssistant 2 个工具加载成功。
- `python -m web.standalone --port 8391` 启动冒烟：面板 `/api/status` 返回 200。
- 代码级逐条对照：旧 `NapCatTools` / `BOTMain` / `Commands` / `ForwardService` /
  `RangeTaskService` / `InterjectService` / `LanguageModule` 的公开方法均已保留
  （改名或委托到新模块的会在对照表注明）。

### 旧去重库导入（2026-10-24）

旧库 `D:\Code\JunJingBOT\data\forward_dedup.db` 没有直接覆盖新库，而是以旧库的
SQLite 一致快照为底，再合并新项目运行期已经产生的记录：

- 旧库快照：**998 条指纹 / 1578 条图片**，全部 `sim_algo=2`，不需要
  `dedup_rebuild` 升级。
- 新库运行期 3 条（2026-10-23 真实 NapCat 消息）：
  - `d97bea8e9a218f82`：旧库没有，插入为新行；
  - `e5b4b1ffcfe7b532` / `6d9c3bda3a5f35be`：旧库已有，合并 `group_ids`，
    `first_seen` 取更早、`last_seen` 取更晚、`hit_count` 取较大值。
- 合并结果：**999 条指纹 / 1578 条图片**，`content_hash` 无重复，图片无孤儿行。
- 日志：旧 361 行 + 新 7 行，按旧在前、新在后合并为 368 行
  （`data/forward_dedup_log.jsonl`，约 6.26 MB）。
- `ForwardDedupStore.stats()` 读取正常；
  `python -m bot.services.dedup_rebuild --dry-run` 报告 0 条需要升级。
- `data/` 不随代码提交；换机器部署时把 `data/forward_dedup.db` 和
  `data/forward_dedup_log.jsonl` 一起带过去。

### 真机 / 外网待验证（代码已具备，未在没有 NapCat / 外网的环境跑）

- NapCat 实际连入后的 @、关键词、搬运、`img_fd`、断点回放。
- B 站真实视频下载 / ffmpeg 合并（单测用 fake 网络验证了解析与调用顺序）。
- 点歌真实搜索与远程 `record` URL 播放。
- Windows `winrt` OCR 与 Linux `tesseract` OCR（当前环境只验证了降级路径）。

### 迁移中修掉的旧 bug

| 位置 | 旧现象 | 新实现 |
|---|---|---|
| `WebPanel /api/status` | 返回面板 token | 只返回 `url` / `host` / `port` / `auth` |
| `NapCatTools._IMGS_DIR` | 导入期缓存图片目录 | 每次通过 `settings.paths.images_dir` |
| `WebPanel /api/groups` | 正向模式可能拿错连接 | 用 `events_connection` |
| B 站文本 / 小程序入口 | 旧运行时缺少部分方法时静默不发送 | `MessageProcessor.try_send_*` 完整实现 |
| 点歌 record | 旧实现字段是 `url`，新实现曾误写成 `file` | 恢复 `{type:"record", data:{"url": ...}}` |
| 图片兜底检索 | 只用 `hash IN (...)` 精确取候选，转码后差 1~2 位的图直接查不到 | 先精确查，未命中再做全表汉明扫描（仍受 `img_hamming_threshold` 限制） |

### 明确保留的旧行为

- `BOTMain.history_solution` 调用不存在的 `get_message_history`：新
  `bot/pipeline.py` 保留入口，但只记录 warning 并返回，不再让消息任务崩溃
  （旧 bug 的“行为”保留为不实现，见 TODO）。
- `get_long_history_only_processor` 继续混用 `message_seq` / `message_id`，
  只加注释，不改语义。

---

## 4. 验收清单

> 下面是原始验收清单，保留作真机逐条核对；代码级项已由上面的测试与启动冒烟覆盖。

### 连接
- [ ] `ws_re` 单连接收事件 + 发 API
- [ ] `ws` 双连接；断线 5 秒重连
- [ ] token 校验与旧版一致
- [ ] 心跳 10~20 分钟

### 消息 / AI
- [ ] 主流程顺序不变
- [ ] `is_recover` 只搬运
- [ ] @ 优先 AI，调用模型后 return
- [ ] 敏感期回复 Bot 触发 AI
- [ ] 上下文渲染预算 / 引用摘要 / 图片占位
- [ ] Agent 输出解析 / 续说 / 沉默 / 去复读
- [ ] 13 个工具
- [ ] OCR source/index/message_id
- [ ] 模型路由 / 插话判断

### 搬运 / 下载
- [ ] 自动搬运 + 机器人白名单
- [x] 去重库兼容旧 DB（已导入 998 条旧指纹 + 1 条新指纹，共 999 条 / 1578 张图片）
- [ ] 回放命中不提示，live 命中提示
- [ ] 图片批量转发
- [ ] B 站下载/合并/路径转换/失败回退
- [ ] 点歌
- [ ] OCR / 图片哈希

### 面板
- [ ] 10 个端点
- [ ] token 中间件
- [ ] 保存 → 校验 → 写盘 → 热加载
- [ ] `agent.*` 重建；`web_panel.*` restart_required

### 工程
- [ ] `python -m compileall .` 通过
- [ ] `pytest -q` 全绿
- [ ] 无 `sys.path` hack
- [ ] 无跨层 import
- [ ] 路径转换只在 `config/environment.py`
