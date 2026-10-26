/**
 * AI Agent 标签页：模型目录 / 单条详情 / 分级与降级 / 生成参数 / 概览。
 *
 * 数据模型（与旧面板一致）：
 *   agent.models          [{ id, name, base_url, api_key, api_key_env, model, temperature?, max_tokens? }]
 *   agent.active_model    使用中的配置 id（同时把扁平字段同步给 agent.*）
 *   agent.judge_model     插话判断器用的 id（空 = 跟随主模型）
 *   agent.fallback_models 429 降级顺序
 */

import { $, el, renderField } from '../dom.js';
import { MODEL_KEYS, markDirty, state } from '../state.js';

export const TAB = { id: 'agent', label: 'AI Agent' };

// 配置目录 → 详情的导航状态：null = 目录（卡片网格），否则为正在编辑的配置 id
let modelDetailId = null;

export function getAgentConfig() {
  if (!state.data.agent || typeof state.data.agent !== 'object') state.data.agent = {};
  return state.data.agent;
}

export function modelList(agent) {
  if (!Array.isArray(agent.models)) agent.models = [];
  return agent.models;
}

export function nextModelId(list) {
  let max = 0;
  for (const item of list) {
    const matched = /^m(\d+)$/.exec(String((item && item.id) || ''));
    if (matched) max = Math.max(max, Number(matched[1]));
  }
  return `m${max + 1}`;
}

/* 新增 / 复制时以「当前生效参数」打底，省得从零填地址和密钥 */
export function entryFromLive(agent, patch = {}) {
  const entry = { id: nextModelId(modelList(agent)), name: '' };
  for (const key of MODEL_KEYS) entry[key] = String(agent[key] || '');
  entry.temperature = null;   // 留空 = 跟随主模型的生成参数
  entry.max_tokens = null;
  return { ...entry, ...patch };
}

/* 把存下来的 agent.models 整理成干净的对象数组；老配置（只有一组 agent.* 参数）
   会自动收成第一条，保证页面里始终可增删 */
export function normalizeAgentModels(agent) {
  const hadList = Array.isArray(agent.models);
  const seen = new Set();
  const kept = [];
  for (const raw of modelList(agent)) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
    const entry = { name: raw.name == null ? '' : String(raw.name) };
    for (const key of MODEL_KEYS) entry[key] = raw[key] == null ? '' : String(raw[key]);
    for (const key of ['temperature', 'max_tokens']) {
      const value = raw[key];
      entry[key] = value == null || value === '' ? null : Number(value);
    }
    let id = String(raw.id == null ? '' : raw.id).trim();
    if (!id || seen.has(id)) {
      id = `m${kept.length + 1}`;
      while (seen.has(id)) id += '_';
    }
    entry.id = id;
    seen.add(id);
    kept.push(entry);
  }
  // 只有「老配置里压根没有 models 字段」时才从当前参数收成一条；
  // 用户自己删空（models = []）就保持空的，不要硬塞回来
  if (!kept.length && !hadList && MODEL_KEYS.some((key) => String(agent[key] || '').trim())) {
    kept.push({ ...entryFromLive(agent), id: 'm1' });
  }
  agent.models = kept;
  if (!kept.some((item) => item.id === agent.active_model)) {
    agent.active_model = kept.length ? kept[0].id : '';
  }
  // 分级 / 降级引用的 id 可能已经删掉了：清成「跟随主模型」/ 空列表
  if (!kept.some((item) => item.id === agent.judge_model)) agent.judge_model = '';
  if (!Array.isArray(agent.fallback_models)) agent.fallback_models = [];
  agent.fallback_models = agent.fallback_models
    .map((id) => String(id || ''))
    .filter((id) => kept.some((item) => item.id === id) && id !== agent.active_model);
  delete agent.provider_index;   // 老字段：模型改由 models 列表维护
  return kept;
}

export function activeModelEntry(agent) {
  return modelList(agent).find((item) => item.id === agent.active_model) || null;
}

