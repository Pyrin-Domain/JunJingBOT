# 前端（独立静态站点）

无构建、无 CDN、离线可用。纯 ES module，直接由浏览器加载。

```text
frontend/
├─ index.html
└─ assets/
   ├─ style.css
   └─ js/
      ├─ app.js          # 启动 / 标签切换 / 保存 / 热加载
      ├─ api.js          # fetch 封装（token / api base / 错误提示）
      ├─ state.js        # 全局状态 + 字段定义
      ├─ dom.js          # el() / 表单 / 列表编辑器
      └─ tabs/
         ├─ overview.js      # 运行状态
         ├─ connection.js    # 连接方式（config.json）
         ├─ paths.js         # 路径映射（environment.json）
         ├─ basic.js         # 基础参数 + 面板自身
         ├─ agent.js         # AI Agent
         ├─ interject.js     # 群聊插话
         ├─ forward.js       # 搬运与去重
         ├─ image_forward.js # 图片批量转发字段
         ├─ prompts.js       # 提示词与回复
         ├─ keywords.js      # 关键词 / 命令
         └─ advanced.js      # 原始 JSON
```

## 方式一：由后端托管（推荐）

```powershell
python run.py                 # Bot + 面板
# 或只起面板，不连 NapCat
python -m web.standalone --port 8390
```

打开 <http://127.0.0.1:8390/>。
后端会把：

- `GET /` → `frontend/index.html`
- `GET /static/js/app.js` → `frontend/assets/js/app.js`（兼容旧引用）
- `GET /static/style.css` → `frontend/assets/style.css`
- `GET /assets/...` → `frontend/assets/...`

## 方式二：独立静态服务器（只调前端）

```powershell
cd frontend
python -m http.server 8080
```

然后打开：

```text
http://127.0.0.1:8080/?api=http://127.0.0.1:8390
```

- `api` 参数指定后端地址，会存进 `localStorage`，下次不用再带；
- 不填 `api` 时默认同源（即假定页面和 API 由同一个后端提供）；
- 后端已经给 `/api/*` 加了 CORS 与 `OPTIONS` 预检，可以跨源调用。

## token

面板设置了 `web_panel.token` 时，两种方式都要带 token：

```text
http://127.0.0.1:8390/?token=你的token
http://127.0.0.1:8080/?api=http://127.0.0.1:8390&token=你的token
```

`api.js` 依次从 `?token=` → `localStorage.panel_token` → `sessionStorage.panel_token`
读取，并同时发送 `Authorization: Bearer` 与 `X-Panel-Token`（兼容旧后端）。

## 修改前端

- 只改 `assets/js/tabs/*.js` 里对应标签页；
- 新增字段：在 `state.js` 的 `FIELDS` 里加一条 `{path, label, type, ...}`，
  支持 `text / number / bool / textarea / intlist / objlist`；
- 没有构建步骤，改完刷新浏览器即可（必要时 Ctrl+F5 清缓存）。
