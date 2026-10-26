/**
 * 路径映射标签页：Bot 路径 ↔ 协议端路径。
 *
 * 数据来自 /api/paths（environment.json 的 bot_env / backend_env /
 * path_profile / path_map）。保存后翻译器会原地热替换，不需要重启 Bot。
 */

import { api, setBanner } from '../api.js';
import { $, el } from '../dom.js';
import { state } from '../state.js';

export const TAB = { id: 'paths', label: '路径映射' };

export function render(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '路径映射' }),
    el('p', {
      class: 'hint',
      text: 'Bot 把本地文件路径交给协议端（NapCat / SnowLuma）时，按这里的规则翻译。'
        + '改完保存立即生效，不需要重启 Bot。',
    }),
    el('div', { class: 'grid', id: 'pathsEnv', text: '加载中…' }),
    el('div', { class: 'row' }, [
      el('button', { class: 'btn primary', type: 'button', text: '保存路径设置', onclick: savePaths }),
    ]),
  ]));

  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '自定义规则（path_map）' }),
    el('p', {
      class: 'hint',
      text: '叠加在预设之上，最长前缀优先。例如 D:\\share → /data/share 只覆盖这个目录，'
        + '其它盘符仍走预设。',
    }),
    el('div', { id: 'pathRules', class: 'list-editor', text: '加载中…' }),
    el('div', { class: 'row' }, [
      el('button', { class: 'btn', type: 'button', text: '＋ 添加规则', onclick: addRule }),
    ]),
  ]));

  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '预览' }),
    el('p', { class: 'hint', text: '输入一条 Bot 本机路径，看看发给协议端时会变成什么。' }),
    el('div', { class: 'tester' }, [
      el('input', {
        type: 'text',
        id: 'pathPreviewInput',
        placeholder: '例如 D:\\Code\\JungJingNew\\imgs\\妈妈.gif',
        onkeydown: (e) => { if (e.key === 'Enter') previewPath(); },
      }),
      el('button', { class: 'btn', type: 'button', text: '预览', onclick: previewPath }),
    ]),
    el('div', { id: 'pathPreviewResult' }),
  ]));

  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '当前解析目录' }),
    el('p', { class: 'hint', text: '这些是 Bot 实际使用的本机绝对路径，只读。' }),
    el('div', { id: 'pathResolved', class: 'kv', text: '加载中…' }),
  ]));
}

/* ══════════════════════ 加载 ══════════════════════ */
export async function loadPaths({ force = false } = {}) {
  if (state.paths && !force) {
    paint();
    return;
  }
  const res = await api('/api/paths');
  const body = res.body || {};
  if (!body.ok) {
    setBanner('err', '路径设置读取失败', body.errors || [`HTTP ${res.status}`]);
    return;
  }
  state.paths = {
    data: body.data || {},
    profiles: body.profiles || [],
    envOptions: body.env_options || [],
    detected: body.detected || {},
    resolved: body.resolved || {},
    dirs: body.dirs || {},
    meta: body.meta || {},
  };
  paint();
}

function paint() {
  renderEnv();
  renderRules();
  renderResolved();
}

/* ══════════════════════ 环境 / 预设 ══════════════════════ */
function field(label, control, hint, full = false) {
  const wrapper = el('div', { class: `field${full ? ' full' : ''}` });
  wrapper.append(el('label', { text: label }));
  wrapper.append(control);
  if (hint) wrapper.append(el('div', { class: 'hint', text: hint }));
  return wrapper;
}

function selectField(options, value, onchange) {
  const node = el('select', { onchange: (e) => onchange(e.target.value) });
  for (const option of options) {
    node.append(el('option', {
      value: option.id,
      selected: option.id === value ? 'selected' : null,
      text: option.label || option.id,
    }));
  }
  return node;
}

function envLabel(id) {
  const found = (state.paths?.envOptions || []).find((item) => item.id === id);
  return found ? found.label : id;
}

function currentProfile() {
  const id = state.paths?.data?.path_profile;
  return (state.paths?.profiles || []).find((item) => item.id === id) || null;
}

