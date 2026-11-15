# Agent 模块设计汇总（详细版）

> 汇总对象：`agent/` 包 + 它在整条消息链路里的位置。
> 侧重：**函数级索引**、**完整触发流程**、**hint 在什么情况下被注入**。
> 行号基于当前工作区；`agent/runtime.py` 约 1662 行。

---

## 目录

1. 定位与依赖
2. 模块地图（文件 → 关键函数）
3. 完整触发链（从 WebSocket 到一条回复）
   - 3.1 传输层
   - 3.2 pipeline 分流
   - 3.3 attention 三档
   - 3.4 AI 分支 `_handle_ai`
   - 3.5 上下文组装
4. `QQBotAgent` 构造
5. `chat()` 逐步骤（重点）
6. 输出协议与解析函数
7. **hint 体系（重点）**
8. 工具详解与触发条件
9. 模型路由与降级
10. 三层记忆
11. 关系档案
12. 主动插话（完整触发链）
13. 心理分析（完整触发链）
14. 日志
15. 配置速查
16. 设计取舍
17. 已知边界

---

## 1. 定位与依赖

`agent/` 是项目里**唯一懂 LLM 的层**。它不懂连接（不碰 WebSocket/OneBot），不管消息数据（`ChatContext` 由 `messaging/` 造好传进来）。

```text
core ← config ← messaging
              ↑
            agent ← bot（唯一同时认识所有子系统的层）
```

实际 import 面（全量 grep 结果）：

| 来源 | 被谁用 |
|---|---|
| `core.jsonstore` | `agent/relations.py` |
| `core.logging` | `agent/psychology.py` |
| `config.paths` | `agent/logger.py`、`agent/tools/methods.py` |
| `messaging.models` | `agent/output.py`（类型）、`agent/hints.py`、`agent/tools/history_bulk.py` |
| `messaging.meme` | `agent/output.py`、`agent/runtime.py` |
| `messaging.forward` | `agent/tools/forward.py` |
| `AbstractionLayer` | `agent/tools/forward.py` |

**不出现** `bot` / `transport` / `web` / `images` / `media` 的 import —— 靠构造参数注入。

对外入口 `agent/__init__.py`：

```python
QQBotAgent, CampusAssistant, LLMCallbackHandler, MODEL_TYPE,
parse_agent_output, split_sendable_messages
```

---

## 2. 模块地图

### 2.1 顶层

| 文件 | 大小 | 关键函数/类 |
|---|---|---|
| `runtime.py` | 69KB | `QQBotAgent`、`LLMCallbackHandler`、`_render_vault_body`、`_cached_tokens` |
| `prompts.py` | 49KB | `SYSTEM_PROMPT`、`build_system_prompt`、`build_persona_parts`、`persona_voice_excerpt`、`interject_hint`、`meme_catalog_lines`、`catchphrase_catalog_lines`、各 `_*_HINT` 常量 |
| `output.py` | 24KB | `parse_agent_output`、`split_sendable_messages`、`render_message_body`、`normalize_next`、`normalize_thread`、`looks_like_repeat`、`pick_scene_line`、`looks_like_dodge`、`parse_interject_verdict`、`InterjectPlan`、`has_side_effect_call` |
| `hints.py` | 23KB | `build_action_hint`、`build_meme_hint`、`wants_followup`、`_wants_dress_photo`、`_dress_is_just_talk`、`_wants_bulk_history`、`_normalize_image_source`、`_urls_for_source`、`_forward_candidates` |
| `relations.py` | 48KB | `RelationStore`（20+ 方法，见 11 节） |
| `psychology.py` | 36KB | `PsychologyAnalyzer`、`PsychMaterial`、`PsychImage`、`PsychEntry`、`NodeDraft` |
| `config.py` | 5.7KB | `LLM_CONFIG`、`resolve_llm_config`、`resolve_model_entry`、`model_label`、`find_model_entry` |
| `router.py` | 8.2KB | `ModelRouter`、`ModelRoute`、`is_rate_limit_error`、`is_quota_error` |
| `logger.py` | 4.3KB | `append_log`、`build_log_entry`、`configure_log_dir`、`reset_log_dir` |
| `thread_turns.py` | 5.3KB | `ThreadTurns`、`Turn` |
| `thread_verdict.py` | 3.7KB | `resolve_verdict`、`VERDICT_KEEP`、`VERDICT_NEW` |
| `result_vault.py` | 6.9KB | `ResultVault`、`StoredResult`、`thread_key` |
| `catchphrase.py` | 9.2KB | `Catchphrase`、`load_catchphrases`、`parse_catchphrases`、`catchphrase_hint`、`catalog_lines`、`render_catalog` |
| `chat.py`/`campus.py` | 13KB | `CampusAssistant` |

### 2.2 `agent/tools/`

| 文件 | 构建函数 | 工具 |
|---|---|---|
| `media.py` | `build_media_tools` | `ocr_img` |
| `forward.py` | `build_forward_tools` | `read_forward` |
| `chat.py` | `build_chat_tools` | `send_message`、`send_dress`、`silent_observe` |
| `history.py` | `build_history_tools` | `get_msg_history`、`get_message` |
| `history_bulk.py` | `build_history_bulk_tools` | `fetch_history`、`digest_user`、`recall_result` |
| `methods.py` | `build_method_tools` | `add_Method`、`delete_Method`、`alter_Method` + `MethodLibrary` |
| `moderation.py` | `build_moderation_tools` | `poke`、`emoji_like`、`set_ban`、`recall_msg` |
| `relations.py` | `build_relation_tools` | `update_person`、`update_group` |
| `registry.py` | — | `tool` 装饰器、5 个 contextvars、`record_tool_call`、`current_*`、`api_of`/`messaging_of`/`media_of`/`settings_of` |
| `__init__.py` | `build_tools(agent, groups=None)` | 组装 + 按 `TOOL_ORDER` 排序 |

---

## 3. 完整触发链

### 3.1 传输层（为什么 Agent 不会被慢调用拖死）

```
SnowLuma/NapCat ──ws──▶ transport/connection.py（reader 循环）
        │
        ▼
transport/listener.py:66  _event_handler(event)
        │  if event["post_type"] != "message": return      ← 心跳 / notice / message_sent 全部丢弃
        ▼
transport/event_dispatcher.py:24  EventDispatcher.submit(event)
        │  ├─ 按会话（group_x / private_x）分队列 → 同会话串行，不同会话并行
        │  ├─ max_concurrency=8，max_pending=256（超了丢弃 + 只告警一次）
        │  └─ 每个会话一个 worker Task
        ▼
bot/app.py:243  BotApp.process_message(event, is_recover=False)
        ▼
bot/pipeline.py:49  MessagePipeline.process_message(event, is_recover)
```

要点：

- `post_type == "message_sent"`（SnowLuma 推送自己发的消息）在 listener 就被丢掉 → 不存在「自己回复自己」。
- worker 是 `asyncio.Task`，所以**同一群的多条消息不会并发打乱上下文**；这正是 `agent` 里用 `contextvars` 而不是实例属性存轮次状态的前提。

### 3.2 pipeline 分流（`bot/pipeline.py:49`）

顺序固定，改顺序会导致双发（见文件头注释）：

```text
1. attention = self._attention(event)                  # 算三档注意力
2. 群消息：mp.check_point[gid] = event                   # 立即记断点
   └─ 非恢复：interject.note_message(event, attention)   # 插话计数
3. 非恢复：mp.remember_thread_message(event, master_ids)  # 临时历史
4. _emoji_function(event)                                # 彩蛋：收集表情包信息
5. auto_forward.forward(...) 命中 → return               # 搬运优先，不进 AI
6. ated_user = mp.is_AT(event, [self_id])
7. replying_to_bot = 非@ 且 非恢复 且 thread_is_hot 且 replies_to_bot
8. summoned = attention.is_summon 且群聊
9. if (ated_user or replying_to_bot or summoned) and not is_recover:
       → _handle_ai(...)   ; return
10. else → _match_keywords(event, is_recover)
```

