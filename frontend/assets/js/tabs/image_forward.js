/**
 * 图片批量转发（img_fd）相关字段。
 *
 * 旧面板把这项放在「搬运与去重」页里；拆成独立模块只是为了让职责清楚，
 * 渲染结果仍然是同一张卡片里的一个字段。
 */

export const IMAGE_FORWARD_FIELDS = [
  {
    path: 'image_forward.auto_target_group_ids',
    label: 'img_fd -a 的默认目标群',
    type: 'intlist',
    item: 'group',
    full: true,
  },
];
