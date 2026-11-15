/**
 * 图片过滤标签页：过滤门开关 + 过滤器目录（命令为 key 的二级页面）。
 *
 * ``image_filter.filters`` 里的一项 = 一个过滤器：
 *   { id, enable, params }
 * 内置三种（phoebe_bullshit / nsfw / image_hash_blacklist）带结构化参数编辑
 * 和中文说明；未知 id（以后手写进配置的）退回 JSON 编辑，不丢字段。
 *
 * 注意：这里的 specs 是 ``messaging/image_filter.py`` 注册表的镜像，
 * 后端加新过滤函数时记得同步这里（模板 + 参数说明）。
 */

import { el, getPath, renderField, renderListEditor, setPath, jsonError } from '../dom.js';
import { state, markDirty } from '../state.js';

export const TAB = { id: 'image_filter', label: '图片过滤' };

// 目录 → 详情的导航状态：null = 目录，否则为正在编辑的过滤 id
let filterDetailId = null;

/* ══════════════════════ 内置过滤器注册表（前端镜像） ══════════════════════ */
export const FILTER_SPECS = {
  phoebe_bullshit: {
    label: '菲比暴论',
    description: '万区殿菲比大人的暴论图。主判据 = 同群最近发过触发命令且发送者对得上；版式（左图右黑底白字）只做兜底',
    template: () => ({
      command: '菲比暴论',
      command_window: 120,
      sender_ids: [3792417235],
      group_ids: [740726382],
    }),
    params: [
      { key: 'command', label: '触发命令文本', kind: 'text', hint: '出图前同群有人发过这句话就会命中' },
      { key: 'command_window', label: '命令有效期（秒）', kind: 'number', step: 1 },
      {
        key: 'sender_ids', label: '出图的发送者 QQ', kind: 'intlist', item: 'qq',
        hint: '⚠ 留空 = 回退内置默认（3792417235）。想不限制发送者，把所有可能出图的 QQ 都列上',
      },
      {
        key: 'group_ids', label: '生效群号', kind: 'intlist', item: 'group',
        hint: '⚠ 留空 = 回退内置默认（740726382）。想不限群，把所有群号都列上',
      },
      { key: 'label', label: '标签名', kind: 'text', placeholder: '默认「菲比暴论」' },
      { key: 'layout_check', label: '版式兜底判定', kind: 'bool', hint: '上下文丢失（Bot 刚重启 / 命令过期）时按「左图右黑底白字」模板识别' },
      { key: 'min_ratio', label: '最小宽高比', kind: 'number', advanced: true },
      { key: 'max_ratio', label: '最大宽高比', kind: 'number', advanced: true },
      { key: 'right_dark', label: '右半灰度上限', kind: 'number', advanced: true },
      { key: 'right_white_min', label: '右半白字占比下限', kind: 'number', advanced: true },
      { key: 'left_gap', label: '左右亮度差下限', kind: 'number', advanced: true },
    ],
  },
  nsfw: {
    label: '色图检测',
    description: 'ViT NSFW ONNX 推理（对齐 D:/Code/sex_filter）。需要 onnxruntime + 模型文件，缺哪个哪个安静停用',
    template: () => ({
      threshold: 0.5,
      model_path: 'D:/Code/sex_filter/models/nsfw_vit_quantized.onnx',
    }),
    params: [
      { key: 'threshold', label: '命中阈值', kind: 'number', step: 0.05, hint: 'score ≥ 阈值判为色图；校准集实测普通图 ≤0.01、色图 ≥0.86，0.5 很宽裕' },
      { key: 'model_path', label: 'ONNX 模型路径', kind: 'text', full: true },
      { key: 'threads', label: '推理线程数', kind: 'number', step: 1 },
      { key: 'use_gpu', label: 'GPU 推理（DirectML/CUDA）', kind: 'bool', hint: '需安装 onnxruntime-directml（或 onnxruntime-gpu）；实测量化 ViT 单张略慢于 CPU，deep 分块批量快约 35%；加载失败自动回退 CPU' },
      { key: 'confirm_model_path', label: '复核模型路径', kind: 'text', full: true, advanced: true, hint: 'nsfwjs 5 分类模型；低饱和图（黑白漫画/截图）ViT 命中后用它复核，hentai+porn > drawings 才拦截，否则标「黑白图待人工」' },
      { key: 'confirm_sat_max', label: '复核饱和度上限', kind: 'number', step: 0.01, advanced: true, hint: '平均饱和度低于它才触发复核（黑白图阈值，默认 0.08）' },
      { key: 'subprocess', label: '子进程推理', kind: 'bool', advanced: true, hint: 'onnxruntime 原生崩溃只损失一次判定不影响 Bot；关掉回退主进程推理（仅调试用）' },
      { key: 'review_low', label: '可疑区间下限', kind: 'number', advanced: true, hint: '[review_low, threshold) 记为 review，只影响报告不影响拦截' },
      { key: 'deep', label: '可疑图加跑分块', kind: 'bool', advanced: true, hint: '可疑区间（0.05~阈值）时再加跑分块视图，专治「整图被浅色背景冲淡、敏感内容局部化」的图，只在该区间多花 ~0.5s' },
      { key: 'tile_grid', label: '分块网格', kind: 'number', advanced: true, hint: 'N×N 分块，3 更稳（擦边图余量大），默认 2' },
      { key: 'tile_min_side', label: '分块最小边（px）', kind: 'number', advanced: true, hint: '整图最长边小于它才跳过分块；300 适配手机图' },
      { key: 'tile_grid', label: '分块数（N x N）', kind: 'number', advanced: true },
      { key: 'tile_min_side', label: '长边超过才分块', kind: 'number', advanced: true },
      { key: 'label', label: '标签名', kind: 'text', placeholder: '默认「色图」' },
    ],
  },
  image_hash_blacklist: {
    label: '图片哈希黑名单',
    description: 'aHash + dHash 感知哈希命中配置里的哈希列表（汉明距离 ≤ max_distance 就算命中）',
    template: () => ({ hashes: [], max_distance: 8 }),
    params: [
      { key: 'hashes', label: '哈希列表', kind: 'strlist', numeric: false, hint: '16 位十六进制一段（aHash/dHash 拼接），从日志或 image_hash 工具里抄' },
      { key: 'max_distance', label: '最大汉明距离', kind: 'number', step: 1, hint: '0 = 只认一模一样；8 以内容忍轻微压缩 / 截边' },
      { key: 'label', label: '标签名', kind: 'text', placeholder: '默认「黑名单图片」' },
    ],
  },
};

