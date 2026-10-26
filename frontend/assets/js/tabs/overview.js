/**
 * 运行状态标签页：只读，每 10 秒刷新一次。
 */

import { api } from '../api.js';
import { el, $ } from '../dom.js';

export const TAB = { id: 'status', label: '运行状态' };

export function render(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '运行状态' }),
    el('p', { class: 'hint', text: '每 10 秒自动刷新；这里只读，不能改' }),
    el('div', { id: 'statusBody', class: 'kv', text: '加载中…' }),
  ]));
}

export function renderStatusInto(status) {
  const body = $('#statusBody');
  if (!body) return;
  body.innerHTML = '';
  // 断点表格是 .kv 的兄弟节点，刷新前先清掉，避免每 10 秒叠一张
  const previous = document.getElementById('statusExtra');
  if (previous) previous.remove();
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
    add('区间任务', (bot.range_tasks || []).length ? bot.range_tasks.join(', ') : '无');
    const cps = bot.check_points || {};
    const keys = Object.keys(cps).filter((k) => k !== 'error');
    add('断点群数', `${bot.checkpoint_count ?? keys.length} 个`);
    if (bot.group_count != null) add('监听来源群', `${bot.group_count} 个`);
    if (bot.known_groups) add('群列表缓存', `${bot.known_groups} 个群`);
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
    const anchor = body.closest('.kv');
    if (anchor) {
      anchor.after(el('div', { id: 'statusExtra', style: 'margin-top:14px' }, [
        el('div', { class: 'hint', text: '断点（每个群最后处理到的消息）', style: 'margin-bottom:6px' }),
        table,
      ]));
    }
  } else if (bot.check_points && bot.check_points.error) {
    const anchor = body.closest('.kv');
    if (anchor) {
      anchor.after(el('div', { id: 'statusExtra', class: 'hint', text: `断点读取失败：${bot.check_points.error}` }));
    }
  }
}

export async function refreshStatus() {
  const res = await api('/api/status');
  if (!res.body || !res.body.ok) {
    renderStatusInto({ bot_running: false, panel: {}, config: {} });
    return;
  }
  renderStatusInto(res.body);
}
