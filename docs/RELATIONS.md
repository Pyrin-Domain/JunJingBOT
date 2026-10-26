# 人设与关系档案

这份文档说明「君景是谁」和「君景怎么记人」是怎么拆开的：

- **人设**回答「我是谁、我怎么说话」，放在 `agent/personas/` 的 Markdown 里；
- **关系档案**回答「这个人是谁、我们是什么关系、他最近怎么样」，放在
  `data/relations/` 的 JSON 里，会随聊天自动维护。

两部分都不写死在 `agent/prompts.py` 里，改角色或改某个人的资料不需要动代码。

---

## 1. 两层人设

| 文件 | 负责什么 | 换角色时 |
|---|---|---|
| `agent/personas/junjing.md` | 角色卡：身份、经历、性格底色、说话方式、和主人的关系底色 | 新建一份角色 Markdown |
| `agent/personas/core_qq.md` | 所有角色共用的群聊行为：先判断再回、短句、边界、人际关系规则 | 一般不动 |

配置在 `config/bot_config.json`：

```json
"agent": {
  "persona_file": "agent/personas/junjing.md",
  "persona_core_file": "agent/personas/core_qq.md"
}
```

- 相对路径按项目根目录解析，也可以写绝对路径。
- 文件缺失或为空时回退到 `agent/prompts.py` 的 `FALLBACK_PERSONA` / `FALLBACK_CORE`。
- 人设文件在 Agent 初始化时读取一次；改完需要重启 Bot。
- 输出格式、工具说明、系统动作、表情包目录仍然由 `agent/prompts.py` 负责。

`agent/prompts.py` 里的 `SYSTEM_PROMPT` 保留两个槽位：

```text
{persona}          <- junjing.md
{core_behavior}    <- core_qq.md
```

`build_system_prompt()` 用 `.replace()` 填入。**不要改成 `.format()`**：提示词里还有
`begin{...}` 这类字面量花括号。

---

## 2. 关系档案的数据结构

```text
data/relations/
├─ users/<QQ>.json      # 关系标签 / 人格画像 / 近期状态 / 重要记忆
├─ groups/<群号>.json   # 群简介 / 常聊话题 / 群梗 / 备注
└─ self.json            # 君景自己的心情 / 状态（默认 12 小时过期）
```

目录可以用 `relations.data_dir` 改；相对路径按项目根目录解析。

### `users/<QQ>.json`

| 字段 | 说明 |
|---|---|
| `qq` / `name` / `aliases` | QQ 号、主称呼、别名 |
| `labels` | 关系标签数组，`{text, at, source}`；如 `主人`、`朋友`、`群主` |
| `persona` | 人格画像，长期性格；变化很慢 |
| `relation` | 和君景的关系描述 |
| `description` | 其他补充描述 |
| `tags` / `boundaries` | 常聊话题 / 雷区 |
| `states` | 近期状态，`{text, at, expires_at, source}`，会自动过期 |
| `memories` | 重要记忆，`{text, at, importance, source}` |
| `group_notes` | 按群号覆盖的称呼 / 关系说明 |
| `stats` | `first_seen` / `last_seen` / `messages` / `last_reply_at`，自动维护 |

`source` 的取值：

- `manual`：人工改 JSON，或主模型调用 `update_person` 写入；
- `seed`：按 `master_qq` 自动补的「主人」标签；
- `auto`：低频自动整理（判断模型）写入。

`labels` 上限默认 8 个，`memories` 上限默认 30 条，二者都能在配置里改。

### 示例：主人

```json
{
  "qq": "10001",
  "name": "Pyrin Domain",
  "aliases": ["馆馆"],
  "labels": [{ "text": "主人", "at": "2026-10-26T00:00:00", "source": "seed" }],
  "persona": "很敏感，容易把话往心里去，需要被认真回应",
  "relation": "君景最重要的人；他说的话会让君景格外在意",
  "description": "世界上最聪明、最伟大、最不可思议的独角兽馆馆",
  "boundaries": ["不编造现实信息", "女装照要先经过他同意"],
  "states": [],
  "memories": [],
  "group_notes": {},
  "stats": {
    "first_seen": "",
    "last_seen": "",
    "messages": 0,
    "last_reply_at": ""
  }
}
```