export function modelTitle(entry, index) {
  const name = String((entry && entry.name) || '').trim();
  if (name) return name;
  const model = String((entry && entry.model) || '').trim();
  return model || `未命名配置 ${index + 1}`;
}

export function modelSelect(agent, { value, onChange, emptyLabel, exclude = [] }) {
  const list = modelList(agent).filter((item) => !exclude.includes(item.id));
  const select = el('select', { onchange: (event) => onChange(event.target.value) }, [
    el('option', { value: '', text: emptyLabel }),
    ...list.map((item) => el('option', {
      value: item.id,
      text: `${modelTitle(item, modelList(agent).indexOf(item))}${item.model ? `　·　${item.model}` : ''}`,
    })),
  ]);
  select.value = value || '';
  return select;
}

/* 分级模型：插话判断器用哪条 / 主模型 429 时按什么顺序降级 */
export function renderRoutingCard(agent) {
  const list = modelList(agent);
  const active = activeModelEntry(agent);

  const judgeSelect = modelSelect(agent, {
    value: agent.judge_model,
    emptyLabel: active ? `跟随主模型（${modelTitle(active, list.indexOf(active))}）` : '跟随主模型',
    onChange: (value) => {
      agent.judge_model = value;
      markDirty();
      rerenderAgentPanel();
    },
  });

  const fallbackIds = Array.isArray(agent.fallback_models) ? agent.fallback_models : [];
  const fallbackRows = fallbackIds.map((id, index) => {
    const used = [agent.active_model, ...fallbackIds.filter((_, i) => i !== index)];
    return el('div', { class: 'routing-row' }, [
      el('span', { class: 'routing-order', text: String(index + 1) }),
      modelSelect(agent, {
        value: id,
        emptyLabel: '选择一条模型',
        exclude: used,
        onChange: (value) => {
          agent.fallback_models[index] = value;
          agent.fallback_models = agent.fallback_models.filter(Boolean);
          markDirty();
          rerenderAgentPanel();
        },
      }),
      el('button', {
        class: 'btn mini ghost', type: 'button', text: '↑',
        disabled: index === 0 ? 'disabled' : null,
        onclick: () => {
          const ids = [...fallbackIds];
          [ids[index - 1], ids[index]] = [ids[index], ids[index - 1]];
          agent.fallback_models = ids;
          markDirty(); rerenderAgentPanel();
        },
      }),
      el('button', {
        class: 'btn mini ghost', type: 'button', text: '↓',
        disabled: index === fallbackIds.length - 1 ? 'disabled' : null,
        onclick: () => {
          const ids = [...fallbackIds];
          [ids[index + 1], ids[index]] = [ids[index], ids[index + 1]];
          agent.fallback_models = ids;
          markDirty(); rerenderAgentPanel();
        },
      }),
      el('button', {
        class: 'btn mini ghost danger', type: 'button', text: '移除',
        onclick: () => {
          agent.fallback_models = fallbackIds.filter((_, i) => i !== index);
          markDirty(); rerenderAgentPanel();
        },
      }),
    ]);
  });

  const available = list.filter((item) => item.id !== agent.active_model && !fallbackIds.includes(item.id));
  const addFallback = el('button', {
    class: 'btn mini', type: 'button', text: '＋ 添加备用模型',
    disabled: available.length ? null : 'disabled',
    onclick: () => {
      agent.fallback_models = [...fallbackIds, available[0].id];
      markDirty(); rerenderAgentPanel();
    },
  });

  const chain = active
    ? [active, ...fallbackIds.map((id) => list.find((item) => item.id === id)).filter(Boolean)]
      .map((item) => modelTitle(item, list.indexOf(item)))
      .join('  →  ')
    : '还没有启用任何模型';

  return el('div', { class: 'card routing-card' }, [
    el('div', { class: 'section-head compact' }, [
      el('div', {}, [
        el('h2', { text: '分级与降级' }),
        el('p', { class: 'hint', text: '插话判断器可以单独用小模型（只回答 yes / no）；主回答模型返回 429 时，按下面的顺序自动换模型重试。普通限流冷却 60 秒，日额度 / 余额用完则冷却 30 分钟' }),
      ]),
    ]),
    el('div', { class: 'routing-block' }, [
      el('div', { class: 'routing-label' }, [
        el('strong', { text: '插话判断器' }),
        el('span', { class: 'hint', text: '群聊插话第一步：判断「现在插一句自不自然」' }),
      ]),
      judgeSelect,
    ]),
    el('div', { class: 'routing-block' }, [
      el('div', { class: 'routing-label' }, [
        el('strong', { text: '429 降级链' }),
        el('span', { class: 'hint', text: '主模型在前，限流 / 额度用尽时依次往后换；全部都在冷却时才重新试主模型' }),
      ]),
      el('div', { class: 'routing-chain', text: chain }),
      ...fallbackRows,
      addFallback,
    ]),
  ]);
}

