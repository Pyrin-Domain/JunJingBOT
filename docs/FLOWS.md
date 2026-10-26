# 业务流（照着读代码）

每个流程都标了“入口 → 关键模块 → 出口”。改代码前先找到对应流程。

---

## 1. 启动

```text
run.py
  ├─ config.load_settings()
  │    ├─ config/environment.json   -> EnvConfig + PathBridge
  │    ├─ config/config.json        -> ConnectionSettings
  │    ├─ config/bot_config.json    -> BotSettings.data
  │    └─ AppPaths.ensure_dirs()
  ├─ core.logging.setup(logs/bot.log)
  ├─ images.configure_from_settings(settings)
  ├─ build_app()
  │    ├─ ws_re: OneBotConnection(role="事件+API")
  │    └─ ws:    OneBotConnection(role="事件") + OneBotConnection(role="API")
  ├─ BotApp.__init__()
  │    ├─ DownloadService + DownloadPool
  │    ├─ MediaService
  │    ├─ MessageProcessor
  │    ├─ ForwardService / RangeTaskService / CommandHandlers
  │    ├─ InterjectService
  │    └─ 后台线程加载 QQBotAgent
  ├─ WebServer.start_if_enabled()
  ├─ transport.listen()         # 注册回调，等 OneBot 后端连上
  └─ BotApp.start()
       ├─ setuserid()           # get_login_info -> user_id
       ├─ read_check_point()
       ├─ start_checkpoint_save()  # 每 10 秒
       ├─ start_heartbeat()        # 10~20 分钟
       ├─ _recovery_ready = True
       └─ recover_from_check_point()

断线重连：
  OneBotConnection 每次连上 -> add_connect_listener 回调
  -> BotApp._on_connection_ready()
  -> recover_from_check_point()          # 按群去重，不会和上一次回放抢同一条历史
```

要点：

- 面板先于“等 OneBot 后端”启动，连接失败也能改配置。
- 断点回放是后台任务，不阻塞新消息。
- 启动恢复和重连恢复共用同一套逻辑；同一个群已有回放任务时跳过，
  避免重连风暴导致重复搬运。
- Agent 初始化失败不会卡死 `@` 消息：`_agent_ready` 一定会 set，
  此时 `agent=None`，自动退回关键词。

---

## 2. 一条消息（严格顺序）

```text
transport.listener
  └─ post_type == "message" -> asyncio.create_task(BotApp.process_message)

BotApp.process_message
  └─ MessagePipeline.process_message
       1. 群消息：mp.check_point[group_id] = event
          非恢复：interject.note_message(event)
       2. 非恢复：mp.remember_thread_message(event)
       3. pipeline._emoji_function(event)        # 20001 彩蛋
       4. forward.auto_forward(...)              # 命中 -> return
       5. mp.is_AT(event, [self_id])
       6. replying_to_bot =
            thread_is_hot(thread_id) and replies_to_bot(event)
       7. (ated_user or replying_to_bot) and not is_recover:
            a. clean_text = mp.get_clean_context(event, True)
            b. ^历史 正则 -> history_solution() -> return
            c. context = mp.build_chat_context(...)
            d. agent = await get_agent()
               没有 agent -> _match_keywords() -> return
            e. reply = agent.chat(event, context, thread_id)
            f. group: interject.note_bot_spoke(group_id)
            g. 发送 reply，逐条 remember_sent_message()
            h. return（调用过模型就不再走关键词）
       8. _match_keywords(event, is_recover)
```

**不能改的顺序**：

- 搬运必须在 AI 前：搬运命中后不再回复。
- AI 必须在关键词前：否则同一条消息会被 AI 和关键词各回一次。
- `is_recover=True` 只允许搬运，不计数、不记临时历史、不走 AI / 关键词。

---

## 3. AI 一轮

```text
agent.runtime.QQBotAgent.chat(event, context, thread_id)
  ├─ context.render()                    # 场景 / 历史 / 当前，一行一条
  ├─ build_action_hint(context)          # 需要工具时点名
  ├─ build_meme_hint(context)            # 情绪明显 / 点名要表情包时提示
  ├─ model_router 选模型（429 / 额度 -> fallback）
  ├─ LangGraph agent loop（工具调用）
  ├─ parse_agent_output(text)
  │    ├─ <status> ... </status>
  │    ├─ <<message> ... </message>       # 可多条
  │    ├─ <meme>名字</meme> → [CQ:mface,…] / [CQ:face,…]
  │    └─ <next>continue|end</next>
  ├─ next=continue -> 最多续说 2 轮，总消息 <= 6
  ├─ 去复读 / 敷衍检测 / 场景台词池
  ├─ 回复优先于沉默：
  │    directed_at_me 且空输出 -> 强制再问一次
  └─ return list[str] / None
```