第一次在上下文里见到 `master_qq` 时会自动补上「主人」标签；这个标签只补不覆盖，
自动整理也不能删掉人工 / seed 写入的标签。

### `groups/<群号>.json`

```json
{
  "group_id": "20009",
  "name": "测试群",
  "description": "晚上比较活跃的闲聊群",
  "topics": ["考试", "游戏"],
  "lore": ["把管理员叫「老大」"],
  "catchphrases": ["乐"],
  "notes": "严肃话题不要插科打诨",
  "common_users": ["10001", "20001"],
  "updated_at": "2026-10-26T00:00:00"
}
```

### `self.json`

```json
{
  "thread_id": "group:20009",
  "status": { "心情": "开心", "动作": "摇尾巴" },
  "at": "2026-10-26T12:00:00",
  "expires_at": "2026-10-27T00:00:00"
}
```

每轮模型输出的 `<status>` 会落到这里，下一轮以「我现在的状态」注入。默认 12 小时过期。

---

## 3. 三种维护方式

### 3.1 人工改 JSON

直接编辑 `data/relations/` 下的文件，重启 Bot 即可。适合：

- 给某个人写一段长期画像；
- 把群梗、雷区、口头禅整理进群档案；
- 修正模型记错的内容，或者手动删掉不该留的记忆。

人工写入的 `source` 保持 `manual`，自动整理不会覆盖或删除它。

### 3.2 主模型主动记：`update_person` / `update_group`

主模型在聊天里判断「这条信息以后还用得上」时调用：

```text
update_person(
  qq="20001",
  label_add="朋友",
  label_remove="",
  state="最近在准备考试",
  state_hours=72,
  memory="不喜欢被叫全名",
  importance=3,
  portrait="说话很直，但很照顾人",
  relation="认识很久的朋友",
)

update_group(topics="考试、复习", lore="把管理员叫老大", notes="晚上比较活跃")
```

边界写在工具说明和 `core_qq.md` 里：

- 只写对方**明确说过的稳定信息**（称呼、雷区、长期喜好、重要经历）；
- 临时情绪写 `state`，它会过期；
- 不记猜测、玩笑、反讽、隐私（住址、学校、真实姓名、恋爱关系、身体缺陷）；
- 没有要记的就不要调用。

工具做了长度和数量限制，模型塞整段聊天进来也只会留下短句。

### 3.3 低频自动整理

每 `relations.auto_every`（默认 8）条**当面消息**，且距离上次整理至少
`relations.auto_extract_min_seconds`（默认 600 秒），用判断模型跑一次：

```text
最近记录 + 已知人物 + 君景刚发的回复
        ↓  判断模型（只输出 JSON）
apply_extraction()
        ↓
labels / state / memory / relation / group 资料
```

规则：

- 只信任「本人明确说出」的信息，拿不准就跳过；
- 置信度低于 `0.6` 的条目直接丢弃；
- 只能更新本轮上下文里出现过的人（当前说话人 / 被 @ / 被引用 / 主人 / 最近历史 /
  群常出现的人），不会凭空新建陌生人；
- 自动写入的标签 `source=auto`，自动整理删除标签时只能删 `auto` 的，
  人工或 seed 的「主人」删不掉；
- 整理在线程里异步跑，失败只写 debug 日志，不影响本轮回复；
- 关掉 `relations.auto_extract` 就只剩人工维护和主模型主动记。

---

## 4. 每轮注入什么

`RelationStore.render(context)` 生成 `<relations>` 块，按以下顺序挑选：

1. 当前说话人；
2. 当前消息里被 @ 的人；
3. 被引用消息的发送者；
4. `master_qq`；
5. 最近历史里出现的人；
6. 当前群 `common_users` 里的人。