/* ══════════════════════ 数据 ══════════════════════ */
export function filterConfig() {
  return getPath(state.data, 'image_filter');
}

function ensureConfig() {
  let cfg = filterConfig();
  if (!cfg || typeof cfg !== 'object') {
    // 后端 config/defaults.py 的默认形状（enable=false，开启由用户决定）
    cfg = {
      enable: false,
      audit_group_id: 1032125562,
      single_tag_mode: 'single',
      record_tag_mode: 'reply',
      block_matched: true,
      scan_all_images: true,
      max_images: 0,
      image_timeout: 8,
      max_tag_images: 6,
      filters: [],
    };
    state.data.image_filter = cfg;
    markDirty();
  }
  if (!Array.isArray(cfg.filters)) cfg.filters = [];
  return cfg;
}

function filterList() {
  const cfg = filterConfig();
  return cfg && Array.isArray(cfg.filters) ? cfg.filters : [];
}

export function filterTitle(entry, index) {
  const id = String((entry && entry.id) || '').trim();
  if (id) return FILTER_SPECS[id]?.label || id;
  return `未命名过滤器 ${index + 1}`;
}

/* ══════════════════════ 通用小渲染 ══════════════════════ */
function selectField(path, label, options, hint) {
  const value = getPath(state.data, path);
  const select = el('select', {
    onchange: (event) => { setPath(state.data, path, event.target.value); markDirty(); },
  });
  for (const [opt, text] of options) {
    select.append(el('option', { value: opt, text, selected: String(value || '') === opt ? 'selected' : null }));
  }
  const wrap = el('div', { class: 'field' }, [el('label', { text: label })]);
  wrap.append(select);
  if (hint) wrap.append(el('div', { class: 'hint', text: hint }));
  return wrap;
}

