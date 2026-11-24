# 协议差异抽象层（`AbstractionLayer/`）

解决一个问题：**同一个 OneBot v11 动作，NapCat 和 SnowLuma 的返回语义不一样**，
而断点恢复、转发展开、去重指纹都在直接消费这些返回，于是行为分叉。

本文所有结论都是 2026-10-26 在真机上实测出来的（NapCat HTTP `:3000`、
SnowLuma HTTP `:3210`，同一批消息、同一个群 `20010`），不是按协议文档推测。

---

## 1. 实测差异

### 1.1 `get_group_msg_history` 的字段语义

用同一批 10 条消息（全局序号 184~193）对比：

| 字段 | NapCat | SnowLuma |
|---|---|---|
| `message_id` | int32 哈希 | int32 哈希，**与 NapCat 完全不同** |
| `message_seq` | **= 哈希**（名不副实） | **全局序号** 184/185/…（int） |
| `real_seq` | 全局序号 `"184"`（str） | **不存在这个字段** |
| `is_reserve` | 传了被忽略 | 传了被忽略 |

结论：**「全局序号」在 NapCat 里叫 `real_seq`，在 SnowLuma 里叫 `message_seq`。**

### 1.2 只有一种游标字段真能翻页

矩阵扫描（`python -m AbstractionLayer.cli sweep`，`count=6`，锚点取中间一条）：

```text
[napcat]   锚点 seq=220 id=1096072527
  字段            rev    条数   锚点位置   结果
  message_seq    True   6      5         ✅ 正常翻页（锚点在末位）
  message_seq    False  4      0         ✅ 正常翻页（锚点在首位）
  message_id     True   6      2         ⚠️ 被忽略 / 居中窗口
  message_id     False  6      2         ⚠️ 被忽略 / 居中窗口
  real_seq       True   6      2         ⚠️ 被忽略 / 居中窗口
  real_seq       False  6      2         ⚠️ 被忽略 / 居中窗口

[snowluma] 锚点 seq=220 id=1195752184
  message_seq    True   6      2         ⚠️ 被忽略 / 居中窗口
  message_seq    False  6      2         ⚠️ 被忽略 / 居中窗口
  message_id     True   6      5         ✅ 正常翻页
  message_id     False  4      0         ✅ 正常翻页
  real_seq       True   6      2         ⚠️ 被忽略 / 居中窗口
  real_seq       False  6      2         ⚠️ 被忽略 / 居中窗口
```

两个陷阱：

1. **发错游标字段不会报错**，后端静默忽略，返回「以锚点为中心的一页」。
2. **全局序号不能当游标**，两边都忽略 `real_seq` 参数。它只能用来「认出同一条
   消息」，要翻页必须拿到那条消息自己的 `message_seq` / `message_id`。

### 1.3 `reverse_order` 只在字段对的时候生效

这是全部差异里最要命的一条：**`reverse_order` 不是独立参数，它依赖游标字段先发对。**

同一台后端、同一个锚点、`count=5`，只换游标字段：

```text
napcat   游标字段=message_seq  reverse_order=True  -> 锚点位置=4（末位）  更旧=4 更新=0  ✅
napcat   游标字段=message_seq  reverse_order=False -> 锚点位置=0（首位）  更旧=0 更新=2  ✅
napcat   游标字段=message_id   reverse_order=True  -> 锚点位置=1（中间）  更旧=1 更新=2  ❌
napcat   游标字段=message_id   reverse_order=False -> 锚点位置=1（中间）  更旧=1 更新=2  ❌

snowluma 游标字段=message_id   reverse_order=True  -> 锚点位置=4（末位）  更旧=4 更新=0  ✅
snowluma 游标字段=message_id   reverse_order=False -> 锚点位置=0（首位）  更旧=0 更新=2  ✅
snowluma 游标字段=message_seq  reverse_order=True  -> 锚点位置=1（中间）  更旧=1 更新=2  ❌
snowluma 游标字段=message_seq  reverse_order=False -> 锚点位置=1（中间）  更旧=1 更新=2  ❌
```

读法：

- **字段对**：`reverse_order` 完全生效。`True` → 锚点在末位、全是更旧的消息；
  `False` → 锚点在首位、全是更新的消息。
- **字段错**：`reverse_order` 被**静默忽略**，四种组合返回同一个「以锚点为中心的
  窗口」——更旧 1 条、更新 2 条、锚点夹在中间。不报错、`status=ok`、`retcode=0`。

所以「一个传 `message_seq`、一个传 `message_id`」不是风格问题：它决定了
`reverse_order` 是否生效，进而决定断点恢复能不能往回走。**锚点是否落在末位，
就是字段对错的唯一可观测信号**——抽象层就是靠这个信号在运行时自检的。

