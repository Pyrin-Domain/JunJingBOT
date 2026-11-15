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
import * as imageFilter from './tabs/image_filter.js';
import * as interject from './tabs/interject.js';
import * as keywords from './tabs/keywords.js';
import * as meme from './tabs/meme.js';
import * as overview from './tabs/overview.js';
import * as paths from './tabs/paths.js';
import * as prompts from './tabs/prompts.js';
import * as psychology from './tabs/psychology.js';

const PANELS = [
  overview,
  connection,
  paths,
  basic,
  agent,
  interject,
  psychology,
  meme,
  forward,
  imageFilter,
  prompts,
  keywords,
  advanced,
];

const TAB_GROUPS = [
  { id: 'runtime', label: '运行与连接', tabs: ['status', 'connection', 'paths'] },
  { id: 'behavior', label: 'Bot 行为', tabs: ['basic', 'agent', 'interject', 'psychology', 'meme', 'forward', 'image_filter'] },
  { id: 'content', label: '内容与命令', tabs: ['text', 'keywords'] },
  { id: 'advanced', label: '高级', tabs: ['raw'] },
];

const TAB_IDS = new Set(TABS.map((tab) => tab.id));
let saving = false;
let reloading = false;

function tabFromHash() {
  const id = window.location.hash.replace(/^#/, '');
  return TAB_IDS.has(id) ? id : '';
}

const initialTab = tabFromHash();
if (initialTab) state.tab = initialTab;

/* ══════════════════════ 渲染 ══════════════════════ */
function renderTabs() {
  const nav = $('#tabs');
  nav.innerHTML = '';
  nav.setAttribute('role', 'tablist');
  nav.setAttribute('aria-orientation', 'vertical');

  const tabsById = new Map(TABS.map((tab) => [tab.id, tab]));
  for (const group of TAB_GROUPS) {
    const items = group.tabs.map((id) => tabsById.get(id)).filter(Boolean);
    if (!items.length) continue;
    const section = el('section', {
      class: 'tab-group',
      'aria-labelledby': `tab-group-${group.id}`,
    });
    section.append(el('div', {
      class: 'tab-group-label',
      id: `tab-group-${group.id}`,
      text: group.label,
    }));
    for (const tab of items) {
      const active = state.tab === tab.id;
      section.append(el('button', {
        class: `tab${active ? ' active' : ''}`,
        id: `tab-${tab.id}`,
        type: 'button',
        role: 'tab',
        text: tab.label,
        title: tab.label,
        'aria-controls': `page-${tab.id}`,
        'aria-selected': active ? 'true' : 'false',
        tabindex: active ? '0' : '-1',
        onclick: () => activateTab(tab.id),
        onkeydown: handleTabKeydown,
        dataset: { tab: tab.id },
      }));
    }
    nav.append(section);
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
      id: `page-${panel.TAB.id}`,
      role: 'tabpanel',
      'aria-labelledby': `tab-${panel.TAB.id}`,
      dataset: { tab: panel.TAB.id },
    });
    panel.render(page);
    panels.append(page);
  }
  showTab();
}

function handleTabKeydown(event) {
  const currentIndex = TABS.findIndex((tab) => tab.id === state.tab);
  if (currentIndex < 0) return;
  let nextIndex = currentIndex;
  if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
    nextIndex = (currentIndex + 1) % TABS.length;
  } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
    nextIndex = (currentIndex - 1 + TABS.length) % TABS.length;
  } else if (event.key === 'Home') {
    nextIndex = 0;
  } else if (event.key === 'End') {
    nextIndex = TABS.length - 1;
  } else {
    return;
  }
  event.preventDefault();
  activateTab(TABS[nextIndex].id, { focus: true });
}

function activateTab(id, { focus = false } = {}) {
  if (!TAB_IDS.has(id)) return;
  state.tab = id;
  showTab();
  const button = $(`#tab-${id}`);
  if (focus && button) button.focus();
}

/* 只切页面显隐与标签高亮，不重建 DOM —— 切标签时未提交的表单内容不会丢 */
function showTab() {
  for (const page of document.querySelectorAll('#panels .page')) {
    const active = page.dataset.tab === state.tab;
    page.classList.toggle('hidden', !active);
    page.setAttribute('aria-hidden', active ? 'false' : 'true');
  }
  for (const btn of document.querySelectorAll('#tabs .tab')) {
    const active = btn.dataset.tab === state.tab;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-selected', active ? 'true' : 'false');
    btn.setAttribute('tabindex', active ? '0' : '-1');
  }

  const cfg = (state.data && state.data.web_panel) || {};
  $('#configPath').textContent = `${state.meta?.path || 'bot_config.json'}`
    + (state.meta?.mtime ? `　·　最后修改 ${state.meta.mtime}` : '')
    + `　·　面板 ${cfg.host || '127.0.0.1'}:${cfg.port ?? 8390}`
    + (token ? '　·　已带 token' : '');
  const current = TABS.find((item) => item.id === state.tab);
  document.title = `${current ? current.label : '参数面板'} · 君景 Bot`;
  if (window.location.hash !== `#${state.tab}`) {
    window.history.replaceState(
      null,
      '',
      `${window.location.pathname}${window.location.search}#${state.tab}`,
    );
  }

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
  if (saving) return;
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

  // 关键词 / 图片过滤都是「就地写回 state.data」的二级页面，不需要 DOM 收集

  saving = true;
  const saveButton = $('#btnSave');
  const originalLabel = saveButton.textContent;
  saveButton.disabled = true;
  saveButton.setAttribute('aria-busy', 'true');
  saveButton.textContent = '正在保存…';
  setBanner('warn', '正在保存…');
  let res;
  try {
    res = await api('/api/config', { method: 'POST', body: { data: state.data } });
  } finally {
    saving = false;
    saveButton.disabled = false;
    saveButton.removeAttribute('aria-busy');
    saveButton.textContent = originalLabel;
  }

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
  if (reloading) return;
  reloading = true;
  const reloadButton = $('#btnReload');
  const originalLabel = reloadButton.textContent;
  reloadButton.disabled = true;
  reloadButton.setAttribute('aria-busy', 'true');
  reloadButton.textContent = '正在加载…';
  let res;
  try {
    res = await api('/api/reload', { method: 'POST' });
  } finally {
    reloading = false;
    reloadButton.disabled = false;
    reloadButton.removeAttribute('aria-busy');
    reloadButton.textContent = originalLabel;
  }
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
  window.addEventListener('keydown', (event) => {
    if (event.isComposing) return;
    if ((event.ctrlKey || event.metaKey) && String(event.key).toLowerCase() === 's') {
      event.preventDefault();
      if (!saving) save();
    }
  });
  window.addEventListener('hashchange', () => {
    const next = tabFromHash();
    if (next && next !== state.tab) activateTab(next);
  });
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
