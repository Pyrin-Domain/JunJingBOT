# SnowLuma 容器化（WSL 里跑协议端，Windows 上跑 run.py）

把 SnowLuma 装进 WSL 的 Docker，Windows 侧的 `run.py` 保持不动、继续当 Bot 主进程。
两者用 **OneBot v11 反向 WebSocket（`ws_re`）** 连接：`run.py` 当服务端监听 8085，
容器里的 SnowLuma 主动连过来，一条连接同时收事件、发 API。

```text
┌─ Windows ────────────────────────────────────────────┐
│  run.py（BotApp + 网页面板 8390）                     │
│  反向 WS 服务端  0.0.0.0:8085  ←── 等 SnowLuma 连入   │
└──────────────────────────▲───────────────────────────┘
                           │ ws://host.docker.internal:8085/
                           │ (token 双向校验)
┌─ WSL2 ───────────────────┴───────────────────────────┐
│  docker: snowluma 容器                               │
│    OneBot v11 + Linux QQ + noVNC                     │
│    6081 noVNC   5099 WebUI   3000 HTTP   3001 WS     │
│    /botdata ← /mnt/d/Code/JungJingNew（文件互通）     │
└──────────────────────────────────────────────────────┘
```

选反向而不是正向的理由：项目默认就是 `ws_re`；一条连接搞定事件与 API；
容器只需要出站，不用把 3000/3001 暴露出去；断线由 SnowLuma 侧按
`reconnectIntervalMs` 自动重连。

---

## 0. 已经帮你改好的东西

| 文件 | 改动 |
|---|---|
| `deploy/snowluma/docker-compose.yml` | 新增：SnowLuma 容器编排（含 `host.docker.internal` 注入、三份数据卷、`/botdata` 挂载） |
| `deploy/snowluma/.env` | 新增：镜像、挂载路径、OneBot token |
| `deploy/snowluma/setup-windows.ps1` | 新增：Windows 防火墙放行 8085 + 环境体检 |
| `deploy/snowluma/check-wsl.sh` | 新增：WSL 里一键体检（容器状态、到 Windows 的连通性、wsClients 配置） |
| `config/config.json` | 改成 `backend=snowluma`、`ws_reverse_host=0.0.0.0`、token、严格校验 |
| `config/environment.json` | 改成 `backend_env=linux` + `custom` + `D:\Code\JungJingNew → /botdata` |

原配置备份在同目录：`config.json.bak-napcat`、`environment.json.bak-napcat`。

> `ws_reverse_host` 从 `127.0.0.1` 改成 `0.0.0.0` 是**必须**的：容器不是本机进程，
> 连不到只监听回环的 8085。

---

## 1. 准备 WSL 和 Docker

```bash
wsl -l -v                     # 确认有发行版（没有就 wsl --install -d Ubuntu）
wsl                           # 进去
```

WSL 里装 Docker（已有可跳过）：

```bash
docker version                # 能打出 Server 段就说明 daemon 在跑
docker compose version        # 需要 v2 的 compose 插件
```

没有的话（Ubuntu）：

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER      # 之后重开一个 shell 免 sudo
sudo service docker start          # WSL 没 systemd 时用这个
```

确认 Docker 可用后，把项目目录映射到 WSL 的路径记下来：`/mnt/d/Code/JungJingNew`。
（如果你改了盘符或目录，同步改 `.env` 的 `BOT_MOUNT`。）

---

## 2. 起容器

```bash
cd /mnt/d/Code/JungJingNew/deploy/snowluma
docker compose up -d
docker compose logs -f --tail 100
```

看到 `Xvfb` / `QQ` / `snowluma` 起来就对了。首次启动会输出两组密码，记下来：

```bash
# 远程桌面（noVNC）密码
docker logs snowluma 2>&1 | grep -E "远程桌面密码|remote desktop password" | tail -n 1

# WebUI 临时密码（只在全新数据卷首次启动时打印一次）
docker logs snowluma 2>&1 | grep -E "临时密码|initial credentials" | tail -n 1
```

> 如果你在 `.env` 里自己填了 `VNC_PASSWD`，就用自己那个，日志里不会再有随机密码。

---

## 3. 扫码登录 QQ

1. Windows 浏览器打开 <http://127.0.0.1:6081/>（WSL2 会把端口转发到 localhost）。
2. 输入上面的远程桌面密码。
3. 桌面里 QQ 已经自己起来了，扫码登录。

> **已知限制**：容器停止时 QQ 是崩溃退出的，登录态来不及落盘。所以
> `docker compose restart`、`wsl --shutdown`、重启电脑之后，大概率要重新扫码。
> 想减少折腾：别频繁重启容器；`docker restart` 前先确认你手边有手机。

---

## 4. 给 SnowLuma 配反向 WebSocket

打开 WebUI <http://127.0.0.1:5099/>，用 `admin` + 日志里的临时密码登录，
进入**连接配置 / 反向 WebSocket（`wsClients`）**，新增一条：

| 字段 | 值 |
|---|---|
| name | `windows-bot` |
| enabled | `true` |
| url | `ws://host.docker.internal:8085/` |
| role | `Universal` |
| accessToken | `cacf58f21475900bd19cda3608bf4d8c`（与 `.env` / `config/config.json` 一致） |
| messageFormat | `array` |
| reportSelfMessage | `false` |
| reconnectIntervalMs | `5000` |

保存即热重载，**不要 restart 容器**（会掉登录）。

等价的手改配置（一般不必）：`/app/data/config/onebot_<uin>.json` 里的
`networks.wsClients` 追加同一份对象，改完热重载或重启才生效。