### 1.4 锚点位置与旧实现的缺陷

两个后端在**字段正确**时一致：`reverse_order=True` 会返回升序（旧→新）、
包含锚点本身、锚点在**末位**。

原实现 `bot/app.py:recover_process` 是这样翻页的：

```python
cursor = anchor_of(messages[0])      # 新游标 = 本页最旧那条
if page_cursor:
    messages.pop(0)                   # 把它从本页摘掉
```

在真实形态（锚点在末位）下跑一遍，页码 41..60、锚点 60、断点 50：

```text
旧实现回放顺序: [60, 59, 58, 57, 56, 55, 54, 53, 52, 51]
是否回放了最新消息 60: True      <-- 锚点被当成待回放消息，重复处理
新游标: 41                       <-- 而 41 被 pop 掉，推到下一页才处理
```

两个缺陷：

1. **最新一条消息被重复回放**。它已经在实时流程里处理过，恢复时又来一遍。
2. **本页最旧那条被推迟到下一页**。一旦中途命中 break（找到断点 / 遇到 Bot
   自己的消息），那条就永远丢了。

根因是它把「锚点在第几位」写死了。改成按**游标身份**剔除之后，两个问题一起消失：

```python
if cursor:
    messages = [m for m in messages if not anchors_equal(m, cursor)]  # 按身份，不按位置
next_cursor = anchor_of(messages[0])     # 本页最旧，保证严格向更旧推进
```

配套的「游标不前进就停」保护，避免字段发错时空翻 `remaining=80` 页。

### 1.5 现状评估（这条差异到底影响了什么）

| 现象 | 是否协议差异 | 说明 |
|---|---|---|
| **`reverse_order` 一边生效一边失效** | **是（根因）** | 游标字段发对才生效；发错则被静默忽略，返回居中窗口。见 1.3 |
| 游标字段一边 `message_seq` 一边 `message_id` | 是（根因） | NapCat 只认 `message_seq`，SnowLuma 只认 `message_id`，配置写错就整体走偏 |
| 同一断点两边回放条数不同 | 部分是 | SnowLuma 入群前的历史不存在，翻到起点也找不到断点，只能报「未找到」 |
| 最新一条消息被重复回放 | 是（旧实现缺陷） | `pop(0)` 假设锚点在首位，真实在末位。见 1.4 |
| 两边的 `message_id` 对不上 | 是 | 本地哈希，跨后端不可比，只能靠归一化后的全局序号 |
| `count` 给大了返回空 | 是 | NapCat `count=500` 超时返回空，看起来像「没有历史」 |
| `is_reserve` 调参有效 | 否 | 两个后端都忽略这个参数（`message_id != 0` 时也测过，四种组合逐字节相同） |

### 1.6 单页条数上限

| | 表现 |
|---|---|
| NapCat | `count=100` 返回 95（过滤了部分消息）；`count>300` 极慢；`count=500` 直接超时空返回 |
| SnowLuma | `count=200/300/500` 都只返回该群可用的全部条数 |

「返回条数少于请求」不等于「历史到头」，不能据此停翻页；反过来把 `count`
开太大也会假装「没有历史」。

### 1.7 `get_forward_msg` 的层级

| | 顶层 id | 内层 id |
|---|---|---|
| NapCat | 5 条，其中嵌套那条**直接带 `data.content`**（34 条内联） | `retcode=200`「消息已过期或者为内层消息，无法获取转发消息」 |
| SnowLuma | 5 条，嵌套那条**只有 `data.id`** | 正常返回 34 条（一次只展开一层） |

所以：NapCat 只能靠内联 `content` 递归，**绝不能**拿内层 id 再发包；
SnowLuma 必须靠内层 id 再发包。同一个转发两边叶子数相同（38 条 / 2 层），
只是取法不同。

> SnowLuma 的 forward id 是长 base64 串（含 `+ / =`，76 字符），
> 截断或二次编码会得到 `download forward message payload is empty`。

#### `get_msg` 只认外层消息 id

`get_msg`（取单条消息）**只认外层**——就是群里那条消息自己的 id。聊天记录里的
**内层**消息 id（`get_forward_msg` 展开出来的那 6 条）拿去 `get_msg`，两端都是
`retcode=100`「message not found」；拿去 `get_forward_msg` 也取不到（SnowLuma 报
"download forward message payload is empty"，NapCat 报「消息已过期或者为内层消息」）。

所以凡是要「回头找到原来那条消息」的逻辑（审计群复搬靠标签文本里的 id 反查原消息），
**存的必须是外层 id**。SnowLuma 的 message_id 还可能是负数（int64 回绕，实测出过
`-2133034063`），解析正则必须允许负号。

### 1.8 搬运：「聊天记录套聊天记录」跨群时会坏

