/**
 * 关键词 / 命令标签页：命令为 key 的二级页面。
 *
 * 目录 = 卡片网格（一张卡片一条命令，标题取「回复文本 / 正则」），
 * 点卡片进详情：触发正则 / action / params / 触发范围，全部就地写回
 * ``state.data.keyword_actions``（不再有「规则只存在 DOM 里」的中间态，
 * 保存时也就不需要 DOM 收集那一说了）。
 */

import { el, jsonError, renderListEditor } from '../dom.js';
import { ACTION_HINTS, state, markDirty } from '../state.js';

export const TAB = { id: 'keywords', label: '关键词 / 命令' };

// 目录 → 详情的导航状态：null = 目录，否则为正在编辑的命令下标
let commandDetail = null;

// 批量设置触发范围：选中的命令下标 + 编辑器开关 + 草稿
let selected = new Set();
let batchOpen = false;
const batchScope = { type: 'group', data: [] };

/** 规则列表（就地保证是数组）。 */
export function ruleList() {
  if (!Array.isArray(state.data.keyword_actions)) state.data.keyword_actions = [];
  return state.data.keyword_actions;
}

/** 卡片标题：优先「回复文本」，其次正则。 */
export function commandLabel(rule) {
  const params = (rule && rule.params) || {};
  const message = params.message;
  if (typeof message === 'string' && message.trim()) return message.trim().slice(0, 24);
  if (Array.isArray(message) && message.length) {
    const first = message.find((item) => typeof item === 'string' && item.trim());
    if (first) return first.trim().slice(0, 24);
  }
  const regex = String((rule && rule.regex) || '').trim();
  if (regex) return regex.slice(0, 24);
  const action = String((rule && rule.action) || '').trim();
  return action || '未命名命令';
}

function actionsForSelect() {
  const list = state.actions.length ? [...state.actions] : Object.keys(ACTION_HINTS);
  for (const key of Object.keys(ACTION_HINTS)) {
    if (!list.includes(key)) list.push(key);
  }
  return list;
}

