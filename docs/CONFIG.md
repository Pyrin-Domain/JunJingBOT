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
| `agent` | LLM 模型、API Key、路由、插话判断模型、人设文件、口头禅文件 |
| `message.extraconfig` | `recent` 是否带最近历史；`tokenizer` 开关 |
| `media` | 发文件 / 视频段的回执超时；B 站下载历史开关 |
| `interject` | 群聊主动插话 |
| `meme` | 表情包：开关、JSON 路径、写进提示词的条数 |
| `relations` | 人物关系档案：标签 / 画像 / 会过期的近期状态 / 长期记忆 |
| `auto_forward` | 自动搬运 + 去重 + 机器人白名单 + 发送者过滤 + 末尾广告裁剪 / 尾部追加 |
| `forward_image_dedup` | 机器人图片查重：总开关、送达记录保留天数、内容身份保留天数、感知哈希阈值、库路径 |
| `image_forward` | `img_fd -a` 的默认目标群 |
| `export` | `export -a` 的默认目标群（留空则沿用 `auto_forward.target_groupid`） |
| `prompts` | 场景备注、历史命令正则 |
| `replies` | 固定回复文案 |
| `web_panel` | 面板监听地址 / 端口 / token / CORS 白名单 |
| `keyword_actions` | 关键词 → 动作 |

#### `web_panel`（参数面板）

```json
"web_panel": {
  "enable": true,
  "host": "127.0.0.1",
  "port": 8390,
  "token": "",
  "cors_origins": []
}
```

- 默认只监听本机；改成 `0.0.0.0`、局域网 IP 或公网地址时，`token` 必填，
  否则 Bot 会拒绝启动面板，避免配置被匿名修改
- `cors_origins` 只给「前端页面与 API 分开部署」使用，填写页面来源，
  例如 `["http://127.0.0.1:8080"]`；同源访问不需要配置，`*` 会允许任意网站
- 面板 token 不写进 `localStorage`，从 `?token=` 读取后会从地址栏移除并只保存在
  当前浏览器会话中

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

#### `media`（发送超时与下载历史）

```json
"media": { "send_timeout": 600, "bili_history": true }
```

- `send_timeout`：发文件 / 视频段等协议端回执的秒数。协议端要等**整个上传结束**
  才回包（实测 36MB≈149s），30 秒的传输层默认值必然不够，这里放宽到 10 分钟。
  超时后**不重试、不换通道、不回复任何文案**（旧版会把解析出来的直链当
  「下载地址」发出去，链接带时效签名、基本是错的，已删除整条回退分支）。
- `bili_history`：按 BV 号索引本地文件，同一条视频只下载一次（`data/bili_history.json`）。
  命中直接发缓存文件；文件被清理则自动删记录重下。关掉后每次都重新下载（旧行为）。
  历史最多保留 200 条，超出丢最旧的。

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

#### `agent.catchphrase_file`（Agent 自己的口头禅）

口头禅单独放在 JSON 文件里，不写死人设 Markdown：

```json
"agent": {
  "persona_file": "agent/personas/junjing.md",
  "persona_core_file": "agent/personas/core_qq.md",
  "catchphrase_file": "agent/catchphrases.json",
  "catchphrase_limit": 0
}
```

文件是数组，每项两个字段：

| 字段 | 说明 |
|---|---|
| `context` | 口头禅正文，实际可以说出来的话 |
| `describe` | 这句口头禅的描述：什么时候用、什么意思 |

```json
[
  {
    "context": "那当然啦~",
    "describe": "被夸可爱、被夸厉害时说；表达开心又有点得意"
  },
  {
    "context": "你猜",
    "describe": "被问到是不是机器人时用；不正面回答，带点调皮"
  }
]
```

- `catchphrase_file`：相对路径按项目根解析；留空或 `false` 关闭。仓库里只放
  `agent/catchphrases.example.json`，实际文件 `agent/catchphrases.json` 已被 `.gitignore`。
- `catchphrase_limit`：写进提示词的条数，`0` = 全部。
- 口头禅会作为「命中时优先使用」的表达写进系统提示词：当前情境对得上 `describe`
  时会优先用；没有合适情境就不说，也不允许为了用而硬接话。
- `describe` 里用「」引号举的例子会被当成轻量触发词；用户消息命中时，本轮还会
  额外提醒模型优先用这条口头禅。
