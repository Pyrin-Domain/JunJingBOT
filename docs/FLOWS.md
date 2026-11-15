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
  │    ├─ PsychologyService        # 心理分析：独立模型，关键词唤醒
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
  └─ post_type == "message" -> EventDispatcher.submit
       ├─ 同一群 / 私聊：按到达顺序串行
       ├─ 不同会话：最多 max_concurrency 个并行 worker
       └─ 总待处理量达到 max_pending：拒绝新事件并告警

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
            # keyword_actions + psychology.keywords 动态规则（整条消息 ≈ 唤醒词）
```

**心理分析**走第 8 步，不占AI分支：关键词命中后在后台跑
`bot/services/psychology.py`（回复消息 → 聊天记录展开 → 过滤门 → 独立模型），
详见 `docs/PSYCHOLOGY.md`。

**不能改的顺序**：

- 搬运必须在 AI 前：搬运命中后不再回复。
- AI 必须在关键词前：否则同一条消息会被 AI 和关键词各回一次。
- `is_recover=True` 只允许搬运，不计数、不记临时历史、不走 AI / 关键词。

---

## 3. AI 一轮

```text
agent.runtime.QQBotAgent.chat(event, context, thread_id)
  ├─ context.render()                    # 场景 / 对话关系 / 历史 / 当前，一行一条
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
  │    ├─ 引用：只留一行摘要 + 图片源（不递归）；取不到内容时留 reply_to_id，
  │    │        渲染成「↳[id] 原消息已取不到」，关系不能凭空消失
  │    ├─ 转发：当前消息展开，历史里的只留【转发 N 条】
  │    └─ 媒体：图片留 [图片]、表情留 [表情:名字]/[表情]，URL 存进 Segment.url（不渲染）
  └─ ChatContext(...)
       ├─ 预算：历史 12 条 / 2000 字，单条 300，当前 800
       ├─ 对话关系：messaging/dialogue.py 算每条「→谁」，行尾标；
       │           群聊且别人说了 >=2 条时补 <dialogue>（谁在说 / 最后一句冲谁）
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

拆成三段，判定和发送不混在一起（`bot/services/forward/`）：

```text
ForwardService.forward(event, is_recover, **settings.auto_forward_kwargs)
│
├─ ① detect.detect_job()   —— 只做判断，不发送、不写库
│    ├─ 来源校验：groupid_list / userid_list（字符串比较）
│    ├─ 机器人白名单先于来源闸门：auto_img_forward_from_bot
│    │    type=all/group/private + group_id/user_id_list
│    ├─ 自己发的消息直接不搬（目标群同时是来源群时，卡片会以 uid=自己 回流）
│    ├─ 类型：image（白名单机器人图片）/ record（聊天记录）/ miniapp（哔哩哔哩卡片）
│    ├─ 目标：target_groupid − 事件所在群；task_userid − 发送者
│    └─ 命中 auto_forward.filter -> job.blocked（已处理，不搬）
│
├─ ② 分派
│    ├─ record / miniapp -> _forward_record()
│    │    ├─ 入库清洗（只影响判定）：装扮/气泡分享卡片（不看位置）
│    │    │    + 末尾 3 条的二维码海报 / 群卡片 / 纯链接 / 吆喝+链接
│    │    ├─ 构造指纹：文本 simhash + 图片 aHash/dHash + 视频帧指纹
│    │    │    （同时算一份「清洗前」的别名文本，见下方老行兼容）
│    │    ├─ forward_dedup_gate() -> GateOutcome
│    │    │    ├─ 查库顺序：清洗后整串 → 清洗前整串（老行）→ SimHash → 图片/视频
│    │    │    ├─ targets = 目标群 − 库里已发过的群
│    │    │    │    （命中不等于全停：上次没发成功的、新加的目标群在这里补齐）
│    │    │    ├─ extra_reply = 【发过了喵】（商城表情），仅当「这个群自己有记录」且它不是目标群
│    │    │    └─ fingerprint：内容不足时为 None（照发，不登记）
│    │    ├─ 图片过滤门（image_filter.scan_all_images）：把记录摊平到叶子消息
│    │    │    （ForwardExpander —— 嵌套记录单层只有内层 forward id，图看不到）
│    │    │    → 逐张下载 + 判定（max_images 默认 0 = 整份全判）
│    │    │    → 命中：记录转发到审计群 + 按标签模式打标签（reply/before/after）
│    │    │    → block_matched=true 时不再搬给普通目标
│    │    ├─ 发送：send_group_forward_msg（重建）/ forward_*_single_msg（整卡）
│    │    └─ 发出去了才 dedup.record(群 ∪ 来源群)
│    │
│    └─ image -> _forward_image()
│         ├─ 过滤门先于送达库：screen_event 下载并判定
│         │    → 命中：按 single_tag_mode 发标签到审计群（single / forward_reply）
│         │    → block_matched=true 时不搬给普通目标，直接结束
│         ├─ 身份 = real_id / (scope, message_id)，取所有能拿到的
│         ├─ delivered = 图片送达库(身份 × 会话)
│         ├─ 还有没送过的会话时再按图片本体判一次（perceptual）：
│         │    ├─ 逐张下载（优先命中过滤门的字节缓存）
│         │    ├─ imgsha:* 字节哈希精确命中 → 该会话送过完全相同的图
│         │    ├─ phash:* 感知指纹（aHash+dHash）汉明距离 ≤ 阈值 → 送过同一张图
│         │    │    （QQ 重新编码后文件名 md5 会变，只有它能认出来）
│         │    └─ 只有「这条消息里的每张图都命中」才跳过该会话 —— 旧图+新图照常搬
│         ├─ targets = 目标 − 已送达的会话
│         ├─ extra_reply 恒为 None（图片没有正文，回「发过了喵」没有意义）
│         └─ 送到后 remember()（消息身份 + 图片内容身份一起记）
```