/* ══════════════════════ 详情页 ══════════════════════ */
function renderDetail(rule, index, container) {
  const list = ruleList();

  const titleNode = el('strong', { class: 'model-detail-name', text: commandLabel(rule) });
  const refreshTitle = () => { titleNode.textContent = commandLabel(rule); };

  /* 触发正则 */
  const regexInput = el('input', {
    type: 'text',
    placeholder: '正则，例如 (男娘|南梁|nn)',
    oninput: (event) => {
      rule.regex = event.target.value;
      markDirty(); refreshTitle(); refreshRegexTip();
    },
  });
  regexInput.value = rule.regex || '';

  const regexTip = el('div', { class: 'rule-tip' });
  function refreshRegexTip() {
    const text = regexInput.value.trim();
    regexTip.className = 'rule-tip';
    if (!text) { regexTip.textContent = '空正则永不命中'; return; }
    try {
      new RegExp(text, 'i');
      regexTip.textContent = '正则合法（匹配时忽略大小写，与 Bot 行为一致）';
    } catch (err) {
      regexTip.className = 'rule-tip bad';
      regexTip.textContent = `正则写错了：${err.message}`;
    }
  }
  refreshRegexTip();

  /* action */
  const select = el('select', {
    onchange: (event) => { rule.action = event.target.value; markDirty(); refreshActionTip(); },
  });
  const actions = actionsForSelect();
  for (const action of actions) {
    select.append(el('option', { value: action, text: action, selected: action === rule.action ? 'selected' : null }));
  }
  if (rule.action && !actions.includes(rule.action)) {
    select.append(el('option', { value: rule.action, text: `${rule.action}（未注册）`, selected: 'selected' }));
  }
  if (!rule.action) rule.action = actions[0] || 'send_message';

  const actionTip = el('div', { class: 'rule-tip' });
  function refreshActionTip() {
    actionTip.textContent = ACTION_HINTS[rule.action] || '未知 action：运行时会被跳过';
  }
  refreshActionTip();

  /* params（JSON） */
  const paramsArea = el('textarea', {
    class: 'params',
    rows: 6,
    placeholder: '{}',
    oninput: (event) => {
      const text = event.target.value.trim();
      paramsTip.className = 'rule-tip';
      if (!text) { delete rule.params; paramsTip.textContent = '无参数'; return; }
      const err = jsonError(text);
      if (err) {
        paramsTip.className = 'rule-tip bad';
        paramsTip.textContent = `params 不是合法 JSON：${err}`;
        return;
      }
      const parsed = JSON.parse(text);
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        paramsTip.className = 'rule-tip bad';
        paramsTip.textContent = 'params 必须是一个 JSON 对象';
        return;
      }
      rule.params = parsed;
      paramsTip.textContent = 'params 合法';
      markDirty(); refreshTitle();
    },
  });
  paramsArea.value = rule.params ? JSON.stringify(rule.params, null, 2) : '';

  const paramsTip = el('div', { class: 'rule-tip', text: rule.params ? 'params（JSON 对象）' : '无参数' });

  // 结构化 filter 编辑会动 params：同步刷新 JSON 文本框，免得保存时被旧文本覆盖
  function syncParamsArea() {
    paramsArea.value = rule.params ? JSON.stringify(rule.params, null, 2) : '';
    paramsTip.className = 'rule-tip';
    paramsTip.textContent = rule.params ? 'params（JSON 对象）' : '无参数';
  }

  /* 触发范围 filter —— 注意运行时语义：filter 放在 params.filter 里
   * （MessageProcessor.pattern_match 从 params 里 pop("filter")） */
  const getFilter = () => {
    const params = rule.params;
    return params && typeof params === 'object' && !Array.isArray(params)
      ? (params.filter && typeof params.filter === 'object' ? params.filter : null)
      : null;
  };
  const setFilter = (value) => {
    if (!value) {
      if (rule.params) delete rule.params.filter;
      if (rule.params && !Object.keys(rule.params).length) delete rule.params;
      return;
    }
    rule.params = { ...(rule.params || {}), filter: value };
  };

  const hasFilter = !!getFilter();
  const filterCheck = el('input', {
    type: 'checkbox',
    checked: hasFilter ? 'checked' : null,
    onchange: (event) => {
      setFilter(event.target.checked ? { type: 'group', data: [] } : null);
      markDirty();
      syncParamsArea();
      renderDetailBody(container, rule, index);   // 重建下半段
    },
  });

  const filterBox = el('div', { class: 'filter-box' });
  const filter = getFilter();
  if (filter) {
    const typeSelect = el('select', {
      onchange: (event) => { filter.type = event.target.value; markDirty(); },
    });
    const types = [
      ['group', 'group：只在下面这些群触发'],
      ['private', 'private：只在下面这些 QQ 的私聊触发'],
      ['ban', 'ban：在下面这些群里不触发'],
    ];
    for (const [value, text] of types) {
      typeSelect.append(el('option', { value, text, selected: filter.type === value ? 'selected' : null }));
    }
    if (!types.some(([value]) => filter.type === value)) {
      typeSelect.append(el('option', { value: filter.type, text: `${filter.type}（未知）`, selected: 'selected' }));
    }
    if (!Array.isArray(filter.data)) filter.data = [];
    filterBox.append(
      el('div', { class: 'obj-sub' }, [
        el('span', { class: 'obj-sub-label', text: '范围类型' }),
        typeSelect,
      ]),
      el('div', { class: 'obj-sub' }, [
        el('span', { class: 'obj-sub-label', text: '范围名单（群号 / QQ 号）' }),
        renderListEditor({
          item: 'group',
          get: () => filter.data,
          set: (value) => { filter.data = value; markDirty(); },
        }),
      ]),
    );
  }

  /* 操作按钮 */
  const actions_row = el('div', { class: 'model-detail-actions' }, [
    el('button', {
      class: 'btn mini', type: 'button', text: '复制此命令',
      onclick: () => {
        const copy = JSON.parse(JSON.stringify(rule));
        list.splice(index + 1, 0, copy);
        markDirty();
        commandDetail = index + 1;
        rerender(container);
      },
    }),
    el('button', {
      class: 'btn mini danger', type: 'button', text: '删除此命令',
      onclick: () => {
        if (!window.confirm(`删除命令「${commandLabel(rule)}」？`)) return;
        list.splice(index, 1);
        markDirty();
        commandDetail = null;
        rerender(container);
      },
    }),
  ]);

  const body = el('div', { class: 'model-detail' }, [
    el('div', { class: 'model-detail-head' }, [
      el('button', {
        class: 'btn mini ghost model-back', type: 'button', text: '← 返回命令列表',
        onclick: () => { commandDetail = null; rerender(container); },
      }),
      el('div', { class: 'model-detail-title' }, [
        titleNode,
        el('span', { class: 'hint', text: `第 ${index + 1} 条 · 修改即时生效（保存后才写入配置文件）` }),
      ]),
      actions_row,
    ]),

    el('div', { class: 'model-body' }, [
      field('触发正则', regexInput, regexTip, true),
      field('action（做什么）', select, actionTip, false),
    ]),
    el('div', { class: 'model-field wide' }, [
      el('div', { class: 'model-field-label', text: 'params（JSON，可留空）' }),
      paramsArea,
      paramsTip,
    ]),
    el('div', { class: 'model-field wide' }, [
      el('div', { class: 'model-field-label' }, [
        filterCheck,
        el('span', { text: '　限定触发范围（可选；默认所有群 + 所有私聊）' }),
      ]),
      filterBox,
    ]),
  ]);
  return body;
}