三个进 AI 的条件，语义完全不同：

| 条件 | 含义 | 判定函数 |
|---|---|---|
| `ated_user` | 消息里有 `at` 段指向 Bot | `MessageProcessor.is_AT` |
| `replying_to_bot` | **敏感期内**（Bot 最近说过话）引用了 Bot 的消息 | `thread_is_hot()` + `replies_to_bot()` |
| `summoned` | 没 @ 但**直呼别名在叫它**（「君景在吗」「君景你觉得呢」） | `messaging/attention.py` |

`replying_to_bot` 是「不需要 @ 也算对话」：Bot 刚在群里说过话（默认 10 分钟 `interject.hot_minutes`），对方引用它的话 → 直接进正式回复。

### 3.3 attention 三档（`messaging/attention.py:148`）

`detect_attention` 是纯字符串判断，不读配置、不发包、不碰时间：

| 等级 | 触发条件（按判定顺序） |
|---|---|
| `SUMMON` | 私聊 / `mentions_me`（有 at 段指向自己）/ `replies_to_me` / 文本含别名且 **去掉别名后剩余 ≤2 字**（「君景呢」「君景！」）/ 含别名且剩余以呼唤词开头（`在吗` `在不在` `出来` `帮我` `你觉得` `你` `怎么` `是不是` …） |
| `ABOUT_ME` | 含别名但不在叫它（「君景好可爱」）/ 含 `机器人` `bot` `自动回复` `是不是人` `ai吧` 等 |
| `AMBIENT` | 其它（空文本也是） |

别名解析 `_aliases()`：默认 `("君景", "独角兽")` + `interject.aliases`，按长度倒序（避免「君景」抢掉「君景酱」）。

用途：

- `summon` → pipeline 走正式 AI 回复（3.2 第 9 步）；**且插话计数跳过**（这条由正式回复接走）。
- `about_me` / `ambient` → 只累计到插话计数，由 `InterjectService` 决定要不要插话。

### 3.4 AI 分支（`bot/pipeline.py:120 _handle_ai`）

```text
clean_text = mp.get_clean_context(event, True)        # 去掉 @ 段等
  ├─ history_command_regex 命中 → history_solution()  （保留旧行为，方法未实现只告警）
  ▼
thread_id = group_<gid> | private_<uid>
context = await mp.build_chat_context(
    event, history=None, history_count=10,
    note=settings.prompt("group_context"|"private_context"),
    note_values={group_id,user_id,message_id,ated_user,self_id,master_id},
    master_ids=settings.master_qq,
    aliases=settings.get("interject.aliases"))
  ▼
agent = await self._get_agent()      # await BotApp.get_agent()
  ├─ None（AI 关闭 / 还没加载好）→ 退回 _match_keywords()，return
  ▼
reply = await agent.chat(event=event, context=context, thread_id=thread_id)
  ▼
群聊：interject.note_bot_spoke(gid)   # 计数清零 + 进入冷却
  ▼
reply 归一成 list[str]
  ├─ 空 → return
  ├─ "__NETWORK__" in reply → 发「校园网升级」+ 转交 CampusAssistant（带 chat 的历史）
  ├─ 全是 "__SILENT__" → return
  └─ 逐条 mp.send_message + mp.remember_sent_message(hot_seconds)
        · 跳过空串和含「已成功发」的条目（工具已发过消息时的回执）
```

`__SILENT__` 是 `silent_observe` 工具的返回值，`send_dress`/`send_message` 成功时 chat() 返回空列表。

### 3.5 上下文组装（`messaging/context_builder.py:167`）

`build_chat_context(mp, event, history_count=10, ...)`：

1. `collect_history()` 按后端游标拉最近 N 条（`reverse_order=True`），自带 `message_seq` + `message_id`；
2. `thread_history_store().snapshot(thread_id)` 补更早的缓存 → `merge_history` 去重排序；
3. **剔除当前消息**（历史接口是「包含当前这条」的）；
4. `collect_message()` 解析当前消息：引用只留一行摘要 + 图片源；转发当前消息展开、历史里只留【转发 N 条】；图片留 `[图片]`、表情留 `[表情:名字]`；
5. 预算：历史 12 条 / 2000 字、单条 300、当前 800；超了标注 `omitted`；
6. `messaging/dialogue.py` 算每行「→谁」，群聊且别人说了 ≥2 条时补 `<dialogue>`；
7. 回写 `thread_history`。

`ChatContext` 提供的接口（Agent 侧用到）：`render()`、`render_increment(since=)`、`render_as_bystander()`、`render_history_only()`、`summary()`、`image_sources()`、`forward_candidates()`、`current`、`history`、`group_id`、`self_id`、`master_ids`、`directed_at_me`、`replies_to_me`、`max_history_messages`、`max_history_chars`、`interest_hits`。

---

## 4. `QQBotAgent` 构造（`agent/runtime.py:234`）

```python
QQBotAgent(
    *, api=None, messaging=None, media=None, images=None,
    settings=None, llm_config=None,
    napcat_api=None, mp=None, extension=None,      # 旧参数名别名
)
```

逐项：

| 步骤 | 代码 | 说明 |
|---|---|---|
| 1 | `self.api/napcat_api`、`self.messaging/mp`、`self.media/extension` | 新旧名互相赋值，两边都能读 |
| 2 | `MethodLibrary(settings)` → `self.rw_tool`、`self._methods` | `data/methods.json`；首启只读回退旧 `Agent/history/method.json` |
| 3 | `RelationStore.from_settings(settings)` | 没 settings = 禁用态（测试友好） |
| 4 | `ResultVault()` | 工具大结果跨轮暂存 |
| 5 | 读 `thread_memory` 段 | `stable_prefix`、`verdict`、`new_after_minutes`、`groups`、`ThreadTurns(ttl/max_turns/max_chars)` |
| 6 | `merged_config = settings.agent_llm_config + settings.agent_model_routing + llm_config` → `resolve_llm_config` | 后面的覆盖前面的 |
| 7 | `self.callbacks = [LLMCallbackHandler()]`；`self.tools = self._build_tools()` | 默认 19 个 |
| 8 | `self.catchphrases = load_catchphrases_for_settings()`；`self.system_prompt = SystemMessage(_build_system_prompt())` | 表情库 + 人设文件只在此时读一次 |
| 9 | `self._setup_model_routes(merged_config)` | 建 `reply_router` / `judge_router` |
| 10 | `self._recent_replies={}`、`self._relation_tasks=set()`、`self._closed=False` | 去复读 / 关系任务 |

`_build_system_prompt()`（:344）：读 `meme` 段 → `configure_memes(path, enable)` → `build_system_prompt(meme_limit, settings)`。
表情库配错只 warning，不影响启动（空库时提示词写「这一轮不要写 `<meme>`」）。

---

## 5. `chat()` 逐步骤（`agent/runtime.py:1088`）

```python
async def chat(
    user_message: str = "", thread_id: str = "default",
    extra_context: Optional[str] = None, event: dict = None,
    context: Optional[Any] = None, hint: str = "", action_check: bool = True,
) -> Optional[list[str]]
```

### 步骤 1：取本轮用户文本

```python
if context is not None:
    context_text = context.render()
    user_text = context.current.text or context.current.miniapp
    is_dom = context.current.sender.is_master
else:
    user_text = user_message
    is_dom = user_text.startswith("{isDom:true}")     # _DOM_PREFIX
```