/** params 对象的结构化参数编辑（就地写 params[key]，未知键保留）。 */
function renderParamsEditor(entry, spec, container) {
  const params = entry.params && typeof entry.params === 'object' ? entry.params : (entry.params = {});
  const knownKeys = new Set((spec?.params || []).map((p) => p.key));
  const box = el('div', { class: 'model-detail' });

  if (spec) {
    const advanced = el('div', { class: 'model-body' });
    let hasAdvanced = false;
    for (const def of spec.params) {
      if (def.advanced) { hasAdvanced = true; continue; }
      advanced.append(paramField(entry, params, def, container));
    }
    box.append(advanced);

    if (hasAdvanced) {
      const advBox = el('div', { class: 'model-body hidden' });
      for (const def of spec.params) {
        if (def.advanced) advBox.append(paramField(entry, params, def, container));
      }
      box.append(el('button', {
        class: 'btn mini ghost', type: 'button', text: '▸ 高级参数',
        onclick: (event) => {
          advBox.classList.toggle('hidden');
          event.target.textContent = advBox.classList.contains('hidden') ? '▸ 高级参数' : '▾ 高级参数';
        },
      }), advBox);
    }

    // 结构化编辑器没覆盖到的键（后端新加的 / 手写的）：JSON 只读提示 + 可编辑
    const extraKeys = Object.keys(params).filter((key) => !knownKeys.has(key));
    if (extraKeys.length) {
      const area = el('textarea', {
        rows: 3,
        oninput: (event) => {
          const err = jsonError(event.target.value);
          extraTip.className = 'rule-tip';
          if (err) {
            extraTip.className = 'rule-tip bad';
            extraTip.textContent = `JSON 错误：${err}`;
            return;
          }
          const parsed = JSON.parse(event.target.value);
          for (const key of extraKeys) delete params[key];
          Object.assign(params, parsed);
          markDirty();
        },
      });
      const extra = {};
      for (const key of extraKeys) extra[key] = params[key];
      area.value = JSON.stringify(extra, null, 2);
      const extraTip = el('div', { class: 'rule-tip', text: '这些参数还没有结构化编辑器，用 JSON 维护' });
      box.append(el('div', { class: 'model-field wide' }, [
        el('div', { class: 'model-field-label', text: '其它参数（JSON）' }),
        area,
        extraTip,
      ]));
    }
    return box;
  }

  // 未知 id：整体 JSON 编辑
  const area = el('textarea', {
    rows: 8,
    oninput: (event) => {
      const err = jsonError(event.target.value);
      tip.className = 'rule-tip';
      if (err) {
        tip.className = 'rule-tip bad';
        tip.textContent = `JSON 错误：${err}`;
        return;
      }
      entry.params = JSON.parse(event.target.value);
      tip.textContent = 'params 合法';
      markDirty();
    },
  });
  area.value = JSON.stringify(params, null, 2);
  const tip = el('div', { class: 'rule-tip', text: '自定义过滤器的 params（JSON 对象，格式由对应过滤函数决定）' });
  return el('div', { class: 'model-field wide' }, [
    el('div', { class: 'model-field-label', text: `params（${entry.id}）` }),
    area,
    tip,
  ]);
}

function paramField(entry, params, def, container) {
  const wrap = el('div', { class: `model-field${def.full ? ' wide' : ''}` });
  wrap.append(el('div', { class: 'model-field-label', text: def.label }));

  if (def.kind === 'bool') {
    const check = el('input', {
      type: 'checkbox',
      checked: params[def.key] ? 'checked' : null,
      onchange: (event) => { params[def.key] = event.target.checked; markDirty(); },
    });
    wrap.append(el('div', { class: 'switch' }, [check]));
  } else if (def.kind === 'intlist' || def.kind === 'strlist') {
    wrap.append(renderListEditor({
      label: def.label,
      item: def.item,
      numeric: def.numeric !== false,
      get: () => (Array.isArray(params[def.key]) ? params[def.key] : []),
      set: (value) => { params[def.key] = value; markDirty(); },
    }));
  } else {
    const input = el('input', {
      type: def.kind === 'number' ? 'number' : 'text',
      oninput: (event) => {
        params[def.key] = def.kind === 'number'
          ? (event.target.value === '' ? null : Number(event.target.value))
          : event.target.value;
        markDirty();
      },
    });
    const value = params[def.key];
    input.value = value == null ? '' : String(value);
    if (def.kind === 'number') input.step = def.step != null ? String(def.step) : 'any';
    if (def.placeholder) input.placeholder = def.placeholder;
    wrap.append(input);
  }
  if (def.hint) wrap.append(el('div', { class: 'model-field-hint', text: def.hint }));
  return wrap;
}