工具执行完可能已经发过消息（`send_message` / `send_dress`），
此时返回空列表是正常的，pipeline 不会重复发送。

---

## 4. 上下文组装

```text
mp.build_chat_context(event, history_count=10, note=..., master_ids=...)
  ├─ collect_history()
  │    ├─ OneBot get_group_msg_history / get_friend_msg_history
  │    ├─ 同时带 message_seq / message_id，由后端档案选一个
  │    └─ 只取最近 N 条，reverse_order=True
  ├─ thread_history.snapshot(thread_id)   # 补更早的缓存
  ├─ merge_history(buffered, fetched)     # 去重 + 排序
  ├─ 剔除当前 message_id（历史接口包含当前消息）
  ├─ collect_message(event)
  │    ├─ parse_event -> ChatMessage
  │    ├─ 引用：只留一行摘要 + 图片源（不递归）
  │    ├─ 转发：当前消息展开，历史里的只留【转发 N 条】
  │    └─ 图片：只留 [图片]，URL 存进 Segment.data["url"]
  └─ ChatContext(...)
       ├─ 预算：历史 12 条 / 2000 字，单条 300，当前 800
       ├─ 超预算标注 omitted
       └─ 回写 thread_history
```

OCR 工具通过 `context.image_sources()` 按 `source + index + message_id`
找到 URL，URL 永远不进入提示词。

---

## 5. 临时会话历史 / 敏感期

```text
每条新消息 -> mp.remember_thread_message(event)
Bot 发消息 -> mp.remember_sent_message(event, text, result, hot_seconds)
                  ├─ 写入 ThreadHistory（sender.is_self=True）
                  └─ mark_bot_spoke(thread_id)  # 开始敏感期

下一条消息：
  mp.thread_is_hot(thread_id, hot_seconds)  # 敏感期内？
  mp.replies_to_bot(event)                  # 引用的是 Bot 的消息？
  -> 两者都真：即使没有 @，也走 AI
```

ThreadHistory 按会话（`group_<id>` / `private_<id>`）保存，
有 TTL / LRU，避免无限增长。

---

## 6. 自动搬运 + 去重

```text
ForwardService.auto_forward(event, is_recover, **settings.auto_forward_kwargs)
  ├─ 来源校验：groupid_list / userid_list（字符串比较）
  ├─ 机器人白名单：auto_img_forward_from_bot
  │    type=all/group/private + group_id/user_id_list
  ├─ 目标：target_groupid（群）/ task_userid（私聊）
  ├─ 构造指纹：
  │    ├─ 文本 simhash
  │    ├─ 图片 aHash + dHash
  │    ├─ 小程序 / 转发卡片
  │    └─ 纯图/短文本额外图片二次校验
  ├─ dedup.find(...)
  │    ├─ 没搬过 -> 放行
  │    ├─ 搬过 + 当前群已标记 -> 发【发过了喵】并结束
  │    └─ 搬过 + 当前群未标记 -> 结束（不搬也不提示）
  ├─ 发送：forward_group_single_msg / forward_friend_single_msg
  │        或 send_group_forward_msg（合并转发）
  └─ 发送成功后 dedup.record(...)
```

回放（`is_recover=True`）同样过去重闸门，但命中时不发【发过了喵】。

---

## 7. 图片批量转发（img_fd）

```text
用户：img_fd -b <起始> -e <结束> [-a]
  └─ CommandHandlers.img_forward_create_task
       ├─ RangeTaskService.register("img_fd", handler)
       ├─ 同一群只允许一个进行中的区间任务
       ├─ 收集区间内消息
       └─ ForwardService.image_forward / image_forward_batch
            ├─ 只收“有图、无视频”的消息
            ├─ 节点 uin / 昵称固定
            └─ 目标群合并转发

img_fd -a：目标群 = settings.image_forward_auto_targets
```

---

## 8. B 站 / 点歌下载