- 也兼容顶层 `{"catchphrases": [...]}` 和 `phrase` / `text` 等字段别名。
- 改文件后需要重启 Bot（Agent 初始化时读一次）。

#### `auto_forward.filter`（按来源群 + 发送者过滤聊天记录）

某些群里只不想搬某位朋友发的聊天记录，可以配这一节：

```json
"auto_forward": {
  "filter": [
    {"group_id": 20002, "user_id": [10003, 10004]},
    {"group_id": 20005, "user_id": [10006]}
  ]
}
```

| 键 | 说明 |
|---|---|
| `group_id` | **事件所在的来源群号**，不是搬运目标群 |
| `user_id` | 该群里要过滤的发送者 QQ 号数组；空数组表示不过滤 |

命中的 `old_forward` / `multimsg` 聊天记录会直接跳过：不搬运、不进去重库，也不交给后面的 AI 处理。机器人图片搬运和小程序卡片不受影响。

面板里对应「搬运与去重 → 转发过滤（按来源群 + 发送者屏蔽）」，一条规则填来源群号和要屏蔽的发送者 QQ。

`user_id` 写成单个数字也能识别，但建议按上面的数组格式配置；配置节也兼容别名 `forward_filter`。

#### `auto_forward.auto_img_forward_from_bot`（机器人发图搬运）

把「其他机器人（或你自己的号）发的图」自动搬到目标群：

```json
"auto_forward": {
  "auto_img_forward_from_bot": {
    "enable": true,
    "bot_list": [
      {"bot_id": 3282647559, "type": "all", "group_id": [], "user_id_list": []},
      {"bot_id": 1013098110, "type": "private"}
    ]
  }
}
```

| 键 | 说明 |
|---|---|
| `enable` | 总开关 |
| `bot_id` | 发图的那个 QQ 号（可以是机器人，也可以是自己的号） |
| `type` | `group` 只看群聊 / `private` 只看私聊 / `all` 都看 |
| `group_id` | `type` 含群聊时生效；留空 = 所有群聊 |
| `user_id_list` | `type` 含私聊时生效；留空 = 所有私聊（面板不显示，手写配置仍认） |

**判定顺序很重要**：这条白名单在「来源群 / 任务私聊」两道闸门**之前**判定。
所以私聊里由白名单号发来的图片，不需要把对方填进 `auto_forward.userid_list`
也会搬运；而群聊来源仍然要求群号在 `auto_forward.groupid_list` 里（避免从
不相干的群往目标群刷图）。

命中后走的是「单条消息转发」（`forward_group_single_msg`），实测支持
私聊 → 群 的跨会话转发；目标为 `target_groupid` / `task_userid`。

图片和聊天记录**不是同一个库**：图片按「消息身份 × 会话」记在
`data/forward_image.db`（`forward_image_delivery` 表），送达过的会话不会再发；
聊天记录按内容指纹记在 `data/forward_dedup.db`。图片这条链路没有【发过了喵】——
图片没有正文，回一句「发过了」只会让人莫名其妙。

```json
"forward_image_dedup": {
  "enable": true,                  // 图片查重总开关
  "db_path": "forward_image.db",   // 相对路径只当文件名，落在 data/ 下
  "keep_days": 30,                 // 送达记录保留天数，0 = 不清理
  "image_keep_days": 2,            // 图片内容身份保留天数，0 = 不单独清理
  "perceptual": true,              // 感知哈希兜底（按图片本体去重）
  "perceptual_max_distance": 4,    // 128bit 双哈希的汉明距离阈值，0~128
  "perceptual_keep_days": 0,       // 感知指纹保留天数，0 = 用 keep_days
  "perceptual_timeout": 8.0        // 单张图下载超时（秒）
}
```

可选项，不写就用默认值（查重开启，`data/forward_image.db`，送达记录 30 天、内容身份 2 天）。
`enable=false` 时白名单机器人发的图直接搬运，不查询也不登记送达记录；图片过滤门仍然生效。
面板在「搬运与去重 → 机器人图片」里可以直接改 `enable` / `keep_days` / `image_keep_days`。