两个库、两条数据线：

| 库 | 文件 | 主键 | 判什么 |
|---|---|---|---|
| `ForwardDedupStore` | `data/forward_dedup.db` | 内容指纹 | 这份内容被哪些群见过 |
| `ForwardImageStore` | `data/forward_image.db` | 身份 × 会话 | 这条消息 / 这张图送给过哪些会话 |

图片送达库里的身份有三类：消息身份（`real_id:*` / `message_id:*`）、文件名内容身份
（`image:*` / `image_fileid:*`）、图片本体身份（`imgsha:*` 字节哈希、`phash:*` 感知
指纹）。前两类都依赖 QQ 给的文件名 / 消息 id；同一张图被重新上传后 md5 会变，
只有感知指纹还能认出「这是同一张图」，所以线上重复搬运主要靠它兜底。

回放（`is_recover=True`）同样过两道门，但**不发**`extra_reply` —— 那是补历史，
不是群里有人重复发。

几个反复踩过的点（改之前先读）：

* **目标群 ≠ 来源群**：配置里两边经常是同一批群，所以「搬进去的卡片」会以
  `user_id=自己` 的身份再进来一次。不掐掉的话，weak 指纹一取不到图就会被当成新
  内容再搬一轮（线上 17:35）。
* **命中不等于全停**：库里说「这些群发过了」，那就只发给没发过的。旧实现是
  「命中就 return」，于是上次失败的那部分永远补不上。
* **目标群里不提示**：这份内容本来就会送到它手里，`extra_reply` 会和卡片叠着刷屏
  （线上 19:12：先一句【发过了喵】，紧接着卡片也到了）。
* **weak 指纹必须有媒体锚点**：纯图转发文本一模一样，图片双哈希算不出来时不能
  按文本判「搬过」，否则会误杀别人的图；但也不能当成「没搬过」乱搬 —— 大图
  （>4MB）以前直接放弃，现在上限 16MB。
* **清洗改写了哈希，老行要兼容**：清洗上线之前入库的行存的是「带广告」的
  content_hash，只查清洗后的文本永远对不上 —— 同一份内容每重发一次就搬一次。
  所以闸门会额外拼一份「清洗前」的指纹（`build_fingerprint(clean=False)`），
  查库时当作**别名**多查一次；命中就把那一行就地升级成清洗后的哈希
  （`_promote_locked`，只改哈希/预览，群标记和 weak 不动），下一次连换马甲的
  变体都能直接命中。别名是更严格的比对（连广告都一样），不会带来新的误判。
