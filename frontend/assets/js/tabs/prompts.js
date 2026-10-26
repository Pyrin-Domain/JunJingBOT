/**
 * 提示词与回复标签页。
 */

import { el, renderFieldGroup } from '../dom.js';
import { FIELDS } from '../state.js';

export const TAB = { id: 'text', label: '提示词与回复' };

export function render(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: 'AI 提示词' }),
    el('p', { class: 'hint', text: '{xxx} 是占位符，运行时按事件内容替换；改坏了 AI 会看不懂上下文' }),
    renderFieldGroup(FIELDS.text),
  ]));
}