function field(label, control, tip, wide) {
  const wrap = el('div', { class: `model-field${wide ? ' wide' : ''}` }, [
    el('div', { class: 'model-field-label', text: label }),
    control,
  ]);
  if (tip) wrap.append(tip);
  return wrap;
}

/* ══════════════════════ 目录页 ══════════════════════ */
/** 触发范围的短摘要（卡片角标用）；filter 放在 params.filter（运行时语义）。 */
function scopeSummary(rule) {
  const filter = rule?.params?.filter;
  if (!filter || typeof filter !== 'object') return '';
  const count = Array.isArray(filter.data) ? filter.data.length : 0;
  const unit = filter.type === 'private' ? '人' : '群';
  return `${filter.type}(${count}${unit})`;
}

function renderGrid(container) {
  const list = ruleList();
  const testInput = el('input', {
    type: 'text',
    placeholder: '输入一段消息，测试哪些命令会命中',
    oninput: () => runTest(),
  });
  const searchInput = el('input', {
    type: 'text',
    placeholder: '按名称 / 正则 / action 过滤…',
    oninput: () => applyFilter(),
  });
  const summary = el('span', { class: 'rule-tip', text: `共 ${list.length} 条命令` });
  const batchStatus = el('span', { class: 'rule-tip' });

  const grid = el('div', { class: 'model-grid' });
  const tiles = [];

  function updateBatchStatus() {
    const n = selected.size;
    batchStatus.textContent = n ? `已选 ${n} 条命令` : '';
    for (const btn of batchButtons) btn.disabled = n === 0;
  }

  function tileFor(rule, index) {
    const tile = el('button', {
      class: `model-tile${selected.has(index) ? ' tile-selected' : ''}`,
      type: 'button',
      onclick: () => { commandDetail = index; rerender(container); },
    }, [
      el('div', { class: 'model-tile-top' }, [
        el('span', { class: 'model-tile-idx', text: String(index + 1) }),
        el('span', { class: 'model-tile-name', text: commandLabel(rule) }),
      ]),
      el('span', { class: 'model-tile-model', text: rule.regex || '（空正则）' }),
      el('div', { class: 'model-tile-foot' }, [
        el('span', { class: 'model-tile-meta', text: rule.action || '?' }),
        scopeSummary(rule)
          ? el('span', { class: 'pill judge', text: scopeSummary(rule) })
          : null,
      ]),
    ]);

    // 勾选框独立于卡片（button 里嵌 input 不合法），盖在右上角
    const wrap = el('div', { class: 'model-tile-wrap' });
    const check = el('input', { type: 'checkbox', class: 'batch-check', title: '选中以批量设置触发范围' });
    check.checked = selected.has(index);
    check.addEventListener('click', (event) => event.stopPropagation());
    check.addEventListener('change', (event) => {
      if (event.target.checked) selected.add(index);
      else selected.delete(index);
      tile.classList.toggle('tile-selected', event.target.checked);
      updateBatchStatus();
    });
    wrap.append(tile, check);
    tiles.push({ tile, rule, index });
    return wrap;
  }

  function applyFilter() {
    const needle = searchInput.value.trim().toLowerCase();
    let visible = 0;
    for (const { tile, rule } of tiles) {
      const haystack = `${commandLabel(rule)} ${rule.regex || ''} ${rule.action || ''}`.toLowerCase();
      const show = !needle || haystack.includes(needle);
      tile.classList.toggle('hidden', !show);
      if (show) visible += 1;
    }
    summary.textContent = `共 ${list.length} 条命令　·　显示 ${visible} 条`;
  }

  function runTest() {
    const text = testInput.value;
    let hitCount = 0;
    for (const { tile, rule } of tiles) {
      tile.classList.remove('hit');
      const regex = rule.regex;
      if (!regex || !text) continue;
      try {
        if (new RegExp(regex, 'i').test(text)) {
          tile.classList.add('hit');
          hitCount += 1;
        }
      } catch { /* 正则错误在详情页里提示 */ }
    }
    summary.textContent = text
      ? `命中 ${hitCount} 条　·　共 ${list.length} 条命令`
      : `共 ${list.length} 条命令`;
  }

  list.forEach((rule, index) => grid.append(tileFor(rule, index)));

  const addTile = el('button', {
    class: 'model-tile add', type: 'button',
    onclick: () => {
      list.push({ regex: '', action: state.actions[0] || 'send_message' });
      markDirty();
      commandDetail = list.length - 1;
      rerender(container);
    },
  }, [
    el('span', { class: 'model-tile-plus', text: '＋' }),
    el('span', { class: 'model-tile-add-text', text: '添加命令' }),
    el('span', { class: 'model-tile-add-hint', text: '正则 + action + params' }),
  ]);
  grid.append(addTile);

  /* ── 批量触发范围 ── */
  const batchButtons = [
    el('button', {
      class: 'btn mini', type: 'button', text: '全选',
      onclick: () => {
        list.forEach((_, index) => selected.add(index));
        rerender(container);
      },
    }),
    el('button', {
      class: 'btn mini ghost', type: 'button', text: '清除选择',
      onclick: () => { selected.clear(); rerender(container); },
    }),
    el('button', {
      class: 'btn mini', type: 'button', text: '批量设置触发范围',
      onclick: () => { batchOpen = !batchOpen; rerender(container); },
    }),
    el('button', {
      class: 'btn mini ghost', type: 'button', text: '清除触发范围',
      onclick: () => {
        if (!selected.size) return;
        if (!window.confirm(`清除选中的 ${selected.size} 条命令的触发范围？（恢复为所有群 + 所有私聊）`)) return;
        for (const index of selected) {
          const rule = list[index];
          if (rule?.params) delete rule.params.filter;
        }
        markDirty();
        selected.clear();
          batchOpen = false;
          rerender(container);
        },
      }),
  ];

  const batchPanel = el('div', { class: 'batch-scope' });
  function buildBatchPanel() {
    batchPanel.innerHTML = '';
    if (!batchOpen) { batchPanel.classList.add('hidden'); return; }
    batchPanel.classList.remove('hidden');
    const typeSelect = el('select');
    const types = [
      ['group', 'group：只在这些群触发'],
      ['private', 'private：只在这些 QQ 的私聊触发'],
      ['ban', 'ban：在这些群里不触发'],
    ];
    for (const [value, text] of types) {
      typeSelect.append(el('option', { value, text, selected: batchScope.type === value ? 'selected' : null }));
    }
    typeSelect.addEventListener('change', (event) => { batchScope.type = event.target.value; });
    batchPanel.append(
      el('div', { class: 'obj-sub' }, [
        el('span', { class: 'obj-sub-label', text: `把范围套用到选中的 ${selected.size} 条命令` }),
        typeSelect,
      ]),
      el('div', { class: 'obj-sub' }, [
        el('span', { class: 'obj-sub-label', text: '范围名单（群号 / QQ 号，应用到每一条）' }),
        renderListEditor({
          item: 'group',
          get: () => batchScope.data,
          set: (value) => { batchScope.data = value; },
        }),
      ]),
      el('div', { class: 'list-actions' }, [
        el('button', {
          class: 'btn mini primary', type: 'button', text: `应用到选中的 ${selected.size} 条`,
          onclick: () => {
            if (!selected.size) return;
            const filter = { type: batchScope.type, data: JSON.parse(JSON.stringify(batchScope.data)) };
            // 运行时语义：filter 放在 params.filter（pattern_match 从 params 里取）
            for (const index of selected) {
              const rule = list[index];
              if (!rule) continue;
              rule.params = { ...(rule.params || {}), filter: { ...filter, data: [...filter.data] } };
            }
            markDirty();
            batchOpen = false;
            selected.clear();
            rerender(container);
          },
        }),
        el('button', {
          class: 'btn mini ghost', type: 'button', text: '取消',
          onclick: () => { batchOpen = false; rerender(container); },
        }),
        el('span', { class: 'rule-tip', text: '名单会在应用时复制进每一条命令，之后各改各的互不影响' }),
      ]),
    );
  }
  buildBatchPanel();
  updateBatchStatus();

  return el('div', { class: 'card' }, [
    el('div', { class: 'section-head compact' }, [
      el('div', {}, [
        el('h2', { text: '命令' }),
        el('p', { class: 'hint', text: '一条命令 = 触发正则 + action + params。点卡片进详情修改；勾选卡片可批量设置触发范围。保存后热加载，下一条消息生效' }),
      ]),
      el('button', {
        class: 'btn mini primary', type: 'button', text: '＋ 添加命令',
        onclick: () => {
          list.push({ regex: '', action: state.actions[0] || 'send_message' });
          markDirty();
          commandDetail = list.length - 1;
          rerender(container);
        },
      }),
    ]),
    el('div', { class: 'tester' }, [testInput, summary]),
    el('div', { class: 'tester' }, [searchInput]),
    grid,
    el('div', { class: 'list-actions', style: 'margin-top:12px' }, [
      ...batchButtons,
      batchStatus,
      el('span', { class: 'list-status', text: `　共 ${list.length} 条，限定范围 ${list.filter((rule) => rule.params?.filter).length} 条` }),
    ]),
    batchPanel,
  ]);
}

