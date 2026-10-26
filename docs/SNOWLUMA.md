# SnowLuma 接入

SnowLuma 是一个 OneBot v11 实现，协议层与 NapCat 基本一致。本项目把它当作
**另一种后端**：连接、消息收发、合并转发、群管理都用同一套代码，差异集中在
`config/backends.py` 的 `SNOWLUMA` 档案里。

> 说明：SnowLuma 的源码许可是「源码可见、非商业使用」。本项目只做协议兼容，
> 不复制它的源码；部署前请自行阅读并遵守其许可。

---

## 1. 最小配置

### 1.1 反向 `ws_re`（推荐）

Bot 起 WebSocket 服务，SnowLuma 主动连过来：

`config/config.json`：

```json
{
  "type": "ws_re",
  "backend": "snowluma",
  "access_token": "换成你的 token",
  "ws_reverse_host": "127.0.0.1",
  "ws_reverse_port": 8085,
  "ws_host": "127.0.0.1",
  "ws_port": 3001
}
```

SnowLuma 侧：

- WebSocket 客户端地址：`ws://127.0.0.1:8085`
- token：与 `access_token` 一致（`Authorization: Bearer ...` 或
  `?access_token=...` 都支持）

### 1.2 正向 `ws`

Bot 主动连 SnowLuma 的 WebSocket 服务端：

`config/config.json`：

```json
{
  "type": "ws",
  "backend": "snowluma",
  "access_token": "换成你的 token",
  "ws_host": "127.0.0.1",
  "ws_port": 3001
}
```

`config/sender_config.json`（发 API 的独立连接）：

```json
{
  "type": "ws",
  "backend": "snowluma",
  "access_token": "换成你的 token",
  "ws_host": "127.0.0.1",
  "ws_port": 3000
}
```

`sender_config.json` 不写 `backend` 时会跟随 `config.json`；面板保存时会
自动同步。

### 1.3 用面板改

网页面板「连接方式」页有「协议后端」下拉框：

- 选 `SnowLuma` 会写入 `"backend": "snowluma"`；
- 页面会显示 SnowLuma 的默认端口和历史游标提示；
- 保存后仍需重启 Bot（连接在启动时建立）。

---

## 2. 与 NapCat 的差异

| 项目 | NapCat | SnowLuma |
|---|---|---|
| 协议 | OneBot v11 | OneBot v11 |
| 历史接口游标 | `message_seq` | `message_id`（有符号 int32，可能为负） |
| WS 服务端默认端口 | `3001` | `3001` |
| HTTP 服务端默认端口 | `3000` | `3000` |
| Bot 自己发出的消息 | `post_type: "message"`（按配置） | `post_type: "message_sent"` |
| token | query / Bearer | query / Bearer |

### 2.1 历史游标

这是唯一需要业务层感知的差异。项目用 `core/anchors.py` 统一处理：

- 每条消息同时保留 `message_seq` / `message_id` / `real_id`；
- `transport/api.py` 按后端档案只把当前后端认识的字段发给历史接口；
- 断点恢复、长区间翻页、上下文补全比较终点时，用
  `anchors_equal` / `anchor_matches` 同时比对所有游标。

因此同一个断点文件在 NapCat 和 SnowLuma 之间切换时不会因为
`message_seq` 对不上而失效。

### 2.2 自己发出的消息

SnowLuma 会把 Bot 自己发出的消息作为 `post_type: "message_sent"` 推送。
`transport/listener.py` 只处理 `post_type == "message"`，所以不会出现
「自己回复自己」的循环。

### 2.3 合并转发

SnowLuma 的转发节点同时接受 `user_id` / `nickname` 和 `uin` / `name`。
项目现有代码使用 `uin` / `name`，两边都兼容，不需要改转发逻辑。

---

## 3. 切换后端

1. 改 `config/config.json` 的 `"backend"`（或面板下拉框）。
2. 确认端口 / token 与 SnowLuma 侧一致。
3. 重启 Bot。
4. 看启动日志：

   ```text
   [启动] 后端：SnowLuma（snowluma）
   [WS] 反向服务器已启动 ws://127.0.0.1:8085，等待 SnowLuma 连接…
   ```

5. 面板「连接方式」页应显示：

   ```text
   协议后端   SnowLuma
   历史游标   message_id
   ```

---

## 4. 常见问题

**Q：历史消息只能拉一页 / 断点恢复找不到消息？**

确认 `backend` 是 `snowluma`。如果仍写成 `napcat`，历史接口会发
`message_seq`，SnowLuma 不认这个游标。

**Q：面板保存后没变化？**

连接在 Bot 启动时建立，保存只改文件；需要重启 Bot。

**Q：token 一直报不匹配？**

- 反向模式：SnowLuma 的 WebSocket 客户端里填的 token 要与
  `access_token` 完全一致；
- 正向模式：`config.json` 和 `sender_config.json` 的 token 都要对；
- `ws_reverse_strict_token=false` 时 token 不一致只会警告并放行，
  生产环境建议打开严格校验。

**Q：能和 NapCat 混用吗？**

事件连接和发送连接应使用同一个后端。面板保存时会把
`sender_config.json` 的 `backend` 同步成 `config.json` 的值。

---

## 5. 相关代码

| 文件 | 作用 |
|---|---|
| `config/backends.py` | 后端档案、别名、默认端口、历史游标 |
| `config/connection.py` | 解析 `backend` / `protocol` / `adapter` |
| `core/anchors.py` | 双游标表示与比较 |
| `transport/api.py` | 按后端选择历史接口参数 |
| `transport/connection.py` | 通用 OneBot WebSocket 连接 |
| `transport/listener.py` | 只处理 `message` 事件 |
| `messaging/history.py` | 双游标翻页 / 断点区间 |
| `web/validation.py` | 面板保存时的后端校验 |
