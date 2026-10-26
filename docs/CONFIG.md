# 配置与路径

本文只讲两件事：**配置放哪儿、怎么改**，以及 **WSL / Windows 路径怎么自动转换**。

---

## 1. 配置文件

```text
config/
├─ config.json           # 连接 OneBot 后端的方式（ws / ws_re + backend）
├─ sender_config.json    # 正向模式下，发 API 用的独立连接
├─ bot_config.json       # 业务参数（关键词、搬运、AI、插话、面板）
└─ environment.json      # 运行环境 + 路径翻译 + 目录（bot_env / backend_env / path_profile）
```

首次启动时，如果文件不存在，会用 `config/defaults.py` 里的默认值生成。
`config.json` / `sender_config.json` 含有 token，已在 `.gitignore` 里忽略。
后端差异（默认端口、历史游标、提示文案）集中在 `config/backends.py`。

### 1.1 `config.json`

```json
{
  "type": "ws_re",
  "backend": "napcat",
  "access_token": "NapCat520",
  "ws_reverse_host": "127.0.0.1",
  "ws_reverse_port": 8085,
  "ws_host": "127.0.0.1",
  "ws_port": 3001
}
```

- `type: "ws_re"`：Bot 起 WebSocket 服务，OneBot 后端主动连过来。
  同一条连接既收事件又发 API。
- `type: "ws"`：Bot 主动连后端收事件；发 API 用 `sender_config.json`
  那条独立连接。
- `backend`：`napcat`（默认）或 `snowluma`。也接受 `protocol` / `adapter`
  作为字段名，以及 `snowluna`、`snow-luma` 等别名。它只影响默认端口、
  历史接口游标和日志文案，协议本身仍是 OneBot v11。
- `access_token`：后端里设置的密码。为空表示不校验。

两个后端的默认端口相同（WS 服务端 `3001`、HTTP `3000`），但历史游标不同：

| backend | 历史游标 | 说明 |
|---|---|---|
| `napcat` | `message_seq` | NapCat 的服务端消息序号 |
| `snowluma` | `message_id` | 有符号 int32，可能为负 |

业务代码不判断后端：`core/anchors.py` 同时携带 `message_seq` /
`message_id` / `real_id`，`transport/api.py` 按后端档案只发当前后端认识的那个。
SnowLuma 的详细接入步骤见 `docs/SNOWLUMA.md`。

### 1.2 `sender_config.json`（仅 `ws` 模式）

```json
{
  "type": "ws",
  "backend": "napcat",
  "access_token": "NapCat520",
  "ws_host": "127.0.0.1",
  "ws_port": 3000
}
```

`backend` 可以省略：省略时跟随 `config.json` 的后端。面板保存时会把两者
同步成同一个后端。

### 1.3 `bot_config.json`

键名与旧项目**完全一致**，面板按同样的结构读写。完整默认值见
`config/defaults.py`。主要分组：

| 分组 | 作用 |
|---|---|
| `master_qq` | 主人 QQ 列表（权限最高） |
| `heartbeat` | 心跳间隔（分钟） |
| `agent` | LLM 模型、API Key、路由、插话判断模型、人设文件 |
| `message.extraconfig` | `recent` 是否带最近历史；`tokenizer` 开关 |
| `interject` | 群聊主动插话 |
| `meme` | 表情包：开关、JSON 路径、写进提示词的条数 |
| `relations` | 人物关系档案：标签 / 画像 / 会过期的近期状态 / 长期记忆 |
| `auto_forward` | 自动搬运 + 去重 + 机器人白名单 + 末尾广告裁剪 / 尾部追加 |
| `image_forward` | `img_fd -a` 的默认目标群 |
| `prompts` | 场景备注、历史命令正则 |
| `replies` | 固定回复文案 |
| `web_panel` | 面板监听地址 / 端口 / token |
| `keyword_actions` | 关键词 → 动作 |

#### `meme`（表情包）

```json
"meme": { "enable": true, "path": "data/memes.json", "prompt_limit": 0 }
```

- `enable`：关掉后模型不再写 `<meme>`，旧消息里的表情标记会被直接丢掉
- `path`：表情清单 JSON。只有文件名时按 `data/` 解析，带目录的按项目根解析
- `prompt_limit`：写进提示词的条数，`0` = 全部；表情很多时可以只给前 N 条省 token