/* ══════════════════════ 渲染入口 ══════════════════════ */
let currentContainer = null;

function rerender(container) {
  container.innerHTML = '';
  container.append(renderTabContent());
}

function renderDetailBody(container, rule, index) {
  // filter 勾选切换后重建整个详情（结构变化，简单可靠）
  rerender(container);
}

function renderTabContent() {
  const list = ruleList();
  if (commandDetail != null) {
    const index = Number(commandDetail);
    if (index >= 0 && index < list.length) {
      return el('div', { class: 'card' }, [
        el('h2', { text: '关键词 / 命令' }),
        renderDetail(list[index], index, currentContainer),
      ]);
    }
    commandDetail = null;   // 那条已经不存在了，退回目录
  }
  return renderGrid(currentContainer);
}

export function render(panel) {
  const container = el('div', { class: 'keywords-page' });
  currentContainer = container;
  panel.append(container);
  container.append(renderTabContent());
}

/* 兼容旧调用（测试 / 外部脚本可能引用）：等价于读 DOM 的旧实现不再需要，
 * 这里直接从 state 收集。 */
export function collectKeywordsFromDom() {
  const rules = ruleList();
  const errors = [];
  rules.forEach((rule, index) => {
    const params = rule.params;
    if (params !== undefined && (params === null || typeof params !== 'object' || Array.isArray(params))) {
      errors.push(`第 ${index + 1} 条：params 必须是 JSON 对象`);
    }
    if (rule.filter !== undefined && (rule.filter === null || typeof rule.filter !== 'object' || Array.isArray(rule.filter))) {
      errors.push(`第 ${index + 1} 条：filter 必须是对象`);
    }
  });
  return { rules, errors };
}
