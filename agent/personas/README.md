# 人设与关系档案

参考 `D:\Code\SuperMaGe\Config\Personas` + `Config/Profiles` 的分层，但把
「只读资料」升级成「会自己维护的关系档案」。

## 两层人设

| 文件 | 负责什么 | 换角色时 |
|---|---|---|
| `junjing.md` | 角色卡：身份、经历、性格底色、说话方式、和主人的关系底色 | 新建一份角色 Markdown |
| `core_qq.md` | 所有角色共用的群聊行为：先判断再回、短句、边界、人际关系规则 | 一般不动 |

配置在 `config/bot_config.json`：

```json
"agent": {
  "persona_file": "agent/personas/junjing.md",
  "persona_core_file": "agent/personas/core_qq.md"
}
```

- 相对路径按项目根目录解析，也可以写绝对路径。
- 文件缺失或为空时回退到 `agent/prompts.py` 的 `FALLBACK_PERSONA` / `FALLBACK_CORE`。
- 改文件后需要重启 Bot（Agent 初始化时读一次）。
- 输出格式、工具、系统动作、表情包目录仍由 `agent/prompts.py` 负责；人设文件只决定「我是谁、我怎么说话」。

## 关系档案（不是提示词）

每个人的**关系标签、人格画像、近期状态、重要记忆**不写在人设里，而在
`data/relations/`：

```text
data/relations/
├─ users/<QQ>.json      # 关系标签 / 人格画像 / 近期状态 / 重要记忆
├─ groups/<群号>.json   # 群简介 / 常聊话题 / 群梗 / 备注
└─ self.json            # 君景自己的心情 / 状态（12 小时过期）
```

维护方式有三种：

1. **人工**：直接改 JSON（改完重启 Bot）。
2. **模型主动记**：主模型在聊天里调用 `update_person` / `update_group`。
3. **自动整理**：每 `relations.auto_every` 条当面消息（且间隔 ≥
   `auto_extract_min_seconds`），用判断模型从最近记录里抽一次，只写本人明确说过的信息。

`<relations>` 每轮按「当前说话人 → 被 @ 的人 → 引用的人 → 主人 → 最近历史 →
群常出现的人」挑选，默认最多 6 人、总预算 1200 字。

## 时间规则（重点）

- **近期状态一定过期**：默认 72 小时，最多 168 小时。渲染时带「2 小时前 / 3 天前」，
  过期后不再注入。几天前的难过不能被当成现在的情绪。
- **重要记忆长期保留**：默认最多 30 条，按重要度 + 时间裁剪；只在相关时自然带出。
- **关系标签相对稳定**：由人工或模型修改；自动整理只能删除 `source=auto` 的标签，
  不会把人工写死的「主人」删掉。
- **君景自己的状态**：每轮 `<status>` 落盘，默认 12 小时过期，下轮作为「我现在的状态」注入。

## 字段速查（`users/<QQ>.json`）

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
| `group_notes` | 按群号覆盖的称呼/关系说明 |
| `stats` | `first_seen` / `last_seen` / `messages` / `last_reply_at`，自动维护 |

## 边界

- 只记录对聊天有帮助的信息；不收集住址、学校、真实姓名、恋爱关系等隐私。
- 自动整理拿不准就跳过；模型猜测、玩笑、反讽都不写。
- 标签会变，以最新的为准；不要凭语气或自称认人。