### 步骤 2：每轮隔离轮次状态

```python
_current_tool_calls_var.set([])      # 本轮副作用记录
_current_tool_cache_var.set({})      # 本轮工具结果缓存（同一 OCR 不重复识别）
_current_user_text_var.set(user_text) # 供 send_dress 判「只是在聊女装」
_current_context_var.set(context)
_current_images_var.set(context.image_sources() 或 {"current": _image_urls(event), ...})
```

为什么用 `contextvars` 而不是实例属性：多条消息并发跑 `chat()` 时互不污染。
为什么默认值必须是**可变容器**：langchain 的 `BaseTool.arun` 在复制出来的 context 里跑工具，工具里 `ContextVar.set()` 传不回调用方，只能原地改已存在的对象。

### 步骤 3：关系档案注入

```python
relations.observe_context(context)            # 记录见到的人 / 群
relation_text = relations.render(context)     # 渲染成提示段
```
失败只 warning（关系档案坏了不能阻断聊天）。

### 步骤 4：拼装 `parts`（**顺序决定提示词结构，也决定缓存命中**）

```text
parts = [
    context_text,                        # ① <scene>/<dialogue>/<history>/<current>
    relation_text,                       # ② 关系档案（若有）
    "<method>…</method>",                # ③ 方法库 structed_method()
    user_text,                           # ④ 仅当没有 context_text 时
    <结果留存：正文 或 索引>,               # ⑤ 见步骤 5
    "[本轮动作检查]\n…",                   # ⑥ build_action_hint + build_meme_hint
    "[本轮口头禅]\n…",                     # ⑦ catchphrase_hint
    hint 或 _REPLY_CHAIN_HINT,            # ⑧ 外部指定 或 对方回复了我
]
full_text = "\n".join(parts)
```

### 步骤 5：结果留存（`ResultVault`）

```python
keys = [thread_id] + [thread_key(context)]        # 两个 key 都试
entry = vault.get(key)                            # 30 分钟 TTL，每会话最多 4 条
if entry:
    if wants_followup(user_text):                 # 「挑几句 / 展开 / 具体说说 / 继续」
        parts.append(_render_vault_body(entry))   # 正文直接铺 6000 字，保证模型一定看到
    else:
        parts.append(vault.render_hint(...))      # 只给两三行索引，取不取由模型 recall_result 决定
```

### 步骤 6：动作检查 hint（详见第 7 节）

```python
if action_check:                                  # 插话时传 False，跳过
    action_hints += [build_action_hint(user_text, event, is_dom, context)]
    action_hints += [build_meme_hint(user_text)]
parts.append(f"[本轮动作检查]\n" + "\n".join(action_hints))
```

### 步骤 7：口头禅 hint

```python
catch_hint = catchphrase_hint(user_text, self.catchphrases)
```

### 步骤 8：多轮留存拼装

```python
memory = self._thread_memory(thread_id)   # 未开启 / 不在灰度群 → None
turns = memory.get(thread_id) if memory else []

if turns:
    incremental = context.render_increment(since=turns[-1].token)   # 只给增量
    messages = _build_turn_messages(turns, incremental)             # H1,A1,H2,A2,…,Hn
else:
    messages = [HumanMessage(full_text)]                            # 单条完整上下文
```

`turn_token` = 当前消息 `message_id`（并发下用它给留存去重，不能用文本 —— 连发两句「好」会被误判重复）。

### 步骤 9：调模型

```python
async def _run_round(convo):
    before = len(calls)
    result = await self._invoke_agent({"messages": convo})
    text = self._extract_ai_response(result)
    parsed = parse_agent_output(text)
    out = split_sendable_messages(parsed)
    if 本轮调过 send_message: out = []          # 工具已发过，别再发一遍
    return {result, text, parsed, out, calls: new_calls}
```

`_invoke_agent`（:517）→ `reply_router.ainvoke(payload, use_agent=True, can_fallback=…)`；
`can_fallback()` = `not self._has_side_effect_call()`（本轮没执行过 `send_message`/`send_dress`/`poke`/`emoji_like`/`set_ban`/`recall_msg`）。

### 步骤 10：`<thread>` 裁决

```python
if memory is None: verdict, src = "", "off"
else: verdict, src = self._verdict_of(parsed, turns=turns, user_text=user_text)
```

**下一轮生效**：模型说「新话题」时，这一轮已经带着旧 turns 生成完了。

### 步骤 11：静默 / 送达判定

```python
silenced  = 调过 silent_observe
delivered = 调过 send_message 或 send_dress          # _ANSWER_TOOLS

if silenced and (messages_out or delivered):        # 自相矛盾
    warning「同时调用了 silent_observe 和回复，按回复处理」; silenced = False

must_reply = context.directed_at_me
if must_reply and not messages_out and not delivered:
    retry_text = await self._force_reply(messages)  # 追加 _FORCE_REPLY_HINT 再来一次
    # 成功则用 retry 的结果覆盖 parsed / messages_out / silenced

if silenced and not messages_out and not delivered:
    _drop_turns_for_new_topic(...)                  # 没接话也要认裁决
    append_log(error="静默观察"); return None
```

`_force_reply`（:1033）：取**最后一条** HumanMessage（不是 `messages[0]`，多轮后会答非所问），追加 `_FORCE_REPLY_HINT` 再 `_invoke_agent`。

### 步骤 12：续说循环

```python
while messages_out and extra < 2 and len(messages_out) < 6:
    wants_next = parsed["next"] == "continue"
    dodge = extra == 0 and not wants_next and _looks_like_dodge(messages_out)
    if not (wants_next or dodge): break
    extra += 1
    convo += [AIMessage(上一轮原文), HumanMessage(_DODGE_CONTINUE_HINT 或 _CONTINUE_HINT)]
    nxt = await _run_round(convo)
    if not nxt["out"]: break
    messages_out += nxt["out"]
messages_out = messages_out[:6]
```

`_looks_like_dodge`：整批回复去掉标点后 ≤16 字且命中 `在说话啦|人家在呢|马上说|这就说|让我想想|等我一下|别急嘛` → 判定「敷衍」，逼它正面回答。

### 步骤 13：记留存

```python
self._remember_turn(memory, thread_id,
    turns_before=turns, full_text=full_text, incremental=incremental,
    ai_text=state["text"], continued=extra>0, replied=bool(messages_out),
    token=turn_token, verdict=verdict if extra==0 else "")
```

规则（`_remember_turn` :581）：

| 情况 | 动作 |
|---|---|
| 续说过 | `memory.reset()` —— 回放序列对不上 |
| 没回话 / 空文本 | 只认裁决（新话题就清空），不记 |
| `verdict == new` 且本来有 turns | `memory.start(完整上下文)` 重开一段 |
| 本来有 turns | `memory.append(增量)`，到上限则 reset |
| 本来没 turns | `memory.start(完整上下文)` |

### 步骤 14：去复读

```python
messages_out = await self._de_repeat(thread_id, messages_out, user_message=user_text, is_dom=is_dom)
```

`_de_repeat`（:671）→ `looks_like_repeat` 判定（完全一致 / 互相包含 / `SequenceMatcher ≥ 0.8`）：
先 `pick_scene_line()` 从高频场景台词池拿一条没用过的（男娘 / 女装照主人 / 女装照非主人，按 `is_dom` 分池）；
池里没有再 `_rephrase()` 调小模型改写（带 `persona_voice_excerpt` 人设语音，避免改出通用腔；>60 字或仍雷同则放弃）。

### 步骤 15：收尾