```text
命令 / 小程序卡片
  └─ MediaService.send_bili_video(event, url)
       ├─ BilibiliProvider
       │    ├─ 解析短链 / BV / av
       │    ├─ 取视频信息（标题、封面、时长）
       │    ├─ durl 优先，否则 dash 最高 id
       │    ├─ 分段下载 -> 合并
       │    ├─ ffmpeg 合并音视频
       │    │    失败 -> 回退无音频视频
       │    └─ DownloadResult(path, size, mime)
       ├─ images.to_backend(local_path)  # Windows <-> WSL / 自定义规则
       └─ api.send_group_message([{type:"video", data:{file:...}}])
            失败 -> 发送下载地址文本

点歌：
  MediaService.send_music(event, keyword)
    └─ MusicProvider -> 搜索页 -> 播放器页 -> music_url
         └─ 发送 {"type":"record","data":{"file": music_url}}
            （不下载音频）
```

下载池：

```text
DownloadPool
  ├─ asyncio.Queue(maxsize=32)
  ├─ N 个 worker（默认 2）
  ├─ 每个任务：timeout=600s，retries=2，指数退避
  ├─ cancel(task_id) / caller cancel
  └─ finally: storage.cleanup(task)  # 删 data/tmp/<task_id>/
```

---

## 9. 断点保存 / 恢复

```text
每条群消息：mp.check_point[group_id] = event
每 10 秒：  mp._save_check_point()
              -> data/check_point/<self_id>.json
                 { "<group_id>": <完整 event> }

重启 / 断线重连：
  read_check_point(self_id)（重启时）或直接用内存 check_point（重连时）
  -> 对每个群 get_group_latest_msg
  -> 后台 recover_process：从最新往前翻，直到遇到断点（message_seq / message_id / real_id 任一匹配）
       ├─ 遇到 Bot 自己的消息 -> 停止（说明断点不可信）
       └─ 期间 process_message(is_recover=True) -> 只搬运
  -> 同一个群已有回放任务时跳过，防止启动/重连同时触发导致重复
```

---

## 10. 网页面板保存

```text
frontend POST /api/config {data}
  └─ web.routes.config
       ├─ validation.validate_config(data) -> errors / warnings
       ├─ settings.update(data)
       ├─ settings.save()                 # 原子写 bot_config.json
       └─ bot.reload_config()
            ├─ settings.reload()
            ├─ mp.extraconfig.update(...)
            ├─ agent.* 变化 -> 后台重建 QQBotAgent
            └─ web_panel.* / 连接方式变化 -> restart_required
```

面板只写 `bot_config.json` / `config.json` / `sender_config.json`；
不写 `environment.json`（环境变化需要人工确认）。

---

## 11. 改代码时看哪里

| 想改 | 去哪 |
|---|---|
| 消息处理顺序 | `bot/pipeline.py` |
| 关键词命令 | `bot/commands/` + `bot/keyword.py` |
| 搬运 / 去重 | `bot/services/forward.py` + `dedup.py` |
| 插话 | `bot/services/interject.py`（触发 / 冷却 / 执行 nod·join·answer·revive） |
| 插话上下文 | `messaging/pulse.py`（局面 + 自我位置）、`messaging/interest.py`（兴趣命中）、`messaging/attention.py`（召唤三档） |
| 断点 | `messaging/processor.py` + `bot/checkpoint.py` |
| Prompt / 工具 | `agent/prompts.py` + `agent/tools/` |
| 模型路由 | `agent/router.py` + `agent/config.py` |
| B 站 / 点歌 | `download_system/providers/` + `media/service.py` |
| OCR / 图片哈希 | `media/ocr.py` + `media/image_hash.py` |
| 路径转换 | `config/environment.py` |
| 图片发送 | `images/` |
| 表情包 | `messaging/meme.py`（名字匹配 → CQ 码 → 消息段）+ `agent/hints.py`（情绪提示）+ `data/memes.json` |
| 面板 API | `web/routes/` |
| 面板 UI | `frontend/assets/js/` |

---

## 12. 当前验证状态

- 单测：`pytest -q` 全绿（`tests/test_jm_command.py` 的 4 例需要先 `pip install jmcomic`）。
- 静态：`python -m compileall -q .` 通过。
- 启动：`python run.py` 能起反向 WS `127.0.0.1:8085` + 面板 `127.0.0.1:8390`，
  Agent 15 个工具（原有 13 + `update_person` / `update_group`）、CampusAssistant 2 个工具加载；`/api/status` 返回 200。
- 独立面板：`python -m web.standalone --port 8391`，`/api/status` 返回 200。
- 待真机：NapCat / SnowLuma 实际消息、B 站真实下载 / ffmpeg、点歌、Windows winrt OCR。
