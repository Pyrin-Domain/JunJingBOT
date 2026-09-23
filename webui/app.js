'use strict';

/* ══════════════════════════ 状态 ══════════════════════════ */
const state = {
  data: null,          // 当前配置（与表单双向绑定）
  meta: null,          // 配置文件信息
  actions: [],         // 可用 action 名（来自后端 _action_registry）
  imgsDir: '',         // 图片目录（{imgs_dir} 替换目标）
  dirty: false,        // 有未保存修改
  ws: null,            // NapCat 连接设置（config/config.json）
  groupList: [],       // NapCat 群列表（群号列表编辑器 / 下拉框用）
  groupNames: {},      // 群号 -> 群名
  groupsError: '',     // 群列表没取到时的原因
  listEditors: [],     // 已渲染的列表编辑器（群列表刷新后重画名字与下拉框）
  tab: 'status',
  statusTimer: null,   // 运行状态自动刷新
  token: new URLSearchParams(location.search).get('token')
      || sessionStorage.getItem('panel_token') || '',
};
if (state.token) sessionStorage.setItem('panel_token', state.token);

const TABS = [
  { id: 'status', label: '运行状态' },
  { id: 'connection', label: '连接方式' },
  { id: 'basic', label: '基础参数' },
  { id: 'forward', label: '搬运与去重' },
  { id: 'text', label: '提示词与回复' },
  { id: 'keywords', label: '关键词 / 命令' },
  { id: 'raw', label: '原始 JSON' },
];

const ACTION_HINTS = {
  send_message: 'params: {"message": "回复文本"}；也可 {"message": ["A", "B"]}（随机挑一条）；可选 {"filter": {"type": "ban", "data": ["群号"]}} 屏蔽某些群',
  send_img: 'params: {"img_addr": "{imgs_dir}/文件名.gif", "summary": "图片说明"}',
  get_long_history_test: '无参数：调试用，打印长历史拉取结果',
  set_tokenizer: '无参数：切换「发送前分词」开关',
  ocr: '无参数：对「被回复的那条消息」里的图片做 OCR',
  export: '无参数：export -g 群号 / export -u QQ号（配合回复一条消息，把区间内历史搬过去）',
  fabric: '无参数：按 [qq:xxx][name:xxx][context:xxx] 造一条转发节点发出去',
  get_music: '无参数：点歌，取消息里 [歌名] 作为搜索词',
  img_forward_create_task: '无参数：img_fd -g 群 / -u QQ / -a，再用 -b / -e 标出区间',
};

const FIELDS = {
  basic: [
    { path: 'master_qq', label: '主人 QQ', type: 'text', hint: '等于这个 QQ 的用户会被当作「主人」写进 AI 上下文' },
    { path: 'heartbeat.min_minutes', label: '心跳最短间隔（分钟）', type: 'number', hint: '在这两个数之间随机取值，防止掉线' },
    { path: 'heartbeat.max_minutes', label: '心跳最长间隔（分钟）', type: 'number' },
    { path: 'agent.enable', label: '启用 AI Agent', type: 'bool', hint: '关闭后只走关键词与搬运（省 token）；改动需重启 Bot' },
    { path: 'message.extraconfig.tokenizer', label: '发送前分词（tokenizer）', type: 'bool' },
    { path: 'message.extraconfig.recent', label: '@ 时带上最近聊天记录', type: 'bool', hint: '关掉可以省 token' },
  ],
  forward: [
    { path: 'auto_forward.enable', label: '启用自动搬运', type: 'bool' },
    { path: 'auto_forward.dedup', label: '启用内容去重（搬过就不再搬）', type: 'bool', hint: '关闭后同一内容会被反复搬运' },
    { path: 'auto_forward.groupid_list', label: '监听的来源群', type: 'intlist', item: 'group', full: true, hint: '只有这些群里发的转发卡片才会被搬运；可用「从群列表添加」按群名选' },
    { path: 'auto_forward.target_groupid', label: '搬运的目标群', type: 'intlist', item: 'group', full: true, hint: '会跳过消息原本所在的群' },
    { path: 'auto_forward.userid_list', label: '私聊来源 QQ（userid_list）', type: 'intlist', item: 'qq', full: true, hint: '私聊消息只有这些 QQ 会被搬运；留空 = 不搬任何私聊。也认 user_id_list 这种写法' },
    { path: 'auto_forward.task_userid', label: '另外私聊转发的好友（task_userid）', type: 'intlist', item: 'qq', full: true, hint: '搬运时除了目标群，还会私聊转发给这些好友' },
    { path: 'auto_forward.auto_img_forward_from_bot.enable', label: '机器人发的图片也自动搬运', type: 'bool' },
    {
      path: 'auto_forward.auto_img_forward_from_bot.bot_list',
      label: '视为机器人的 QQ（可限定生效范围）',
      type: 'objlist',
      full: true,
      idKey: 'bot_id',
      hint: '每个机器人一条：group = 只在指定群生效（群号留空 = 所有群聊）、private = 只私聊、all = 群聊与私聊都算。这些 QQ 发的图片会被自动搬运',
      empty: () => ({ bot_id: '', type: 'group', group_id: [] }),
      subFields: [
        { key: 'bot_id', label: '机器人 QQ', kind: 'id' },
        {
          key: 'type', label: '生效范围', kind: 'select',
          options: [['group', 'group（只看群聊）'], ['private', 'private（只看私聊）'], ['all', 'all（群聊 + 私聊）']],
        },
        // 只有群聊需要指定范围：私聊的发信人就是这个机器人自己，没什么可填的
        { key: 'group_id', label: '生效群号（留空 = 所有群聊）', kind: 'intlist', item: 'group', showWhen: (o) => o.type !== 'private' },
      ],
    },
    { path: 'image_forward.auto_target_group_ids', label: 'img_fd -a 的默认目标群', type: 'intlist', item: 'group', full: true },
  ],
  text: [
    { path: 'prompts.group_context', label: '群聊上下文模板', type: 'textarea', full: true, hint: '占位符：{group_id} {user_id} {message_id} {ated_user} {self_id} {master_id}' },
    { path: 'prompts.private_context', label: '私聊上下文模板', type: 'textarea', hint: '占位符：{user_id}' },
    { path: 'prompts.history_context', label: '历史消息模板', type: 'textarea', hint: '占位符：{history}' },
    { path: 'prompts.history_command_regex', label: '「历史」指令前缀正则', type: 'text' },
    { path: 'replies.forwarded_already', label: '「搬过了」图片说明', type: 'text' },
    { path: 'replies.network_upgrade', label: '校园网升级提示文案', type: 'text' },
  ],
};

