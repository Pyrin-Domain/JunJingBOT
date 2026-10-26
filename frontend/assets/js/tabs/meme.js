/**
 * 表情包标签页。
 */

import { el, renderFieldGroup } from '../dom.js';
import { FIELDS } from '../state.js';

export const TAB = { id: 'meme', label: '表情包' };

export function render(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '表情包' }),
    el('p', { class: 'hint', text: '君景用 <meme>名字</meme> 发 QQ 商城表情。表情清单放在 JSON 文件里，改表情包不用改代码：文件结构与 SuperMaGe 的 Meme.json 一致（faces = QQ 内置小表情，mfaces = 商城表情包，stickers = 本地图片）。加载时会把「名字：描述」写进提示词，模型只能从这份清单里挑。' }),
    el('p', { class: 'hint', text: '名字对不上时会按「包含 → 描述关键词」退让匹配；三级都匹配不到就跳过这一条，不会乱发。改完配置需要重启机器人生效（启动时读一次）。' }),
    renderFieldGroup(FIELDS.meme),
  ]));
}
