/**
 * 搬运与去重标签页：自动搬运字段 + 图片转发默认目标群。
 * 群列表加载也放在这里（列表编辑器需要群名 / 下拉框）。
 */

import { api, setBanner } from '../api.js';
import { el, renderFieldGroup } from '../dom.js';
import { FIELDS, state } from '../state.js';

import { IMAGE_FORWARD_FIELDS } from './image_forward.js';

export const TAB = { id: 'forward', label: '搬运与去重' };

export function render(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '自动搬运与去重' }),
    el('p', { class: 'hint', text: '来源群里的转发卡片 → 搬到目标群；去重按「内容指纹 + 标记的群」判定' }),
    renderFieldGroup([...FIELDS.forward, ...IMAGE_FORWARD_FIELDS]),
  ]));
}

/** 群列表：给列表编辑器显示群名 / 提供下拉框（取不到也不影响手填）。 */
export async function loadGroups({ silent = true } = {}) {
  const res = await api('/api/groups');
  const body = res.body || {};
  if (Array.isArray(body.groups)) {
    state.groupList = body.groups;
    state.groupNames = {};
    for (const item of body.groups) state.groupNames[String(item.group_id)] = item.group_name;
  }
  state.groupsError = body.error || '';
  // 重画各列表的群名 / 下拉框；返回 false 的（DOM 已被重建）顺手清掉
  state.listEditors = state.listEditors.filter((fn) => fn() !== false);
  if (!silent && state.groupsError) setBanner('warn', '群列表没取到', [state.groupsError]);
}