/* ══════════════════════════ 小工具 ══════════════════════════ */
const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function getPath(obj, path) {
  return path.split('.').reduce((node, key) => (node == null ? undefined : node[key]), obj);
}

function setPath(obj, path, value) {
  const parts = path.split('.');
  let node = obj;
  for (let i = 0; i < parts.length - 1; i += 1) {
    if (typeof node[parts[i]] !== 'object' || node[parts[i]] === null) node[parts[i]] = {};
    node = node[parts[i]];
  }
  node[parts[parts.length - 1]] = value;
}

function parseNumList(text) {
  return String(text)
    .split(/[,\s，、;；]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => (/^\d+$/.test(s) ? Number(s) : s));
}

function fmtNumList(value) {
  return Array.isArray(value) ? value.join(', ') : '';
}

function jsonError(text) {
  try {
    JSON.parse(text);
    return null;
  } catch (err) {
    return String(err.message || err);
  }
}

/* ══════════════════════════ 与后端通信 ══════════════════════════ */
async function api(path, { method = 'GET', body = null } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (state.token) headers['X-Panel-Token'] = state.token;
  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === null ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    return { status: 0, body: { ok: false, errors: [`网络错误：${err}`] } };
  }
  let payload = null;
  try {
    payload = await res.json();
  } catch (err) {
    payload = { ok: false, errors: [`返回内容不是 JSON（HTTP ${res.status}）`] };
  }
  return { status: res.status, body: payload };
}

/* ══════════════════════════ 提示条 / 脏标记 ══════════════════════════ */
function setBanner(kind, title, lines = []) {
  const banner = $('#banner');
  banner.className = `banner ${kind}`;
  banner.innerHTML = '';
  banner.append(el('div', { text: title }));
  if (lines.length) {
    const ul = el('ul');
    for (const line of lines) ul.append(el('li', { text: line }));
    banner.append(ul);
  }
}

function clearBanner() {
  const banner = $('#banner');
  banner.className = 'banner hidden';
  banner.innerHTML = '';
}

function markDirty() {
  state.dirty = true;
  const badge = $('#stateBadge');
  badge.className = 'state dirty';
  badge.textContent = '未保存';
}

function markClean(text = '已保存') {
  state.dirty = false;
  const badge = $('#stateBadge');
  badge.className = 'state saved';
  badge.textContent = text;
}

/* ══════════════════════════ 字段渲染 ══════════════════════════ */
function renderField(field) {
  const value = getPath(state.data, field.path);
  const wrapper = el('div', { class: `field${field.full ? ' full' : ''}` });
  const inputId = `f_${field.path.replace(/\./g, '_')}`;
  wrapper.append(el('label', { for: inputId, text: field.label }));

  let control;
  if (field.type === 'bool') {
    control = el('input', {
      type: 'checkbox',
      id: inputId,
      checked: value ? 'checked' : null,
      onchange: (e) => { setPath(state.data, field.path, e.target.checked); markDirty(); },
    });
    wrapper.append(el('div', { class: 'switch' }, [control, el('span', { text: value ? '已开启' : '已关闭' })]));
    control.addEventListener('change', (e) => {
      e.target.parentElement.lastChild.textContent = e.target.checked ? '已开启' : '已关闭';
    });
  } else if (field.type === 'textarea') {
    control = el('textarea', {
      id: inputId,
      rows: 3,
      oninput: (e) => { setPath(state.data, field.path, e.target.value); markDirty(); },
    });
    control.value = value == null ? '' : String(value);
    wrapper.append(control);
  } else if (field.type === 'intlist') {
    // 群号 / QQ 号这种多值参数：表格化（一行一项，可增可删）
    wrapper.append(renderListEditor(field));
  } else if (field.type === 'objlist') {
    // 对象列表：每个对象自带若干子参数（如机器人 QQ + 生效范围 + 群号列表）
    wrapper.append(renderObjectList(field));
  } else {
    control = el('input', {
      type: field.type === 'number' ? 'number' : 'text',
      id: inputId,
      oninput: (e) => {
        const raw = e.target.value;
        setPath(state.data, field.path, field.type === 'number' ? (raw === '' ? null : Number(raw)) : raw);
        markDirty();
      },
    });
    control.value = value == null ? '' : String(value);
    if (field.type === 'number') control.step = 'any';
    wrapper.append(control);
  }
  if (field.hint) wrapper.append(el('div', { class: 'hint', text: field.hint }));
  return wrapper;
}

function renderFieldGroup(items) {
  const grid = el('div', { class: 'grid' });
  for (const field of items) grid.append(renderField(field));
  return grid;
}

/* ══════════════════════════ 各标签页 ══════════════════════════ */
function renderStatusPanel(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '运行状态' }),
    el('p', { class: 'hint', text: '每 10 秒自动刷新；这里只读，不能改' }),
    el('div', { id: 'statusBody', class: 'kv', text: '加载中…' }),
  ]));
}

