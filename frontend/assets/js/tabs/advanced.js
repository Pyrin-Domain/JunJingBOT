/**
 * 原始 JSON 标签页：直接编辑整份配置。
 * 通过 state.hooks 调用 app.js 的 renderAll / setBanner，避免循环 import。
 */

import { $, el } from '../dom.js';
import { hooks, markDirty, state } from '../state.js';

export const TAB = { id: 'raw', label: '原始 JSON' };

export function render(panel) {
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
            hooks.setBanner('ok', '已把 JSON 应用到表单，别忘了点「保存并热加载」');
            hooks.renderAll();
          } catch (err) {
            hooks.setBanner('err', 'JSON 解析失败', [String(err.message || err)]);
          }
        },
      }),
      el('button', {
        class: 'btn', type: 'button', text: '从表单刷新',
        onclick: () => {
          $('#rawJson').value = JSON.stringify(state.data, null, 2);
          hooks.setBanner('ok', '已用当前表单内容刷新 JSON');
        },
      }),
      el('button', {
        class: 'btn', type: 'button', text: '格式化',
        onclick: () => {
          const text = $('#rawJson').value;
          try {
            $('#rawJson').value = JSON.stringify(JSON.parse(text), null, 2);
          } catch (err) {
            hooks.setBanner('err', 'JSON 解析失败', [String(err.message || err)]);
          }
        },
      }),
    ]),
    textarea,
  ]));
}