export function rerenderAgentPanel() {
  const page = document.querySelector('.page[data-tab="agent"]');
  if (!page) return;
  page.replaceChildren();
  render(page);
}

export function openModelDetail(id) {
  modelDetailId = id;
  rerenderAgentPanel();
}

export function closeModelDetail() {
  modelDetailId = null;
  rerenderAgentPanel();
}

export function modelHost(baseUrl) {
  const raw = String(baseUrl || '').trim();
  if (!raw) return '未填服务地址';
  return raw.replace(/^https?:\/\//i, '').replace(/\/+$/, '');
}

/* 启用一条配置：把它整组参数写进 agent.*（Bot 只认这几个扁平字段） */
export function activateModel(id) {
  const agent = getAgentConfig();
  const entry = modelList(agent).find((item) => item.id === id);
  if (!entry) return;
  agent.active_model = id;
  for (const key of MODEL_KEYS) agent[key] = entry[key] || '';
  markDirty();
  rerenderAgentPanel();
}

export function addModel(copyFrom = null) {
  const agent = getAgentConfig();
  const list = modelList(agent);
  const entry = copyFrom
    ? { ...copyFrom, id: nextModelId(list), name: copyFrom.name ? `${copyFrom.name} 副本` : '' }
    : entryFromLive(agent);
  list.push(entry);
  if (!agent.active_model) agent.active_model = entry.id;
  // 新增/复制后直接进这条的详情页，省得再点一次
  modelDetailId = entry.id;
  markDirty();
  rerenderAgentPanel();
}

export function removeModel(id) {
  const agent = getAgentConfig();
  const list = modelList(agent);
  const index = list.findIndex((item) => item.id === id);
  if (index < 0) return;
  list.splice(index, 1);
  if (agent.active_model === id) {
    // 删掉的正好是使用中的那条：顺位启用下一条（没有了就往前找）
    const next = list[index] || list[index - 1] || null;
    agent.active_model = next ? next.id : '';
    if (next) for (const key of MODEL_KEYS) agent[key] = next[key] || '';
  }
  if (modelDetailId === id) {
    const next = list[index] || list[index - 1] || null;
    modelDetailId = next ? next.id : null;
  }
  markDirty();
  rerenderAgentPanel();
}

export function moveModel(id, delta) {
  const list = modelList(getAgentConfig());
  const index = list.findIndex((item) => item.id === id);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= list.length) return;
  const [entry] = list.splice(index, 1);
  list.splice(target, 0, entry);
  markDirty();
  rerenderAgentPanel();
}

export function updateAgentSummary() {
  const host = $('#agentSummary');
  if (host) host.replaceChildren(renderAgentSummary(getAgentConfig()));
}