function renderStatusInto(status) {
  const body = $('#statusBody');
  if (!body) return;
  body.innerHTML = '';
  const rows = [];
  const add = (key, value, cls) => rows.push([key, value, cls]);

  const panel = status.panel || {};
  add('面板地址', panel.url || '-');
  add('访问鉴权', panel.auth ? '已设置 token' : '无 token（仅本机访问）');
  const cfg = status.config || {};
  add('配置文件', cfg.path || '-');
  add('文件更新时间', cfg.mtime || (cfg.exists ? '-' : '文件不存在（使用内置默认值）'));

  const conn = status.connection || {};
  if (conn.label || conn.error) {
    add('NapCat 连接',
      conn.error ? `读取失败：${conn.error}`
        : `${conn.label}　${conn.endpoint || ''}`.trim(),
      conn.connected ? 'on' : (conn.connected === false ? 'off' : null));
    if (conn.api_endpoint) {
      add('发送连接（API）', `${conn.api_endpoint}　${conn.api_connected ? '已连接' : '未连接'}`,
        conn.api_connected ? 'on' : 'off');
    }
  }

  if (!status.bot_running) {
    add('Bot 状态', '未连接（面板在独立模式下运行，只能改参数）');
  } else {
    const bot = status.bot || {};
    add('Bot 状态', '运行中', 'on');
    add('自身 QQ 号', bot.user_id ?? '未获取');
    add('AI Agent', bot.agent_enabled
      ? (bot.agent_loaded ? '已启用并加载完成' : '已启用，后台加载中…')
      : '已在配置里关闭', bot.agent_enabled ? 'on' : 'off');
    add('校园网助手', bot.campus_loaded ? '已加载' : '未加载', bot.campus_loaded ? 'on' : 'off');
    add('心跳保活', bot.heartbeat_running ? '运行中' : '未运行', bot.heartbeat_running ? 'on' : 'off');
    add('自动搬运', bot.auto_forward_enabled ? '已启用' : '已关闭', bot.auto_forward_enabled ? 'on' : 'off');
    add('关键词规则', `${bot.keyword_count ?? 0} 条`);
    add('去重指纹库', bot.dedup_store_ready ? '已打开' : '尚未使用（惰性创建）');
    add('图片搬运任务', (bot.image_tasks || []).length ? bot.image_tasks.join(', ') : '无');
    const cps = bot.check_points || {};
    const keys = Object.keys(cps).filter((k) => k !== 'error');
    add('断点群数', `${keys.length} 个`);
  }

  for (const [key, value, cls] of rows) {
    body.append(el('dt', { text: key }));
    body.append(el('dd', {}, [
      cls ? el('span', { class: `pill ${cls}`, text: String(value) }) : String(value),
    ]));
  }

  const bot = status.bot || {};
  const cps = bot.check_points || {};
  const cpKeys = Object.keys(cps).filter((k) => k !== 'error');
  if (cpKeys.length) {
    const table = el('table');
    table.append(el('tr', {}, ['群号', 'message_id', 'real_id', '最后发言 QQ'].map((h) => el('th', { text: h }))));
    for (const key of cpKeys) {
      const cp = cps[key] || {};
      table.append(el('tr', {}, [
        el('td', { class: 'mono', text: key }),
        el('td', { class: 'mono', text: cp.message_id ?? '-' }),
        el('td', { class: 'mono', text: cp.real_id ?? '-' }),
        el('td', { class: 'mono', text: cp.user_id ?? '-' }),
      ]));
    }
    body.append(el('div', { class: 'field full' }));
    body.closest('.kv').after(el('div', { style: 'margin-top:14px' }, [
      el('div', { class: 'hint', text: '断点（每个群最后处理到的消息）', style: 'margin-bottom:6px' }),
      table,
    ]));
  } else if (bot.check_points && bot.check_points.error) {
    body.closest('.kv').after(el('div', { class: 'hint', text: `断点读取失败：${bot.check_points.error}` }));
  }
}

async function refreshStatus() {
  const res = await api('/api/status');
  if (!res.body || !res.body.ok) {
    renderStatusInto({ bot_running: false, panel: {}, config: {} });
    return;
  }
  renderStatusInto(res.body);
}

function renderBasicPanel(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '基础参数' }),
    el('p', { class: 'hint', text: '主人 QQ、心跳、AI 开关、消息处理开关' }),
    renderFieldGroup(FIELDS.basic),
  ]));
  panel.append(renderPanelConfigCard());
}

function renderForwardPanel(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '自动搬运与去重' }),
    el('p', { class: 'hint', text: '来源群里的转发卡片 → 搬到目标群；去重按「内容指纹 + 标记的群」判定' }),
    renderFieldGroup(FIELDS.forward),
  ]));
}

function renderTextPanel(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: 'AI 提示词' }),
    el('p', { class: 'hint', text: '{xxx} 是占位符，运行时按事件内容替换；改坏了 AI 会看不懂上下文' }),
    renderFieldGroup(FIELDS.text),
  ]));
}

function renderPanelConfigCard() {
  const wp = state.data.web_panel || {};
  return el('div', { class: 'card' }, [
    el('h2', { text: '网页参数面板' }),
    el('p', { class: 'hint', text: '面板自身的设置；改动需要重启 Bot 才会生效' }),
    el('div', { class: 'grid' }, [
      renderField({ path: 'web_panel.enable', label: '启用参数面板', type: 'bool' }),
      renderField({ path: 'web_panel.host', label: '监听地址', type: 'text', hint: '默认 127.0.0.1，只允许本机访问' }),
      renderField({ path: 'web_panel.port', label: '监听端口', type: 'number' }),
      renderField({ path: 'web_panel.token', label: '访问 token', type: 'text', hint: '留空表示不需要；监听非本机地址时强烈建议设置' }),
    ]),
    el('div', { class: 'hint', text: wp.token ? '当前已设置 token，浏览器需通过 ?token=xxx 打开面板' : '当前未设置 token' }),
  ]);
}

/* ── 关键词规则 ── */
function ruleFromDom(row) {
  const regex = $('input.regex', row).value;
  const action = $('select', row).value;
  const paramsText = $('textarea.params', row).value.trim();
  let params = {};
  if (paramsText) {
    try {
      params = JSON.parse(paramsText);
    } catch (err) {
      return { error: `params 不是合法 JSON：${err.message}`, regex, action };
    }
  }
  if (params === null || typeof params !== 'object' || Array.isArray(params)) {
    return { error: 'params 必须是一个 JSON 对象，例如 {"message": "在的"}', regex, action };
  }
  const rule = { regex, action };
  if (Object.keys(params).length) rule.params = params;
  return rule;
}

function collectKeywordsFromDom() {
  const rows = [...document.querySelectorAll('#ruleList .rule')];
  const rules = [];
  const errors = [];
  rows.forEach((row, index) => {
    const parsed = ruleFromDom(row);
    if (parsed.error) errors.push(`第 ${index + 1} 条：${parsed.error}`);
    else rules.push(parsed);
  });
  return { rules, errors };
}

function renderKeywordRow(rule, index) {
  const row = el('div', { class: 'rule', dataset: { index: String(index) } });

  const regexInput = el('input', {
    class: 'regex',
    type: 'text',
    placeholder: '正则，例如 (男娘|南梁|nn)',
    oninput: () => { markDirty(); runKeywordTest(); },
  });
  regexInput.value = rule.regex || '';

  const select = el('select', {
    onchange: () => { markDirty(); updateRuleTip(row); },
  });
  const actions = state.actions.length ? state.actions : Object.keys(ACTION_HINTS);
  for (const action of actions) {
    select.append(el('option', { value: action, text: action, selected: action === rule.action ? 'selected' : null }));
  }
  if (rule.action && !actions.includes(rule.action)) {
    select.append(el('option', { value: rule.action, text: `${rule.action}（未注册）`, selected: 'selected' }));
  }

  const head = el('div', { class: 'rule-head' }, [
    el('span', { class: 'idx', text: `#${index + 1}` }),
    regexInput,
    select,
    el('button', { class: 'btn mini', type: 'button', text: '↑', onclick: () => moveRule(row, -1) }),
    el('button', { class: 'btn mini', type: 'button', text: '↓', onclick: () => moveRule(row, 1) }),
    el('button', { class: 'btn mini', type: 'button', text: '删除', onclick: () => { row.remove(); renumberRules(); markDirty(); runKeywordTest(); } }),
  ]);

  const params = el('textarea', {
    class: 'params',
    rows: 2,
    placeholder: 'params（JSON，可留空）',
    oninput: () => { markDirty(); updateRuleTip(row); },
  });
  params.value = rule.params ? JSON.stringify(rule.params, null, 0) : '';

  const tip = el('div', { class: 'rule-tip' });

  row.append(head);
  row.append(el('div', { class: 'rule-body' }, [params]));
  row.append(el('div', { class: 'rule-foot' }, [tip]));
  updateRuleTip(row);
  return row;
}

