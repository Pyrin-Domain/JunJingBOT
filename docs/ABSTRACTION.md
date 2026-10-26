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
- 内层是 `xml/json` 死卡 → 一律重建，把 `<title>` 摘要内联成嵌套 node，
  自包含、不引用源会话临时文件。`RelayContent.lossy` 会标出这次有损；
- 想**保住图**，只能在源会话缓存还没失效时就搬（那时能 `get_forward_msg` 到真内容）。

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

---

## 2. 抽象层怎么用

```python
from AbstractionLayer import OneBotKernel, HistoryPager, ForwardExpander

kernel = OneBotKernel.http("http://127.0.0.1:3210", token, backend="snowluma")
await kernel.detect(group_id=20010)      # 运行时实测，不靠猜版本

pager = HistoryPager(kernel)
walk = await pager.walk(group_id, stop_at=checkpoint_event, count=20)
# walk.messages  断点之后的消息，升序，直接回放
# walk.found     是否真的找到断点
# walk.reached_start  是否翻到历史起点（SnowLuma 入群前的历史不存在时会出现）

expander = ForwardExpander(kernel)
expansion = await expander.expand(forward_id)
# expansion.messages  摊平后的叶子消息，两边取法不同、结果相同

relay = ForwardRelay(kernel)
plan = plan_relay(kernel.capabilities, messages)
content = await relay.collect(forward_id)      # 或 collect(messages=已拉好的)
# plan.needs_rebuild  这次该不该整卡搬
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
7. 后端忽略了游标（返回方向不对）时**报错**，不静默继续翻。

`ForwardExpander` 保证：内联优先、没内联才发包、`max_depth` / `max_items` /
`visited` 三重防递归、既没内联也发不了包时留占位而不是崩。

### 2.2 可跨后端的身份：`MessageCursor`

```python
cursor = MessageCursor.from_message(message)
cursor.real_seq       # 全局序号（NapCat 取 real_seq，SnowLuma 取 message_seq）
cursor.message_id     # 后端本地哈希，跨后端不可比
cursor.matches(other) # 优先比 real_seq，其次 message_id
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

端到端实测（故意把两边配置写反）：

```text
napcat:   配置=message_id    实测=message_seq   -> 实发游标字段=['message_seq']
snowluma: 配置=message_seq   实测=message_id    -> 实发游标字段=['message_id']
```

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
python -m AbstractionLayer.cli call    --backend napcat --action get_login_info
```

游标写法：`seq:184`（全局序号，只能用于比对）、`id:501493501`、
`raw:184`（原始 `message_seq`）、裸数字（= 全局序号）。

---

## 4. 新增后端时

1. 跑 `probe --url http://新地址 --backend generic`，看实测出来的
   `history_cursor` / `history_global_seq_field` 对不对；
2. 跑 `sweep` 确认只有 `history_cursor` 指定的字段能翻页；
3. 如果和现有两种形状都不同，在 `kernal.py` 加一个
   `BackendCapabilities` 常量并在 `_classify` 里加判据；
4. 在 `tests/test_abstraction_layer.py` 的 `FakeBackend` 里复刻它的怪癖，
   再补一条 `walk` 断言。

**不要**在上层写 `if backend == "napcat"`。差异只允许出现在
`BackendCapabilities` 的数据里。