export function renderAgentSummary(agent) {
  const list = modelList(agent);
  const entry = activeModelEntry(agent);
  const judge = list.find((item) => item.id === agent.judge_model) || null;
  const fallbacks = (Array.isArray(agent.fallback_models) ? agent.fallback_models : [])
    .map((id) => list.find((item) => item.id === id))
    .filter(Boolean);
  const keySource = agent.api_key
    ? '配置文件中的显式密钥'
    : (agent.api_key_env ? `环境变量 ${agent.api_key_env}` : '未配置（读不到密钥）');
  const items = [
    ['接入状态', agent.enable ? '已启用' : '已关闭', agent.enable ? 'on' : 'off'],
    ['使用中的配置', entry ? modelTitle(entry, list.indexOf(entry)) : '未选择', ''],
    ['服务地址', agent.base_url || '未配置', ''],
    ['模型名', agent.model || '未配置', ''],
    ['采样温度', String(agent.temperature ?? 0.7), ''],
    ['回复上限', agent.max_tokens ? `${agent.max_tokens} tokens` : '环境变量 / 默认 10240', ''],
    ['密钥来源', keySource, ''],
    ['插话判断器', judge ? modelTitle(judge, list.indexOf(judge)) : '跟随主模型', ''],
    ['429 降级链', fallbacks.length
      ? [entry, ...fallbacks].filter(Boolean).map((item) => modelTitle(item, list.indexOf(item))).join(' → ')
      : '无备用模型（429 直接失败）', fallbacks.length ? 'on' : 'off'],
  ];
  const grid = el('div', { class: 'summary-grid' });
  for (const [label, value, className] of items) {
    grid.append(el('div', { class: 'summary-item' }, [
      el('span', { class: 'summary-label', text: label }),
      el('span', { class: `summary-value${className ? ` ${className}` : ''}`, text: value }),
    ]));
  }
  return grid;
}

/* 密码框 + 显示/隐藏 */
export function secretInput({ value, onChange, id, placeholder }) {
  const input = el('input', {
    type: 'password',
    id,
    autocomplete: 'new-password',
    placeholder,
    oninput: (event) => onChange(event.target.value),
  });
  input.value = value || '';
  const toggle = el('button', {
    class: 'secret-toggle',
    type: 'button',
    text: '显示',
    onclick: (event) => {
      const visible = input.type === 'text';
      input.type = visible ? 'password' : 'text';
      event.target.textContent = visible ? '显示' : '隐藏';
    },
  });
  return el('div', { class: 'secret-row' }, [input, toggle]);
}

export function modelField(label, control, hint, wide = false) {
  return el('div', { class: `model-field${wide ? ' wide' : ''}` }, [
    el('span', { class: 'model-field-label', text: label }),
    control,
    hint ? el('span', { class: 'model-field-hint', text: hint }) : null,
  ]);
}

/* 目录里的一张配置卡片：点进去才编辑，避免一屏全是输入框 */
export function renderModelTile(agent, entry, index) {
  const isActive = entry.id === agent.active_model;
  const isJudge = entry.id === agent.judge_model;
  const fallbackIndex = (Array.isArray(agent.fallback_models) ? agent.fallback_models : []).indexOf(entry.id);
  const name = String(entry.name || '').trim();
  const keyHint = entry.api_key
    ? '已填密钥'
    : (entry.api_key_env ? `密钥：${entry.api_key_env}` : '未填密钥');
  return el('button', {
    class: `model-tile${isActive ? ' active' : ''}`,
    type: 'button',
    title: '打开这条配置',
    onclick: () => openModelDetail(entry.id),
  }, [
    el('span', { class: 'model-tile-top' }, [
      el('span', { class: 'model-tile-idx', text: String(index + 1) }),
      el('span', {
        class: `model-tile-name${name ? '' : ' unnamed'}`,
        text: name || `未命名配置 ${index + 1}`,
      }),
      isActive ? el('span', { class: 'pill on', text: '使用中' }) : null,
      isJudge ? el('span', { class: 'pill judge', text: '判断器' }) : null,
      fallbackIndex >= 0 ? el('span', { class: 'pill fallback', text: `备用 ${fallbackIndex + 1}` }) : null,
    ]),
    el('span', { class: 'model-tile-model', text: entry.model || '未填模型名' }),
    el('span', { class: 'model-tile-url', text: modelHost(entry.base_url) }),
    el('span', { class: 'model-tile-foot' }, [
      el('span', { class: 'model-tile-meta', text: keyHint }),
      el('span', { class: 'model-tile-enter', text: '进入 ›' }),
    ]),
  ]);
}