function updateRuleTip(row) {
  const select = $('select', row);
  const params = $('textarea.params', row);
  const tip = $('.rule-tip', row);
  const action = select ? select.value : '';
  const text = params ? params.value.trim() : '';
  const parts = [ACTION_HINTS[action] || '未知 action：运行时会被跳过'];
  if (text) {
    const err = jsonError(text);
    if (err) {
      tip.className = 'rule-tip bad';
      tip.textContent = `params 不是合法 JSON：${err}`;
      return;
    }
  }
  tip.className = 'rule-tip';
  tip.textContent = parts.join('　·　');
}

function moveRule(row, delta) {
  const list = $('#ruleList');
  const rows = [...list.children];
  const index = rows.indexOf(row);
  const target = index + delta;
  if (target < 0 || target >= rows.length) return;
  if (delta < 0) list.insertBefore(row, rows[target]);
  else list.insertBefore(rows[target], row);
  renumberRules();
  markDirty();
  runKeywordTest();
}

function renumberRules() {
  [...document.querySelectorAll('#ruleList .rule')].forEach((row, index) => {
    const idx = $('.idx', row);
    if (idx) idx.textContent = `#${index + 1}`;
  });
}

function runKeywordTest() {
  const input = $('#kwTest');
  if (!input) return;
  const text = input.value;
  const rows = [...document.querySelectorAll('#ruleList .rule')];
  let hitCount = 0;
  let badCount = 0;
  for (const row of rows) {
    const regex = $('input.regex', row).value;
    row.classList.remove('hit', 'bad');
    const tip = $('.rule-tip', row);
    let matched = false;
    if (regex) {
      try {
        matched = text.length > 0 && new RegExp(regex, 'i').test(text);
      } catch (err) {
        row.classList.add('bad');
        badCount += 1;
        if (tip) {
          tip.className = 'rule-tip bad';
          tip.textContent = `正则写错了：${err.message}`;
        }
        continue;
      }
    }
    if (matched) {
      row.classList.add('hit');
      hitCount += 1;
      if (tip) {
        tip.className = 'rule-tip hit';
        tip.textContent = '这条会命中当前测试文本';
      }
    } else {
      updateRuleTip(row);
    }
  }
  const summary = $('#kwSummary');
  if (summary) {
    summary.textContent = text
      ? `命中 ${hitCount} 条${badCount ? `，${badCount} 条正则写错` : ''}`
      : '输入一段消息，测试哪些规则会命中（同一段文本可能命中多条）';
  }
}

function renderKeywordsPanel(panel) {
  const list = el('div', { class: 'rules', id: 'ruleList' });
  const rules = Array.isArray(state.data.keyword_actions) ? state.data.keyword_actions : [];
  rules.forEach((rule, index) => list.append(renderKeywordRow(rule, index)));

  const testInput = el('input', { type: 'text', id: 'kwTest', placeholder: '测试文本，例如：我想听[晴天]', oninput: () => runKeywordTest() });

  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '关键词 / 命令' }),
    el('p', { class: 'hint', text: `命中规则后调用 action（send_message / send_img / export / fabric / get_music…）；图片目录：${state.imgsDir || '-'}` }),
    el('div', { class: 'tester' }, [testInput, el('span', { class: 'rule-tip', id: 'kwSummary' })]),
    list,
    el('div', { class: 'row', style: 'margin-top:12px' }, [
      el('button', {
        class: 'btn',
        type: 'button',
        text: '+ 添加规则',
        onclick: () => {
          list.append(renderKeywordRow({ regex: '', action: state.actions[0] || 'send_message', params: {} }, list.children.length));
          markDirty();
        },
      }),
      el('span', { class: 'hint', text: '正则会用「忽略大小写」匹配（与 Bot 行为一致）' }),
    ]),
  ]));
}

function renderRawPanel(panel) {
  const textarea = el('textarea', { id: 'rawJson', rows: 24, spellcheck: 'false' });
  textarea.value = JSON.stringify(state.data, null, 2);

  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '原始 JSON' }),
    el('p', { class: 'hint', text: '直接编辑整份配置；改完先点「应用到表单」，再点右上角保存' }),
    el('div', { class: 'row', style: 'margin-bottom:10px' }, [
      el('button', {
        class: 'btn', type: 'button', text: '应用到表单',
        onclick: () => {
          const text = $('#rawJson').value;
          try {
            const parsed = JSON.parse(text);
            if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
              throw new Error('顶层必须是一个 JSON 对象');
            }
            state.data = parsed;
            markDirty();
            setBanner('ok', '已把 JSON 应用到表单，别忘了点「保存并热加载」');
            renderAll();
          } catch (err) {
            setBanner('err', 'JSON 解析失败', [String(err.message || err)]);
          }
        },
      }),
      el('button', {
        class: 'btn', type: 'button', text: '从表单刷新',
        onclick: () => {
          $('#rawJson').value = JSON.stringify(state.data, null, 2);
          setBanner('ok', '已用当前表单内容刷新 JSON');
        },
      }),
      el('button', {
        class: 'btn', type: 'button', text: '格式化',
        onclick: () => {
          const text = $('#rawJson').value;
          try {
            $('#rawJson').value = JSON.stringify(JSON.parse(text), null, 2);
          } catch (err) {
            setBanner('err', 'JSON 解析失败', [String(err.message || err)]);
          }
        },
      }),
    ]),
    textarea,
  ]));
}

/* ══════════════ 多值参数（群号 / QQ 号）的表格编辑器 ══════════════ */
function normalizeListItem(text) {
  const raw = String(text == null ? '' : text).trim();
  if (raw === '') return '';
  return /^\d+$/.test(raw) ? Number(raw) : raw;
}