默认最多 `max_people=6` 人、总预算 `max_chars=1200` 字，每人最多
`max_person_chars=320` 字。只注入本轮相关的人，不会把整个通讯录塞进提示词。

单人渲染示例：

```text
- 小明(20001)：标签：朋友；画像：说话很直，但很照顾人；近期状态：最近在准备考试（2小时前）；重要记忆：不喜欢被叫全名（2026-10-20）
```

### 时间规则（重点）

- **近期状态一定过期**：默认 72 小时，最长 168 小时。渲染时带
  「12 分钟前 / 2 小时前 / 3 天前」；过期后不再注入。
- 几天前的难过 / 生气不能被当成现在的情绪；只有这一轮的消息能证明他现在的情绪。
- **重要记忆长期保留**：默认最多 30 条，按「重要度 + 时间」裁剪；
  只在相关时自然带出，不逐条复述。
- **关系标签相对稳定**：由人工或模型修改；自动整理只删自己加的标签。
- **君景自己的状态**：每轮落盘，默认 12 小时过期。

---

## 5. 配置

`config/bot_config.json` 的 `relations` 段（默认值见 `config/defaults.py`）：

```json
"relations": {
  "enable": true,
  "data_dir": "data/relations",
  "master_label": "主人",
  "max_people": 6,
  "max_chars": 1200,
  "max_person_chars": 320,
  "state_ttl_hours": 72,
  "state_max_hours": 168,
  "self_ttl_hours": 12,
  "max_states": 6,
  "max_memories": 30,
  "max_labels": 8,
  "auto_extract": true,
  "auto_every": 8,
  "auto_extract_min_seconds": 600
}
```

| 键 | 作用 |
|---|---|
| `enable` | 总开关；关掉后不读写关系档案，提示词里也没有 `<relations>` |
| `data_dir` | 档案目录；相对路径按项目根目录解析 |
| `master_label` | 第一次见到 `master_qq` 时自动补的标签 |
| `max_people` / `max_chars` / `max_person_chars` | 每轮注入的人数 / 总字数 / 单人字数预算 |
| `state_ttl_hours` | 近期状态默认保留多久 |
| `state_max_hours` | 状态最长保留多久（`state_hours` 会被截到这个值） |
| `self_ttl_hours` | 君景自己的状态保留多久 |
| `max_states` / `max_memories` / `max_labels` | 每人状态 / 记忆 / 标签上限 |
| `auto_extract` | 是否启用低频自动整理 |
| `auto_every` | 每多少条当面消息触发一次 |
| `auto_extract_min_seconds` | 两次自动整理的最小间隔（秒） |

---

## 6. 代码位置

| 文件 | 作用 |
|---|---|
| `agent/personas/*.md` | 两层人设 |
| `agent/prompts.py` | 系统提示词、人设槽位、自动整理 prompt |
| `agent/relations.py` | `RelationStore`：加载 / 保存 / 过期 / 渲染 / 自动整理写回 |
| `agent/tools/relations.py` | `update_person` / `update_group` 工具 |
| `agent/runtime.py` | 注入 `<relations>`、记录自身状态、调度自动整理 |
| `config/defaults.py` | `relations` 与 `agent.persona_file` 默认值 |
| `tests/test_relations.py` | 标签、过期、记忆裁剪、自动整理、工具、提示词槽位 |

---

## 7. 边界与排查

- 只记录对聊天有帮助的信息；不收集住址、学校、真实姓名、恋爱关系等隐私。
- 自动整理拿不准就跳过；模型猜测、玩笑、反讽都不写。
- 标签会变，以最新的为准；不要凭语气或自称认人。
- 关系档案坏了不会阻断聊天：`agent/runtime.py` 读取失败只写 warning。
- 某个人的档案没生效时，先确认：
  1. `relations.enable` 是否为 `true`；
  2. 文件是否在 `relations.data_dir` 下，文件名是否为纯 QQ 号；
  3. JSON 是否能解析（`python -m json.tool data/relations/users/<QQ>.json`）；
  4. 这个人是否在本轮上下文里出现（只注入相关的人）。