表情文件的格式与 SuperMaGe 的 `Meme.json` 一致：

| 键 | 类型 | 发送方式 |
|---|---|---|
| `faces` | QQ 内置小表情，只有数字 `id` | `[CQ:face,id=…]` |
| `mfaces` | 商城表情包，需要 `emoji_package_id` + `emoji_id` + `key` + `summary` | `mface` 消息段 |
| `stickers` | 本地图片路径（`file` / `path`） | `[CQ:image,file=…]` |

名字匹配顺序是「完全一致 → 名字互相包含 → 描述关键词」，三级都没命中就**不发**
（发错表情比不发更尴尬）。改表情包不用改代码，重启机器人即可。

提示词默认把表情包当作日常表达；情绪明显（安慰、撒娇、无语、震惊、害羞、游戏等）
或对方点名要表情包时，`agent/hints.py` 还会追加一条「这轮带一个贴切表情」的提示，
候选名字会先和当前表情库核对。关掉 `meme.enable` 可以整体停用这套行为。

#### `agent.persona_file` / `relations`（人设与关系档案）

```json
"agent": {
  "persona_file": "agent/personas/junjing.md",
  "persona_core_file": "agent/personas/core_qq.md"
},
"relations": {
  "enable": true,
  "data_dir": "data/relations",
  "master_label": "主人",
  "state_ttl_hours": 72,
  "state_max_hours": 168,
  "self_ttl_hours": 12,
  "max_memories": 30,
  "auto_extract": true,
  "auto_every": 8,
  "auto_extract_min_seconds": 600
}
```

- `persona_file` / `persona_core_file`：角色卡和通用群聊行为，相对路径按项目根解析；
  文件缺失时回退到 `agent/prompts.py` 的内置兜底。
- `relations.data_dir`：每个人 / 每个群的 JSON 档案目录，含 `users/`、`groups/`、`self.json`。
- `state_ttl_hours`：近期状态默认保留时长；`state_max_hours` 是硬上限。
- `self_ttl_hours`：君景自己的 `<status>` 保留时长。
- `auto_extract` / `auto_every` / `auto_extract_min_seconds`：低频自动整理开关、触发条数、最小间隔。

字段格式、三种维护方式（人工改 JSON / `update_person` / 自动整理）和时间规则见
[`docs/RELATIONS.md`](RELATIONS.md)。

#### `auto_forward.strip_tail_ads`（末尾广告裁剪 + 尾部追加）

群里的聊天记录经常在末尾挂广告：带二维码的进群海报、群推荐卡片、或者干脆一条链接。
打开这一节之后，搬运**不再整卡转发**，而是把内容取回来重建成一份新的聊天记录：

```json
"auto_forward": {
  "strip_tail_ads": {
    "enable": true,
    "scan_tail": 3,
    "drop_qr": true,
    "drop_group_card": true,
    "drop_link": true,
    "image_timeout": 8,
    "append": {
      "enable": true,
      "items": [
        {"uin": 10004, "name": "N552AA🍥🍜🍣🍕💊🍫", "content": "独角兽君景搬运喵~"}
      ]
    }
  }
}
```

| 键 | 说明 |
|---|---|
| `enable` | 总开关。关掉就完全回到原来的整卡转发 |
| `scan_tail` | 只检查末尾几条（`0` = 不检查，只做追加）。广告一般就挂在最后两三条 |
| `drop_qr` | 删掉带二维码的图片（OpenCV 检测；没装 opencv 时这一项自动跳过） |
| `drop_group_card` | 删掉群推荐卡片：`com.tencent.tuwen.lua` / 群公告 / 落地页是 `qun.qq.com`、`m.q.qq.com/a/p/` 的卡片 |
| `drop_link` | 删掉「整条就是一个链接」的消息。正文里顺带提一句网址不算，不会误删正常聊天 |
| `image_timeout` | 单张图片下载 + 识别二维码的超时（秒） |
| `append.enable` | 是否在末尾追加自己的消息 |
| `append.items[]` | 追加的消息，按列表顺序排在最后。`content` 写字符串就是文本；要多条就多配几项；要发图片就写消息段数组 |

追加条目的字段：