/* 一条配置的详情页：字段编辑 + 启用 / 复制 / 排序 / 删除 */
export function renderModelDetail(agent, entry, index) {
  const list = modelList(agent);
  const isActive = entry.id === agent.active_model;
  const setValue = (key, value) => {
    entry[key] = value;
    // 只有「使用中」那条才同步给 Bot 真正读取的 agent.* 字段
    if (isActive && MODEL_KEYS.includes(key)) agent[key] = value;
    markDirty();
    updateAgentSummary();
  };

  const titleNode = el('strong', { class: 'model-detail-name', text: modelTitle(entry, index) });

  const nameInput = el('input', {
    type: 'text',
    placeholder: '备注名（留空则显示模型名）',
    oninput: (event) => {
      setValue('name', event.target.value);
      titleNode.textContent = modelTitle(entry, index);
    },
  });
  nameInput.value = entry.name || '';
  const baseUrlInput = el('input', {
    type: 'text',
    placeholder: 'https://api.example.com/v1',
    oninput: (event) => setValue('base_url', event.target.value),
  });
  baseUrlInput.value = entry.base_url || '';
  const modelInput = el('input', {
    type: 'text',
    placeholder: '服务商文档里的模型 ID（区分大小写）',
    oninput: (event) => setValue('model', event.target.value),
  });
  modelInput.value = entry.model || '';
  const envInput = el('input', {
    type: 'text',
    placeholder: '可选，例如 MY_API_KEY',
    oninput: (event) => setValue('api_key_env', event.target.value.trim()),
  });
  envInput.value = entry.api_key_env || '';

  // 这条模型自己的生成参数：留空 = 跟随「生成参数」卡片里的主模型设置
  const tempInput = el('input', {
    type: 'number', min: '0', max: '2', step: '0.1',
    placeholder: '留空 = 跟随主模型',
    oninput: (event) => {
      entry.temperature = event.target.value === '' ? null : Number(event.target.value);
      markDirty();
    },
  });
  tempInput.value = entry.temperature == null ? '' : String(entry.temperature);
  const tokensInput = el('input', {
    type: 'number', min: '1', step: '1',
    placeholder: '留空 = 跟随主模型',
    oninput: (event) => {
      entry.max_tokens = event.target.value === '' ? null : Number(event.target.value);
      markDirty();
    },
  });
  tokensInput.value = entry.max_tokens == null ? '' : String(entry.max_tokens);

  const head = el('div', { class: 'model-detail-head' }, [
    el('button', {
      class: 'btn mini ghost model-back', type: 'button', text: '← 配置列表',
      title: '返回配置目录', onclick: closeModelDetail,
    }),
    el('div', { class: 'model-detail-title' }, [
      titleNode,
      el('span', {
        class: 'hint',
        text: `第 ${index + 1} / ${list.length} 条${isActive ? '　·　当前使用中' : ''}`,
      }),
    ]),
    el('div', { class: 'model-detail-head-actions' }, [
      isActive
        ? el('span', { class: 'pill on', text: '使用中' })
        : el('button', {
          class: 'btn mini primary', type: 'button', text: '设为使用中',
          onclick: () => activateModel(entry.id),
        }),
    ]),
  ]);

  const body = el('div', { class: 'model-body' }, [
    modelField('备注名', nameInput, '只是给自己看的名字，留空显示模型名'),
    modelField('模型名（model）', modelInput, '填服务商文档里的模型 ID'),
    modelField('服务地址（base_url）', baseUrlInput, 'OpenAI 兼容接口地址，通常带 /v1 这类前缀', true),
    modelField('环境变量名（api_key_env）', envInput, 'API Key 留空时读这个环境变量'),
    modelField('API Key（可留空）', secretInput({
      id: `f_model_key_${entry.id}`,
      value: entry.api_key,
      placeholder: '留空 = 用上面的环境变量',
      onChange: (value) => setValue('api_key', value),
    }), '优先使用这里填的密钥；概览不会回显', true),
    modelField('temperature（可留空）', tempInput, '这条模型自己的采样温度；留空 = 跟随主模型'),
    modelField('max_tokens（可留空）', tokensInput, '这条模型自己的回复上限；留空 = 跟随主模型'),
  ]);

  const foot = el('div', { class: 'model-detail-foot' }, [
    el('div', { class: 'model-detail-actions' }, [
      el('button', {
        class: 'btn mini', type: 'button', text: '复制这条',
        title: '复制这条配置，改个模型名就是另一条', onclick: () => addModel(entry),
      }),
      el('button', {
        class: 'btn mini ghost', type: 'button', text: '↑ 上移', title: '上移',
        disabled: index === 0 ? 'disabled' : null, onclick: () => moveModel(entry.id, -1),
      }),
      el('button', {
        class: 'btn mini ghost', type: 'button', text: '↓ 下移', title: '下移',
        disabled: index === list.length - 1 ? 'disabled' : null, onclick: () => moveModel(entry.id, 1),
      }),
      el('button', {
        class: 'btn mini ghost danger', type: 'button', text: '删除这条',
        onclick: () => removeModel(entry.id),
      }),
    ]),
    el('span', { class: 'hint', text: isActive
      ? '这条正在生效：改动实时同步到当前参数，点右上角「保存并热加载」后应用'
      : '未启用：只作为备用配置保存，点「设为使用中」才会切换成实际调用的模型' }),
  ]);

  return el('div', { class: 'card model-detail' }, [head, body, foot]);
}

