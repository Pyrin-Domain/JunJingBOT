/**
 * 前端入口：加载配置、渲染标签页、保存 / 热加载。
 *
 * 无构建、无 CDN、离线可用；所有标签页一次性渲染，只用 CSS 控制显隐。
 * 原因：关键词规则（含 params）只存在于 DOM 里，切标签时销毁页面
 * 会让保存读到空列表，把规则全删掉。
 */

import { api, token } from './api.js';
import { $, el, setBanner } from './dom.js';
import { hooks, markClean, state, TABS } from './state.js';

import * as advanced from './tabs/advanced.js';
import * as agent from './tabs/agent.js';
import * as basic from './tabs/basic.js';
import * as connection from './tabs/connection.js';
import * as forward from './tabs/forward.js';
import * as interject from './tabs/interject.js';
import * as keywords from './tabs/keywords.js';
import * as meme from './tabs/meme.js';
import * as overview from './tabs/overview.js';
import * as paths from './tabs/paths.js';
import * as prompts from './tabs/prompts.js';

const PANELS = [
  overview,
  connection,
  paths,
  basic,
  agent,
  interject,
  meme,
  forward,
  prompts,
  keywords,
  advanced,
];

/* ══════════════════════ 渲染 ══════════════════════ */
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
        showTab();   // 只切显隐，不重建页面
      },
      dataset: { tab: tab.id },
    }));
  }
}

export function renderAll() {
  renderTabs();
  state.listEditors = [];   // 页面重建，旧的列表编辑器引用作废
  const panels = $('#panels');
  panels.innerHTML = '';
  if (!state.data) {
    panels.append(el('div', { class: 'card', text: '正在读取配置…' }));
    return;
  }
  for (const panel of PANELS) {
    const page = el('div', {
      class: `page${state.tab === panel.TAB.id ? '' : ' hidden'}`,
      dataset: { tab: panel.TAB.id },
    });
    panel.render(page);
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
    + (token ? '　·　已带 token' : '');

  // 运行状态页每 10 秒自动刷新，切到其它页面就停掉定时器
  if (state.statusTimer) {
    clearInterval(state.statusTimer);
    state.statusTimer = null;
  }
  if (state.tab === 'status') {
    overview.refreshStatus();
    state.statusTimer = setInterval(overview.refreshStatus, 10000);
  }
  if (state.tab === 'connection') connection.loadWs();
  if (state.tab === 'paths') paths.loadPaths();
  if (state.tab === 'forward') forward.loadGroups();   // 顺手刷新群名 / 群下拉框
}

/* ══════════════════════ 保存 / 加载 ══════════════════════ */
async function loadConfig({ silent = false } = {}) {
  const res = await api('/api/config');
  if (!res.body || !res.body.ok) {
    const errors = (res.body && res.body.errors) || [`HTTP ${res.status}`];
    setBanner('err', '读取配置失败', errors);
    return false;
  }
  state.data = res.body.data;
  if (state.data.agent && typeof state.data.agent === 'object') agent.normalizeAgentModels(state.data.agent);
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

  // 模型配置在 DOM 里也可能被改动（启用 / 删除 / 排序），保存前统一整理一遍
  if (state.data.agent && typeof state.data.agent === 'object') agent.normalizeAgentModels(state.data.agent);

  // 关键词规则只存在 DOM 里：读到 #ruleList 才回写 state.data；
  // 读不到就保留原值，否则会把 keyword_actions 覆盖成空数组（等于删光规则）
  if ($('#ruleList')) {
    const { rules, errors: localErrors } = keywords.collectKeywordsFromDom();
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
  if (state.data.agent && typeof state.data.agent === 'object') agent.normalizeAgentModels(state.data.agent);
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

/* ══════════════════════ 启动 ══════════════════════ */
async function boot() {
  hooks.renderAll = renderAll;
  hooks.setBanner = setBanner;

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
    if (!token) {
      setBanner('err', '读取配置失败', ['如果面板设置了 token，请用 http://主机:端口/?token=xxx 打开']);
    }
    return;
  }
  await connection.loadWs();
  await forward.loadGroups();   // 群名要赶在第一次渲染之前拿到
  renderAll();
}

boot();