/* 一行一项：序号 + 输入框 +（群名）+ 删除；支持回车新增、批量粘贴、从群列表选 */
function renderListEditor(field) {
  const box = el('div', { class: 'list-editor' });
  const rowsBox = el('div', { class: 'list-rows' });
  const foot = el('div', { class: 'list-actions' });
  const footStatus = el('span', { class: 'list-status' });
  const batchBox = el('div', { class: 'list-batch hidden' });
  const batchText = el('textarea', {
    rows: 3,
    placeholder: '每行一个，或用逗号 / 空格分隔；从别处拷来的一串群号可以直接粘进来',
  });
  const isGroup = field.item === 'group';
  const itemLabel = isGroup ? '群号' : (field.item === 'qq' ? 'QQ 号' : '值');

  // field.get / field.set 用于嵌在其它结构里的子列表（如机器人卡片里的群号列表），
   // 没有就按 field.path 读写 state.data
  const getList = () => {
    const value = field.get ? field.get() : getPath(state.data, field.path);
    return Array.isArray(value) ? value : [];
  };
  const commit = (list) => {
    if (field.set) field.set(list);
    else setPath(state.data, field.path, list);
    markDirty();
  };

  function decorate() {
    const list = getList();
    const counts = new Map();
    for (const item of list) counts.set(String(item == null ? '' : item), (counts.get(String(item == null ? '' : item)) || 0) + 1);
    let blank = 0; let dup = 0; let bad = 0;
    const rows = rowsBox.querySelectorAll('.list-row');
    rows.forEach((row, index) => {
      const key = String(list[index] == null ? '' : list[index]);
      const isBlank = key === '';
      const isDup = !isBlank && counts.get(key) > 1;
      const isBad = !isBlank && !/^\d+$/.test(key);
      if (isBlank) blank += 1;
      if (isDup) dup += 1;
      if (isBad) bad += 1;
      row.classList.toggle('bad', isBlank || isDup || isBad);
      const name = $('.list-name', row);
      if (name) name.textContent = isBlank ? '' : (isGroup ? (state.groupNames[key] || '（没查到群名）') : '');
    });
    const notes = [`共 ${list.length} 项`];
    if (blank) notes.push(`${blank} 项空着`);
    if (dup) notes.push(`${dup} 项重复`);
    if (bad) notes.push(`${bad} 项不是数字`);
    if (isGroup && state.groupsError) notes.push(state.groupsError);
    footStatus.textContent = notes.join('　·　');
    footStatus.className = `list-status${blank || dup || bad ? ' warn' : ''}`;
  }

  function buildFoot() {
    foot.innerHTML = '';
    foot.append(el('button', {
      class: 'btn mini', type: 'button', text: '＋ 添加一项',
      onclick: () => addItem('', null),
    }));
    foot.append(el('button', {
      class: 'btn mini ghost', type: 'button', text: '批量添加',
      onclick: () => toggleBatch(),
    }));
    if (isGroup && state.groupList.length) {
      const have = new Set(getList().map(String));
      const rest = state.groupList.filter((g) => !have.has(String(g.group_id)));
      if (rest.length) {
        const picker = el('select', {
          class: 'list-picker',
          onchange: (e) => { const v = e.target.value; if (v) addItem(Number(v), null); },
        });
        picker.append(el('option', { value: '', text: `＋ 从群列表添加（还有 ${rest.length} 个）` }));
        rest.slice().sort((a, b) => String(a.group_name).localeCompare(String(b.group_name), 'zh'))
          .forEach((g) => picker.append(el('option', {
            value: String(g.group_id),
            text: `${g.group_name}（${g.group_id}）`,
          })));
        foot.append(picker);
      }
    }
    foot.append(el('button', {
      class: 'btn mini ghost', type: 'button', text: '清空',
      onclick: () => {
        const list = getList();
        if (list.length && !window.confirm(`清空「${field.label}」里的 ${list.length} 项？`)) return;
        commit([]); renderRows();
      },
    }));
    foot.append(footStatus);
  }

  function renderRows() {
    rowsBox.innerHTML = '';
    const list = getList();
    list.forEach((item, index) => {
      const row = el('div', { class: 'list-row' });
      row.append(el('span', { class: 'list-idx', text: String(index + 1) }));
      row.append(el('input', {
        type: 'text',
        class: 'list-input',
        placeholder: itemLabel,
        value: item == null ? '' : String(item),
        oninput: (e) => {
          const arr = getList();
          arr[index] = normalizeListItem(e.target.value);
          commit(arr);
          decorate();
        },
        onkeydown: (e) => {
          if (e.key === 'Enter') { e.preventDefault(); addItem('', index + 1); }
        },
      }));
      if (isGroup) row.append(el('span', { class: 'list-name' }));
      row.append(el('button', {
        class: 'btn mini ghost list-del', type: 'button', text: '删除',
        onclick: () => { const arr = getList(); arr.splice(index, 1); commit(arr); renderRows(); },
      }));
      rowsBox.append(row);
    });
    if (!list.length) {
      rowsBox.append(el('div', { class: 'hint', text: '还没有内容：点「＋ 添加一项」，或者用「批量添加」粘一串进来' }));
    }
    buildFoot();
    decorate();
  }

  function addItem(value, at) {
    const arr = getList();
    const index = at == null ? arr.length : Math.max(0, Math.min(at, arr.length));
    arr.splice(index, 0, value);
    commit(arr);
    renderRows();
    const inputs = rowsBox.querySelectorAll('.list-input');
    if (inputs[index]) inputs[index].focus();
  }

  function toggleBatch(force) {
    const show = force === undefined ? batchBox.classList.contains('hidden') : force;
    batchBox.classList.toggle('hidden', !show);
    if (show) batchText.focus();
  }

  batchBox.append(batchText, el('div', { class: 'list-actions' }, [
    el('button', {
      class: 'btn mini primary', type: 'button', text: '添加这些',
      onclick: () => {
        const items = batchText.value.split(/[\s,，、;；]+/).map((s) => s.trim())
          .filter(Boolean).map(normalizeListItem);
        if (items.length) {
          const arr = getList();
          for (const item of items) arr.push(item);
          commit(arr);
          batchText.value = '';
        }
        toggleBatch(false);
        renderRows();
      },
    }),
    el('button', { class: 'btn mini ghost', type: 'button', text: '取消', onclick: () => toggleBatch(false) }),
  ]));

  box.append(rowsBox, foot, batchBox);
  renderRows();
  // 群列表刷新后只重画底部与群名，不动正在编辑的输入框
  state.listEditors.push(() => {
    if (!box.isConnected) return false;   // 已被重建掉的（如机器人卡片里的嵌套编辑器）自动丢弃
    buildFoot(); decorate();
    return true;
  });
  return box;
}