| 键 | 说明 |
|---|---|
| `uin` | 显示用的 QQ 号。**必须是不存在以外的真实号**，NapCat 认不出时会把发送者换成占位号 |
| `name` | 聊天记录里显示的名字 |
| `content` | 文本字符串，或消息段数组，例如 `[{"type":"image","data":{"file":"D:/imgs/a.png"}}]` |
| `time` | 可选，时间戳；不写就用当前时间 |

本地图片路径会自动转成 `base64://` 再发 —— NapCat 常常跑在 WSL / 容器里，读不到
Windows 路径（直接传路径会报「识别URL失败」）。

实现上，只有「真的丢了广告」或「真的追加了消息」才会重建发送；一条都没动就还是原来的
整卡转发，零额外开销。判定逻辑在 `messaging/forward_filter.py`，搬运编排在
`bot/services/forward.py::forward_rebuilt`。


### 1.4 `environment.json`

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

- `bot_env`：Bot 跑在哪儿。`windows` / `wsl` / `linux`
- `backend_env`：协议端（NapCat / SnowLuma）跑在哪儿。同上。
  旧名字 `onebot_env` / `napcat_env` 仍然可读；两者同时出现时
  `backend_env` 优先。**写盘只写 `backend_env`**，首次保存会自动迁移。
- `path_profile`：路径翻译预设，见下一节。缺省时按两端环境推导。
- `wsl_distro`：WSL 发行版名（处理 `\\wsl$\<distro>\...` 时用）
- `wsl_mount_root`：WSL 里 Windows 盘的挂载点，默认 `/mnt`
- `imgs_dir`：回复图片目录。可以是绝对路径，也可以是相对项目根的路径
- `downloads_dir`：下载完成后的输出目录
- `temp_dir`：下载/合并时的临时目录
- `path_map`：额外映射，叠加在 `path_profile` 之上，**最长前缀优先**。
  给「windows ↔ linux（非 WSL）」或 Docker / 远程后端这种无法自动推导的情况用

```json
"path_map": [
  {"from": "D:\\share", "to": "/data/share", "note": "NAS 挂载"}
]
```

`note` 只用于面板展示，不参与匹配。

---

## 2. 路径翻译（`bot_env` / `backend_env` / `path_profile`）

### 2.1 探测顺序

```text
环境变量 JJ_BOT_ENV / JJ_BACKEND_ENV（兼容 JJ_ONEBOT_ENV、JJ_NAPCAT_ENV）
  > config/environment.json
  > config/config.json 里的同名键
  > 自动探测
```

自动探测：

- `os.name == "nt"` → `windows`
- Linux 且 `/proc/version` 含 `microsoft` → `wsl`
- 其它 → `linux`

`backend_env` 没写时的默认值：

- Bot 在 Windows → **`wsl`**（与旧项目 `_to_napcat_file_path` 的默认行为一致；
  Bot 和后端都在 Windows 时改成 `windows`）
- Bot 在 WSL / Linux → 与 `bot_env` 相同

### 2.2 `path_profile` 预设

| profile | 适用场景 | 例子 |
|---|---|---|
| `same` | 同机 / 共享挂载 | `D:\a\b` → `D:\a\b` |
| `windows_to_wsl` | Bot 在 Windows，协议端在 WSL | `D:\Code\x\a.png` → `/mnt/d/Code/x/a.png` |
| `wsl_to_windows` | Bot 在 WSL，协议端在 Windows | `/mnt/d/Code/x/a.png` → `D:\Code\x\a.png` |
| `custom` | 只用 `path_map`，不做任何自动推导 | 由规则决定 |

`path_map` 永远叠加在预设上。例如 `windows_to_wsl` + `D:\share → /data/share`：

```text
D:\share\a.png  → /data/share/a.png   （更长的自定义规则优先）
D:\Code\a.png   → /mnt/d/Code/a.png   （回落到盘符规则）
```

匹配规则：

- 最长前缀优先，与列表顺序无关（同长度时先出现的优先）
- 边界感知：`D:\share` 不会匹配 `D:\shareholder`
- Windows 盘符 / UNC 的源前缀大小写不敏感，但保留剩余部分的原始大小写
- `\` 和 `/` 在比较时等价；输出分隔符跟随目标风格
- `http://` / `https://` / `base64://` / `file://` / `data:` 原样返回
- 相对路径先按项目根目录展开，再翻译

