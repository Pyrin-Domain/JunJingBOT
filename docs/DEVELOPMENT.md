# 开发指南

## 1. 环境

```bash
python -m venv .venv
pip install -r requirements.txt
```

Windows 下如果同时有 WSL 的 NapCat / SnowLuma，`config/environment.json` 保持：

```json
{"bot_env": "windows", "backend_env": "wsl", "path_profile": "windows_to_wsl"}
```

Bot 和后端都在 Windows：

```json
{"bot_env": "windows", "backend_env": "windows", "path_profile": "same"}
```

`onebot_env` / `napcat_env` 是 `backend_env` 的旧名字，仍然兼容；
写盘只写 `backend_env`。

## 2. 运行

```bash
python run.py                  # Bot + 面板
python -m web.standalone       # 只启动面板（不连 NapCat）
pytest -q                      # 测试
python -m compileall .         # 静态语法检查
```

## 3. 依赖规则

```text
core ← config ← images
  ↑        ↑       ↑
transport messaging media download_system
  ↑        ↑       ↑        ↑
  └──────── bot ───┴────────┘
              ↑
            agent
              ↑
             web
```

- `core` 不 import 任何项目模块
- `config` 只 import `core`
- `images` 只 import `core` / `config`
- `messaging` 需要发消息时接收 API 协议对象，不 import `transport`
- `bot` 是唯一允许同时认识所有子系统的层
- 不允许循环 import；不允许 `sys.path.insert`

## 4. 常见修改

### 加关键词命令

1. `bot/commands/xxx.py` 写处理函数。
2. `bot/commands/registry.py` 注册 action 名。
3. `config/bot_config.json` 的 `keyword_actions` 加一条。

### 占位命令 `/jm`（只返回本地占位图）

`/jm <id>` 是一条**刻意半成品**的命令，用来固定「命令行 → 下载池 → 发文件」的形状：

- `bot/commands/jm.py`：只响应私聊，群里只回一句提示，永不发群；
- `download_system/providers/jm.py`：`download()` 不抓取任何站点，直接返回
  `download_system/providers/assets/jm_placeholder.jpg`（可用环境变量
  `JM_PLACEHOLDER_FILE` 换成别的本地图片）；
- `DownloadService.download_jm()` 在占位图缺失时**不进队列**，
  避免池子的重试把「文件不存在」包装成 `DownloadError`；
- `config/bot_config.json` 的 `jm.enable` 默认 `true`，私聊发 `/jm` 会收到那张占位图。

要做别的东西，别往这个 provider 里塞网络请求——它是测试用的桩。

### 加 NapCat / SnowLuma API

在 `transport/api.py` 照 `delete_msg` 加：

```python
async def delete_msg(self, message_id: int) -> dict:
    return await self._call_api("delete_msg", {"message_id": message_id})
```

### 加一个 OneBot 后端

后端差异只允许放在 `config/backends.py`，不要在业务代码里写
`if backend == "xxx"`：

1. 在 `BACKENDS` 里加一个 `BackendProfile`：`id`、`label`、别名、
   默认端口、`history_anchor`（`message_seq` / `message_id`）。
2. 需要的话在 `docs/CONFIG.md` / `docs/SNOWLUMA.md` 补配置说明。
3. 补测试：`tests/test_backends.py`、`tests/test_history_anchors.py`。

历史接口统一传两个游标：

```python
await api.get_group_msg_history(
    group_id=group_id, message_seq=seq, message_id=mid
)
```

`transport/api.py` 会按后端档案只发一个；比较断点 / 区间时用
`core/anchors.py` 的 `anchors_equal` / `anchor_matches`，不要只比
`message_seq`。

### 加下载源

1. `download_system/providers/xxx.py` 实现 `BaseProvider`。
2. `download_system/service.py` 注册。
3. 业务层只调 `downloads.download_xxx(...)`。

### 加 Agent 工具

1. `agent/tools/` 对应文件写 `@tool`。
2. `agent/tools/__init__.py` 的 `build_tools()` 加入。
3. `agent/prompts.py` 说明使用场景。
4. `tests/` 加测试。

### 改人设 / 关系档案

- **换角色**：改 `agent/personas/junjing.md`（角色卡）和
  `agent/personas/core_qq.md`（通用群聊行为），或在 `agent.persona_file` /
  `agent.persona_core_file` 指向新的 Markdown；文件缺失时会回退到
  `agent/prompts.py` 的 `FALLBACK_PERSONA` / `FALLBACK_CORE`。
- **改某个人**：直接编辑 `data/relations/users/<QQ>.json`；标签、画像、状态、
  记忆的字段说明见 [`docs/RELATIONS.md`](RELATIONS.md)。
- **让模型自己记**：主模型调用 `update_person` / `update_group`；低频自动整理由
  `agent/relations.py` + `agent/runtime.py` 负责，不要在业务层再写一套。
- 近期状态一定带 `expires_at`，过期后不再注入；不要把几天的情绪当成现在。
- 相关测试：`tests/test_relations.py`；提示词改动至少再跑
  `tests/test_language_module.py`。

### 改路径规则

分三层，别在业务代码里加 `if env == ...`：

1. **纯翻译**：`core/pathmap.py` 的 `PathRule` / `PathTranslator`。
   只做最长前缀、边界、分隔符、大小写；不读配置。
2. **预设**：`config/path_profiles.py` 的 `PROFILES`。
   新增一种部署形态时加一个 `PathProfile`，并在 `normalize_profile` 里补别名。
3. **绑定**：`config/environment.py` 的 `EnvConfig` / `PathBridge`。
   负责读 `environment.json`、缓存 translator、兼容旧名字。

业务代码只调 `PathBridge.to_backend()` / `to_local()`（旧名
`to_napcat()` / `to_bot()`），或 `images.resolve()`。
面板端点 / 校验在 `web/routes/paths.py` / `web/validation.py`，
前端在 `frontend/assets/js/tabs/paths.js`。

改完至少补：`tests/test_pathmap.py`、`tests/test_path_profiles.py`、
`tests/test_environment.py`、`tests/test_web_paths.py`。
业务代码里出现手写 `/mnt/` 或 `D:\` 都算 bug。

## 5. 调试

- 启动日志会打印配置目录、数据目录、连接方式。
- `data/check_point/` 是断点；删掉某个群的文件即可让它下次从头处理。
- 去重库：`data/forward_dedup.db`；`bot/services/dedup_rebuild.py` 可从日志重建。
- 面板保存后：关键词 / 搬运 / 文案 / 路径映射立即生效；`agent.*` 后台重建；
  `web_panel.*` 和连接方式需要重启。

## 6. 迁移旧代码

- 旧仓库：`D:\Code\JunJingBOT`，只读参考，**不要修改**。
- 新仓库：本目录。
- 逐模块“搬运 + 拆解”：算法不改，只改结构和 import。
- 每迁移一个模块，至少保证 `python -m compileall` 通过。
- 测试是行为规范：旧 `tests/` 迁移后应全部通过。

## 7. 子代理协作约定

- 每个子代理只写自己名下的文件（见 `PROJECT_DESIGN.md` 第 10 节）。
- 不改别人的文件；需要接口变更时先在 `PROJECT_DESIGN.md` / 任务说明里写清楚。
- 不碰 `D:\Code\JunJingBOT`。
- 完成后报告：改了哪些文件、公开接口、未完成项、如何验证。
