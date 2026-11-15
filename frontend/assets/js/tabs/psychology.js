/**
 * 心理分析标签页。
 *
 * 这一页只改 ``bot_config.psychology``：唤醒词、图片处理方式、独立模型、
 * 素材范围与提示词。和主对话的 ``agent.*`` 完全分开配——这个功能可以单独
 * 换一家服务商、单独关掉，不影响群聊。
 *
 * 数据形状：
 *   psychology.enable / keywords / image_mode
 *   psychology.model_id + base_url / api_key / api_key_env / model / temperature / max_tokens
 *   psychology.vision.{model_id, base_url, api_key, api_key_env, model}
 *   psychology.max_images / max_records / image_max_bytes / image_max_side
 *   psychology.use_image_filter / cooldown_seconds / groups / blacklist_groups / allow_private
 *   psychology.prompt / vision_prompt
 */

import { el, renderField } from '../dom.js';
import { markDirty, state } from '../state.js';
import { modelSelect } from './agent.js';

export const TAB = { id: 'psychology', label: '心理分析' };

/* ══════════════════════ 数据 ══════════════════════ */
export function psychologyConfig() {
  if (!state.data.psychology || typeof state.data.psychology !== 'object') {
    state.data.psychology = {};
  }
  const cfg = state.data.psychology;
  if (!cfg.vision || typeof cfg.vision !== 'object') cfg.vision = {};
  return cfg;
}

function agentConfig() {
  if (!state.data.agent || typeof state.data.agent !== 'object') state.data.agent = {};
  return state.data.agent;
}

function touch(cfg, key, value) {
  cfg[key] = value;
  markDirty();
}

/* ══════════════════════ 小控件 ══════════════════════ */
function fieldBox(label, control, hint, { full = false } = {}) {
  return el('div', { class: `field${full ? ' full' : ''}` }, [
    el('label', { text: label }),
    control,
    hint ? el('div', { class: 'hint', text: hint }) : null,
  ]);
}

function switchBox(label, checked, onChange, hint) {
  const text = el('span', { text: checked ? '已开启' : '已关闭' });
  const input = el('input', {
    type: 'checkbox',
    checked: checked ? 'checked' : null,
    onchange: (event) => {
      text.textContent = event.target.checked ? '已开启' : '已关闭';
      onChange(event.target.checked);
    },
  });
  return fieldBox(label, el('div', { class: 'switch' }, [input, text]), hint, { full: true });
}

function keywordsInput(cfg) {
  const input = el('input', {
    type: 'text',
    placeholder: '用逗号或空格分隔，例如：分析心理, 心理侧写',
    oninput: (event) => {
      const items = event.target.value
        .split(/[,，、\s]+/)
        .map((item) => item.trim())
        .filter(Boolean);
      touch(cfg, 'keywords', items);
    },
  });
  input.value = Array.isArray(cfg.keywords) ? cfg.keywords.join(', ') : '';
  return fieldBox(
    '唤醒词（整条消息等于其中一个词才触发）',
    input,
    '「帮我分析心理学」这种聊天不会触发；发词的时候可以顺手回复要分析的那条消息 / 聊天记录。',
    { full: true },
  );
}

function textArea(cfg, key, placeholder) {
  const area = el('textarea', {
    rows: 6,
    placeholder,
    oninput: (event) => touch(cfg, key, event.target.value),
  });
  area.value = cfg[key] == null ? '' : String(cfg[key]);
  return area;
}

function numberInput(cfg, key, { step = 1, placeholder = '' } = {}) {
  const input = el('input', {
    type: 'number',
    step,
    placeholder,
    oninput: (event) => {
      const raw = event.target.value;
      touch(cfg, key, raw === '' ? null : Number(raw));
    },
  });
  input.value = cfg[key] == null ? '' : String(cfg[key]);
  return input;
}

/* 模型配置：下拉引用 agent.models，或者展开一套独立参数 */
function modelPicker(cfg, key, { emptyLabel, onChanged }) {
  const select = modelSelect(agentConfig(), {
    value: cfg[key] || '',
    emptyLabel,
    onChange: (value) => {
      touch(cfg, key, value);
      onChanged();
    },
  });
  return select;
}

