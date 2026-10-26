/**
 * 连接方式标签页：读 / 写 config/config.json 与 sender_config.json。
 * 面板只改文件，不主动断线重连；保存后需要重启 Bot。
 */

import { api, setBanner } from '../api.js';
import { $, el, getPath, setPath } from '../dom.js';
import { state } from '../state.js';

export const TAB = { id: 'connection', label: '连接方式' };

export function render(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: 'OneBot 连接' }),
    el('p', { class: 'hint', text: '当前连接方式来自 config/config.json；支持 NapCat / SnowLuma 等 OneBot v11 后端。面板只改文件，不主动断线重连' }),
    el('div', { id: 'wsInfo', class: 'kv', text: '加载中…' }),
  ]));
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '修改连接设置' }),
    el('p', { class: 'hint', text: '反向 ws_re：Bot 监听、后端连过来；正向 ws：Bot 连后端。改完需重启 Bot' }),
    el('div', { class: 'grid', id: 'wsForm' }),
    el('div', { class: 'row' }, [
      el('button', { class: 'btn primary', type: 'button', text: '保存连接设置', onclick: saveWs }),
    ]),
  ]));
}

export async function loadWs() {
  const grid = $('#wsForm');
  if (grid && !state.ws) grid.append(el('div', { class: 'hint', text: '加载中…' }));
  const res = await api('/api/ws');
  const body = res.body || {};
  if (!body.ok) {
    renderWsInfo({ error: (body.errors || [`HTTP ${res.status}`]).join('；') });
    return;
  }
  state.ws = {
    data: body.data || {},
    sender: body.sender || {},
    meta: body.meta || {},
    backends: body.backends || [],
    default_backend: body.default_backend || 'napcat',
  };
  if (!state.ws.data.type) state.ws.data.type = 'ws_re';
  if (!state.ws.data.backend) state.ws.data.backend = state.ws.default_backend;
  if (!state.ws.sender.backend) state.ws.sender.backend = state.ws.data.backend;
  renderWsInfo(body.info || {});
  renderWsForm();
}

function currentBackend() {
  const id = (state.ws && state.ws.data && state.ws.data.backend) || 'napcat';
  const list = (state.ws && state.ws.backends) || [];
  return list.find((item) => item.id === id) || {
    id,
    label: id === 'snowluma' ? 'SnowLuma' : 'NapCat',
    history_anchor: id === 'snowluma' ? 'message_id' : 'message_seq',
    defaults: {},
    notes: [],
  };
}

export function renderWsInfo(info) {
  const box = $('#wsInfo');
  if (!box) return;
  box.innerHTML = '';
  if (info.error) {
    box.append(el('div', { class: 'hint', text: `连接信息读取失败：${info.error}` }));
    return;
  }
  const rows = [
    ['协议后端', info.backend_label || info.backend || '-'],
    ['历史游标', info.history_anchor || '-'],
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

export function wsField(path, label, type, hint, options) {
  const value = getPath(state.ws, path);
  const inputId = `ws_${path.replace(/\./g, '_')}`;
  const wrapper = el('div', { class: 'field' });
  wrapper.append(el('label', { for: inputId, text: label }));

  if (type === 'select') {
    const select = el('select', {
      id: inputId,
      onchange: (e) => { setPath(state.ws, path, e.target.value); renderWsForm(); },
    });
    const items = options || [
      ['ws_re', '反向 ws_re（后端连过来）'],
      ['ws', '正向 ws（Bot 连后端）'],
    ];
    for (const [val, text] of items) {
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

export function renderWsForm() {
  const grid = $('#wsForm');
  if (!grid) return;
  grid.innerHTML = '';
  if (!state.ws) {
    grid.append(el('div', { class: 'hint', text: '连接设置还没读到（切到本页会重试）' }));
    return;
  }
  const backend = currentBackend();
  const defaults = backend.defaults || {};
  const backendChoices = (state.ws.backends && state.ws.backends.length
    ? state.ws.backends
    : [{ id: 'napcat', label: 'NapCat', history_anchor: 'message_seq' },
      { id: 'snowluma', label: 'SnowLuma', history_anchor: 'message_id' }]
  ).map((item) => [
    item.id,
    `${item.label}${item.history_anchor ? `（${item.history_anchor}）` : ''}`,
  ]);

  grid.append(wsField('data.backend', '协议后端（backend）', 'select',
    '决定历史翻页用的游标：NapCat 用 message_seq，SnowLuma 用 message_id', backendChoices));
  grid.append(wsField('type', '连接方式（type）', 'select', '只按 type 对应的那一组参数连接'));
  grid.append(wsField('data.access_token', 'access_token', 'text', `${backend.label} 里设置的密码；不一致会被拒/报警告`));

  if (state.ws.data.type === 'ws') {
    grid.append(wsField('data.ws_host', `${backend.label} 地址（ws_host）`, 'text'));
    grid.append(wsField('data.ws_port', `${backend.label} 端口（ws_port）`, 'number',
      `${backend.label}「WebSocket 服务器」监听的端口，默认 ${defaults.ws_port ?? 3001}`));
    grid.append(wsField('sender.ws_host', '发送 Bot 地址（sender_config.json）', 'text',
      `正向模式下事件与发消息是两条连接：这是用来发消息的那个 ${backend.label}`));
    grid.append(wsField('sender.ws_port', '发送 Bot 端口', 'number',
      `默认 ${defaults.sender_port ?? 3000}`));
  } else {
    grid.append(wsField('data.ws_reverse_host', '监听地址（ws_reverse_host）', 'text', '默认 127.0.0.1，只监听本机'));
    grid.append(wsField('data.ws_reverse_port', '监听端口（ws_reverse_port）', 'number',
      `${backend.label}「WebSocket 客户端」填这个端口，默认 ${defaults.ws_reverse_port ?? 8085}`));
    grid.append(wsField('data.ws_reverse_strict_token', '严格校验 token', 'bool', '打开后 token 不对直接拒连，不会放行'));
  }

  const notes = backend.notes || [];
  if (notes.length) {
    const list = el('div', { class: 'hint' });
    for (const note of notes) list.append(el('div', { text: `• ${note}` }));
    grid.append(list);
  }
}

export async function saveWs() {
  if (!state.ws) return;
  // 发送连接与事件连接共用后端；面板只维护一个选择器。
  if (state.ws.sender) state.ws.sender.backend = state.ws.data.backend;
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