**为什么要感知哈希**：图片身份 `image:*` 取的是图片段 `data.file`（QQ 上传时算的 md5）。
同一张图被重新上传 / 重新压缩后 md5 就变了，只认它会把同一张图当成新图再搬一遍
（线上实例：2026-11-06 08:41 与 14:57 同一组 5 张截图、像素完全相同、md5 大小写不同，
各搬了一轮全部目标群）。开启 `perceptual` 后，机器人图片会额外按图片本体算
aHash+dHash，按汉明距离模糊匹配：同一张图重新压缩/缩放后距离 0~2，不同截图约 5 以上，
所以默认阈值 4。判定要求**这条消息里的每张图都命中**才跳过 —— 多图消息里
「旧图 + 新图」照常搬，不会吞掉新图。取图字节时优先复用图片过滤门的下载缓存，
过滤门没开才会多一次下载；取不到就退回旧的「消息身份 + 文件名哈希」判定（宁可多搬一次）。

#### `forward_dedup.clean_*`（入库清洗）

拼指纹**之前**先把噪声从转发内容里去掉，让「同一份内容换个马甲转发」也能命中。
只影响判定，不影响发出去的样子（那是 `auto_forward.strip_tail_ads` 的事）。

```json
"forward_dedup": {
  "clean_enable": true,
  "clean_scan_tail": 3,
  "clean_drop_promo": true,
  "clean_drop_qr": true,
  "clean_drop_group_card": true,
  "clean_drop_link": true,
  "clean_image_timeout": 3.0,
  "clean_qr_images": 3
}
```

| 键 | 说明 |
|---|---|
| `clean_enable` | 总开关 |
| `clean_scan_tail` | 末尾几条算「广告区」 |
| `clean_drop_promo` | 装扮 / 气泡分享卡片，**不看位置**（`bizsrc=tianxuan.shareArk`、`prompt=发现超好看的装扮`、`jumpUrl` 里的 `vas_gxh_dress_detail` 都认） |
| `clean_drop_qr` | 末尾图片里的二维码海报（要下载解码，慢一点；关掉就只按文本/卡片判） |
| `clean_drop_group_card` | 末尾的群推荐卡片 / 加群落地页 |
| `clean_drop_link` | 末尾的纯链接、「吆喝 + 链接」（`更多xx↓ https://…`） |
| `clean_image_timeout` | 二维码检测的单图下载超时（秒） |
| `clean_qr_images` | 整条记录最多下载几张图做二维码检测（默认 3，防止十几张图拖慢入库） |

`clean_drop_promo` 是线上实测补的：一条记录里 32 张气泡广告做占位，真正的内容
只有末尾一个视频；不洗的话广告条数一变，同一份内容就变成两条指纹。

清洗会改变 content_hash，所以在它上线**之前**入库的老行会自动兼容：闸门会多拼
一份「清洗前」的指纹当别名查一次（`build_fingerprint(..., clean=False)`），命中就
把那一行就地升级成清洗后的哈希。不需要手工重建；实在要批量对齐再用上面的
`--clean` 重建命令。

老库可以用日志重新入库（只处理日志里能还原原文的行，先 `--dry-run` 看数字）：

    python -m bot.services.dedup_rebuild --clean --dry-run
    python -m bot.services.dedup_rebuild --clean

本项目日志只覆盖它自己启动之后的判定，迁移过来的老行要**追加老项目的日志**才还原
得出来（`--log` 可重复，本项目日志永远参与）：

    python -m bot.services.dedup_rebuild --clean --dry-run \
        --log D:/Code/JunJingBOT/data/forward_dedup_log.jsonl

日志里仍然没有原文的行不会被改（也改不了），它们靠判定时的「清洗前别名查询」兜底。

#### `auto_forward.forwarded_already_mface`（「这条发过了」的提示表情）

去重闸门判定「这份内容在这个群发过了」时，会在群里回一个提示。以前回的是本地图片
`imgs/news.gif` + 一句「[发过了喵]」的文字说明，现在回的是**一个商城表情**（mface）：
表情自带表情名，一只猫就把话说清楚了，不用再配文字。

```json
"auto_forward": {
  "forwarded_already_mface": {
    "summary": "[此条news已被其他群友发布过]",
    "emoji_id": "2d831a9731974a5bd0fa57e892bc0bd8",
    "emoji_package_id": 233590,
    "key": "fe89fd36de66bdc2"
  }
}
```

| 键 | 说明 |
|---|---|
| `emoji_id` | 表情本体（32 位 md5）。**缺这一项发不出商城表情** |
| `emoji_package_id` | 表情包 ID |
| `key` | 包内密钥。**缺这一项同样发不出去** |
| `summary` | 表情名，客户端就显示它（一般就是表情包里那个名字） |

