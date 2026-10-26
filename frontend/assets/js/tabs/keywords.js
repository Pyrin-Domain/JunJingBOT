/**
 * 关键词 / 命令标签页。
 *
 * 规则只存在于 DOM 里（正则需要用户边输边测）；保存时由
 * ``collectKeywordsFromDom`` 回写 ``state.data.keyword_actions``。
 */

import { $, el, jsonError } from '../dom.js';
import { ACTION_HINTS, state, markDirty } from '../state.js';

export const TAB = { id: 'keywords', label: '关键词 / 命令' };

export function ruleFromDom(row) {
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

export function collectKeywordsFromDom() {
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

export function renderKeywordRow(rule, index) {
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

export function updateRuleTip(row) {
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

export function moveRule(row, delta) {
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

export function renumberRules() {
  [...document.querySelectorAll('#ruleList .rule')].forEach((row, index) => {
    const idx = $('.idx', row);
    if (idx) idx.textContent = `#${index + 1}`;
  });
}

export function runKeywordTest() {
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

export function render(panel) {
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
