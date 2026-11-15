# 心理分析（`psychology`）

群里**回复一条消息或一段聊天记录**，发「分析心理」，Bot 用一套**独立于主对话**的
模型做一次心理侧写，并（默认）把它重建成**一条聊天记录**发出来：每条原话后面跟
`<潜台词>`，图片原样附上——就是 `message_id:876984233` 那种形态。

它和主对话（`agent.*`）是两套配置：可以换一家服务商、换成多模态模型、单独关掉，
都不影响群聊；反过来，主对话换模型也不会改变心理分析的输出。

## 1. 触发流程

```text
关键词命中（psychology.keywords，整条消息基本等于其中某个词）
  └─ PsychologyService.handle(event)
       ├─ 群白名单 / 黑名单 / 私聊开关（psychology.groups / blacklist_groups / allow_private）
       ├─ 冷却（psychology.cooldown_seconds，同一会话）
       ├─ 是不是回复消息？（mp.is_reply → get_msg 取回被回复的消息）
       │    ├─ 是聊天记录（forward 段 / multimsg 卡片 / raw_message 里的 CQ:forward）
       │    │    → ForwardExpander 摊平到叶子消息（max_records 上限）
       │    │    → 图片过滤门只判「将要发给模型的前 max_images 张」
       │    │    → 素材（逐条原文 + [图片N] 占位）
       │    ├─ 是普通消息 → 单条素材
       │    └─ 取不到 → 回一句「可能已过期」
       └─ 不是回复 → 用当前消息本身（去掉唤醒词后没内容就回用法提示）
            ↓
       按 reply_mode 分支：
         text    → PsychologyAnalyzer.analyze()       # 一段总结
         forward → PsychologyAnalyzer.analyze_nodes() # 逐条原文 + <侧写>
         both    → 先总结，再逐条（两次模型调用）
            ↓
       forward：build_node 拼成聊天记录 → send_*_forward_msg
       text：引用触发消息回复（超长按段落拆成最多 3 条）
```

「先判断是不是回复消息，再判断回复的是不是聊天记录」是刻意的：没有回复时不要把
当前这句「分析心理」喂给模型（它会分析这四个字），直接给用法提示。

## 2. 配置

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

面板：**心理分析**页（`Bot 行为` 分组）。

| 键 | 说明 |
|---|---|
| `enable` | 关闭时**连唤醒词都不注册**，群里发词毫无反应（不是「回一句未启用」） |
| `keywords` | 唤醒词。正则按「整条消息 ≈ 唤醒词」匹配（允许前后空白和标点），`帮我分析心理学` 不触发；清空 = 不注册（功能等于关掉）。**不要再往 `keyword_actions` 里加一条 `action=psychology` 的规则**，否则同一条消息会被调度两次 |
| `reply_mode` | `forward`（默认，聊天记录逐条侧写）/ `text`（一段总结）/ `both`（两者都发，多一次调用） |
| `image_mode` | `image` / `ocr` / `vision`，见下一节 |
| `model_id` | 引用 `agent.models` 里某条的 `id`；填了就用那条（包括它的密钥和地址） |
| `base_url` / `api_key` / `api_key_env` / `model` | 独立配置。`model_id` 为空时才生效。`base_url` 留空沿用主对话地址；**换了地址不会借用主对话密钥**（与模型分级一致） |
| `vision.*` | 视觉模型，只在 `image_mode=vision` 时用；留空 = 用分析模型看图 |
| `prompt` / `vision_prompt` | 留空用内置提示词。内置的已经写了「不做临床诊断 / 不编造隐私 / 材料不足就说」 |
| `nodes_prompt` | 逐条侧写（聊天记录模式）的提示词；留空用内置的「#编号 + 原话 + \<侧写\>」格式 |
| `forward_max_tokens` | 逐条侧写的输出额度，默认 2000；太小会被截断，后面的消息只剩原话 |
| `forward_include_images` | 聊天记录节点里是否附原图；总预算 12MB，超了后面的图自动不附 |
| `max_images` | 送进模型的图片张数；`0` = 不带图（记录里仍有 `[图片N]` 占位） |
| `max_records` | 聊天记录最多展开几条，超出标「只截取了一部分」 |
| `image_max_bytes` / `image_max_side` | 单张图压缩上限（默认 4MB / 最长边 1568）。压缩后仍超就跳过这张 |
| `use_image_filter` | 发图前过图片过滤门；命中的图不发给模型（过滤规则在「图片过滤」页） |
| `cooldown_seconds` | 同一会话两次分析的最小间隔，防刷屏 |
| `groups` / `blacklist_groups` / `allow_private` | 生效范围；留空 = 所有群 / 都不屏蔽 / 私聊可用 |

## 3. 回复形式（`reply_mode`）