function renderEnv() {
  const grid = $('#pathsEnv');
  if (!grid || !state.paths) return;
  grid.innerHTML = '';
  const data = state.paths.data;
  const profile = currentProfile();

  grid.append(field(
    'Bot 本机环境（bot_env）',
    el('div', { class: 'hint', text: envLabel(data.bot_env) }),
    `自动探测：${envLabel(state.paths.detected.bot_env)}。一般不需要改。`,
  ));

  grid.append(field(
    '协议端环境（backend_env）',
    selectField(state.paths.envOptions, data.backend_env, (value) => {
      data.backend_env = value;
      renderEnv();
    }),
    'NapCat / SnowLuma 所在的环境。它只影响路径翻译，不改变连接方式。',
  ));

  grid.append(field(
    '翻译模式（path_profile）',
    selectField(state.paths.profiles, data.path_profile, (value) => {
      data.path_profile = value;
      renderEnv();
    }),
    profile ? profile.description : '选择一套预设；自定义规则会叠加在它上面。',
  ));

  const needsMount = profile ? profile.needs_mount_root : false;
  grid.append(field(
    'WSL 挂载根（wsl_mount_root）',
    el('input', {
      type: 'text',
      value: data.wsl_mount_root || '/mnt',
      disabled: needsMount ? null : 'disabled',
      oninput: (e) => { data.wsl_mount_root = e.target.value; },
    }),
    needsMount
      ? 'WSL 里 Windows 盘的挂载点，默认 /mnt。'
      : '只有 Windows ↔ WSL 的预设会用到；当前预设下忽略。',
  ));

  grid.append(field(
    'WSL 发行版（wsl_distro）',
    el('input', {
      type: 'text',
      value: data.wsl_distro || 'Ubuntu',
      oninput: (e) => { data.wsl_distro = e.target.value; },
    }),
    '处理 \\\\wsl$\\<发行版>\\... 这类 UNC 路径时用；只填名字，例如 Ubuntu。',
  ));

  if (profile && profile.notes && profile.notes.length) {
    const notes = el('div', { class: 'hint full' });
    for (const note of profile.notes) notes.append(el('div', { text: `• ${note}` }));
    grid.append(notes);
  }
}

/* ══════════════════════ 自定义规则 ══════════════════════ */
function renderRules() {
  const box = $('#pathRules');
  if (!box || !state.paths) return;
  box.innerHTML = '';
  const rules = Array.isArray(state.paths.data.path_map) ? state.paths.data.path_map : [];
  if (!rules.length) {
    box.append(el('div', { class: 'hint', text: '还没有自定义规则。预设能覆盖常见情况时不需要加。' }));
    return;
  }
  const rows = el('div', { class: 'list-rows' });
  rules.forEach((rule, index) => {
    const row = el('div', { class: 'path-rule' });
    row.append(el('span', { class: 'list-idx', text: String(index + 1) }));

    const from = el('input', {
      type: 'text',
      class: 'list-input',
      placeholder: 'Bot 路径前缀，例如 D:\\share',
      oninput: (e) => { rule.from = e.target.value; },
    });
    from.value = rule.from || '';

    const arrow = el('span', { class: 'path-arrow', text: '→' });

    const to = el('input', {
      type: 'text',
      class: 'list-input',
      placeholder: '协议端路径前缀，例如 /data/share',
      oninput: (e) => { rule.to = e.target.value; },
    });
    to.value = rule.to || '';

    const note = el('input', {
      type: 'text',
      class: 'list-input path-note',
      placeholder: '备注（可选）',
      oninput: (e) => { rule.note = e.target.value; },
    });
    note.value = rule.note || '';

    row.append(from, arrow, to, note);
    row.append(el('button', {
      class: 'btn list-del',
      type: 'button',
      text: '删除',
      onclick: () => {
        rules.splice(index, 1);
        renderRules();
      },
    }));
    rows.append(row);
  });
  box.append(rows);
}

function addRule() {
  if (!state.paths) return;
  if (!Array.isArray(state.paths.data.path_map)) state.paths.data.path_map = [];
  state.paths.data.path_map.push({ from: '', to: '', note: '' });
  renderRules();
}