```python
_remember_replies(thread_id, messages_out)          # 最近 8 条，用于下次去复读
relations.record_self_status(parsed["status"])      # <status> 只进档案，不发群
relations.record_reply(context)
_schedule_relation_extract(context, messages_out)   # 低频异步，不阻塞
append_log(build_log_entry(...))
return messages_out
```

异常路径：任何异常 → `error` 日志 + `append_log(error=...)` + `return None`，**不抛出**给 pipeline。

---

## 6. 输出协议与解析函数

### 6.1 协议

```text
<status>
心情: … / 状态: … / 记忆: … / 动作: …
</status>
<think>…</think>
<thread>保留|新话题</thread>
<output>
  <message>消息1</message>
  <message>消息2</message>
  <next>继续|结束</next>
</output>
```

### 6.2 `parse_agent_output(text)`（`agent/output.py:154`）

返回 `{"status","think","next","messages","thread","raw"}`。

| 步骤 | 细节 |
|---|---|
| 1 | `_THREAD_RE` 先摘 `<thread>`，**必须最先**（它常写在 `<output>` 外，留着会被当正文发出去） |
| 2 | `_BLOCK_RE` 抓 `status/think/next/output` 四块 |
| 3 | `_NEXT_RE` 单独再抓一次 `<next>`（它嵌在 `<output>` 里会被上一步一并匹配掉） |
| 4 | 没有 `<output>` 块 → 把除上述块以外的正文当一条消息（容错） |
| 5 | `_MESSAGE_RE` 抓每条 `<message attrs>`；`quote="ID"` 属性 → 前置 `[CQ:reply,id=…]` |
| 6 | 每条走 `render_message_body()` 转 CQ 码 |
| 7 | 游离在 `</message>` 外的 `<meme>` 补到最后一条（否则这一轮白写） |

### 6.3 渲染与归一

| 函数 | 输入 → 输出 |
|---|---|
| `render_message_body` (:101) | `<at>QQ</at>`→`[CQ:at,qq=…]`；`<sticker>x</sticker>`→`[CQ:image,file=…]`；`<meme>名</meme>`→表情包 CQ 码（`[CQ:mface]`/`[CQ:face]`/自定义）；`<voice>` 去壳；最后清游离标签 |
| `normalize_next` (:114) | `continue/yes/…` → `"continue"`，其余（含漏写）→ `"end"` |
| `normalize_thread` (:134) | `keep/same/继续/保留/1` → `"keep"`；`new/drop/新话题/清空/0` → `"new"`；认不出 → `""`（交给代码兜底） |
| `split_sendable_messages` (:233) | 过滤空串 → `list[str]` |
| `looks_like_repeat` (:296) | 与 `TEMPLATE_LINES`（从 `SYSTEM_PROMPT` 自动抽的示例台词）或最近 8 条比相似度 |
| `pick_scene_line` (:362) | 匹配 `男娘` / 女装照（分主人/非主人）场景池，优先没说过的一句 |
| `looks_like_dodge` (:386) | 见 5.12 |
| `has_side_effect_call` (:587) | 本轮是否执行过有副作用工具 |

---

## 7. hint 体系（重点）

hint 分四层，注入点在 `chat()` 的 `parts` 里，**顺序即优先级**：

| 层 | 生成函数 | 注入前缀 | 什么时候注入 | 谁能关 |
|---|---|---|---|---|
| ① 动作检查 | `build_action_hint` | `[本轮动作检查]` | 命中动作意图正则 | `action_check=False`（插话） |
| ② 表情包 | `build_meme_hint` | 同上（拼接） | 情绪明显 / 点名要表情 | 同上 + `meme.enable=false` |
| ③ 口头禅 | `catchphrase_hint` | `[本轮口头禅]` | 消息命中 describe 里的引号例子 | `catchphrase_file=false` |
| ④ 场景硬指令 | `_CONTINUE_HINT` / `_DODGE_CONTINUE_HINT` / `_FORCE_REPLY_HINT` / `_REPLY_CHAIN_HINT` / `interject_hint(act)` | 无前缀或 `<continue>` 包 | 由代码判定分支时追加 | 无 |

另外 `hint` 参数（`chat(hint=...)`）由调用方直接插入，优先级等同 ④。

### 7.1 `build_action_hint`（`agent/hints.py:348`）

签名：

```python
build_action_hint(user_message, event=None, is_dom=False, context=None) -> str
```

返回空串表示**不注入**（普通闲聊不注入，省 token 也避免误触发工具）。命中后返回 `- xxx` 每行一条。

7 条规则、触发条件与注入文本：

#### 规则 1：女装照（明确要图）

触发：`_wants_dress_photo(text)` —— 必须同时满足

1. `_DRESS_ASK` 命中：`发|发张|来一张|整|拍|晒|看看|瞅瞅|康康|欣赏|让我看` 等动词 + `女装|腿照|白丝|黑丝|裙子照|裙子|腿`（动词在前或名词在前两种）；
2. **不是否定**：`_DRESS_NEGATION`（`不/别/没/甭/勿/无需/不用` + 0~3 字 + 名词）未命中；
3. **不是提问/感想**：`_DRESS_QUESTION`（`为什么|为啥|怎么|是不是|羡慕|觉得|评价|关于`）未命中。

| 条件 | 注入 |
|---|---|
| `is_dom` 且有群号/发送者 | 主人要女装照 → 必须**先**调 `send_dress(group_id=…)` 或 `send_dress(message_type="private", user_id=…)`，再输出 `<status>/<think>/<output>`；只回文字属失职 |
| 非主人 | **不要**调 `send_dress`，用 `<message>` 俏皮拒绝 |

#### 规则 1.5：笼统要照片

触发：`_wants_dress_photo` 不成立，但文本含 `发张|发一张|来一张|来个图|发个图|看看照片` 且有群号/发送者。
（君景唯一图源就是女装照，所以笼统要图也指到 `send_dress`，同样分主人 / 非主人两套文案。）

#### 规则 2：戳一戳

触发：`戳一戳|戳一下|戳戳|戳我|poke`（忽略大小写）。
目标：优先 `_at_targets(event)` 的第一个，否则发送者。
注入：`poke(user_id=…, group_id=…)`（群聊）/ `poke(user_id=…)`（私聊），要求**先调用**再输出。

#### 规则 3：撤回

触发：`撤回|撤了|撤一下|删掉那条|把上一条`。
目标：`_replied_message_id(event)`（消息里的 `reply` 段 id）或占位符。
注入：`recall_msg(message_id=…)`，要求**先调用**。

#### 规则 4：禁言（反向约束）

触发：`禁言|关小黑屋`。
注入：**严禁**真的调用 `set_ban`（这几乎一定是玩笑），改用语言吐槽 / `emoji_like` / `poke`。
这条是 hint 体系里唯一的「禁止调用」型提示。

#### 规则 5：图片文字识别

触发（三个条件同时成立）：

1. 有图可读：`current_urls` 或 `quote_urls` 或 `wants_history` 或 `wants_forward_ocr`；
2. 文本有 OCR 意图：`写了什么|什么字|识别|这图|图片|图上|截图|ocr`；
3. 其中：
   - `current_urls` = `context.image_sources()["current"]`（或 event 里的 image 段）
   - `quote_urls` = 被引用消息的图
   - `wants_history` = 历史里有图 **且** 文本含 `上一条|上一张|前面那|之前那|刚才那|那个图|历史…图`（只有回指才去翻历史，避免普通提「图片」就 OCR 十楼前的图）
   - `wants_forward_ocr` = 有转发卡片 **且** 当前/引用都没图 **且** `_FORWARD_OCR_INTENT` 命中

