/**
 * DOM 小工具 + 通用字段渲染。
 *
 * 旧 app.js 里的 `el` / `renderField` / 列表编辑器都搬到这里，
 * 让各标签页只关心「这一页有哪些字段」。
 */

import { state, markDirty } from './state.js';

export const $ = (selector, root = document) => root.querySelector(selector);

/** 轻量 createElement：class/text/html/dataset/on* 简写。 */
export function el(tag, props = {}, children = []) {
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

export function getPath(obj, path) {
  return path.split('.').reduce((node, key) => (node == null ? undefined : node[key]), obj);
}

export function setPath(obj, path, value) {
  const parts = path.split('.');
  let node = obj;
  for (let i = 0; i < parts.length - 1; i += 1) {
    if (typeof node[parts[i]] !== 'object' || node[parts[i]] === null) node[parts[i]] = {};
    node = node[parts[i]];
  }
  node[parts[parts.length - 1]] = value;
}

export function parseNumList(text) {
  return String(text)
    .split(/[,\s，、;；]+/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => (/^\d+$/.test(s) ? Number(s) : s));
}

export function fmtNumList(value) {
  return Array.isArray(value) ? value.join(', ') : '';
}

export function jsonError(text) {
  try {
    JSON.parse(text);
    return null;
  } catch (err) {
    return String(err.message || err);
  }
}

/* ══════════════════════ 提示条 ══════════════════════ */
export function setBanner(kind, title, lines = []) {
  const banner = $('#banner');
  if (!banner) return;
  banner.className = `banner ${kind}`;
  banner.innerHTML = '';
  banner.append(el('div', { text: title }));
  if (lines.length) {
    const ul = el('ul');
    for (const line of lines) ul.append(el('li', { text: line }));
    banner.append(ul);
  }
}

export function clearBanner() {
  const banner = $('#banner');
  if (!banner) return;
  banner.className = 'banner hidden';
  banner.innerHTML = '';
}

/* ══════════════════════ 字段渲染 ══════════════════════ */
export function renderField(field) {
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
  } else if (field.type === 'strlist') {
    // 词表 / 别名：一行输入，用逗号（中英）或空格分隔，写回字符串数组
    control = el('input', {
      type: 'text',
      id: inputId,
      placeholder: field.placeholder || '用逗号或空格分隔',
      oninput: (e) => {
        const items = e.target.value
          .split(/[,，、\s]+/)
          .map((item) => item.trim())
          .filter(Boolean);
        setPath(state.data, field.path, items);
        markDirty();
      },
    });
    control.value = Array.isArray(value) ? value.join(', ') : (value == null ? '' : String(value));
    wrapper.append(control);
  } else if (field.type === 'intlist') {
    // 群号 / QQ 号这种多值参数：表格化（一行一项，可增可删）
    wrapper.append(renderListEditor(field));
  } else if (field.type === 'objlist') {
    // 对象列表：每个对象自带若干子参数（如机器人 QQ + 生效范围 + 群号列表）
    wrapper.append(renderObjectList(field));
  } else {
    control = el('input', {
      type: field.type === 'number' ? 'number' : field.type === 'password' ? 'password' : 'text',
      autocomplete: field.type === 'password' ? 'new-password' : null,
      id: inputId,
      oninput: (e) => {
        const raw = e.target.value;
        setPath(state.data, field.path, field.type === 'number' ? (raw === '' ? null : Number(raw)) : raw);
        markDirty();
      },
    });
    control.value = value == null ? '' : String(value);
    if (field.type === 'number') control.step = field.step != null ? field.step : 'any';
    wrapper.append(control);
  }
  if (field.hint) wrapper.append(el('div', { class: 'hint', text: field.hint }));
  return wrapper;
}

export function renderFieldGroup(items) {
  const grid = el('div', { class: 'grid' });
  for (const field of items) grid.append(renderField(field));
  return grid;
}

/* ══════════════ 多值参数（群号 / QQ 号）的表格编辑器 ══════════════ */
export function normalizeListItem(text) {
  const raw = String(text == null ? '' : text).trim();
  if (raw === '') return '';
  return /^\d+$/.test(raw) ? Number(raw) : raw;
}

/* 一行一项：序号 + 输入框 +（群名）+ 删除；支持回车新增、批量粘贴、从群列表选 */
export function renderListEditor(field) {
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
export function normalizeObjectEntry(field, raw) {
  const idKey = field.idKey || 'id';
  const entry = Object.assign({}, typeof field.empty === 'function' ? field.empty() : {});
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) Object.assign(entry, raw);
  else if (raw !== null && raw !== undefined && String(raw).trim() !== '') entry[idKey] = normalizeListItem(raw);
  for (const sub of field.subFields || []) {
    if (sub.kind === 'intlist' && !Array.isArray(entry[sub.key])) entry[sub.key] = [];
  }
  return entry;
}

export function renderObjectList(field) {
  const box = el('div', { class: 'list-editor' });
  const rowsBox = el('div', { class: 'obj-rows' });
  const foot = el('div', { class: 'list-actions' });
  const footStatus = el('span', { class: 'list-status' });
  const idKey = field.idKey || 'id';
  // 文案可配：搬运列表里用来加「末尾追加消息」，机器人列表里还是老样子
  const addLabel = field.addLabel || '＋ 添加一个机器人';
  const blankNote = field.blankNote || '个没填 QQ';
  const dupNote = field.dupNote || '个 QQ 重复';

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
    if (blank) notes.push(`${blank} ${blankNote}`);
    if (dup) notes.push(`${dup} ${dupNote}`);
    footStatus.textContent = notes.join('　·　');
    footStatus.className = `list-status${blank || dup ? ' warn' : ''}`;
  }

  function buildFoot() {
    foot.innerHTML = '';
    foot.append(el('button', {
      class: 'btn mini', type: 'button', text: addLabel,
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
      rowsBox.append(el('div', { class: 'hint', text: `还没有内容：点「${addLabel}」加一条` }));
    }
    buildFoot();
    decorate();
  }

  box.append(rowsBox, foot);
  renderRows();
  return box;
}

export function renderObjectCard(field, entry, index, hooks) {
  const card = el('div', { class: 'obj-card' });
  const head = el('div', { class: 'obj-head' });
  head.append(el('span', { class: 'list-idx', text: String(index + 1) }));

  for (const sub of field.subFields || []) {
    if (sub.kind === 'id') {
      head.append(el('span', { class: 'obj-label', text: sub.label }));
      head.append(el('input', {
        type: 'text', class: 'list-input obj-id', placeholder: sub.placeholder || 'QQ 号',
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
    if (sub.kind === 'text' || sub.kind === 'textarea') {
      // 普通文本 / 多行文本：昵称、追加消息内容这类自由输入
      const wrap = el('div', { class: 'obj-sub' });
      wrap.append(el('span', { class: 'obj-sub-label', text: sub.label }));
      const input = sub.kind === 'textarea'
        ? el('textarea', { class: 'obj-text', rows: 2, placeholder: sub.placeholder || '' })
        : el('input', { type: 'text', class: 'obj-text', placeholder: sub.placeholder || '' });
      input.value = entry[sub.key] == null ? '' : String(entry[sub.key]);
      input.addEventListener('input', (e) => { entry[sub.key] = e.target.value; markDirty(); });
      wrap.append(input);
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