function modelFields(cfg, prefix, labels = {}) {
  return el('div', { class: 'grid model-fields' }, [
    renderField({ path: `${prefix}.base_url`, label: labels.base_url || '服务地址（base_url）', type: 'text', full: true, hint: '留空 = 沿用 AI Agent 里的服务地址；换服务商时这里必须填（密钥不会跟着借过去）' }),
    renderField({ path: `${prefix}.model`, label: labels.model || '模型名', type: 'text', hint: '例如 qwen-vl-max / deepseek-v4-flash / glm-4v' }),
    renderField({ path: `${prefix}.api_key`, label: labels.api_key || 'API Key', type: 'password', hint: '留空 = 读下面的环境变量；同服务商可以留空沿用主对话密钥' }),
    renderField({ path: `${prefix}.api_key_env`, label: labels.api_key_env || '密钥环境变量名', type: 'text', hint: '例如 DASHSCOPE_API_KEY' }),
  ]);
}

/* ══════════════════════ 回复形式 ══════════════════════ */
const REPLY_MODES = [
  {
    value: 'forward',
    title: '聊天记录（逐条侧写）',
    desc: '重建成一条聊天记录：每条原话后面跟 <心理侧写>，图片原样附上。就是 message_id:876984233 那种形态。',
  },
  {
    value: 'text',
    title: '一段文字总结',
    desc: '只发一段侧写：一句结论 / 情绪与动机 / 潜台词 / 怎么接。',
  },
  {
    value: 'both',
    title: '两者都发',
    desc: '先发一段总结，再发逐条聊天记录。会多一次模型调用，额度翻倍。',
  },
];

function renderReplyCard(cfg, mode, refresh) {
  const active = (cfg.reply_mode || 'forward') === mode.value;
  const input = el('input', {
    type: 'radio',
    name: 'psychology_reply_mode',
    value: mode.value,
    checked: active ? 'checked' : null,
    onchange: () => {
      touch(cfg, 'reply_mode', mode.value);
      refresh();
    },
  });
  return el('label', { class: `mode-card${active ? ' active' : ''}` }, [
    input,
    el('div', { class: 'mode-body' }, [
      el('div', { class: 'mode-title', text: mode.title }),
      el('div', { class: 'hint', text: mode.desc }),
    ]),
  ]);
}

/* ══════════════════════ 三个图片模式 ══════════════════════ */
const MODES = [
  {
    value: 'image',
    title: '原样发送图片',
    desc: '图片直接作为图片输入发给分析模型。信息最全（表情、排版、截图细节都在），但分析模型必须是多模态模型，token 也最贵。',
  },
  {
    value: 'ocr',
    title: '不发送图片（仅 OCR）',
    desc: '只把图片里的文字 OCR 出来拼进聊天记录，不把图片本身发出去。最省、最保守；表情、构图和 OCR 认不出的部分会丢。',
  },
  {
    value: 'vision',
    title: '辅助视觉分析',
    desc: '先用视觉模型逐张写客观描述，再把描述交给分析模型。分析模型可以用便宜的纯文本模型，视觉模型单独选。',
  },
];

function renderModeCard(cfg, mode, refresh) {
  const active = (cfg.image_mode || 'vision') === mode.value;
  const input = el('input', {
    type: 'radio',
    name: 'psychology_image_mode',
    value: mode.value,
    checked: active ? 'checked' : null,
    onchange: () => {
      touch(cfg, 'image_mode', mode.value);
      refresh();
    },
  });
  return el('label', { class: `mode-card${active ? ' active' : ''}` }, [
    input,
    el('div', { class: 'mode-body' }, [
      el('div', { class: 'mode-title', text: mode.title }),
      el('div', { class: 'hint', text: mode.desc }),
    ]),
  ]);
}