export function renderModelsCard(agent) {
  const list = modelList(agent);
  if (modelDetailId) {
    const entry = list.find((item) => item.id === modelDetailId);
    if (entry) return renderModelDetail(agent, entry, list.indexOf(entry));
    modelDetailId = null;   // 那条已经不存在了，退回目录
  }

  const active = activeModelEntry(agent);
  const addTile = el('button', {
    class: 'model-tile add', type: 'button', onclick: () => addModel(),
  }, [
    el('span', { class: 'model-tile-plus', text: '＋' }),
    el('span', { class: 'model-tile-add-text', text: '新增配置' }),
    el('span', { class: 'model-tile-add-hint', text: '自定义服务地址 / 密钥 / 模型名' }),
  ]);

  const content = list.length
    ? el('div', { class: 'model-grid' }, [
      ...list.map((entry, index) => renderModelTile(agent, entry, index)),
      addTile,
    ])
    : el('div', { class: 'model-empty' }, [
      el('div', { class: 'hint', text: '还没有模型配置。点「＋ 新增配置」，填上服务地址、API Key 和模型名即可；想加几个就加几个。' }),
      el('div', { class: 'hint', text: `Bot 现在用的是已保存的接入参数：${agent.base_url || '未配置地址'}　${agent.model || '未配置模型'}` }),
      el('div', { class: 'model-empty-actions' }, [addTile]),
    ]);

  return el('div', { class: 'card' }, [
    el('div', { class: 'section-head compact' }, [
      el('div', {}, [
        el('h2', { text: '模型配置' }),
        el('p', { class: 'hint', text: '一条配置 = 服务地址 + 密钥 + 模型名；点卡片进入详情修改，可新增、复制、排序、删除' }),
      ]),
      el('button', { class: 'btn mini primary', type: 'button', text: '＋ 新增配置', onclick: () => addModel() }),
    ]),
    content,
    el('div', { class: 'list-actions' }, [
      el('span', {
        class: 'list-status',
        text: `共 ${list.length} 条配置${active ? `　·　使用中：${modelTitle(active, list.indexOf(active))}` : '　·　还没有启用任何一条'}`,
      }),
    ]),
  ]);
}