/* ══════════════════════ 详情页 ══════════════════════ */
function renderFilterDetail(cfg, entry, container) {
  const list = cfg.filters;
  const index = list.indexOf(entry);
  const spec = FILTER_SPECS[entry.id];

  const enableCheck = el('input', {
    type: 'checkbox',
    checked: entry.enable ? 'checked' : null,
    onchange: (event) => { entry.enable = event.target.checked; markDirty(); rerender(container); },
  });

  return el('div', { class: 'card' }, [
    el('div', { class: 'model-detail' }, [
      el('div', { class: 'model-detail-head' }, [
        el('button', {
          class: 'btn mini ghost model-back', type: 'button', text: '← 返回过滤器列表',
          onclick: () => { filterDetailId = null; rerender(container); },
        }),
        el('div', { class: 'model-detail-title' }, [
          el('strong', { class: 'model-detail-name', text: filterTitle(entry, index) }),
          el('span', { class: 'hint', text: entry.id || '（id 为空）' }),
        ]),
        el('div', { class: 'model-detail-head-actions' }, [
          el('label', { class: 'switch' }, [
            enableCheck,
            el('span', { text: entry.enable ? '已启用' : '已停用' }),
          ]),
          el('button', {
            class: 'btn mini danger', type: 'button', text: '删除此过滤器',
            onclick: () => {
              if (!window.confirm(`删除过滤器「${filterTitle(entry, index)}」？`)) return;
              list.splice(index, 1);
              markDirty();
              filterDetailId = null;
              rerender(container);
            },
          }),
        ]),
      ]),
      spec ? el('p', { class: 'hint', text: spec.description }) : el('p', { class: 'hint', text: '自定义过滤器：id 必须和后端注册表里的 filter_id 一致才会真正执行' }),
      el('div', { class: 'model-body' }, [
        (() => {
          const wrap = el('div', { class: 'model-field' }, [el('div', { class: 'model-field-label', text: '过滤器 id（注册表里的 filter_id）' })]);
          const input = el('input', {
            type: 'text',
            placeholder: '例如 phoebe_bullshit',
            oninput: (event) => { entry.id = event.target.value.trim(); markDirty(); },
          });
          input.value = entry.id || '';
          wrap.append(input);
          return wrap;
        })(),
      ]),
      renderParamsEditor(entry, spec, container),
    ]),
  ]);
}

/* ══════════════════════ 目录页 ══════════════════════ */
function renderFiltersCard(cfg, container) {
  const list = cfg.filters;
  if (filterDetailId != null) {
    const entry = list.find((item) => item && item.id === filterDetailId);
    if (entry) return renderFilterDetail(cfg, entry, container);
    filterDetailId = null;
  }

  const grid = el('div', { class: 'model-grid' });
  for (const entry of list) {
    const index = list.indexOf(entry);
    const known = FILTER_SPECS[entry.id];
    grid.append(el('button', {
      class: 'model-tile', type: 'button',
      onclick: () => { filterDetailId = entry.id; rerender(container); },
    }, [
      el('div', { class: 'model-tile-top' }, [
        el('span', { class: 'model-tile-idx', text: String(index + 1) }),
        el('span', { class: 'model-tile-name', text: filterTitle(entry, index) }),
        el('span', { class: `pill ${entry.enable ? 'on' : 'off'}`, text: entry.enable ? '启用' : '停用' }),
      ]),
      el('span', { class: 'model-tile-model', text: entry.id }),
      el('div', { class: 'model-tile-foot' }, [
        el('span', { class: 'model-tile-meta', text: known ? known.description.split('。')[0] : '自定义过滤器' }),
      ]),
    ]));
  }

  /* 添加区：内置模板下拉 + 自定义 id */
  const existing = new Set(list.map((item) => String(item.id || '').trim()));
  const addSelect = el('select', { class: 'filter-add-select' });
  addSelect.append(el('option', { value: '', text: '选择内置过滤器…' }));
  for (const [id, spec] of Object.entries(FILTER_SPECS)) {
    if (!existing.has(id)) addSelect.append(el('option', { value: id, text: `${spec.label}（${id}）` }));
  }
  const customInput = el('input', {
    type: 'text',
    placeholder: '或输入自定义过滤器 id',
    oninput: (event) => { addSelect.value = ''; },
  });
  const addButton = el('button', {
    class: 'btn mini primary', type: 'button', text: '＋ 添加过滤器',
    onclick: () => {
      const id = (addSelect.value || customInput.value || '').trim();
      if (!id) return;
      if (existing.has(id)) {
        filterDetailId = id;
        rerender(container);
        return;
      }
      const spec = FILTER_SPECS[id];
      const entry = { id, enable: true, params: spec ? spec.template() : {} };
      list.push(entry);
      markDirty();
      customInput.value = '';
      filterDetailId = id;
      rerender(container);
    },
  });

  const emptyHint = list.length
    ? null
    : el('div', { class: 'hint', text: '还没有过滤器：从下面选一个内置的（推荐先加「菲比暴论」），或输入自定义 id' });

  return el('div', { class: 'card' }, [
    el('div', { class: 'section-head compact' }, [
      el('div', {}, [
        el('h2', { text: '过滤器' }),
        el('p', { class: 'hint', text: '命中任一启用过滤器的图不会搬进普通目标群，而是打上标签发到审计群。filters 列表保存即热更新，不用重启' }),
      ]),
    ]),
    emptyHint,
    grid,
    el('div', { class: 'row', style: 'margin-top:12px' }, [addSelect, customInput, addButton]),
    el('div', { class: 'list-actions' }, [
      el('span', { class: 'list-status', text: `共 ${list.length} 个过滤器，启用 ${list.filter((item) => item && item.enable).length} 个` }),
    ]),
  ]);
}