/* ══════════════════════ 页面 ══════════════════════ */
export function render(panel) {
  const cfg = psychologyConfig();
  const currentMode = cfg.image_mode || 'vision';
  const currentReply = cfg.reply_mode || 'forward';

  // 这一页有「切换模式后整页重建」的需求（视觉模型是否显示），
  // 但输入都是即时写回 state 的，重建不会丢数据。
  const refresh = () => {
    panel.innerHTML = '';
    render(panel);
  };

  panel.append(
    el('div', { class: 'card' }, [
      el('div', { class: 'section-head compact' }, [
        el('div', {}, [
          el('h2', { text: '心理分析' }),
          el('p', {
            class: 'hint',
            text: '群里发唤醒词（默认「分析心理」）并回复一条消息或一段聊天记录，Bot 用这里配置的独立模型做一次心理侧写。模型没配好时只回一句提示，不会占用主对话的模型。',
          }),
        ]),
      ]),
      switchBox('启用心理分析', Boolean(cfg.enable), (value) => touch(cfg, 'enable', value), '关闭时连唤醒词都不注册，群里 @ 也不会触发。'),
      keywordsInput(cfg),
      renderField({ path: 'psychology.cooldown_seconds', label: '同一会话冷却（秒）', type: 'number', step: 1, hint: '两次分析之间的最小间隔，防止刷屏；0 = 不限制' }),
      renderField({ path: 'psychology.groups', label: '只在这些群生效（留空 = 所有群）', type: 'intlist', item: 'group', full: true }),
      renderField({ path: 'psychology.blacklist_groups', label: '这些群永不触发', type: 'intlist', item: 'group', full: true }),
      switchBox('私聊也能用', Boolean(cfg.allow_private), (value) => touch(cfg, 'allow_private', value)),
    ]),

    el('div', { class: 'card' }, [
      el('div', { class: 'section-head compact' }, [
        el('div', {}, [
          el('h2', { text: '回复形式' }),
          el('p', { class: 'hint', text: '默认发成一条聊天记录：每条原话后面跟 <心理侧写>，图片原样附上。想换回一段总结就切到「文字总结」。' }),
        ]),
      ]),
      el('div', { class: 'mode-grid' }, REPLY_MODES.map((mode) => renderReplyCard(cfg, mode, refresh))),
      ...(currentReply === 'text'
        ? []
        : [
            switchBox(
              '聊天记录里附带原图',
              cfg.forward_include_images !== false,
              (value) => touch(cfg, 'forward_include_images', value),
              '图片已过过滤门，命中的不会附；总量超过 12MB 时后面的图自动不附，避免请求过大。',
            ),
            renderField({
              path: 'psychology.forward_max_tokens',
              label: '逐条侧写的输出额度',
              type: 'number',
              step: 100,
              hint: '默认 2000。条数多 / 分析写得长时需要更大；太小会被截断，后面的消息只剩原话。',
            }),
          ]),
    ]),

    el('div', { class: 'card' }, [
      el('div', { class: 'section-head compact' }, [
        el('div', {}, [
          el('h2', { text: '图片怎么处理' }),
          el('p', { class: 'hint', text: '图片要发到外部模型，所以单独给一档开关。默认「辅助视觉分析」：先用视觉模型看懂，再让分析模型说话。' }),
        ]),
      ]),
      el('div', { class: 'mode-grid' }, MODES.map((mode) => renderModeCard(cfg, mode, refresh))),
      el('div', { class: 'grid' }, [
        renderField({
          path: 'psychology.max_images',
          label: '每次最多送进模型几张图',
          type: 'number',
          step: 1,
          hint: '0 = 不带图；记录里超过这个数的图只在文本里留 [图片N] 占位。',
        }),
        renderField({
          path: 'psychology.max_records',
          label: '聊天记录最多展开几条',
          type: 'number',
          step: 1,
          hint: '防止超大记录把请求撑爆；超出部分会标「只截取了一部分」。',
        }),
      ]),
      switchBox(
        '发图前过一遍图片过滤门',
        Boolean(cfg.use_image_filter),
        (value) => touch(cfg, 'use_image_filter', value),
        '命中的图片不会发给模型（记录里仍然保留占位，不会假装没这张图）。过滤门本身在「图片过滤」页配置。',
      ),
      el('details', { class: 'advanced-block' }, [
        el('summary', { text: '图片压缩参数（一般不用改）' }),
        el('div', { class: 'grid' }, [
          renderField({ path: 'psychology.image_max_bytes', label: '单张图上限（字节）', type: 'number', step: 1024, hint: '压缩后仍超过就跳过这张图；默认 4MB。' }),
          renderField({ path: 'psychology.image_max_side', label: '最长边（像素）', type: 'number', step: 1, hint: '默认 1568：视觉模型再大也基本看不细，白花 token。' }),
        ]),
      ]),
    ]),

    el('div', { class: 'card' }, [
      el('div', { class: 'section-head compact' }, [
        el('div', {}, [
          el('h2', { text: '分析模型（和主对话分开）' }),
          el('p', {
            class: 'hint',
            text: currentMode === 'image'
              ? '当前是「原样发送图片」，这个模型必须支持图片输入（多模态），否则会把图片当文字报错。'
              : '这个模型只负责最后的分析和说话，用便宜的文本模型就够。留空 model_id 时可单独填服务地址和密钥。',
          }),
        ]),
      ]),
      fieldBox(
        '从 AI Agent 的模型里选一条',
        modelPicker(cfg, 'model_id', {
          emptyLabel: '（不用现成的，填下面的独立配置）',
          onChanged: refresh,
        }),
        '列表来自「AI Agent」页的模型目录；选一条后下面的独立配置会被忽略。',
        { full: true },
      ),
      cfg.model_id ? el('p', { class: 'hint', text: '已选择上面的现成模型，下面的独立配置会被忽略（留着不会生效，可以清空）。' }) : null,
      modelFields(cfg, 'psychology', { model: '模型名（必填）' }),
      el('div', { class: 'grid' }, [
        renderField({ path: 'psychology.temperature', label: 'temperature', type: 'number', step: 0.1, hint: '侧写建议 0.4~0.6：太低像判决书，太高容易编。' }),
        renderField({ path: 'psychology.max_tokens', label: 'max_tokens', type: 'number', step: 100, hint: '默认 900；太长会拆成多条发出去。' }),
      ]),
    ]),

    currentMode === 'ocr' ? null : el('div', { class: 'card' }, [
      el('div', { class: 'section-head compact' }, [
        el('div', {}, [
          el('h2', { text: '视觉模型（可选）' }),
          el('p', {
            class: 'hint',
            text: currentMode === 'vision'
              ? '「辅助视觉分析」先用它逐张写客观描述。留空 model_id 且不填模型名时，直接用上面的分析模型看图。'
              : '「原样发送图片」不经过视觉模型，这一块不用配。',
          }),
        ]),
      ]),
      fieldBox(
        '从 AI Agent 的模型里选一条',
        modelPicker(cfg.vision || {}, 'model_id', {
          emptyLabel: '（不用现成的，填下面的独立配置）',
          onChanged: refresh,
        }),
        '留空 = 用分析模型看图；只有分析模型是纯文本模型时才需要单独配。',
        { full: true },
      ),
      (cfg.vision && cfg.vision.model_id) ? el('p', { class: 'hint', text: '已选择现成模型，下面的独立配置会被忽略。' }) : null,
      modelFields(cfg.vision || {}, 'psychology.vision', { model: '视觉模型名（例如 qwen-vl-max）' }),
    ]),

    el('div', { class: 'card' }, [
      el('div', { class: 'section-head compact' }, [
        el('div', {}, [
          el('h2', { text: '提示词' }),
          el('p', { class: 'hint', text: '留空用内置提示词（含「不做临床诊断 / 不编造隐私 / 材料不足就说」这些边界）。想换输出结构时再改这里。' }),
        ]),
      ]),
      fieldBox('分析提示词', textArea(cfg, 'prompt', '留空 = 内置提示词（一段总结）'), null, { full: true }),
      fieldBox('逐条侧写提示词', textArea(cfg, 'nodes_prompt', '留空 = 内置提示词（聊天记录模式：每条原话 + <心理侧写>）'), null, { full: true }),
      fieldBox('视觉描述提示词', textArea(cfg, 'vision_prompt', '留空 = 内置提示词（只描述，不分析）'), null, { full: true }),
    ]),
  );
}