### 2.3 谁负责转换

**引擎在 `core/pathmap.py`，绑定在 `config/environment.py`。**

- `core/pathmap.py::PathTranslator`：纯字符串翻译，不读配置、不碰文件系统
- `config/path_profiles.py`：预设规则（`same` / `windows_to_wsl` / ...）
- `config/environment.py::PathBridge`：把两者接到 `environment.json` 上

`images/` 是唯一对业务暴露的入口：

```python
from images import images

ref = images.resolve("imgs/妈妈.gif")   # 本机路径 + 协议端路径
images.to_backend(ref)                   # 发消息时用（旧名 to_napcat）
```

`PathBridge` 的主接口：

| 方法 | 方向 | 旧名字 |
|---|---|---|
| `to_backend()` | Bot → 协议端 | `to_napcat()` / `to_runtime()` |
| `to_local()` | 协议端 → Bot | `to_bot()` |

业务模块里出现手写的 `/mnt/` 或 `D:\` 都算 bug。

### 2.4 常见场景

**Bot 在 Windows，协议端在 WSL（最常见）**

```json
{"bot_env": "windows", "backend_env": "wsl", "path_profile": "windows_to_wsl"}
```

- 下载到 `D:\Code\JungJingNew\data\downloads\a.mp4`
- 发送时自动变成 `/mnt/d/Code/JungJingNew/data/downloads/a.mp4`

**Bot 和协议端都在 Windows**

```json
{"bot_env": "windows", "backend_env": "windows", "path_profile": "same"}
```

- 不做盘符转换，路径原样发送。

**Bot 在 WSL，协议端在 Windows**

```json
{"bot_env": "wsl", "backend_env": "windows", "path_profile": "wsl_to_windows"}
```

- `/mnt/d/...` → `D:\...`

**Docker / 远程后端**

自动转换不可靠，用 `custom` + `path_map`：

```json
{
  "bot_env": "linux",
  "backend_env": "linux",
  "path_profile": "custom",
  "path_map": [{"from": "/app/data", "to": "/napcat/data"}]
}
```

### 2.5 网页面板「路径映射」

「连接方式」旁边的**路径映射**页可以改：

- 协议端环境（`backend_env`）
- 翻译模式（`path_profile`）
- WSL 挂载根 / 发行版
- 自定义规则（`path_map`，可增删改）
- 路径预览：输入一条本机路径，同时看到 `to_backend` / `to_local` 的结果和命中的规则

保存后调用 `settings.reload()`，`PathBridge` 原地 `reconfigure()`，
`ImageResolver` 等持有者不需要重建，**不需要重启 Bot**。

---

## 3. 目录约定

| 目录 | 内容 | 是否提交 |
|---|---|---|
| `config/` | 配置 | `config.json` / `sender_config.json` 不提交 |
| `data/` | 去重库、断点、下载、临时文件 | 不提交 |
| `imgs/` | 回复图片 | 提交（`imgs/dress/` 除外） |
| `logs/` | 日志 | 不提交 |
| `frontend/` | 前端静态文件 | 提交 |
| `tests/` | 测试 | 提交 |

`data/` 下的结构：

```text
data/
├─ forward_dedup.db          # 去重库（与旧库兼容）
├─ forward_dedup.db-wal
├─ forward_dedup.db-shm
├─ forward_dedup_log.jsonl   # 可选日志
├─ check_point/
│  └─ <user_id>.json         # 断点
├─ downloads/                # 下载成品
├─ tmp/                      # 下载临时文件（自动清理）
├─ memes.json                # 表情包清单（faces / mfaces / stickers）
└─ methods.json              # Agent 方法库（旧：Agent/history/method.json）
```

---

## 4. 网页面板的配置写入

- 面板只写 `config/bot_config.json` 和 `config/config.json` / `sender_config.json`。
- 写入是原子的：先写临时文件，再替换。
- 保存后调用 `BotSettings.reload()` + `BotApp.reload_config()`。
- `agent.*` 改动：后台重建 Agent，不需要重启。
- `web_panel.*`、连接方式：返回 `restart_required: true`，需要重启 Bot。
- 面板 token 只保护 `/api/*`，不保护静态页面。