function collectRules() {
  const rules = state.paths?.data?.path_map;
  if (!Array.isArray(rules)) return [];
  return rules.filter((rule) => (
    String(rule.from || '').trim()
    || String(rule.to || '').trim()
    || String(rule.note || '').trim()
  ));
}

/* ══════════════════════ 预览 ══════════════════════ */
function ruleText(result) {
  if (!result) return '-';
  if (result.special) return '特殊值（URL / base64），原样返回';
  if (!result.matched || !result.rule) return '未命中规则，原样返回';
  const note = result.rule.note ? `（${result.rule.note}）` : '';
  return `${result.rule.from} → ${result.rule.to}${note}`;
}

function renderPreview(body) {
  const box = $('#pathPreviewResult');
  if (!box) return;
  box.innerHTML = '';
  if (!body.ok) {
    box.append(el('div', { class: 'hint', text: (body.errors || ['预览失败']).join('；') }));
    return;
  }
  const kv = el('dl', { class: 'kv' });
  const rows = [
    ['输入', body.input],
    ['发给协议端（to_backend）', body.to_backend?.output ?? '-'],
    ['命中规则（to_backend）', ruleText(body.to_backend)],
    ['协议端 → 本机（to_local）', body.to_local?.output ?? '-'],
    ['命中规则（to_local）', ruleText(body.to_local)],
  ];
  for (const [key, value] of rows) {
    kv.append(el('dt', { text: key }));
    kv.append(el('dd', { text: String(value) }));
  }
  box.append(kv);
  if (body.warnings && body.warnings.length) {
    for (const warning of body.warnings) {
      box.append(el('div', { class: 'hint', text: `⚠ ${warning}` }));
    }
  }
}

async function previewPath() {
  if (!state.paths) return;
  const input = $('#pathPreviewInput');
  const value = input ? input.value.trim() : '';
  if (!value) {
    setBanner('warn', '先在预览框里填一条路径');
    return;
  }
  const res = await api('/api/paths/preview', {
    method: 'POST',
    body: { value, data: state.paths.data },
  });
  renderPreview(res.body || { ok: false, errors: [`HTTP ${res.status}`] });
}

/* ══════════════════════ 解析目录 ══════════════════════ */
function renderResolved() {
  const box = $('#pathResolved');
  if (!box || !state.paths) return;
  box.innerHTML = '';
  const resolved = state.paths.resolved || {};
  const dirs = state.paths.dirs || {};
  const meta = state.paths.meta || {};
  const rows = [
    ['项目根目录', resolved.root],
    ['图片目录（imgs_dir）', resolved.imgs_dir],
    ['下载目录（downloads_dir）', resolved.downloads_dir],
    ['临时目录（temp_dir）', resolved.temp_dir],
    ['配置里写的 imgs_dir', dirs.imgs_dir],
    ['配置文件', meta.env_path],
  ];
  for (const [key, value] of rows) {
    if (!value) continue;
    box.append(el('dt', { text: key }));
    box.append(el('dd', { text: String(value) }));
  }
}

/* ══════════════════════ 保存 ══════════════════════ */
async function savePaths() {
  if (!state.paths) return;
  const data = { ...state.paths.data, path_map: collectRules() };
  const res = await api('/api/paths', { method: 'POST', body: { data } });
  const body = res.body || {};
  if (!body.ok) {
    setBanner('err', '路径设置没能保存', body.errors || [`HTTP ${res.status}`]);
    return;
  }
  const lines = [];
  if (body.saved_to) lines.push(`已写入 ${body.saved_to}`);
  if (body.applied && body.applied.length) lines.push(`已应用：${body.applied.join('、')}`);
  if (body.warnings && body.warnings.length) {
    lines.push(...body.warnings.map((warning) => `⚠ ${warning}`));
  }
  setBanner(body.warnings && body.warnings.length ? 'warn' : 'ok', '路径设置已保存', lines);
  state.paths = null;
  await loadPaths({ force: true });
}
