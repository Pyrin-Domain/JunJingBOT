/**
 * 群聊插话标签页。
 */

import { el, renderFieldGroup } from '../dom.js';
import { FIELDS } from '../state.js';

export const TAB = { id: 'interject', label: '群聊插话' };

export function render(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '群聊主动插话' }),
    el('p', { class: 'hint', text: '群里攒够若干条消息、或者安静一阵后还有零星新消息，就把最近那段对话扒下来。判断的不是「要不要回」，而是「这一刻群里缺什么角色」：答一个没人接的问题、随口接一句、只贴个表情表示在场、或者干脆不说话。断点恢复的消息、以及当面叫它的消息（@我 / 引用我 / 直呼名字）都不计数。' }),
    renderFieldGroup(FIELDS.interject),
  ]));
}