* **入库清洗和发送裁剪是两件事**：入库清洗（`forward_dedup.clean_*`）只为让指纹
  对齐，可以比发送狠（末尾链接从「吆喝 + 链接」就算广告）；发送侧的
  `auto_forward.strip_tail_ads` 保持保守，免得把「这个库 https://… 挺好用」删掉。
  装扮/气泡卡片两边都丢 —— 它们在任何位置都是噪声（线上一条记录塞 32 张）。
* **图片过滤门是「函数 + 回调」的注册表**（`messaging/image_filter.py` +
  `bot/services/forward/image_gate.py`）：过滤函数只回答命中什么（`FilterHit`），
  发标签 / 拦截由编排层做；`filters` 列表每次判定都从配置重建，面板保存即热更新，
  没重启这一说。菲比暴论的主判据是**同群最近发过触发命令**（命令和出图是两条
  事件，`forward()` 一进来就 `note_event` 记文本），版式判定只做兜底。配置见
  `docs/CONFIG.md` 的 `image_filter` 一节。

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

## 7.5 历史区间搬运（export）

```text
用户：export [-g <群>]... [-u <QQ>] [-a] [-b] [-e]
  └─ CommandHandlers.export
       ├─ parse_targets：-g 可重复；没写 -g 且带 -a 时用默认目标群
       ├─ 没有 -b/-e：回复=起点、命令消息=终点，直接搬
       └─ 带 -b/-e：RangeTaskService 按群收集两端，齐了回调 run_export_range
            └─ ForwardService.auto_forward(target_groupid=[...] / task_userid=[...])

export -a：目标群 = settings.export_auto_targets
  ├─ export.auto_target_group_ids 非空 → 用它
  └─ 为空 → 退回 auto_forward.target_groupid（与自动搬运同一批群）
```

---

## 8. B 站 / 点歌下载

```text
命令 / 小程序卡片
  └─ MediaService.send_bili_video(event, url)
       ├─ BiliHistory.get(BV号)            # 命中直接发缓存文件，跳过下载
       │    └─ 文件不存在 -> 删记录，正常下
       ├─ BilibiliProvider（未命中时）
       │    ├─ 解析短链 / BV / av
       │    ├─ 取视频信息（标题、封面、时长）
       │    ├─ durl 优先，否则 dash 最高 id
       │    ├─ 分段下载 -> 合并
       │    ├─ ffmpeg 合并音视频
       │    │    失败 -> 回退无音频视频
       │    └─ DownloadResult(path, size, mime)
       ├─ BiliHistory.remember(BV号, path)  # 先落库再发：超时也不算白下
       └─ MediaService.send_video_file(event, path)
            ├─ 通道顺序由 BackendCapabilities 决定（file → video）
            ├─ 明确拒绝退避重试，超时直接收工（再发一份是重复）
            ├─ file 段的 name 由 OneBotAPI._call_api 统一补
            │    （缺 name 时 QQ 会在 UploadGroupFileRequest 阶段回 -134）
            └─ 失败 -> 静默（不回复任何文案，旧的「下载地址」回退已删）

点歌：
  MediaService.send_music(event, keyword)
    └─ MusicProvider -> 搜索页 -> 播放器页 -> music_url
         └─ 发送 {"type":"record","data":{"file": music_url}}
            （不下载音频）

jm：
  /jm <id> -> DownloadService.download_jm -> 本地 PDF
    └─ bot/commands/jm._send_private_file
         ├─ 文件通道：OneBotAPIError（明确拒绝）退避重试；超时收工
         │    └─ file 段的 name 在 OneBotAPI._call_api 统一补（SnowLuma 必需）
         └─ 闪传通道：create_flash_task + send_flash_msg
              （通道顺序来自 BackendCapabilities.file_send_order）
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
  -> 按 BackendCapabilities.history_latest_is_authoritative 分派：
     ├─ True（NapCat）：get_group_latest_msg 取现取的最新 id
     │    -> 后台 recover_process(latest=…)：从最新往回翻，直到遇到断点
     └─ False（SnowLuma）：后台 recover_process：从断点往后翻，直到没有更多
        （无游标的「最新」可能只是本地观测值，不能当判据）
     两种方向都用 message_seq / message_id / real_id 任一匹配来认断点
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
| 搬运 / 去重 | `bot/services/forward/`（job / detect / service）+ `dedup.py` + `image_dedup.py` |
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