| 值 | 发什么 | 模型调用 | 说明 |
|---|---|---|---|
| `forward`（默认） | 一条聊天记录：每个节点 = `原文 + <侧写>`，图片附在节点里 | 1 次（逐条） | 和 `message_id:876984233` 一致；原文保真，模型只补潜台词 |
| `text` | 一段侧写：结论 / 情绪 / 潜台词 / 怎么接 | 1 次 | 最省，适合不想刷屏的群 |
| `both` | 先一段总结，再一条聊天记录 | 2 次 | 额度翻倍 |

聊天记录模式的两个兜底：

- 模型没按 `#编号` 格式返回 → 自动退回 `text` 模式（会多一次调用），并说明原因；
- 聊天记录发送失败（协议端拒绝 / 超时）→ 把节点文本拼成普通消息发出去，内容不丢。

节点结构（`send_group_forward_msg`）：

```json
{
  "type": "node",
  "data": {
    "uin": "709917580",
    "name": "通天之塔",
    "time": 1793627989,
    "content": [
      {"type": "text", "data": {"text": "我买杯子干什么 <人家是好男孩，不会一天到晚想着这些事情的>"}},
      {"type": "image", "data": {"file": "base64://...", "summary": "图片"}}
    ]
  }
}
```

- 发送者 / 昵称 / 时间都取原消息，所以卡片里看起来还是原来那群人在聊；
- 图片优先用 base64（跨机、rkey 过期都稳），单张超过 4MB 退回 URL；
- 被回复的那条消息会渲染成 `<回复消息<原话>>` 前缀，和示例一致。

## 4. 三档图片策略

| `image_mode` | 做什么 | 要求 | 代价 |
|---|---|---|---|
| `image` | 图片转 base64 作为 `image_url` 直接发给分析模型 | 分析模型必须支持图片输入 | 最全（排版、表情、截图细节都在），token 最贵 |
| `ocr` | 只把 OCR 文字拼进记录，不发送图片 | 有可用 OCR（`media/ocr.py`：Windows 内置 / pytesseract） | 最省最保守；表情、构图和 OCR 认不出的部分会丢 |
| `vision` | 先用视觉模型逐张写**客观描述**，再把描述交给分析模型 | 视觉模型（或分析模型本身）支持图片 | 折中：分析模型可以用便宜文本模型，两次调用 |

`vision` 是默认值：群里遇到的图大概率是聊天截图，先描述再分析，比让小模型直接看图稳。
逐条模式同样走这套策略：`vision` 的描述会作为材料的一部分给模型，`image` 则把图直接附在
请求里。

## 5. 过滤门在这里的边界

心理分析只判**将要发给模型的前 `max_images` 张图**，不是整份记录：

- 整段记录可能有几百张图（实测 512 张），全量下载判定会让命令等几分钟；
- 心理分析真正会**外发**的就是这几张，判它们才有意义；
- 过滤规则沿用「图片过滤」页的配置（`image_filter.filters`），只是不发送审计标签、
  不拦截消息，命中就把那张图从模型输入里去掉，并计入「有 N 张图片没有纳入分析」；
- 聊天记录节点里也不会附命中的图（`blocked` 的图片直接跳过）。

## 6. 排查

| 现象 | 看哪里 |
|---|---|
| 发唤醒词没反应 | `enable`；关键词是否整条匹配（「分析心理一下」不算）；群白名单；冷却；日志 `[心理分析]` |
| 回「心理分析模型还没配好：未配置（缺 …）」 | `model` / `base_url` / `api_key` 三个必填项；用 `model_id` 时检查那条模型是否还在 `agent.models` |
| 回「心理分析模型被限流了（429）」 | 换模型或等冷却（额度类冷却 30 分钟，普通限流 60 秒） |
| 图片没带上 | 图片下载失败（rkey 过期 / 超时）、被过滤门命中、`max_images=0`、压缩后仍超 `image_max_bytes` |
| 聊天记录打不开 | 记录太大 / 已过期 / 后端限制；先试试只回复其中一小段 |
| 聊天记录里后面几条只有原话 | `forward_max_tokens` 太小被截断，调大；或模型没按 `#编号` 返回（日志会写「退回文本模式」） |
| 发出去的是文字而不是聊天记录 | `reply_mode` 是不是被改成 `text`；节点发送失败时也会退回文字（日志有原因） |

日志前缀统一是 `[心理分析]`，包含来源、素材摘要（字数 / 条数 / 图片张数）和失败原因。

## 7. 测试

```bash
pytest tests/test_psychology.py -q
```

覆盖：关键词整条匹配、素材渲染（图片 / 表情 / `content` 键兼容）、三档图片策略、
逐条格式解析（严格 `#编号` / 宽松编号 / 续行 / 缺条兜底）、聊天记录节点结构、
发送失败退回文本、未配置模型的可读提示、冷却与群过滤。
模型调用全部是替身，不碰网络。