三件套缺任何一件、或者整段被删掉 → **退回老的本地图片** `imgs/news.gif`，图片说明
取 `replies.forwarded_already`。这条提示是「别重复发」的告知，静默丢掉的代价比发一张
旧图大，所以不因为配置不全会整个消失。

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
`bot/services/forward/service.py::forward_rebuilt`。

#### `image_filter`（图片过滤门 + 审计标签）

搬运图片 / 聊天记录本来就要下载图片（去重指纹、二维码识别），顺手加一道过滤门：
命中的图**不打进普通目标**，而是打上「类型标签」发到审计群（`audit_group_id`，
本项目用 1032125562）。以后想加新的过滤，只是往注册表里加一个函数 + 配置里加一项，
不动搬运主流程。

```json
"image_filter": {
  "enable": true,
  "audit_group_id": 1032125562,
  "single_tag_mode": "single",
  "record_tag_mode": "reply",
  "block_matched": true,
  "scan_all_images": true,
  "max_images": 0,
  "image_timeout": 8,
  "max_tag_images": 6,
  "filters": [
    {
      "id": "phoebe_bullshit",
      "enable": true,
      "params": {
        "sender_ids": [3792417235],
        "group_ids": [740726382],
        "command": "菲比暴论",
        "command_window": 120
      }
    },
    {
      "id": "nsfw",
      "enable": true,
      "params": {
        "threshold": 0.5,
        "model_path": "D:/Code/sex_filter/models/nsfw_vit_quantized.onnx"
      }
    }
  ]
}
```

| 键 | 说明 |
|---|---|
| `enable` | 总开关。关掉就完全不过滤、不下载、不发标签 |
| `audit_group_id` | 审计群号。命中后的图 / 聊天记录连同标签都发到这里；留空时命中只拦不报 |
| `audit_operators` | 审计员 QQ 白名单（**「复搬」命令**用）。留空 = 审计群里谁都能复搬；填了就只有这些人能操作 |
| `single_tag_mode` | 单图标签样式：`single` = 一条 `[标签文本, 图片]` 消息；`forward_reply` = 先转发原消息，再**回复这条转发**发标签 |
| `record_tag_mode` | 聊天记录标签样式：`reply` = 先转发记录，再回复它发标签；`before` / `after` = 不回复，先发标签再转发（或反过来） |
| `block_matched` | 命中后是否拦截普通搬运。`false` = 只审计不拦截，搬运照旧 |
| `scan_all_images` | 是否扫描**聊天记录里**的图片（单图路径恒扫描）。关掉就只过滤白名单机器人的单图 |
| `short_circuit` | **命中即停**：开启后按批（4 张）下载+判定，一批命中就不再碰后面的图——拦截语义不变，省流量；代价是审计标签只剩命中的图。默认关（全量判定，每张图都有结论） |
| `max_images` | 单条聊天记录最多判定前多少张图。**默认 `0` = 不设上限**（推荐）。老默认 `12` 是漏判的直接来源：SnowLuma 的嵌套记录单张就有 170 条消息 / 500 多张图，第 13 张之后一张没下、一张没判，却被当成「安全」放过去 |
| `image_timeout` | 单张图下载超时（秒） |
| `max_tag_images` | 一条标签消息里最多带几张命中图 |
| `max_image_bytes` | 单张图下载 / 判定的字节上限（默认 20MB = 20971520）。超限的图标「超过大小上限未判定」，**不会**被当成安全图放行；要判更大的图就把这个值调大 |
| `napcat_http` | NapCat HTTP 地址（如 `http://127.0.0.1:3000`）。配置后取图走四级链路：内存缓存 → **本机 QQ 缓存**（各号 `nt_data/Pic` 的 Ori 原图 / Thumb `_720` 高清预览 + NapCat temp，按 image 段 file 字段的 md5 找，零网络）→ 网络直连 → **NapCat `download_file` 代下载兜底**。留空则只有网络直连 |
| `napcat_token` | NapCat HTTP 的 token |
| `filters` | 当前启用的过滤列表。**列表里没有的就是停用**，写了未知 `id` 直接忽略；每次判定都从配置重建，改完面板保存即生效，不用重启 |

`filters[]` 每项：`id`（注册过的过滤名）+ `enable` + `params`（传给过滤函数的参数）。
内置的三个：

