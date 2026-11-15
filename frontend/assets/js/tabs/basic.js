/**
 * 基础参数标签页：主人 QQ、心跳、消息开关，以及面板自身设置。
 */

import { el } from '../dom.js';
import { FIELDS, state } from '../state.js';
import { renderField, renderFieldGroup } from '../dom.js';

export const TAB = { id: 'basic', label: '基础参数' };

export function render(panel) {
  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '基础参数' }),
    el('p', { class: 'hint', text: '主人 QQ、心跳、消息处理开关' }),
    renderFieldGroup(FIELDS.basic),
  ]));
  panel.append(renderPanelConfigCard());
}

export function renderPanelConfigCard() {
  const wp = state.data.web_panel || {};
  return el('div', { class: 'card' }, [
    el('h2', { text: '网页参数面板' }),
    el('p', { class: 'hint', text: '面板自身的设置；改动需要重启 Bot 才会生效' }),
    el('div', { class: 'grid' }, [
      renderField({ path: 'web_panel.enable', label: '启用参数面板', type: 'bool' }),
      renderField({ path: 'web_panel.host', label: '监听地址', type: 'text', hint: '默认 127.0.0.1，只允许本机访问' }),
      renderField({ path: 'web_panel.port', label: '监听端口', type: 'number' }),
      renderField({ path: 'web_panel.token', label: '访问 token', type: 'text', hint: '监听非本机地址时必填；本机访问可留空' }),
      renderField({ path: 'web_panel.cors_origins', label: '允许跨源的页面地址', type: 'strlist', full: true, hint: '通常留空。只有前端单独部署时才填，例如 https://panel.example.com；不要填 *，除非明确要允许任意网站调用 API' }),
    ]),
    el('div', { class: 'hint', text: wp.token ? '当前已设置 token，浏览器需通过 ?token=xxx 打开面板' : '当前未设置 token' }),
  ]);
}
