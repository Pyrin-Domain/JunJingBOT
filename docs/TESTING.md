# 测试迁移指南

旧测试在 `D:\Code\JunJingBOT\tests`，是**行为规范**。迁移时只改 import 和
fake 的构造方式，**不要改断言**。断言改了，就等于把 bug 写进新项目。

## 运行

```bash
pytest -q
```

`pyproject.toml` 已配置 `pythonpath = ["."]`，从项目根目录直接跑即可。

## 旧测试 → 新模块

| 旧测试 | 主要覆盖 | 新 import |
|---|---|---|
| `test_agent_config.py` | LLM 配置解析 / 面板校验 / BotConfig | `agent.config`, `config.BotSettings`, `web.validation` |
| `test_bili_info_processor.py` | B 站信息解析 | `download_system.providers.bilibili` |
| `test_chat_context.py` | 上下文结构 / 渲染 | `messaging.models`（已复制） |
| `test_forward_dedup_gate.py` | 搬运去重闸门 | `bot.services.forward.ForwardService` |
| `test_interject.py` | 插话判定 / 阈值 | `bot.services.interject.InterjectService`, `agent.prompts` |
| `test_language_module.py` | Agent 输出协议 / 工具 / 续说 | `agent.runtime`, `agent.output`, `agent.hints` |
| `test_model_routing.py` | 模型路由 / 429 / fallback | `agent.router`, `agent.config` |
| `test_process_message_order.py` | 主流程顺序 | `bot.pipeline.MessagePipeline` |
| `test_range_task_service.py` | -b/-e 区间任务 / 命令 | `bot.services.range_task`, `bot.commands` |
| `test_thread_history.py` | 临时会话历史 | `messaging.thread_history`（已复制） |

## 常见改法

### BotConfig → BotSettings

旧：

```python
from Minitor.BotConfig import BotConfig
config = BotConfig(overrides={"agent": {...}})
```

新：

```python
from config import BotSettings
settings = BotSettings(overrides={"agent": {...}}, auto_create=False)
```

`BotSettings` 保留了旧访问器：`get/section/save/reload/path/keyword_actions/
auto_forward_kwargs/master_qq/agent_llm_config/agent_model_routing/extraconfig/
heartbeat_range/prompt/reply`。

测试里如果需要隔离文件，用 `tmp_path`：

```python
settings = BotSettings(tmp_path, overrides={...}, auto_create=False)
```

### BOTMain → BotApp / MessagePipeline

旧测试直接 `BOTMain(config, conn=...)` 并 monkeypatch 各服务。
新结构拆成：

- `BotApp`：组装 / 生命周期（依赖真实连接，测试里不实例化）
- `MessagePipeline`：只做流程编排，适合用 fake 依赖

用 `MessagePipeline` 构造 fake：

```python
pipeline = MessagePipeline(
    mp=fake_mp,
    settings=fake_settings,
    forward=fake_forward,
    interject=fake_interject,
    agent_getter=async_agent_getter,
    keyword_builder=lambda: [],
)
await pipeline.process_message(event)
```

重点断言仍然成立：

- 搬运命中 -> 不调用 AI / 关键词
- 被 @ -> 调用 AI 后 return，不调用关键词
- AI 关闭 -> 退回关键词
- `is_recover=True` -> 不计数、不记历史、不走 AI / 关键词

### LanguageModule 拆分

旧测试 `from Agent.LanguageModule import QQBotAgent, parse_agent_output, ...`。
新位置：

- `QQBotAgent` → `agent`
- `parse_agent_output` / `split_sendable_messages` → `agent.output`
- `build_action_hint` → `agent.hints`
- `_INTERJECT_JUDGE_SYSTEM` / `SYSTEM_PROMPT` → `agent.prompts`
- 模型路由 → `agent.router`
- LLM 配置 → `agent.config`

为了减少测试改动，`agent/runtime.py` 和 `agent/__init__.py` 应 re-export
这些常用符号。

### ForwardService

旧构造：`ForwardService(mp=mp, nc=nc, config=config)`。
新构造：`ForwardService(mp=mp, api=api, settings=settings, media=media)`。
如果测试只测去重闸门，可以直接构造 `ForwardDedupStore(db_path=tmp_path/"dedup.db")`
并调用 `find/record`，不必启动整个服务。

### BiliInfoProcessor

旧类 `BiliInfoProcessor` 的纯解析方法保留在新
`download_system.providers.bilibili.BilibiliProvider` 上；测试用
`BilibiliProvider()` 即可。需要网络的方法用 monkeypatch 替换。

## 当前状态

- 旧 10 个测试文件已全部迁移，断言未改；新增测试见下表。
- 新增架构测试（当前实际文件名）：

| 文件 | 覆盖 |
|---|---|
| `tests/test_environment.py` | Windows ↔ WSL 转换、URL 直通、`path_map`、env 优先级 |
| `tests/test_images_resolver.py` | `{imgs_dir}` / `{root}` 占位符、远程 / base64 / 本地路径 |
| `tests/test_settings.py` | 默认值合并、覆盖、保存 / 重载、`environment.json` 目录覆盖 |
| `tests/test_download_pool.py` | 并发池成功 / 重试 / 失败 / 超时 / 取消 / 临时目录清理 |
| `tests/test_web_validation.py` | 配置校验、正则、action、面板 / WS 配置、backend 别名 |
| `tests/test_media_bili_music.py` | B 站文本 / 小程序转发、点歌 record 形状 |
| `tests/test_backends.py` | NapCat / SnowLuma 档案、别名、默认端口、连接配置 |
| `tests/test_anchors.py` | `message_seq` / `message_id` / `real_id` 双游标比较 |
| `tests/test_history_anchors.py` | 历史接口按后端选游标、双游标区间翻页 |
| `tests/test_checkpoint_anchors.py` | 断点恢复认 SnowLuma `message_id` |

## 迁移时实际遇到的改法

- 旧 `BotConfig(path=tmp_path/"bot_config.json")` 的测试，统一用：

  ```python
  def BotConfig(*, path=None, overrides=None):
      root = Path(path).parent if path is not None else None
      return BotSettings(root=root, overrides=overrides, auto_create=False)
  ```

  因为新项目固定把配置放在 `<root>/config/bot_config.json`，不再接受任意
  文件名；包装函数只用于让旧断言少改。
- 旧 `BOTMain.__new__` 的测试改用 `BotApp.__new__`，字段名从 `bot.bot_config`
  改为 `bot.settings`，从 `bot.nc` 改为 `bot.api`。
- 面板保存测试直接调用 `web.routes.config.handle_save_config`，用
  `SimpleNamespace(app=server.app)` 伪造 aiohttp request 即可。
- `BiliInfoProcessor` 保留别名，旧测试可以继续 import；新代码推荐
  `BilibiliProvider`。

## 测试约定

- 测试绝不读写真实 `config/bot_config.json`：用 `tmp_path` + `auto_create=False`。
- 不联网：HTTP 一律 monkeypatch；B 站只测纯解析 / 解析后的调用顺序。
- `asyncio_mode` 未安装 `pytest-asyncio` 时用 `asyncio.run(...)` 包住用例。
- Agent 日志测试 monkeypatch `agent.runtime.append_log`（runtime 直接 import 了
  这个符号），不要 patch `agent.logger.append_log`。

## 验收

- `pytest -q` 全绿（旧测试 + 新增后端 / 游标测试）
- 旧 10 个测试文件的断言覆盖不减少
- 新增路径 / 下载池 / 图片 / 配置 / 面板校验测试