注入：按来源拼调用串，例如
`ocr_img()` / `ocr_img(index=0)（当前消息共 N 张，依次用 index=0..N-1）` /
`ocr_img(source="quote")` / `ocr_img(source="history")（默认取历史上最近一张带图的消息）` /
`read_forward(message_id="…") 展开卡片，再 ocr_img(source="forward", index=0)`，要求**先调用**。

#### 规则 6：解析转发卡片 / 查历史（二选一，互斥）

```python
if forward_card and _FORWARD_INTENT.search(text):     # 有卡片 + 在说「聊天记录/转发」
    → read_forward(message_id="…")，卡里有图再 ocr_img(source="forward")
elif 命中历史词 或 (要很多条 且 提到条/消息/聊天):
    ├─ 要很多条（_wants_bulk_history）且有群号
    │    → fetch_history(group_id=…, count=目标条数)   「不要用 get_msg_history 一轮一轮翻」
    └─ 否则有群号
         → get_msg_history(group_id=…, message_id=当前, count=20)
```

历史词：`历史|刚才说|之前说|上一条|前面说|记录|统计`。
`_wants_bulk_history`：`[几上]百|[几上]千|上千|中文数字+百千|全部|所有|整个群`，或文本里任何 ≥100 的整数。

为什么互斥：上下文里明明有卡片，「总结一下这个聊天记录」会同时命中历史正则，两条提示一起给小模型会把它带偏（去翻群历史然后说「没有记录」）。

#### 规则 7：承接上一轮

触发：`wants_followup(text)` —— `挑(几|出|一下|点|…)|展开(说|讲|看看)?|具体(说|讲|点)|详细(说|点)|再说说|接着(说|讲)|继续(说|讲)|给我看|发我看看`。

注入：两种处理

- 正文已经带上「上一轮的结果原文 · R… · 已经自动带上」→ 直接用那段回答；
- 只看到「[可复用结果]」索引 → **先** `recall_result(result_id="<编号>")` 取回；
两种都别重新翻历史、也别凭印象编。

### 7.2 `build_meme_hint`（`agent/hints.py:310`）

前置：`messaging.meme.is_enabled()` 且库非空，否则返回 `""`（表情包坏了不能连累主流程）。

| 触发 | 注入 |
|---|---|
| `_MEME_REQUEST_RE`：`发/来/整/给/要/看看/来个/发个/发张` + ≤8 字 + `表情包/表情/贴纸/meme` | 「必须真的发一个，用 `<message>短句<meme>名字</meme></message>`，名字从目录照抄，不要只回文字，也不要用 `[表情:名字]`」 |
| 命中 `_MEME_EMOTIONS` 里任一情绪正则 | 「情绪明显（<标签>），带一个贴切表情包；可选：<库里真实存在的名字，最多 4 个>」 |

12 组情绪正则（按顺序首个命中即返回）：

| 标签 | 触发词 | 优先候选 |
|---|---|---|
| 撒娇 | 撒娇/贴贴/抱我/要抱/抱抱/亲亲/摸摸头/蹭蹭 | 抱抱、要抱、亲亲、摸头、嘬嘬 |
| 安慰 | 呜呜/哭/难过/委屈/心疼/安慰/好累/辛苦了/难受/不开心/低落/摸摸 | 摸头、抱抱、抱哭、亲亲、要抱、妈咪抱抱 |
| 开心 | 哈哈/笑死/好笑/好耶/太好了/开心/高兴/快乐/乐死/绷不住 | 笑哭、赞、吐舌、可爱打钱、嘬嘬 |
| 无语 | 无语/离谱/逆天/服了/啊这/汗/太离谱/什么鬼 | 逆天、问号、捂脸、6w9、对的不对 |
| 震惊 | 震惊/卧槽/真的假的/不会吧/吓/惊了/天哪/我去 | 震惊、问号、逆天、哇哇哇哇 |
| 害羞 | 害羞/脸红/别说了/讨厌/不好意思/羞涩/捂脸/羞/骗人/撒谎/才没有/被发现 | 捂脸、吐舌、亲亲、嘬嘬 |
| 疑惑 | 是不是/对不对/好不好/行不行/真的吗/你说呢 | 问号、对的不对、6w9、智慧 |
| 生气 | 生气/哼/不理你/烦/气死/可恶 | 不乖、急了、坏东西、给爷爬、对的不对 |
| 得意 | 厉害/好棒/夸/得意/骄傲/聪明/牛/强 | 聪明小马、赞、顶你、智慧 |
| 干饭 | 饿/吃/干饭/饭/好吃/馋/奶茶 | 吃吃吃、先吃饭、冰淇淋、喝奶茶、美味角马 |
| 游戏 | 游戏/开黑/上号/来不来/带我/打游戏/吃鸡/王者/联盟 | 我来c、小马出击、吃兵、辅助宣言 |
| 摸鱼 | 摸鱼/摆烂/不想动/困/睡/躺/懒 | 似了、没我事、摸肚子、喝晕晕 |

`_available_meme_names(candidates, keywords)`：先按名字精确查库，不够再按关键词 `library.search()` 补；库里一个都没有 → 返回 `""`（不注入不存在的名字）。

### 7.3 `catchphrase_hint`（`agent/catchphrase.py:199`）

触发：把用户消息去 CQ 码去空白后，与每条口头禅 `triggers`（describes 里「」引号内的例子）做三种匹配：

| 匹配 | 得分 |
|---|---|
| 完全相等 | 1000 + 长度 |
| trigger 是消息的子串 | 500 + 长度 |
| 消息是 trigger 的子串 | 300 + 长度 |

取最高分的一条注入：

```text
- 当前消息命中了口头禅「<正文>」的触发例子「<例子>」：<describe>
  情境确实吻合时优先用这条口头禅回应，不要用「在呢」「怎么啦」之类的普通应答盖过去；不合适就正常说。
```

### 7.4 场景硬指令（无前缀，追加在最后）

| 常量 | 何时追加 |
|---|---|
| `_CONTINUE_HINT` | `<next>继续</next>` → 续说循环里 |
| `_DODGE_CONTINUE_HINT` | 首轮 `looks_like_dodge` 成立 → 逼正面回答（1~2 条，5~25 字） |
| `_FORCE_REPLY_HINT` | `context.directed_at_me` 却沉默 → `_force_reply()` 再来一次 |
| `_REPLY_CHAIN_HINT` | `context.replies_to_me` 且没有外部 `hint`（对方在回复我上一条） |
| `interject_hint(act)` | 插话判断出 `join/answer/revive` 后传给 `chat(hint=...)` |
| `_REPHRASE_SYSTEM` + 人设语音 | `_rephrase()` 改写时作为 SystemMessage |
| `_RELATION_EXTRACT_SYSTEM` | 关系自动整理时作为 SystemMessage |
| `_INTERJECT_JUDGE_SYSTEM` | `maybe_interject` 判断时作为 SystemMessage |

### 7.5 注入后的效果示例

「君景在吗」+ 群里有图 + 主人 → `full_text` 尾部：

```text
[本轮动作检查]
- 对方想知道图片内容：你必须**先进行原生工具调用** ocr_img()，等工具返回后再输出。
[本轮口头禅]
- 当前消息命中了口头禅「在呢」的触发例子「在吗？」：只是叫一声时用；……
```

### 7.6 hint 的边界

- hint 只是**提示**，不替模型做决定；工具是否真调用由模型的 tool call 决定。
- 所有 hint 都基于**正则 + 结构体信号**，没有一次额外模型调用（唯一例外是去复读改写与关系整理，都是小事后处理）。
- `action_check=False` 时 ①② 整体跳过（插话场景：此刻不是「有人对它提要求」，给动作检查会让它去执行别人的请求）。