/* ══════════ 对象列表：每个对象带自己的子参数（如「机器人 QQ + 范围 + 群号列表」） ══════════ */
/* 规范成对象：兼容列表里直接写裸 QQ 号这种老写法 */
function normalizeObjectEntry(field, raw) {
  const idKey = field.idKey || 'id';
  const entry = Object.assign({}, typeof field.empty === 'function' ? field.empty() : {});
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) Object.assign(entry, raw);
  else if (raw !== null && raw !== undefined && String(raw).trim() !== '') entry[idKey] = normalizeListItem(raw);
  for (const sub of field.subFields || []) {
    if (sub.kind === 'intlist' && !Array.isArray(entry[sub.key])) entry[sub.key] = [];
  }
  return entry;
}

function renderObjectList(field) {
  const box = el('div', { class: 'list-editor' });
  const rowsBox = el('div', { class: 'obj-rows' });
  const foot = el('div', { class: 'list-actions' });
  const footStatus = el('span', { class: 'list-status' });
  const idKey = field.idKey || 'id';

  const getList = () => {
    const value = getPath(state.data, field.path);
    return Array.isArray(value) ? value : [];
  };

  function decorate() {
    const list = getList();
    const counts = new Map();
    for (const item of list) {
      const key = String((item || {})[idKey] ?? '').trim();
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    let blank = 0; let dup = 0;
    rowsBox.querySelectorAll('.obj-card').forEach((card, index) => {
      const key = String((list[index] || {})[idKey] ?? '').trim();
      const isBlank = key === '';
      const isDup = !isBlank && counts.get(key) > 1;
      if (isBlank) blank += 1;
      if (isDup) dup += 1;
      card.classList.toggle('bad', isBlank || isDup);
    });
    const notes = [`共 ${list.length} 个`];
    if (blank) notes.push(`${blank} 个没填 QQ`);
    if (dup) notes.push(`${dup} 个 QQ 重复`);
    footStatus.textContent = notes.join('　·　');
    footStatus.className = `list-status${blank || dup ? ' warn' : ''}`;
  }

  function buildFoot() {
    foot.innerHTML = '';
    foot.append(el('button', {
      class: 'btn mini', type: 'button', text: '＋ 添加一个机器人',
      onclick: () => {
        const arr = getList().map((item) => normalizeObjectEntry(field, item));
        arr.push(normalizeObjectEntry(field, null));
        setPath(state.data, field.path, arr); markDirty(); renderRows();
      },
    }));
    foot.append(el('button', {
      class: 'btn mini ghost', type: 'button', text: '清空',
      onclick: () => {
        const list = getList();
        if (list.length && !window.confirm(`清空「${field.label}」里的 ${list.length} 项？`)) return;
        setPath(state.data, field.path, []); markDirty(); renderRows();
      },
    }));
    foot.append(footStatus);
  }

  function renderRows() {
    rowsBox.innerHTML = '';
    const list = getList();
    list.forEach((raw, index) => {
      const entry = normalizeObjectEntry(field, raw);
      if (entry !== raw) list[index] = entry;   // 顺手把老写法（裸 QQ 号）升级成对象
      rowsBox.append(renderObjectCard(field, entry, index, { renderRows, decorate }));
    });
    if (!list.length) {
      rowsBox.append(el('div', { class: 'hint', text: '还没有内容：点「＋ 添加一个机器人」加一条' }));
    }
    buildFoot();
    decorate();
  }

  box.append(rowsBox, foot);
  renderRows();
  return box;
}

function renderObjectCard(field, entry, index, hooks) {
  const card = el('div', { class: 'obj-card' });
  const head = el('div', { class: 'obj-head' });
  head.append(el('span', { class: 'list-idx', text: String(index + 1) }));

  for (const sub of field.subFields || []) {
    if (sub.kind === 'id') {
      head.append(el('span', { class: 'obj-label', text: sub.label }));
      head.append(el('input', {
        type: 'text', class: 'list-input obj-id', placeholder: 'QQ 号',
        value: entry[sub.key] == null ? '' : String(entry[sub.key]),
        oninput: (e) => { entry[sub.key] = normalizeListItem(e.target.value); markDirty(); hooks.decorate(); },
      }));
    }
  }

  head.append(el('button', {
    class: 'btn mini ghost list-del', type: 'button', text: '删除',
    onclick: () => {
      const list = getPath(state.data, field.path);
      if (Array.isArray(list)) list.splice(index, 1);
      markDirty(); hooks.renderRows();
    },
  }));
  card.append(head);

  const body = el('div', { class: 'obj-body' });
  for (const sub of field.subFields || []) {
    if (sub.kind === 'select') {
      // 范围选择器单独占一行：卡片窗口较窄时挤在标题行会被拆行
      const wrap = el('div', { class: 'obj-sub' });
      wrap.append(el('span', { class: 'obj-sub-label', text: sub.label }));
      const sel = el('select', {
        class: 'obj-type',
        onchange: (e) => { entry[sub.key] = e.target.value; markDirty(); hooks.renderRows(); },
      });
      for (const [value, text] of sub.options || []) {
        sel.append(el('option', { value, text, selected: String(entry[sub.key] || '') === value ? 'selected' : null }));
      }
      wrap.append(sel);
      body.append(wrap);
      continue;
    }
    if (sub.kind !== 'intlist') continue;
    if (typeof sub.showWhen === 'function' && !sub.showWhen(entry)) continue;
    const wrap = el('div', { class: 'obj-sub' });
    wrap.append(el('span', { class: 'obj-sub-label', text: sub.label }));
    wrap.append(renderListEditor({
      label: sub.label,
      item: sub.item,
      get: () => entry[sub.key],
      set: (value) => { entry[sub.key] = value; },
    }));
    body.append(wrap);
  }
  card.append(body);
  return card;
}

/* 群列表：给上面的编辑器显示群名 / 提供下拉框（取不到也不影响手填） */
async function loadGroups({ silent = true } = {}) {
  const res = await api('/api/groups');
  const body = res.body || {};
  if (Array.isArray(body.groups)) {
    state.groupList = body.groups;
    state.groupNames = {};
    for (const item of body.groups) state.groupNames[String(item.group_id)] = item.group_name;
  }
  state.groupsError = body.error || '';
  // 重画各列表的群名 / 下拉框；返回 false 的（DOM 已被重建）顺手清掉
  state.listEditors = state.listEditors.filter((fn) => fn() !== false);
  if (!silent && state.groupsError) setBanner('warn', '群列表没取到', [state.groupsError]);
}

/* ══════════════════ NapCat 连接设置（config/config.json） ══════════════════ */
async function loadWs() {
  const grid = $('#wsForm');
  if (grid && !state.ws) grid.append(el('div', { class: 'hint', text: '加载中…' }));
  const res = await api('/api/ws');
  const body = res.body || {};
  if (!body.ok) {
    renderWsInfo({ error: (body.errors || [`HTTP ${res.status}`]).join('；') });
    return;
  }
  state.ws = { data: body.data || {}, sender: body.sender || {}, meta: body.meta || {} };
  if (!state.ws.data.type) state.ws.data.type = 'ws_re';
  renderWsInfo(body.info || {});
  renderWsForm();
}

function renderWsInfo(info) {
  const box = $('#wsInfo');
  if (!box) return;
  box.innerHTML = '';
  if (info.error) {
    box.append(el('div', { class: 'hint', text: `连接信息读取失败：${info.error}` }));
    return;
  }
  const rows = [
    ['当前模式', info.label || '-'],
    ['地址', info.endpoint || '-'],
    ['配置文件', info.config_path || '-'],
    ['连接状态', info.connected === null || info.connected === undefined
      ? '未知（面板没跟 Bot 同进程）'
      : (info.connected ? '已连接' : '未连接')],
  ];
  for (const [key, value] of rows) {
    box.append(el('dt', { text: key }));
    const cls = key === '连接状态' && info.connected !== null && info.connected !== undefined
      ? (info.connected ? 'on' : 'off') : null;
    box.append(el('dd', {}, [cls ? el('span', { class: `pill ${cls}`, text: String(value) }) : String(value)]));
  }
}

function wsField(path, label, type, hint) {
  const value = getPath(state.ws, path);
  const inputId = `ws_${path.replace(/\./g, '_')}`;
  const wrapper = el('div', { class: 'field' });
  wrapper.append(el('label', { for: inputId, text: label }));

  if (type === 'select') {
    const select = el('select', {
      id: inputId,
      onchange: (e) => { setPath(state.ws, path, e.target.value); renderWsForm(); },
    });
    for (const [val, text] of [['ws_re', '反向 ws_re（NapCat 连过来）'], ['ws', '正向 ws（Bot 连 NapCat）']]) {
      select.append(el('option', { value: val, selected: value === val ? 'selected' : null, text }));
    }
    wrapper.append(select);
  } else if (type === 'bool') {
    const control = el('input', {
      type: 'checkbox',
      id: inputId,
      checked: value ? 'checked' : null,
      onchange: (e) => {
        setPath(state.ws, path, e.target.checked);
        e.target.parentElement.lastChild.textContent = e.target.checked ? '已开启' : '已关闭';
      },
    });
    wrapper.append(el('div', { class: 'switch' }, [control, el('span', { text: value ? '已开启' : '已关闭' })]));
  } else {
    const control = el('input', {
      type: type === 'number' ? 'number' : 'text',
      id: inputId,
      oninput: (e) => {
        const raw = e.target.value;
        setPath(state.ws, path, type === 'number' ? (raw === '' ? null : Number(raw)) : raw);
      },
    });
    control.value = value == null ? '' : String(value);
    wrapper.append(control);
  }
  if (hint) wrapper.append(el('div', { class: 'hint', text: hint }));
  return wrapper;
}

function renderWsForm() {
  const grid = $('#wsForm');
  if (!grid) return;
  grid.innerHTML = '';
  if (!state.ws) {
    grid.append(el('div', { class: 'hint', text: '连接设置还没读到（切到本页会重试）' }));
    return;
  }
  grid.append(wsField('type', '连接方式（type）', 'select', '只按 type 对应的那一组参数连接'));
  grid.append(wsField('data.access_token', 'access_token', 'text', 'NapCat 里设置的密码；不一致会被拒/报警告'));

  if (state.ws.data.type === 'ws') {
    grid.append(wsField('data.ws_host', 'NapCat 地址（ws_host）', 'text'));
    grid.append(wsField('data.ws_port', 'NapCat 端口（ws_port）', 'number', 'NapCat「WebSocket 服务器」监听的端口'));
    grid.append(wsField('sender.ws_host', '发送 Bot 地址（sender_config.json）', 'text',
      '正向模式下事件与发消息是两条连接：这是用来发消息的那个 NapCat'));
    grid.append(wsField('sender.ws_port', '发送 Bot 端口', 'number'));
  } else {
    grid.append(wsField('data.ws_reverse_host', '监听地址（ws_reverse_host）', 'text', '默认 127.0.0.1，只监听本机'));
    grid.append(wsField('data.ws_reverse_port', '监听端口（ws_reverse_port）', 'number', 'NapCat「WebSocket 客户端」填这个端口'));
    grid.append(wsField('data.ws_reverse_strict_token', '严格校验 token', 'bool', '打开后 token 不对直接拒连，不会放行'));
  }
}

async function saveWs() {
  if (!state.ws) return;
  const body = { data: state.ws.data };
  if (state.ws.data.type === 'ws') body.sender = state.ws.sender;
  const res = await api('/api/ws', { method: 'POST', body });
  const result = res.body || {};
  if (!result.ok) {
    setBanner('err', '连接设置没能保存', result.errors || [`HTTP ${res.status}`]);
    return;
  }
  setBanner('warn', '连接设置已保存', [
    `已写入 ${(result.saved_to || []).join('、')}`,
    result.hint || '需要重启 Bot 才会生效',
  ]);
  await loadWs();
}

function renderConnectionPanel(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: 'NapCat 连接' }),
    el('p', { class: 'hint', text: '当前连接方式来自 config/config.json；面板只改文件，不主动断线重连' }),
    el('div', { id: 'wsInfo', class: 'kv', text: '加载中…' }),
  ]));
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '修改连接设置' }),
    el('p', { class: 'hint', text: '反向 ws_re：Bot 监听、NapCat 连过来；正向 ws：Bot 连 NapCat。改完需重启 Bot' }),
    el('div', { class: 'grid', id: 'wsForm' }),
    el('div', { class: 'row' }, [
      el('button', { class: 'btn primary', type: 'button', text: '保存连接设置', onclick: saveWs }),
    ]),
  ]));
}