```json
{
  "name": "windows-bot",
  "enabled": true,
  "url": "ws://host.docker.internal:8085/",
  "role": "Universal",
  "accessToken": "cacf58f21475900bd19cda3608bf4d8c",
  "messageFormat": "array",
  "reportSelfMessage": false,
  "reconnectIntervalMs": 5000
}
```

`host.docker.internal` 能指向 Windows，靠的是 compose 里的
`extra_hosts: host.docker.internal:host-gateway`——WSL 内自装的 Docker Engine
不自带这条 DNS。连不上时换备选地址：`ws://<WSL 的 vEthernet(WSL) 地址>:8085/`，
再不行用 Windows 局域网 IP（`setup-windows.ps1` 会把可用地址都列出来）。

---

## 5. 放行 Windows 入站 8085

WSL 的流量打 Windows 宿主要过防火墙，管理员 PowerShell 跑一次：

```powershell
powershell -ExecutionPolicy Bypass -File D:\Code\JungJingNew\deploy\snowluma\setup-windows.ps1
```

脚本会：加一条入站 TCP 8085 规则 → 检查 8085 是否已被占用/绑在回环 →
列出可以填进 WebUI 的地址 → 顺便体检 WSL 和 Docker。

撤销：`... setup-windows.ps1 -Remove`。

---

## 6. 启动 run.py

Windows 侧照旧：

```powershell
cd D:\Code\JungJingNew
.venv_win\Scripts\python.exe run.py
```

正常应该看到：

```text
[启动] 后端：SnowLuma（snowluma）
[启动] 连接方式：反向(ws_re)（ws://0.0.0.0:8085）
[WS] 反向服务器已启动 ws://0.0.0.0:8085，等待 SnowLuma 连接…
[WS] SnowLuma 已连接（反向 ws_re） 172.x.x.x:xxxxx
```

最后一行出现就通了。面板 <http://127.0.0.1:8390/> 的「连接方式」页应显示
协议后端 `SnowLuma`、历史游标 `message_id`。

---

## 7. 一键体检

```bash
# WSL 里
cd /mnt/d/Code/JungJingNew/deploy/snowluma
bash check-wsl.sh
```

它依次检查：docker daemon → 容器状态 → 容器到 `host.docker.internal:8085`
的 TCP 连接 → 已注册的 `wsClients` → 最近的 OneBot 连接日志 → 两组密码。
输出里出现 FAIL 就照提示修。

再从 Bot 自己的视角确认（Windows PowerShell）：

```powershell
(Invoke-RestMethod http://127.0.0.1:8390/api/status).connection
```

期望看到 `connected : True`、`backend : snowluma`、`history_anchor : message_id`。
最后在群里发一条指令，机器人有回复就是全链路打通了。

---

## 8. 排错对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| `run.py` 一直停在「等待 SnowLuma 连接」 | WebUI 里没加 `wsClients`，或 url 写错 | 按第 4 步补，`check-wsl.sh` 能看到有没有配 |
| 容器日志 `ECONNREFUSED` | Windows 8085 没监听，或只绑了 127.0.0.1 | 确认 `run.py` 在跑；`config.json` 的 `ws_reverse_host` 是 `0.0.0.0` |
| 容器日志 `TIMEOUT` | 防火墙丢包 | 管理员跑 `setup-windows.ps1` |
| `ENOTFOUND host.docker.internal` | 缺 `extra_hosts` 或用的是老 docker | 确认真在用本目录的 compose 文件；`docker --version` ≥ 20.10 |
| 连上秒断 / 401 / token 不匹配 | 两边 token 不一致 | `.env`、`config/config.json`、WebUI 三处对齐（`ws_reverse_strict_token=true` 时会直接拒） |
| 消息能收，历史翻页只有一页 | `backend` 还是 napcat | `config/config.json` 必须是 `"backend": "snowluma"` |
| 发图/发视频失败「识别URL失败」 | 协议端读不到 Windows 路径 | 图片已自动转 `base64://`；其它文件走 `/botdata`（compose 已挂 `D:\Code\JungJingNew`），`environment.json` 的 `path_map` 已配 |
| noVNC 打不开 6081 | 用了代理 / 端口冲突 | 关掉系统代理再开；`docker compose ps` 看端口映射 |
| 重启后 QQ 要求重新扫码 | 已知限制（登录态没落盘） | 重新扫码，见第 3 步 |
| 容器白屏 / QQ 崩溃 | `shm_size` 不够 | 保持 compose 里的 `shm_size: 1gb` |

---

## 9. 回滚到原来的 NapCat 配置

```bash
cd D:/Code/JungJingNew/config
cp config.json.bak-napcat config.json
cp environment.json.bak-napcat environment.json
```

容器可以留着不用，或 `docker compose down`（**别加 `-v`**，加卷会丢登录态）。

---

## 10. 安全提醒

- 3000/3001 端口已映射到宿主，同局域网可访问。compose 里的端口可以改成
  `"127.0.0.1:3001:3001"` 只给本机用（改完 `docker compose up -d` 重建容器不会丢数据卷）。
- OneBot token 现在是 `cacf58f21475900bd19cda3608bf4d8c`，泄露等于把 QQ 交给别人。
  要换就三处一起换：`.env` 的 `ONEBOT_TOKEN`、`config/config.json` 的 `access_token`、
  SnowLuma WebUI 的 `accessToken`。
- `config/config.json` 本来就在 `.gitignore` 里，别把 token 提交上去。
- 生产建议把镜像 tag 钉死（`.env` 的 `SNOWLUMA_IMAGE=motricseven7/snowluma:v x.y.z`），
  `latest` 升级可能带行为变化。
