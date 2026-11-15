/**
 * 搬运与去重标签页：自动搬运字段 + 图片转发默认目标群。
 * 群列表加载也放在这里（列表编辑器需要群名 / 下拉框）。
 */

import { api, setBanner } from '../api.js';
import { el, renderFieldGroup, renderSubTabs } from '../dom.js';
import { FIELDS, state } from '../state.js';

import { IMAGE_FORWARD_FIELDS } from './image_forward.js';

export const TAB = { id: 'forward', label: '搬运与去重' };

const ALL_FIELDS = [...FIELDS.forward, ...IMAGE_FORWARD_FIELDS];
const FIELD_BY_PATH = new Map(ALL_FIELDS.map((field) => [field.path, field]));

function pickFields(paths) {
  return paths.map((path) => FIELD_BY_PATH.get(path)).filter(Boolean);
}

const SECTIONS = [
  {
    id: 'basic',
    label: '搬运设置',
    paths: [
      'auto_forward.enable',
      'auto_forward.dedup',
      'auto_forward.groupid_list',
      'auto_forward.target_groupid',
      'auto_forward.userid_list',
      'auto_forward.task_userid',
    ],
  },
  {
    id: 'filter',
    label: '转发过滤',
    paths: ['auto_forward.filter'],
  },
  {
    id: 'bot-image',
    label: '机器人图片',
    paths: [
      'auto_forward.auto_img_forward_from_bot.enable',
      'auto_forward.auto_img_forward_from_bot.bot_list',
      'forward_image_dedup.enable',
      'forward_image_dedup.keep_days',
      'forward_image_dedup.image_keep_days',
    ],
  },
  {
    id: 'tail-ads',
    label: '末尾广告 / 追加',
    paths: [
      'auto_forward.strip_tail_ads.enable',
      'auto_forward.strip_tail_ads.scan_tail',
      'auto_forward.strip_tail_ads.drop_qr',
      'auto_forward.strip_tail_ads.drop_group_card',
      'auto_forward.strip_tail_ads.drop_link',
      'auto_forward.strip_tail_ads.image_timeout',
      'auto_forward.strip_tail_ads.append.enable',
      'auto_forward.strip_tail_ads.append.items',
    ],
  },
  {
    id: 'image-fd',
    label: '图片批量转发',
    paths: ['image_forward.auto_target_group_ids'],
  },
];

export function render(panel) {
  const sections = SECTIONS.map((section) => ({
    id: section.id,
    label: section.label,
    content: renderFieldGroup(pickFields(section.paths)),
  }));

  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '自动搬运与去重' }),
    el('p', { class: 'hint', text: '来源群里的转发卡片 → 搬到目标群；去重按「内容指纹 + 标记的群」判定。「转发过滤」可以按来源群 + 发送者屏蔽某些聊天记录' }),
    renderSubTabs(sections, 'forward'),
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