/* ══════════════════════════ 渲染 ══════════════════════════ */
function renderTabs() {
  const nav = $('#tabs');
  nav.innerHTML = '';
  for (const tab of TABS) {
    nav.append(el('button', {
      class: `tab${state.tab === tab.id ? ' active' : ''}`,
      type: 'button',
      text: tab.label,
      onclick: () => {
        state.tab = tab.id;
        showTab();   // 只切显隐，不重建页面：否则关键词页里未提交的修改会被冲掉
      },
      dataset: { tab: tab.id },
    }));
  }
}

function renderAll() {
  renderTabs();
  state.listEditors = [];   // 页面重建，旧的列表编辑器引用作废
  const panels = $('#panels');
  panels.innerHTML = '';
  if (!state.data) {
    panels.append(el('div', { class: 'card', text: '正在读取配置…' }));
    return;
  }
  // 所有页面都渲染出来，只用 CSS 控制显隐。
  // 原因：关键词规则（含 params）只存在于 DOM 里，如果切标签时就销毁页面，
  // 保存时从 DOM 读到的就是空列表，会把规则全删掉。
  const PAGES = [
    ['status', renderStatusPanel],
    ['connection', renderConnectionPanel],
    ['basic', renderBasicPanel],
    ['forward', renderForwardPanel],
    ['text', renderTextPanel],
    ['keywords', renderKeywordsPanel],
    ['raw', renderRawPanel],
  ];
  for (const [id, render] of PAGES) {
    const page = el('div', {
      class: `page${state.tab === id ? '' : ' hidden'}`,
      dataset: { tab: id },
    });
    render(page);
    panels.append(page);
  }

  showTab();
}