| id | 说明 |
|---|---|
| `phoebe_bullshit` | 菲比大人的暴论图。主判据 = **同群最近 `command_window` 秒内出现过触发命令（默认「菲比暴论」）且发送者 / 群号对得上**（Bot 重启丢了上下文时退回版式判定：宽高比 2.2~4.5、右半近黑、白字、左半明显更亮的模板） |
| `nsfw` | ViT NSFW ONNX 检测（对齐 `D:/Code/sex_filter`）。需要 `onnxruntime` + 模型文件，缺哪个哪个停用，不影响其它过滤。**推理默认在独立子进程里跑**（`"subprocess": false` 可退回主进程，调试用）——onnxruntime 是原生代码，偶发崩溃会秒杀整个 Bot，子进程隔离后崩溃只损失一次打分；连续崩 3 次自动停用该过滤并打日志，其余过滤不受影响。生产配置开了 `deep`（可疑区间 0.05~阈值 加跑 3×3 分块，专治整图被浅色背景冲淡的擦边图；校准集零误报零漏报，正常图不受影响）。**黑白图三层判定**：黑白漫画/截图是 ViT 的误报重灾区（黑白配色骗模型），低饱和图命中后用 nsfwjs 5 分类复核（`confirm_model_path`，`hentai+porn > drawings` 才拦）；复核放行的改判「黑白图待人工」发审计群**人工确认、照常搬运**（`FilterHit.manual`），人工确认后把 md5 加进哈希黑名单即可一劳永逸。**GPU 可选**：`"use_gpu": true` + `pip install onnxruntime-directml`（RTX 4060 实测量化 ViT 单张略慢于 CPU、deep 分块批量快约 35%），没装或加载失败自动回退 CPU，崩溃照样有子进程隔离兜底 |
| `image_hash_blacklist` | 图片 aHash/dHash 黑名单（`params.hashes` + `max_distance`），以后想「就封这张图」用 |

判定和发送是分开的：`messaging/image_filter.py` 只回答「命中什么」（过滤函数 +
注册表 + 最近命令追踪，纯逻辑可单测）；`bot/services/forward/image_gate.py`
决定「怎么发标签」（模式选择、审计群发送、同一命中去重）。

**在线测试命令** `junjingtest_sex_filter`（目前限定群 1079845768 触发）：回复一条
聊天记录发送，Bot 会把注册表里的过滤函数**全部跑一遍**（不看 enable 开关），先回
逐张图的命中报告（含 NSFW 原始打分），然后把**每张判定过的图带结论逐条发出来**
（`[文本, 图片]` 消息，文案如「是色图（nsfw=0.9997）/ 不是色图（nsfw=0.0001 ok）」）；
不回复就直接发，会自动找本群往上数最近的一条聊天记录（嵌套记录走抽象层递归展开）。
纯判定不拦截、不发审计群，实现见 `bot/commands/sex_filter_test.py`。

**审计群「复搬」**（关键词 `复搬`，限定审计群触发，实现见 `bot/commands/audit_release.py`）：
`block_matched` 拦下的内容，人工看了标签后想真的搬——**回复那条【过滤标签】消息
发「复搬」**即可。Bot 从标签文本里解析来源群 + 消息 id（无状态，重启不丢），
登记一次性放行（复搬那次过滤门跳过，之后同一内容再进来照样过滤），然后按自动搬运
配置重搬一遍（**跳过去重闸门**——拦截前指纹已 reserve 进库，再走只会原地踏步），
审计群里回报「已放行并重新搬运 / 失败原因」。注意要回复**带【过滤标签】开头的那条**，
回复转发的记录卡片解析不到原始 id。

标签里的定位信息写的是**外层**消息：聊天记录命中时，「来源群：/ 发送者：/ 消息：」
记的是**卡片本身**那条消息（群里那张聊天记录卡片），不是记录里被拦的那条**内层**
消息——后端 `get_msg` 只认外层消息，内层 id 实测一律 "message not found"，拿它复搬
必然取不回原消息；而且三行必须指同一条消息，否则按标签回溯时对不上人。

内层那条（记录里真正发图的人）另起一行「内层来源：群 X · 名字（QQ 号）」，内层
消息 id 放在详情行里（`[内层 xxxx]`），人工排查「记录里第几条」被拦时看它。
**「内层来源」那一行不能出现「来源群：」三个字**——`audit_release.parse_tag` 靠
这个正则定位外层群，内层群混进去会被当成外层群解析出来。
测试命令和生产差一处（只影响测试，不影响生产搬运）：