把一张「外层 5 条、其中第 1 条本身又是一张聊天记录」的卡片搬到**另一个群**，
三种搬法的结果（SnowLuma 2.2.0 实测）：

| 方式 | NapCat | SnowLuma |
|---|---|---|
| 整卡 `forward_group_single_msg` | ✅ 生成 `json(com.tencent.multimsg)`，内层是 `[聊天记录]`，可点开 | ✅ 内层仍是 `forward` 段，可点开（见下方版本说明） |
| 重建 + `{"type":"forward"}` 段 | ✅ 保留 `forward` 段 | ✅ 保留 `forward` 段 |
| 重建 + 嵌套 `node` | ⚠️ 退化成 `[卡片消息]` | ✅ 生成真正的双层聊天记录 |

> **版本分界（重要）**：SnowLuma ≤ 1.14.15 的 `forward_*_single_msg` 是「原样重发
> 缓存里那条消息」，把**源会话的 resid** 塞进目标会话，结果是一张空壳卡
> （桌面端打不开、手机端能进但图片加载失败）。上游 issue
> [#437](https://github.com/SnowLuma/SnowLuma/issues/437) 记录的正是这个，修复提交
> `201d10c`「rebuild merged forwards for the target chat」改成：展开内层树 →
> 对目标会话 `upload` 一次拿**新 resid** → 用新 resid 发送。本机 2.2.0-node 已生效。
> 因此 `relay_single_preserves_nested` 在 2.2.0 上是 `True`，老版本是 `False`。

仍然救不回来的情况：**内层已经是 xml 死卡片**。

```xml
<msg brief="[聊天记录]" m_fileName="MultiMsg_C125CE25-…"
     action="viewMultiMsg" tSum="2" serviceID="35">
```

`m_fileName` 指向**源会话**的临时多消息文件。同群内搬时缓存还在，看着正常；
**一旦跨群，文件失效**——这就是「只要不是本群搬到本群就打不开」的原因，连手机版
NTQQ 也只能渲染成 `[XML消息]`。

这种卡片的来源是**入站降级**：SnowLuma 收到别人发的合并转发时，本地就把它存成
`xml(viewMultiMsg)`（NapCat 存的是 `forward` + 内联 `content`）。存下来那一刻，
内容就只剩 `<title>` 摘要（`某某: [图片]`），`get_forward_msg` 再也拿不到 id，
之后搬多少次都只是把这张死卡再发一遍。**与后端能力无关，重建也救不回原图。**

结论：

- 内层是 `forward` 段 → 整卡搬即可（两个后端都会按目标会话重建 resid）；
- 内层是 `xml/json` 死卡，按 `m_resid` 分两种：

| `m_resid` | 能不能拿到内容 | 做法 |
|---|---|---|
| **为空** | 不能。只剩 `<title>` 摘要，连子消息 id 都没有 | **不搬**（`RelayPlan.action == "skip"`）。重建只能把摘要伪装成一条正常记录，接收方会以为看到的就是原内容 |
| **非空** | 不能直接读，但有 id 可追 | 重建：把 `<title>` 摘要内联成嵌套 node，自包含、不引用源会话临时文件。`RelayContent.lossy` 标出这次有损 |

`m_resid` 的取法有坑：**xml 段只有 `m_resid` 属性算入口**，同层 `data.resid` 是
`serviceID`（实测恒为 `35`），拿去 `get_forward_msg` 只会得到
`download forward message payload is empty`。json 段（`com.tencent.multimsg`）的
resid 在 `meta.detail.resid`。判定函数：`AbstractionLayer.relay.multimsg_resid_of()`。

反过来说，想**保住图**只有一条路：趁源会话缓存还没失效时就搬（那时
`get_forward_msg` 还能拿到真内容）。缓存一失效，剩下的就只有摘要。

跳过的日志长这样（`bot/services/forward/service.py`）：

```
[INFO] 不搬这张卡片：内层聊天记录卡的 m_resid 为空，只剩 <title> 摘要、没有可展开的子消息 id，搬过去也不完整，按策略不搬（目标 1054955587）
```

#### 为什么 NTQQ 自己的转发就能做到

NTQQ 客户端从来不做「把这张卡再发一遍」这件事，它做的是**复制内容 + 重新注册资源**：

1. 展开源 `resid` 的整棵树（含嵌套层，逐层展开）；
2. 对**目标会话**重新 upload 一次，服务器分配**新的** `MultiMsg_<uuid>` / resid，
   只有这个新 resid 才在目标会话的作用域内；
3. 发送时带新 resid。

关键在于 QQ 的资源有两个作用域：

| 资源 | 作用域 | 跨会话转发时 |
|---|---|---|
| 长消息 resid / `m_fileName` | **会话级**，绑定在收到它的那个会话 | 必须重新 upload，旧的一定失效 |
| 图片 / 视频 / 语音文件 | **账号级**，按 `md5`/`sha1` 寻址（OIDB 秒传） | 只传指纹即可，服务端命中同一份文件，不需要旧 URL |

所以 NTQQ 转发图片时不会去下载源图再上传，也不会复用源图的临时下载链接——它把
媒体指纹交给上传模块走 md5/sha1 快路径（SnowLuma 内部叫 `noByteFallback`），
服务端按 md5 命中同一个文件。这就是「转发后图片照样能看」的原因，也是
`xml m_fileName` 会坏而图片不会坏的原因：**坏的是会话级的那一半。**

> 实测佐证：源图 URL 带 `rkey=`（会轮换的临时凭证），当时 `GET` 返回
> HTTP 200 / 203 571 字节；但对同一条记录重建后，产出的是**全新 resid**，
> 4 张图全部可下载。而只传 `{"type":"image","data":{"file":"xxx.jpg"}}`
> （去掉 `url`）会直接失败：`ENOENT: no such file or directory, stat
> 'D:\Code\snowluna\A20F5ABF….jpg'` —— 说明 OneBot 的 `file` 字段是**本地路径**语义，
> 重建时 `url` 必须保留，由发送端下载后重新上传。

### 1.9 断点恢复：两个后端必须走相反方向

| | NapCat | SnowLuma |
|---|---|---|
| 无游标取「最新一条」 | 服务端真·最新 | 可能是**本地观测过**的最新（关闭登录历史同步时） |
| `message_id` 跨重启 | **会变**（每次重启重新分配） | 稳定（`SHA1(sequence:group:event)` 的 int32） |
| 恢复方向 | 先取最新，再**往回**翻到断点 | 从断点**往后**翻 |
| 依据 | `history_latest_is_authoritative=True` | `history_latest_is_authoritative=False` |

这两个条件正好互补：NapCat 的「最新」可信但断点里的 id 不可信，所以必须用
现取的 id 当游标；SnowLuma 的 id 可信但「最新」不可信，所以必须用断点当游标。

方向判断和翻页循环都在 `HistoryPager.replay()` 里：**给不给 `latest` 就是方向**
（给了 → 从最新往回翻；没给 → 从断点往后翻）。`bot/app.py` 只做剩下两件业务上的
事——按「新 → 旧」回放（遇到 Bot 自己的消息就停）和推进断点，不再自己写循环。

---

### 1.10 小程序卡片：`jumpUrl` 是页面路径，卡片不能改字

实测 2026-11-15，测试群 `1128873261`，SnowLuma 与 NapCat 两端都验过。

**生成**（`get_mini_app_ark`，真去问服务器，不是本地拼串）：

- QQ 侧命令是 `LightAppSvc.mini_app_share.AdaptShareInfo`，返回的卡带
  `config.token`（**内容签名**）和 `meta.detail_1.url`（`m.q.qq.com/a/s/<hash>`）。
- **`jumpUrl` 的语义是「小程序页面路径」，不是网址**：视频用
  `/pages/video/video?bvid=BV…`，番剧用 `/pages/pgcvideo/pgcvideo?epid=ep…`。
  传 b23 链接不报错，但点开只到 B 站**首页**。
- **`webURL` 才是 `qqdocurl` 的来源**。`qqdocurl` 是卡片里的网页兜底链接。
  NapCat 有 `webUrl` 参数但默认不传；SnowLuma 原版根本没发这个字段（它的
  protobuf 只写 6 个字段），所以两边出的卡都没有 `qqdocurl`，表现为
  「能进小程序、定位不到视频」。

**发送**（`{"type":"json","data":{"data":<卡片 json 字符串>}}`）：

- **返回的卡一个字都不能改**。`prompt` / `desc` / `preview` 任改一项，QQ
  **静默丢弃**：SnowLuma 报 `retcode=100 group message delivery was not
  confirmed`，日志 `result=0 sequence=null responseBytes=18`，历史里查不到。
- **`qqdocurl` 同样在签名覆盖范围内**（2026-11-15 补测）：拿一张能正常渲染的
  真卡，只把 `meta.detail_1.qqdocurl` 换成别的链接（b23 短链 / 全站 URL 都试过），
  发送立刻 `retcode=100` 被丢；同一时间窗内把同一张卡**原样**重发则成功
  （`message_id 952866865` / `986375953`，历史里是 `json` 段）。所以「转发壳 +
  自定义落点」这条路是死的——**真卡只能逐字节转发**。
- 顶层加未知字段（如 `needShareCallBack`）**不破坏签名**；重复发同一张也不拦。
- 想改文案/落点只能改**生成请求的参数**，不能事后改返回值。

**请求体字段编号**：1 extInfo / 2 appid / 3 title / 4 desc / 5 time(uint64) /
6 scene / 7 templateType / 8 businessType / 9 picUrl / 10 vidUrl / 11 jumpUrl /
12 iconUrl / 13 verType / 14 shareType / 15 versionId / 16 withShareTicket /
17 webURL / 18 appidRich / 19 template / 20 field20。`scene` 服务端不认（传 1 或
1036 都回显 0）——真卡的 1036 来自 B 站 App 的分享链路，机器人侧无法设置。

### 1.11 点开能不能播，取决于**对方的播放环境**，不是卡片

同一张卡：平板 / 部分机型正常出画；手机 QQ（内置 webview）里——

- 小程序卡：播放器壳在、时长也读到了（`00:00/01:05`），画面黑、点播放没反应；
- 图文卡：**有声音、没画面**。

后者是典型的「视频轨解码失败、音轨正常」：B 站按浏览器能力探测结果递
HEVC/AV1 流，QQ 的 webview 硬解合成起不来，解码线程静默退出。
判据是电脑端截图里能同时看到 B 站自绘进度条和浏览器原生 `<video controls>`
**两套控件**——说明跑的是 H5，不是原生小程序。

结论：**「点开能播」不是发卡方能保证的**。要保证能看只有两条路——
发视频文件（`media.send_bili_video`，按 BV 去重 + 失败静默），
或者卡片只当入口、让用户自己用浏览器打开。

**最终对照（2026-11-15 22:59，把卡片因素彻底排除）**：手机上

| 入口 | 结果 |
|---|---|
| 真卡（B 站 App 分享，`qqdocurl` 带 `bbid/ts`） | 黑屏/播不动 |
| 机器人签的卡（`jumpUrl` = BV 路径） | 同上 |
| 机器人签的卡 + `webURL` → `qqdocurl` | 同上 |
| 图文卡（`send_tuwen_ark`） | 有声音、无画面 |
| `?platform=html5`（强制 H.264 渐进流） | 同上，没用 |
| **把 b23 链接直接粘在聊天里点开（不经卡片、不经小程序）** | **同样播不了** |
| 同一台设备换成平板 / 换别的机型 | 正常出画 |

最后一行是关键判据：**连"直链"都不行**，说明和卡片、小程序、跳转方式全都无关，
纯粹是「这台手机上 QQ 内置 webview 播不了 B 站视频」。发卡方无解。

### 1.12 手拼卡片 JSON 一律被降级成文本

实测 2026-11-15，同一份 json 分别从 SnowLuma 与 NapCat 发到群里，结果一致：

| 发的什么 | 群里看到 |
|---|---|
| `com.tencent.miniapp_01`（`get_mini_app_ark` 现签） | 正常卡片 |
| `com.tencent.tuwen.lua`（`send_tuwen_ark` 现签） | 正常卡片 |
| 手拼 `com.tencent.structmsg` view=news | 纯文本「发送者版本过低，无法展示内容」 |
| 手拼 `tuwen.lua`（字段照抄真卡） | 同上 |
| 官方 ark 模板 23 / 24 / 37（`com.tencent.channel.robot` 等） | 直接丢包（`retcode=100`） |
| 任何删掉 `config.token` 的卡 | 文本「发送者版本过低」 |
| `config.token` 在、但 `prompt`/`desc`/`preview` 改过 | 直接丢包（`retcode=100`） |

结论：**`config.token` 必须由服务器签**，本地拼不出来；改字、删 token 都会被降级或丢弃。
想自定义内容只能改**生成请求的参数**。

因此图文卡只能走后端封装的动作：`BackendCapabilities.tuwen_ark_action`
（SnowLuma = `send_tuwen_ark`，NapCat = `None`）。`media.send_bili_news_card`
已按能力表实现，NapCat 上会直接返回 False，调用方需要自己回退。

官方 ark（模板 23/24/37）另有两条硬门槛：**要向平台运营申请权限**，
**链接要提前在「消息 URL 配置」报备**——即便换官方机器人也换不来「B 站小程序卡」。

## 2. 抽象层怎么用

```python
from AbstractionLayer import OneBotKernel, HistoryPager, ForwardExpander

kernel = OneBotKernel.http("http://127.0.0.1:3210", token, backend="snowluma")
await kernel.detect(group_id=20010)      # 运行时实测，不靠猜版本

pager = HistoryPager(kernel)

# 断点回放：方向由「给不给 latest」决定，业务层不用判断后端
result = await pager.replay(group_id, checkpoint=checkpoint_event)          # 从断点往后翻
result = await pager.replay(group_id, checkpoint=checkpoint_event, latest=latest_event)  # 从最新往回翻
# result.messages  断点之后的消息，升序，直接回放（不含断点本身）
# result.found     是否真的找到断点（往回翻时才有意义）
# result.reason    提前收尾的原因：游标没前进 / 字段不对 / 接口报错

# 只想翻一段、自己控制循环时用 walk
walk = await pager.walk(group_id, stop_at=checkpoint_event, count=20)

expander = ForwardExpander(kernel)
expansion = await expander.expand(forward_id)
# expansion.messages  摊平后的叶子消息，两边取法不同、结果相同

relay = ForwardRelay(kernel)
plan = plan_relay(kernel.capabilities, messages)
content = await relay.collect(forward_id)      # 或 collect(messages=已拉好的)
# plan.needs_rebuild  这次该不该整卡搬
# plan.skip           按策略不搬（内层多消息卡 m_resid 为空，内容拿不全）
# content.nodes       可直接交给 send_group_forward_msg
# content.lossy       内层有没有只能降级成摘要的卡片
```

接项目里现成的连接，不用改传输层：

```python
from AbstractionLayer import OneBotKernel, CallableTransport

kernel = OneBotKernel(CallableTransport(bot.api._call_api), capabilities=NAPCAT)
```

### 2.1 归一化契约

`HistoryPager` 保证：

1. 返回的消息**永远升序**；
2. 锚点**永远被剔除**（exclusive），翻页不重不漏；
3. 下一页游标从**边界那条消息**取，不按下标猜；
4. 单页条数自动夹到后端上限；
5. `is_reserve` 只在后端真支持时才发（两个后端都不支持）；
6. 游标字段不对时**直接拒发**并报错，而不是改发另一个字段；
7. 后端忽略了游标（返回方向不对）时**报错**，不静默继续翻；
8. 游标连续 `stall_limit`（默认 2）页不前进就收尾，不硬翻满 `max_pages`。

`HistoryPager.replay` 额外保证：断点**本身不出现**在结果里，且结果已按身份去重
（跨页重复不会回放两遍）。

`ForwardExpander` 保证：内联优先、没内联才发包、`max_depth` / `max_items` /
`visited` 三重防递归、既没内联也发不了包时留占位而不是崩。

### 2.2 可跨后端的身份：`MessageCursor`

```python
cursor = MessageCursor.from_message(message)
cursor.real_seq       # 全局序号（NapCat 取 real_seq，SnowLuma 取 message_seq）
cursor.real_id        # NapCat 的服务端消息 id：重启后仍指向同一条，断点靠它认
cursor.message_id     # 后端本地哈希，跨后端不可比
cursor.matches(other) # 优先比 real_seq，其次 real_id，再次 message_id
```

实测：同一批消息两边 `message_id` 0/8 一致、`message_seq` 0/8 一致，
但**归一化后的 `real_seq` 8/8 一致**。这就是能跨后端对齐的唯一字段。

### 2.3 已经接进项目的地方

`transport/api.py` 不再只看配置里声明的 `history_anchor`：

```python
api.configured_history_anchor   # 配置写的（可能写错）
await api.verify_history_cursor(group_id)   # 第一次用时实测一次，缓存
api.history_anchor              # 实发值：实测优先，测不出来才回退配置
```

`_history_params` 用的是 `api.history_anchor`，所以**所有历史调用**（不只是断点
恢复）都会用实测字段。配置写错时控制台会打一行：

```text
[历史游标] 配置写的是 message_id，实测应为 message_seq，已按实测值发包（NapCat）
```

`bot/app.py:recover_process` 的两处改动：

1. 进循环前 `await api.verify_history_cursor(group_id)`（探测失败只警告，不阻塞恢复）；
2. 锚点改为**按身份剔除**，新游标取本页最旧那条，并加「游标不前进就停」保护。

往回翻时 `replay` 会剔除起翻用的 `latest`（锚点永远 exclusive），但`latest`
**本身可能也是断点之后才到的消息**。`recover_process` 会把它补回来：

```python
# latest 不等于断点、也不在已收集的结果里 -> 追加到最末尾（它是最新的）
# 例外：latest 是 Bot 自己发的 -> 不回放，也不推进断点（可能已经处理过）
```

不补的话有个静默丢消息的坑：找到断点后断点会被推进到 `latest`，于是这条再也
不会被回放（旧实现就是这样）。

端到端实测（故意把两边配置写反）：

```text
napcat:   配置=message_id    实测=message_seq   -> 实发游标字段=['message_seq']
snowluma: 配置=message_seq   实测=message_id    -> 实发游标字段=['message_id']
```

### 2.4 媒体发送：回退顺序也是能力

群文件上传失败时该改发什么，两个后端不一样，所以顺序不写在业务代码里：

```python
# AbstractionLayer/kernal.py
media_send_order    = ("file", "video")   # "file"=群文件, "video"=视频消息（NTV2）
media_retry_times   = 1                   # 同一通道先重试几次
media_retry_delay   = 1.0                 # 每次重试前等几秒
video_max_bytes     = 100 * 1024 * 1024   # 超过就不试 video 通道，直接报错走链接兜底

# 普通文件（PDF / ZIP）没有 video 段可退，失败后改走闪传：
file_send_order     = ("file", "flash")   # "flash"=create_flash_task + send_flash_msg
flash_fileset_path  = "fileset_id"        # 从响应 data 里取 fileset id 的点分路径
```

`media/service.py:send_video_file` 按这张表循环，业务层只表达“要发一个视频”：

```python
await media.send_video_file(event, path)   # 内部：按 caps.media_send_order 逐个通道试
```

`bot/commands/jm.py` 对普通文件同理，按 `caps.file_send_order` 循环：文件通道带头退避重试，
失败再走闪传（QQ 的独立文件集服务，与群文件配额互不影响）。闪传的返回结构两个后端不一样：
SnowLuma 是 `data.fileset_id`，NapCat 透传内核结构 `data.createFlashTransferResult.fileSetId`，
所以取哪个字段由 `flash_fileset_path` 决定；未知后端（`GENERIC`）保守地只走 `("file",)`。

实测依据（2026-11-01，同一个 30.2 MB PDF、同一个测试群）：

```text
{"type":"file","data":{"file": pdf}}                 -> retcode=100, code=-134，0.6s
{"type":"file","data":{"file": pdf,"name":"a.pdf"}}  -> retcode=0，1.2s
```

`-134` 在 QQ 的 `UploadGroupFileRequest` 阶段就返回，字节根本没开始传，所以
不是网络 / 大小 / 风控问题，而是 **file 段缺 `name`**。SnowLuma 只凭 `file`
构造请求；NapCat 本来就把 `name` 当可选显示名。补名字统一放在抽象层：

```python
# AbstractionLayer/segments.py
normalize_outgoing(message)   # 给 file 段补 name（从本地路径 / URL 推断）
# transport/api.py:_call_api
# send_group_msg / send_private_msg 的 message 自动过一遍 normalize_outgoing
```

因此业务层照旧只写 `{"type":"file","data":{"file": path}}`，两个后端都兼容。
补完 `name` 后，JM 的群文件一次成功，重试和闪传只是真正的兜底。

新增后端时如果它还有第三种通道，在 `MediaService._send_segment` 里加一个分支 +
一个 `media_send_order` 取值即可，不用改调用方。

### 2.5 失败可见性：`strict_actions`

OneBot 的响应可能是 `{"status":"ok","retcode":-134}` —— 这个形状**必须**当失败：

```python
# AbstractionLayer/kernal.py
MUTATING_ACTIONS = frozenset({
    "send_group_msg", "send_private_msg",
    "send_group_forward_msg", "send_private_forward_msg",
    "forward_group_single_msg", "forward_friend_single_msg",
    "create_flash_task", "send_flash_msg",  # 闪传：上传 + 发卡片
    "group_poke", "friend_poke",          # 戳一戳按会话分成两个 action
    "delete_msg", "set_group_ban", "set_msg_emoji_like",
})
```

`OneBotAPI._call_api` 是唯一的收口：动作在这张表里就 `ensure_ok`，否则原样返回。

```python
await api.send_group_message(...)        # retcode != 0 -> OneBotAPIError
await api.get_group_list()               # retcode != 0 -> 原样返回，不抛
```

为什么不一刀切：`get_*` 的失败是**正常业务**（消息撤回了、查不到陌生人），
抛异常会让上层到处 try/except；而 `send_*` / `forward_*` 失败不会自己重来，
也没人补发，静默就是丢消息。

改动前实际踩过：SnowLuma 群文件上传返回 `retcode=-134`，旧代码当成功，于是
既不重试，也不会退化成「发下载链接」——用户什么也收不到。

**新增有副作用的动作时，把它加进 `MUTATING_ACTIONS`。** 否则失败仍然是静默的。

---

## 3. 调试命令

```bash
python -m AbstractionLayer.cli probe   --backend napcat        # 实测能力
python -m AbstractionLayer.cli probe   --backend snowluma
python -m AbstractionLayer.cli diff    --group 20010      # 两后端逐字段对比
python -m AbstractionLayer.cli sweep   --group 20010      # 哪个字段真能翻页
python -m AbstractionLayer.cli history --backend napcat --count 10
python -m AbstractionLayer.cli recover --backend snowluma --from seq:220 --count 5
python -m AbstractionLayer.cli forward --backend napcat --id 7700868210009142942
python -m AbstractionLayer.cli forward --backend snowluma --message 1753375808
python -m AbstractionLayer.cli face    --backend snowluma              # 列出收藏表情
python -m AbstractionLayer.cli face    --backend napcat --send 不够色情 --group 20010
python -m AbstractionLayer.cli call    --backend napcat --action get_login_info
```

游标写法：`seq:184`（全局序号，只能用于比对）、`id:501493501`、
`raw:184`（原始 `message_seq`）、裸数字（= 全局序号）。

---

## 3.1 收藏表情（账号自定义表情）

实测于 2026-11-01，NapCat `:3000` / SnowLuma `:3210`，同一个测试群 `1132313813`。

### 两个后端给的数据不一样

`fetch_custom_face_detail` 的返回：

| 字段 | SnowLuma | NapCat |
|---|---|---|
| `emoji_id` | ✅（= `resId`） | ❌ **没有这个字段** |
| `resId` | ✅ | ✅ |
| `url` / `md5` | ✅ | ✅ |
| `desc`（用户备注） | ✅ | ✅ |
| `uin` / `emoId` | ❌ | ✅（账号内序号） |
| `emoPath` / `thumbPath` | ❌ | ✅（本地路径，可能不存在） |

**主键用 `desc`（用户自己写的备注）**，因为它跨账号稳定：同一个表情在两个账号上
`md5` 与 `desc` 完全一致，但 `emoji_id` / `resId` 带的是**各自的 uin 前缀**
（`3122793026_0_0_0_<MD5>_0_0` vs `3521775163_0_0_0_<MD5>_0_0`）。
`desc` 为空（用户没起名）时退回 `md5`——同样是内容哈希，一样跨账号稳定。

### 发送：一条专用动作 + 一条兜底路

| 后端 | 走的路 | 实测 |
|---|---|---|
| SnowLuma | 专用动作 `send_custom_face(emoji_id, group_id\|user_id)` | ✅ `emoji_id` 可传完整 ID，也可传 32 位 MD5 |
| NapCat | 兜底 `send_group_msg` + image 段 | ✅ 没有这个动作（`retcode=200`），但 image 段一样能保住外观 |

兜底段的关键是 `sub_type`：

```json
{"type": "image", "data": {"file": "<表情URL>", "sub_type": 1, "summary": "[动画表情]"}}
```

同一个表情 URL，**带** `sub_type=1` 回读是 `sub_type=1 / summary="[动画表情]"`（收藏表情），
**不带**就是 `sub_type=0 / summary="[图片]"`（普通图片）。SnowLuma 的
`send_custom_face` 内部发的也就是这个段，所以兜底路在两个后端上都成立。

### 怎么用

```python
from AbstractionLayer.face import CustomFaceService

service = CustomFaceService(kernel)
library = await service.refresh()          # 抓收藏表情
face = library.find("不够色情")             # 按备注名找
await service.send("不够色情", group_id=20001)   # 按备注名发
```

### 已经接进 bot 的哪几处

- `transport/api.py`：`fetch_custom_faces()` / `send_custom_face(key, …)`；
- `bot/app.py` `start()`：启动时抓一次，并进表情包库（`_sync_custom_faces`）；
- `messaging/meme.py`：新增 `customs` 类型，并入后模型就能像用静态表情包一样
  写 `<meme>备注名</meme>`——CQ 码里带 `sub_type=1`，转消息段时不能丢。

抓取时机有个坑：Agent 是在后台线程里建提示词的，而提示词里的表情目录必须在
建 Agent 之前就位（模型得知道能写哪些名字）。所以 `BotApp` 用一个
`threading.Event` 把两件事串起来：`_sync_custom_faces()` 合并完才置位，
Agent 线程等这个信号（最多等 15 秒，超时就按现有表情包照常加载）。

---

## 4. 新增后端时

1. 跑 `probe --url http://新地址 --backend generic`，看实测出来的
   `history_cursor` / `history_global_seq_field` 对不对；
2. 跑 `sweep` 确认只有 `history_cursor` 指定的字段能翻页；
3. 如果和现有两种形状都不同，在 `kernal.py` 加一个
   `BackendCapabilities` 常量并在 `_classify` 里加判据；
4. 在 `tests/test_abstraction_layer.py` 的 `FakeBackend` 里复刻它的怪癖，
   再补一条 `walk` 断言；
5. 实测一遍媒体通道（群文件 / 视频消息），把结果写进 `media_send_order` ——
   顺序写错只是多失败一次，但会拖慢每次发文件；
6. 实测一遍收藏表情：`face --backend 新端 --send <备注名>`，看发出去的是不是
   收藏表情（回读 `sub_type=1`）而不是普通图片；有专用动作就填
   `custom_face_send_action`，没有就留空走 image 段兜底。

**不要**在上层写 `if backend == "napcat"`。差异只允许出现在
`BackendCapabilities` 的数据里。