/* 只切页面显隐与标签高亮，不重建 DOM —— 切标签时未提交的表单内容不会丢 */
function showTab() {
  for (const page of document.querySelectorAll('#panels .page')) {
    page.classList.toggle('hidden', page.dataset.tab !== state.tab);
  }
  for (const btn of document.querySelectorAll('#tabs .tab')) {
    btn.classList.toggle('active', btn.dataset.tab === state.tab);
  }

  const cfg = state.data.web_panel || {};
  $('#configPath').textContent = `${state.meta?.path || 'bot_config.json'}`
    + (state.meta?.mtime ? `　·　最后修改 ${state.meta.mtime}` : '')
    + `　·　面板 ${cfg.host || '127.0.0.1'}:${cfg.port ?? 8390}`
    + (state.token ? '　·　已带 token' : '');

  // 运行状态页每 10 秒自动刷新，切到其它页面就停掉定时器
  if (state.statusTimer) {
    clearInterval(state.statusTimer);
    state.statusTimer = null;
  }
  if (state.tab === 'status') {
    refreshStatus();
    state.statusTimer = setInterval(refreshStatus, 10000);
  }
  if (state.tab === 'connection') loadWs();
  if (state.tab === 'forward') loadGroups();   // 顺手刷新群名 / 群下拉框
}

/* ══════════════════════════ 保存 / 加载 ══════════════════════════ */
async function loadConfig({ silent = false } = {}) {
  const res = await api('/api/config');
  if (!res.body || !res.body.ok) {
    const errors = (res.body && res.body.errors) || [`HTTP ${res.status}`];
    setBanner('err', '读取配置失败', errors);
    return false;
  }
  state.data = res.body.data;
  state.meta = res.body.meta;
  state.actions = res.body.actions || [];
  state.imgsDir = res.body.imgs_dir || '';
  if (!silent) renderAll();
  return true;
}

async function save() {
  if (state.tab === 'raw') {
    const raw = $('#rawJson');
    if (raw) {
      try {
        const parsed = JSON.parse(raw.value);
        if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
          state.data = parsed;
        }
      } catch (err) {
        setBanner('err', '原始 JSON 里有语法错误，先修好再保存', [String(err.message || err)]);
        return;
      }
    }
  }

  // 关键词规则只存在 DOM 里：读到 #ruleList 才回写 state.data；
  // 读不到就保留原值，否则会把 keyword_actions 覆盖成空数组（等于删光规则）
  if ($('#ruleList')) {
    const { rules, errors: localErrors } = collectKeywordsFromDom();
    if (localErrors.length) {
      setBanner('err', '关键词规则有问题，未提交保存', localErrors);
      return;
    }
    state.data.keyword_actions = rules;
  }

  $('#btnSave').disabled = true;
  setBanner('warn', '正在保存…');
  const res = await api('/api/config', { method: 'POST', body: { data: state.data } });
  $('#btnSave').disabled = false;

  const body = res.body || {};
  if (!body.ok) {
    setBanner('err', '保存失败，配置未写入', body.errors || [`HTTP ${res.status}`]);
    return;
  }
  if (body.data) state.data = body.data;
  if (body.meta) state.meta = body.meta;
  markClean();

  const result = body.result || {};
  const lines = [];
  if (result.saved_to) lines.push(`已写入 ${result.saved_to}`);
  if (Array.isArray(result.applied)) lines.push(`已热加载：${result.applied.join('、')}`);
  if (result.restart_required) lines.push(`⚠ 需要重启 Bot 才生效${result.restart_reason ? `：${result.restart_reason}` : ''}`);
  if (result.reload_error) lines.push(`热加载失败：${result.reload_error}`);
  if (body.warnings && body.warnings.length) lines.push(...body.warnings.map((w) => `⚠ ${w}`));

  setBanner(body.warnings && body.warnings.length ? 'warn' : 'ok', '保存成功', lines);
  renderAll();
}

async function reloadFromDisk() {
  const res = await api('/api/reload', { method: 'POST' });
  const body = res.body || {};
  if (!body.ok) {
    setBanner('err', '重新加载失败', body.errors || [`HTTP ${res.status}`]);
    return;
  }
  if (body.data) state.data = body.data;
  if (body.meta) state.meta = body.meta;
  markClean('已加载配置');
  const applied = (body.result && body.result.applied) || [];
  setBanner('ok', '已加载配置', [
    `配置文件：${body.meta?.path || '-'}`,
    applied.length ? `已应用：${applied.join('、')}` : '',
  ].filter(Boolean));
  renderAll();
}

/* ══════════════════════════ 启动 ══════════════════════════ */
async function boot() {
  $('#btnSave').addEventListener('click', save);
  $('#btnReload').addEventListener('click', reloadFromDisk);
  window.addEventListener('beforeunload', (event) => {
    if (!state.dirty) return undefined;
    event.preventDefault();
    event.returnValue = '';
    return '';
  });

  const ok = await loadConfig();
  if (!ok) {
    renderTabs();
    if (!state.token) {
      setBanner('err', '读取配置失败', ['如果面板设置了 token，请用 http://主机:端口/?token=xxx 打开']);
    }
    return;
  }
  await loadWs();
  await loadGroups();   // 群名要赶在第一次渲染之前拿到
  renderAll();
}

boot();