---

## 8. 工具详解与触发条件

### 8.1 排序（`agent/tools/__init__.py:43 TOOL_ORDER`）

```text
ocr_img, read_forward, send_message, get_message,
add_Method, delete_Method, alter_Method,
silent_observe, poke, emoji_like, set_ban, recall_msg,
send_dress, get_msg_history, fetch_history, digest_user,
recall_result, update_person, update_group
```

`build_tools(agent, groups=None)`：`groups` 可只建某几组（`media`/`chat`/`history`/`methods`/`moderation`…），最后按 `TOOL_ORDER` 排序 —— **模型看到的工具顺序与旧版一致**。

### 8.2 各工具的触发方式

| 工具 | 签名 | 主要触发 |
|---|---|---|
| `ocr_img` | `(index=0, source="current", message_id="")` | hint 规则 5；模型看到 `[图片]` 主动调 |
| `read_forward` | `(message_id="")` | hint 规则 6 上半；模型看到 `【转发聊天记录 N 条】` |
| `send_message` | `(message_type, group_id, user_id, message)` | **只有**跨群/私聊转发，默认回复不调 |
| `send_dress` | `(group_id=None, message_type="group", user_id=None)` | hint 规则 1 / 1.5；内部再拦一次「只是在聊女装」 |
| `silent_observe` | `(reason)` | 模型判断这轮不该说话 |
| `poke` | `(user_id, group_id=0)` | hint 规则 2 |
| `emoji_like` | `(message_id, emoji_id="66")` | 模型自主 / 插话 `nod` 走的是 service 内部 API |
| `set_ban` | `(group_id, user_id, duration=60)` | hint 规则 4 明确**阻止** |
| `recall_msg` | `(message_id)` | hint 规则 3 |
| `get_msg_history` | `(group_id, message_id, count=20)` | hint 规则 6 下半 |
| `get_message` | `(message_id)` | 模型要看被回复那条的详情 |
| `fetch_history` | `(count=200, group_id=0, before_message_id=0)` | hint 规则 6「要很多条」 |
| `digest_user` | `(user="", count=300, context=2, group_id=0, max_chars=6000)` | 「总结某人说话风格」；代码侧算统计 |
| `recall_result` | `(result_id="", offset=0, max_chars=6000)` | hint 规则 7 |
| `add_Method` / `delete_Method` / `alter_Method` | `(context)` / `(index)` / `(index, context)` | 主人让它记方法论 |
| `update_person` | `(qq, label_add, label_remove, state, state_hours=72, memory, importance=2, portrait, relation)` | 模型听到稳定信息 |
| `update_group` | `(topics, lore, notes, description)` | 同上，群维度 |

### 8.3 工具内部约束

- 服务未连接 → 返回 `[工具不可用] …` 字符串，**不抛异常**（异常会打断 LangGraph）。
- 同轮同参数结果缓存（`tool_cache`）：同一张图不会 OCR 两次。
- `fetch_history` / `digest_user` / `recall_result` 结果同时 `put` 进 `ResultVault`，并写 `result_id`（形如 `R3`）供下一轮取。
- `send_dress` 成功时**不**记带副作用的 `send_dress` 调用记录？——不，它记；但 `chat()` 里 `_ANSWER_TOOLS = {"send_message","send_dress"}` 会把这类算作「已送达」。
- `read_forward` 把卡内图片 URL 登记到 `source="forward"`，之后 `ocr_img(source="forward", index=0)` 才能读。
- `digest_user` / `fetch_history` 内部走 `AbstractionLayer.HistoryPager`（锚点剔除、方向自检、单页条数夹取、游标不前进就收尾）。

---

## 9. 模型路由与降级（`agent/router.py`）

### 9.1 两条路由

| 路由 | 构建（`_setup_model_routes` :393） | 用途 |
|---|---|---|
| `reply_router` | `primary`（`active_model` 指定，否则 `models[0]`，再否则顶层配置） + `fallback_models` 逐个 | `chat()` 主回答，`use_agent=True`（带工具图） |
| `judge_router` | `judge_model`（若有）→ `primary` 的裸 LLM | 插话判断、关系抽取、改写，`use_agent=False`，`max_tokens ≤ 256` |

`ModelRoute.target(use_agent)`：`use_agent and agent is not None` → 带工具的图，否则裸 `llm`。
所以在插话判断 / 关系抽取里拿到的是**同一模型但不带工具**的轻量调用。

初始化失败：主模型 `required=True`（抛），fallback / judge `required=False`（warning + 跳过）。

### 9.2 `ModelRouter.ainvoke`（:150）

```python
routes = self._ordered()                          # 没冷却的优先；全冷却则退回原始顺序
for index, route in enumerate(routes):
    try: return await route.target(use_agent).ainvoke(payload)
    except Exception as exc:
        if not is_rate_limit_error(exc): raise     # 非 429 直接抛，不掩盖 bug
        seconds = self.mark_rate_limited(route, exc)
        if can_fallback and not can_fallback(): raise   # 有副作用工具已执行 → 不重跑
        # 有下一个就降级，否则记日志后继续（最后抛 last_error）
```

### 9.3 429 / 额度识别

`is_rate_limit_error`（:63）：`status_code`/`http_status`/`code` == 429、`response.status_code == 429`、文本里独立成数的 `429`（`(?<!\d)429(?!\d)`，避免 `message_id 429123` 误判）、或命中 `rate limit|too many requests|insufficient_quota|quota exceeded|throttl`。

`is_quota_error`（:78）：`per day|per-day|daily|insufficient_quota|quota exceeded|exceeded your current quota|credits|billing|free-models-per|余额|额度`。

| 类型 | 冷却 |
|---|---|
| 普通 429 | `RATE_LIMIT_COOLDOWN_SECONDS = 60` |
| 额度类 | `QUOTA_COOLDOWN_SECONDS = 30 * 60` |

额度类 60 秒后必再 429，拉长到 30 分钟避免每分钟白撞一次再降级（日志噪音 + 多一次往返）。

---

## 10. 三层记忆

| 层 | 类 | 粒度 | 生命周期 | 谁读写 |
|---|---|---|---|---|
| 跨轮对话 | `ThreadTurns` | 一段对话（首轮完整 + 每轮增量） | TTL 30 分钟 / 8 轮 / 12000 字 | `chat()` 读写，`thread_verdict` 裁决 |
| 工具结果 | `ResultVault` | 单次工具输出 | TTL 30 分钟 / 每会话 4 条 | 工具 `put`，`chat()` 给索引，`recall_result` 取 |
| 长期档案 | `RelationStore` | 人 / 群 / 自己 | 状态 TTL 72h（硬上限 168h），记忆长期 | `observe_context` / `record_*` / 自动整理 |

三者互不替代：`ThreadTurns` 管「这段还接不接」，`ResultVault` 管「上一轮翻出来的原文还在不在」，`RelationStore` 管「这个人是谁」。

### 10.1 `ThreadTurns`（`agent/thread_turns.py`）

问题：每轮重渲染 `ChatContext` → `<scene>` / `<history>` / `现在 HH:MM` 前缀全变 → 服务端前缀缓存永不命中，历史还会重复出现。

解法：

```text
第 1 轮: H1(完整上下文) → A1
第 2 轮: H1, A1, H2(增量)              前缀与上轮逐字节相同
第 3 轮: H1, A1, H2, A2, H3
```

超 `max_turns` / `max_chars` / `ttl` → **整段清空**（滑动窗口每轮改前缀，缓存永远不命中，还会让模型看到「缺失的中间轮」）。
续说那轮是 `H→A→H→A`，只存一对 `(H,A)` 对不上回放序列 → 直接重置。
默认**关闭**（`thread_memory.stable_prefix=false`），先看日志 `[LLM] prompt … 缓存命中 X%`。

