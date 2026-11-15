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
         ├─ image_filter.js  # 图片过滤门（过滤器卡片 + 详情，内置三种模板）
         ├─ prompts.js       # 提示词与回复
         ├─ keywords.js      # 关键词 / 命令（命令为 key 的卡片 + 详情二级页面）
         └─ advanced.js      # 原始 JSON
```

## 二级页面（master-detail）模式

`agent.js` / `keywords.js` / `image_filter.js` 都是「卡片网格 → 点卡片进详情」
的二级页面：模块级变量记录当前选中的 key（模型 id / 命令下标 / 过滤 id），
渲染函数据此决定画目录还是画详情，点击后 `rerender(container)` 重画。
编辑直接就地写回 `state.data`，保存时不需要从 DOM 收集。

新增这类页面照抄 `image_filter.js`：`render(panel)` 建容器 → `renderTabContent()`
按选中状态分发 → 详情页顶部放「← 返回」按钮。

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
- 跨源访问要先在 `web_panel.cors_origins` 里加入
  `http://127.0.0.1:8080`；白名单之外的前端会被拒绝。

## token

面板设置了 `web_panel.token` 时，两种方式都要带 token：

```text
http://127.0.0.1:8390/?token=你的token
http://127.0.0.1:8080/?api=http://127.0.0.1:8390&token=你的token
```

`api.js` 从 `?token=` 读取后立刻从地址栏移除，只放进当前标签页的
`sessionStorage`，不会写进 `localStorage`。请求同时发送
`Authorization: Bearer` 与 `X-Panel-Token`（兼容旧后端）。

## 修改前端

- 只改 `assets/js/tabs/*.js` 里对应标签页；
- 新增字段：在 `state.js` 的 `FIELDS` 里加一条 `{path, label, type, ...}`，
  支持 `text / number / bool / textarea / intlist / objlist`；
- 没有构建步骤，改完刷新浏览器即可（必要时 Ctrl+F5 清缓存）。