* **下载大小上限 30MB**（生产 `max_image_bytes` 默认 20MB）——超过上限的大 GIF
  （9~25MB 很常见）在生产判定里会标「超过大小上限未判定」，测试命令全量下下来照判。
  QQ 图床的 URL 不支持缩略图参数（spec 非 0 一律 400），协议端本地也没有缩略图缓存
  （Thumb 只由带 UI 的 QQ 客户端生成，且只覆盖实际收过的图）；截断下载实测
  会漏判（GIF 截断画面和原图对不上），所以判定要么全量下、要么诚实标失败
  （失败原因会写明：超过大小上限 / HTTP xxx（rkey 过期）/ 下载超时）。

判定范围两边一致（2026-11-11 起）：**记录先摊平到叶子消息再判**，`max_images`
用配置值（默认 `0`，即整份记录全判）。以前测试命令有个「判定上限自适应」
（`max(配置值, (forward 段数+1)×2)`），生产却没有 —— 结果是测试命令报「全部正常」、
生产照样漏，比不测更误导，已经删掉。

#### `psychology`（心理分析）

```json
"psychology": {
  "enable": false,
  "keywords": ["分析心理", "心理分析", "心理侧写"],
  "reply_mode": "forward",
  "image_mode": "vision",
  "model_id": "",
  "base_url": "",
  "api_key": "",
  "api_key_env": "",
  "model": "",
  "temperature": 0.5,
  "max_tokens": 900,
  "vision": { "model_id": "", "base_url": "", "api_key": "", "api_key_env": "", "model": "" },
  "prompt": "",
  "vision_prompt": "",
  "nodes_prompt": "",
  "forward_max_tokens": 2000,
  "forward_include_images": true,
  "max_images": 6,
  "image_max_bytes": 4194304,
  "image_max_side": 1568,
  "max_records": 60,
  "use_image_filter": true,
  "cooldown_seconds": 30,
  "groups": [],
  "blacklist_groups": [],
  "allow_private": true
}
```

回复一条消息 / 聊天记录 + 唤醒词 → 用独立模型做心理侧写。

- `reply_mode`：`forward`（默认，重建成一条聊天记录：每条原话后跟 `<侧写>`，图片原样附上）/
  `text`（一段总结）/ `both`（两者都发，两次模型调用）。
- `image_mode`：`image`（原样发图，分析模型需多模态）/ `ocr`（只发 OCR 文字）/
  `vision`（先让视觉模型写描述，再交给分析模型）。
- `model_id` 引用 `agent.models` 的 id；留空则用本节 `base_url` / `api_key` /
  `model`。三个都填全才算「已配置」，否则触发时只回一句提示。
- `vision.*` 是视觉模型的独立配置，留空 = 用分析模型。
- `nodes_prompt` / `forward_max_tokens` 只管聊天记录模式（逐条侧写）；
  `prompt` 管一段总结。留空用内置提示词。
- 完整说明（流程、节点结构、过滤门边界、排查）见 `docs/PSYCHOLOGY.md`。

#### `export.auto_target_group_ids`（`export -a` 的默认目标群）

```json
"export": { "auto_target_group_ids": [] }
```

`export` 命令带了 `-a` 就不用再写 `-g`，直接搬到这一节列出的群：

- **留空（`[]`）时沿用 `auto_forward.target_groupid`**，也就是面板「自动搬运」里那组
  目标群，不用把同一批群号配两遍；
- 填了就以这里为准，方便手动导出走另一批群；
- 与 `img_fd -a` 的区别：`img_fd -a` 读的是 `image_forward.auto_target_group_ids`，
  两条命令各管一摊，互不影响。

```text
export -g 123456 -b        # 指定一个群
export -g 123456 -g 234567 -b   # -g 可以重复，一次多个群
export -a -b               # 用默认目标群
```

写 `-g` 时 `-a` 被忽略（`-g` 优先）；`-u/-user <QQ>` 与群目标互相独立，可以同时给。


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
├─ forward_dedup.db          # 聊天记录指纹库（内容级：文本 + 图片双哈希 + 视频）
├─ forward_dedup.db-wal
├─ forward_dedup.db-shm
├─ forward_dedup_log.jsonl   # 可选日志
├─ forward_image.db          # 图片送达库（身份 × 会话，独立于指纹库）
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