### 10.2 `ResultVault`（`agent/result_vault.py`）

`put(thread_id, text, tool, label)` → `result_id`（`R1`…）；
`get(thread_id, result_id="")`（空=最近一条）；
`render_hint(thread_id)` → 两三行索引（编号 / 工具 / 字数 / 多久前）；
`thread_key(context)` = `group_x` / `private_x`（与 `MessageProcessor.thread_id_of` 一致）。

现场依据：2026-11-13 23:08 群 583261953，模型翻 1500 条后问「要不要挑几句给你看」，主人回「挑」，此时工具结果已不在上下文 → 要么重翻 30 秒，要么凭印象编。

---

## 11. 关系档案（`agent/relations.py`）

```text
data/relations/
├─ users/<QQ>.json      # labels / persona / relation / states(带 TTL) / memories
├─ groups/<群号>.json   # description / topics / lore / notes
└─ self.json            # 君景自己的心情状态（12h 过期）
```

`RelationStore` 只做「存 / 取 / 渲染 / 过期」，**不调模型**：

| 方法 | 作用 |
|---|---|
| `from_settings(settings)` | 构造；`relations.enable=false` 或没 settings → 禁用态 |
| `observe_context(context)` | 见到的人 / 群登记（不写记忆） |
| `render(context)` | 渲染成提示段（按相关度挑，带预算上限） |
| `record_reply(context)` | 记录最近一次被回复时间（用于自动整理阈值） |
| `record_self_status(status, thread_id)` | 存 `<status>` 里的心情 / 状态 |
| `update_person(...)` / `update_group(...)` | 工具写入，带长度/数量限制 |
| `should_extract(thread_id, direct)` | 是否该跑自动整理（`auto_every` 条 + `auto_extract_min_seconds` 最小间隔） |
| `build_extract_prompt(context, replies)` | 生成抽取 prompt |
| `apply_extraction(raw, context)` | 解析模型 JSON 并写回（唯一写入口，保证格式边界一致） |
| `reset()` | 清空（测试用） |

默认上限：`max_people=6`、`max_chars=1200`、`max_person_chars=320`、`state_ttl=72h`、`state_max=168h`、`self_ttl=12h`。

三种维护方式：人工改 JSON / 工具 `update_person`·`update_group` / 自动整理（低频异步，失败静默）。
`close()` 取消所有在跑的整理任务（`_relation_tasks`）。

---

## 12. 主动插话（完整触发链）

### 12.1 计数与触发

```text
pipeline.process_message → interject.note_message(event, attention)      (interject.py:200)
  ├─ 非群聊 / summon / AI 关闭 / 插话关闭 / 群不在白名单或命中黑名单 / 自己发的 → return None
  ├─ state.count += 1; state.last_event = event
  ├─ _ensure_timer(state)                       # 每群一个 _timer_loop
  ├─ hot = _is_hot(state)                       # Bot 最近说过话（敏感期）
  ├─ threshold = hot ? hot_frequent : frequent   # 默认 3 : 10
  └─ count >= threshold → _schedule(state, "攒够条数")
```

`_timer_loop`（:324）：每 `idle` 秒（敏感期用 `hot_idle`，默认 30 分钟 : 5 分钟）醒一次；`count > 0` 且不 busy 且不在冷却 → `_run(state, "安静了一阵")`。

`_cooldown_loop`（:349）：冷却结束补一次判断（普通阈值触发和敏感期单条追问都会走到）。
`note_bot_spoke(gid)`：Bot 说过话 → `count=0`、`pending=False`、进冷却（敏感期冷却被夹到 ≤60 秒）。

### 12.2 `_run`（:408）

```text
agent = agent_getter()                      # None 或没有 maybe_interject → return
context = _build_context(event, params)      # 扒 frequent+extra 条；历史预算放宽到 20 条/3200 字
  └─ context.interest_hits = read_interests(...)   # 只提示，不构成开口理由
if context.directed_at_me: return            # 已由正式回复接走，让开（否则回两遍）
plan = await agent.maybe_interject(context, thread_id, reason)
  ├─ None → return
  ├─ str / list（旧实现）→ 按 join
  └─ plan.act
       ├─ "nod"  → _nod(): api.set_msg_emoji_like(message_id, random(_NOD_EMOJIS))（不打字）
       └─ 其它   → 逐条 mp.send_message + mp.remember_sent_message(hot_seconds) → triggered += 1
finally: busy=False；pending 且 count>0 → _ensure_cooldown_wake()
```

### 12.3 `maybe_interject`（`agent/runtime.py:1583`）

```text
rendered = context.render_as_bystander() ?: context.render()
   # <tail>（最后一句，不一定是冲你）/ <me>（我上次说了什么、多久前）
   # <pulse>（几个人在聊、安静多久、有没有人提问没人接）/ <interest>（命中的在意话题）
verdict = await _invoke_judge([System(_INTERJECT_JUDGE_SYSTEM), Human(rendered)])
plan = parse_interject_verdict(verdict)      # → InterjectPlan(act, why, reply_to, messages)
  ├─ act == "silent" → log（+ 若 why 含「可以答/能答/该答」→ 单独 warning「动作和理由打架」）→ None
  ├─ act == "nod"    → 直接返回 plan（不调模型生成，nod 的语义就是「只表示在场」）
  └─ act ∈ join/answer/revive
        replies = await self.chat(context=context, thread_id=thread_id,
                                  hint=interject_hint(plan.act), action_check=False)
        plan.messages = replies; return plan if plan.messages else None
```

注意插话时 `action_check=False`：此刻不是「有人对它提要求」，给「[本轮动作检查]」会让它去执行别人的请求。

### 12.4 judge 提示词的设计取舍

判断问题从「要不要插一句」改成「这一刻群里缺什么角色」：
前者逼模型在说/不说之间二选一，兴趣一命中就硬凑；后者先找缺口，没有缺口自然 silent。
兴趣（`interest_hits`）只用来在差不多的局面里挑话题，不构成开口理由。

### 12.5 动作解析（`parse_interject_verdict` :548）

认动作顺序（刻意宽松，宁可多说一句也别吞掉该答的提问）：

1. `<act>answer</act>` 标准标签；容忍标签里混话、多个标签、中文（`回答`/`接话`/`沉默`/`不答`）；
2. 正文里的动作词（排除 `<why>` 里的理由文本，避免「我可以回答」被当动作）；
3. 旧格式 `<judge>yes</judge>`；
4. 全都认不出 → `silent`。

`InterjectPlan.act` 语义：`silent` 不说 / `nod` 只贴表情 / `join` 接一句 / `answer` 回没人接的提问 / `revive` 主动开新话题（极少）。

---

## 13. 心理分析（完整触发链）

```text
关键词命中（psychology.keywords，整条消息基本等于其中一个词）
  └─ PsychologyService.handle(event)                     bot/services/psychology.py:375
       ├─ 群白名单/黑名单/私聊开关
       ├─ 冷却（同会话 psychology.cooldown_seconds）
       ├─ 是不是回复消息？mp.is_reply → get_msg 取回被回复的
       │    ├─ 聊天记录（forward 段 / multimsg / raw_message CQ:forward）
       │    │    → ForwardExpander 摊平到叶子（max_records）
       │    │    → 图片过滤门只判「将要发给模型的前 max_images 张」
       │    └─ 普通消息 → 单条素材
       └─ 不是回复 → 用当前消息本身（去唤醒词后为空就回用法提示）
            ↓ reply_mode
              text    → PsychologyAnalyzer.analyze()         # 一段总结
              forward → PsychologyAnalyzer.analyze_nodes()   # 逐条原文 + <侧写>
              both    → 先总结，再逐条（两次模型调用）
            ↓
       forward: build_node 拼聊天记录 → send_*_forward_msg
       text   : 引用触发消息回复（超长按段落拆 ≤3 条）
```