/* ══════════════════════ 页面入口 ══════════════════════ */
let currentContainer = null;

function rerender(container) {
  container.innerHTML = '';
  container.append(...renderTabContent());
}

function renderTabContent() {
  const cfg = ensureConfig();
  const generalFields = [
    { path: 'image_filter.enable', label: '启用图片过滤门', type: 'bool', full: true, hint: '关闭后完全不过滤、不下载图片、不发审计标签' },
    { path: 'image_filter.audit_group_id', label: '审计群号', type: 'text', hint: '命中后的图 / 聊天记录连同标签发到这个群；留空时命中只拦不报' },
    selectField('image_filter.single_tag_mode', '单图标签样式', [['single', 'single：一条 [标签文本, 图片] 消息'], ['forward_reply', 'forward_reply：先转发原消息，再回复它发标签']]),
    selectField('image_filter.record_tag_mode', '聊天记录标签样式', [['reply', 'reply：先转发记录，再回复它发标签'], ['before', 'before：先发标签，再转发记录'], ['after', 'after：先转发记录，再发标签']]),
    { path: 'image_filter.block_matched', label: '命中后拦截普通搬运', type: 'bool', hint: '关闭 = 只审计不拦截，命中图照样搬' },
    { path: 'image_filter.scan_all_images', label: '扫描聊天记录里的图片', type: 'bool', hint: '关闭只过滤白名单机器人的单图；单图路径恒扫描' },
    { path: 'image_filter.short_circuit', label: '命中即停（省流量）', type: 'bool', hint: '开启后按批（4 张）下载+判定，一批命中就停——后面的图不下载不判定。拦截语义不变，但审计标签只剩命中的图' },
    { path: 'image_filter.max_images', label: '单条记录最多扫描几张图', type: 'number', step: 1, hint: '0 = 不设上限（推荐）。卡张数等于把没判过的图当安全图放过——大记录实测单张就有 170 条消息 / 500 多张图' },
    { path: 'image_filter.image_timeout', label: '图片下载超时（秒）', type: 'number', step: 1 },
    { path: 'image_filter.max_tag_images', label: '一条标签最多带几张图', type: 'number', step: 1 },
    { path: 'image_filter.audit_operators', label: '审计员 QQ（复搬白名单）', type: 'strlist', numeric: true, hint: '留空 = 审计群里谁都能回复标签发「复搬」放行复搬；填了就只有这些人能操作' },
  ];

  return [
    el('div', { class: 'card' }, [
      el('h2', { text: '图片过滤门' }),
      el('p', { class: 'hint', text: '搬运图片本来就要下载（去重指纹 / 二维码识别），顺手在这里过滤。命中 = 打类型标签 + 发审计群 1032125562（可改）' }),
      el('div', { class: 'grid' }, generalFields.map((field) => (field.path ? renderField(field) : field))),
    ]),
    renderFiltersCard(cfg, currentContainer),
  ];
}

export function render(panel) {
  const container = el('div', { class: 'image-filter-page', style: 'display:flex;flex-direction:column;gap:14px' });
  currentContainer = container;
  panel.append(container);
  // image_filter 段不存在时不主动创建配置：先展示引导，点「启用」才落盘
  if (!filterConfig()) {
    container.append(el('div', { class: 'card' }, [
      el('h2', { text: '图片过滤门' }),
      el('p', { class: 'hint', text: '配置里还没有 image_filter 段。点下面按钮创建默认配置（总开关默认关闭），再按需添加过滤器' }),
      el('button', {
        class: 'btn primary', type: 'button', text: '创建图片过滤配置',
        onclick: () => { ensureConfig(); rerender(currentContainer); },
      }),
    ]));
    return;
  }
  for (const node of renderTabContent()) container.append(node);
}