export function renderAgentOverview(agent) {
  const list = modelList(agent);
  const entry = activeModelEntry(agent);
  return el('div', { class: 'card agent-overview' }, [
    el('div', { class: 'section-head' }, [
      el('div', {}, [
        el('h2', { text: 'AI 模型接入' }),
        el('p', { class: 'hint', text: '页面不内置任何服务商或模型：全部由你自己配置，保存后自动热重建 Agent' }),
      ]),
      renderField({ path: 'agent.enable', label: '启用 AI Agent', type: 'bool' }),
    ]),
    el('div', { class: 'agent-live' }, [
      el('span', { class: 'agent-live-label', text: '当前生效' }),
      el('span', { class: 'agent-live-model', text: entry ? modelTitle(entry, list.indexOf(entry)) : '未选择配置' }),
      el('span', { class: 'agent-live-url', text: `${agent.model || '未配置模型'}　${agent.base_url || '未配置服务地址'}` }),
    ]),
  ]);
}

export function render(panel) {
  const agent = getAgentConfig();
  normalizeAgentModels(agent);

  panel.append(renderAgentOverview(agent));
  panel.append(renderModelsCard(agent));
  panel.append(renderRoutingCard(agent));

  const tempRange = el('input', {
    class: 'temp-range',
    type: 'range',
    min: '0', max: '2', step: '0.1',
    oninput: (event) => {
      agent.temperature = Number(event.target.value);
      const number = $('#f_agent_temperature');
      if (number) number.value = event.target.value;
      markDirty(); updateAgentSummary();
    },
  });
  tempRange.value = String(agent.temperature ?? 0.7);
  const tempNumber = el('input', {
    type: 'number',
    id: 'f_agent_temperature',
    min: '0', max: '2', step: '0.1',
    oninput: (event) => {
      const value = Number(event.target.value);
      if (!Number.isFinite(value)) return;
      agent.temperature = value;
      tempRange.value = String(Math.max(0, Math.min(2, value)));
      markDirty(); updateAgentSummary();
    },
  });
  tempNumber.value = String(agent.temperature ?? 0.7);
  const maxTokens = el('input', {
    type: 'number',
    id: 'f_agent_max_tokens',
    min: '1', step: '1',
    placeholder: '留空 = 10240',
    oninput: (event) => {
      agent.max_tokens = event.target.value === '' ? null : Number(event.target.value);
      markDirty(); updateAgentSummary();
    },
  });
  maxTokens.value = agent.max_tokens == null ? '' : String(agent.max_tokens);

  panel.append(el('div', { class: 'card' }, [
    el('h2', { text: '生成参数' }),
    el('p', { class: 'hint', text: '这两个参数对「使用中」的那条模型生效：temperature 控制随机程度，max_tokens 限制单次回复长度' }),
    el('div', { class: 'grid' }, [
      el('div', { class: 'field full' }, [
        el('div', { class: 'range-head' }, [
          el('label', { for: 'f_agent_temperature', text: 'temperature' }),
          tempNumber,
        ]),
        tempRange,
        el('div', { class: 'range-labels' }, ['保守 0', '均衡 0.7', '发散 2'].map((text) => el('span', { text }))),
      ]),
      el('div', { class: 'field' }, [
        el('label', { for: 'f_agent_max_tokens', text: '最大回复长度（max_tokens）' }),
        maxTokens,
        el('div', { class: 'hint', text: '正整数；留空时使用 LLM_MAX_TOKENS，缺省 10240' }),
      ]),
    ]),
  ]));

  panel.append(el('div', { class: 'card' }, [
    el('div', { class: 'section-head compact' }, [
      el('h2', { text: '配置概览' }),
      el('span', { class: 'hint', text: '保存后立即热加载，无需重启 Bot' }),
    ]),
    el('div', { id: 'agentSummary' }, [renderAgentSummary(agent)]),
  ]));
}