`PsychologyAnalyzer`（`agent/psychology.py:496`）：

| 方法 | 说明 |
|---|---|
| `reload()` | 重读 `psychology` 段 |
| `available()` / `status()` | 是否配置可用 |
| `analyze(material)` | 一次总结 |
| `analyze_nodes(material)` | 逐条 → `list[NodeDraft]` |
| `_content_parts(material, numbered=)` | 拼文本 + 图片；按 `image_mode` 分流 |
| `_ocr_block(images)` / `_describe_images(images)` | `ocr` / `vision` 档的前置处理 |
| `_resolve_model(section, agent_section, defaults)` | 模型可选：`model_id` 引用 `agent.models`，或单独 `base_url/api_key/model` |

三档 `psychology.image_mode`：

| 值 | 行为 |
|---|---|
| `image` | 原图发给分析模型（要求多模态） |
| `ocr` | 不发图，只把 OCR 文字并进聊天记录 |
| `vision` | 先用视觉模型逐张客观描述，再把描述 + 分析任务一起交给分析模型（两个模型可不同） |

**独立于主对话**：不建 Agent、不带工具、不写任何记忆、不参与群聊输出协议；主对话换模型不影响它，反之亦然。

---

## 14. 日志（`agent/logger.py`）

`logs/agent/YYYY-MM-DD.json`，整个文件是一个 JSON 数组，每次 `append_log(entry)` 读→append→写回（进程内 `RLock` 串行）。

`build_log_entry` 字段：

```json
{ "timestamp", "thread_id", "user_message", "ai_response",
  "extra_context", "method_context", "tool_calls",
  "status", "think", "raw_response", "error" }
```

`LLMCallbackHandler`（:195）：

| 回调 | 记录 |
|---|---|
| `on_llm_start` | `[LLM] 发送请求, prompts=N 条`（debug） |
| `on_llm_end` | `[LLM] prompt N tokens，缓存命中 M（X%）`（info）—— 服务端前缀缓存是否命中只有 usage 能看出来 |
| `on_llm_error` | `[LLM] 请求出错` |
| `on_tool_start` / `on_tool_end` | 工具名 + 入参/出参前 80 字（debug） |

`_cached_tokens()` 兼容 OpenAI 的 `prompt_tokens_details.cached_tokens` 与 DeepSeek 的 `prompt_cache_hit_tokens`。

日志前缀约定：`[模型降级]` / `[会话 …]` / `[插话]` / `[关系]` / `[工具调用]` / `[工具缓存]` / `[反复读]`。

---

## 15. 配置速查

| 键 | 作用 | 默认 |
|---|---|---|
| `agent.base_url` | LLM 服务地址 | `https://api.deepseek.com/v1` |
| `agent.api_key` / `api_key_env` | 密钥（显式 > 环境变量） | `DEEPSEEK_API_KEY` |
| `agent.model` | 主模型名 | `deepseek-v4-flash` |
| `agent.temperature` / `max_tokens` | 采样 / 上限 | 0.7 / 10240 |
| `agent.models[]` | 候选池（`id`/`name` + 可覆盖上述） | `[]` |
| `agent.active_model` | 主模型 id | 空 = `models[0]` |
| `agent.fallback_models[]` | 429 降级顺序 | `[]` |
| `agent.judge_model` | 判断器模型 id | 空 = 跟随主模型 |
| `agent.persona_file` / `persona_core_file` | 角色卡 / 通用行为 | `agent/personas/junjing.md`、`core_qq.md` |
| `agent.catchphrase_file` / `catchphrase_limit` | 口头禅 | `agent/catchphrases.json` / 0=全部 |
| `thread_memory.stable_prefix` | 开多轮留存 | `false` |
| `thread_memory.verdict` / `new_after_minutes` | 裁决开关 / 隔多久当新话题 | `true` / 15 |
| `thread_memory.ttl_minutes` / `max_turns` / `max_chars` / `groups` | 留存预算 / 灰度群 | 30 / 8 / 12000 / `[]` |
| `relations.*` | 关系档案 | 见 11 节 |
| `meme.enable` / `path` / `prompt_limit` | 表情包 | `true` / — / 0 |
| `psychology.*` | 心理分析 | 见 13 节 |
| `interject.frequent` / `hot_frequent` | 插话阈值（普通 / 敏感期） | 10 / 3 |
| `interject.idle_minutes` / `hot_idle_minutes` | 安静触发 | 30 / 5 |
| `interject.cooldown_minutes` | 冷却 | 5 |
| `interject.hot_minutes` | 敏感期长度（也是 `replying_to_bot` 的窗口） | 10 |
| `interject.aliases` | Bot 别名（attention 判定） | `("君景","独角兽")` |
| `interject.groups` / `blacklist_groups` | 插话白/黑名单 | `[]` |
| `interject.interests.high/low` | 在意话题 | `[]` |

热加载：`agent.*` 改动 → `bot/app.py` 后台重建 `QQBotAgent`（`_restart_agent` 带 generation 号，旧 generation 的结果直接丢弃，避免竞态）；
`persona_file` / `catchphrase_file` 只在构造时读一次 → 改文件需重启。

---

## 16. 设计取舍

| 取舍 | 原因 |
|---|---|
| `chat()` 无状态，历史靠 `ChatContext` 带入 | 上下文组装归 `messaging/`，Agent 只消费；可单测、并发安全 |
| 轮次状态放 `contextvars` | 多条消息并发 `chat()` 互不污染，且 `BaseTool.arun` 的 context 复制限制下仍能传结果 |
| 只有 429 降级 | 掩盖真 bug 比降级更贵 |
| 有副作用工具已执行则禁止降级重跑 | 否则群里收到重复消息 |
| 工具结果只给索引 | 不白占 token；承接上一轮时才自动铺全文 |
| 多轮留存超限整段清空 | 滑动窗口每轮改前缀，缓存永不命中 |
| 裁决默认「留」 | 丢错上下文不可逆，多带几句只是 token 成本 |
| 插话问「缺什么角色」 | 问「要不要说」会让模型硬凑 |
| `nod` 不调模型 | 语义就是「只表示在场」，再生成一遍纯属浪费 |
| 插话时 `action_check=False` | 此刻不是它的任务，给动作检查会去执行别人的请求 |
| hint 全用正则 + 结构体信号 | 零额外模型调用、可单测、可解释 |
| hint 互斥（转发卡片 vs 查历史） | 两条同时给小模型会把它带偏 |
| 图片 URL 不进提示词 | 防长 URL 污染上下文与缓存 |
| 心理分析独立模型 | 主对话常是小模型 / 纯文本，看群里长截图又贵又不准 |
| `print(res)` 留在 history 工具 | 调试残留（见 17） |

---

## 17. 已知边界

- `agent/tools/history.py:72` 仍有一处调试用 `print(res)`。
- 关系档案是 JSON 文件，量大时无索引；`ThreadHistoryStore` 是内存 LRU。
- `ResultVault` 纯内存，进程重启即丢（30 分钟 TTL 本就不需要持久化）。
- `thread_memory` 默认关闭，线上效果需看 `[LLM] … 缓存命中 X%` 再决定灰度范围。
- `campus.py` 与主对话共用 `agent.*` 的 `base_url`，换服务商时两边一起变。
- `hints.py` 的正则是经验值，新话术可能需要追加；漏命中时模型只能靠工具描述自救。
