/**
 * 前端渲染冒烟测试（node，无浏览器）：最小 DOM stub + 真实模块渲染。
 *
 * 覆盖：keywords.js / image_filter.js 的目录渲染、进详情、返回、
 * 添加 / 删除命令与过滤器、就地写回 state.data。
 * 运行：node frontend/smoke.mjs
 */

/* ── 最小 DOM stub ── */
class Elem {
  constructor(tag, text = '') {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this._className = '';
    this._text = text;
    this.value = '';
    this.checked = false;
    this.style = {};
    this.isConnected = true;
    this.nodeType = 1;
    const host = this;
    this.classList = {
      _set() { return new Set((host._className || '').split(/\s+/).filter(Boolean)); },
      add(c) { const s = this._set(); s.add(c); host._className = [...s].join(' '); },
      remove(c) { const s = this._set(); s.delete(c); host._className = [...s].join(' '); },
      toggle(c, force) {
        const s = this._set();
        const on = force === undefined ? !s.has(c) : force;
        if (on) s.add(c); else s.delete(c);
        host._className = [...s].join(' ');
        return on;
      },
      contains(c) { return this._set().has(c); },
    };
  }
  get className() { return this._className; }
  set className(v) { this._className = String(v); }
  get textContent() {
    if (this._text) return this._text;
    return this.children.map((c) => c.textContent).join('');
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get innerHTML() { return ''; }
  set innerHTML(v) { this.children = []; this._text = ''; }
  append(...nodes) {
    for (const node of [].concat(...nodes.map((n) => (Array.isArray(n) ? n : [n])))) {
      if (node == null || node === false) continue;
      const child = node instanceof Elem ? node : new Elem('#text', String(node));
      child.parentNode = this;
      this.children.push(child);
    }
  }
  appendChild(node) { this.append(node); return node; }
  remove() {
    if (this.parentNode) {
      const i = this.parentNode.children.indexOf(this);
      if (i >= 0) this.parentNode.children.splice(i, 1);
    }
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, event = {}) {
    for (const fn of this.listeners[type] || []) fn({ target: this, preventDefault() {}, ...event });
  }
  classList = {
    _cls: null,
    _set() { return new Set((this._cls?._className || '').split(/\s+/).filter(Boolean)); },
    add(c) { const s = this._set(); s.add(c); this._cls._className = [...s].join(' '); },
    remove(c) { const s = this._set(); s.delete(c); this._cls._className = [...s].join(' '); },
    toggle(c, force) {
      const s = this._set();
      const on = force === undefined ? !s.has(c) : force;
      if (on) s.add(c); else s.delete(c);
      this._cls._className = [...s].join(' ');
      return on;
    },
    contains(c) { return this._set().has(c); },
  };
  _matchesOne(part) {
    // 支持复合选择器：tag.class#id（.model-tile.hit 这类多类单元素）
    const segs = part.split('.');
    const tag = segs[0];
    if (tag) {
      if (tag.startsWith('#')) {
        if (this.attributes.id !== tag.slice(1)) return false;
      } else if (tag !== this.tagName.toLowerCase()) return false;
    }
    for (const cls of segs.slice(1)) {
      if (cls && !this.classList.contains(cls)) return false;
    }
    return true;
  }
  _matches(sel) {
    // 「A B」后代组合器：最后一段匹配自身，其余段在祖先链上按任意深度匹配
    const parts = sel.split(/\s+/).filter(Boolean);
    if (!parts.length || !this._matchesOne(parts[parts.length - 1])) return false;
    let node = this.parentNode;
    for (let i = parts.length - 2; i >= 0; i -= 1) {
      while (node && !node._matchesOne(parts[i])) node = node.parentNode;
      if (!node) return false;
      node = node.parentNode;
    }
    return true;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    for (const child of this.children) {
      if (child._matches(sel)) out.push(child);
      out.push(...child.querySelectorAll(sel));
    }
    return out;
  }
  focus() {}
  get parentElement() { return this.parentNode; }
}

globalThis.window = {
  addEventListener() {},
  confirm: () => true,
  location: { search: '' },
};
globalThis.document = {
  createElement: (tag) => new Elem(tag),
  createTextNode: (text) => new Elem('#text', text),
  querySelector: () => null,
  querySelectorAll: () => [],
};

/* ── 被测模块 ── */
const { state, markDirty } = await import('./assets/js/state.js');
const keywords = await import('./assets/js/tabs/keywords.js');
const imageFilter = await import('./assets/js/tabs/image_filter.js');
const forward = await import('./assets/js/tabs/forward.js');
const psychology = await import('./assets/js/tabs/psychology.js');

let failed = 0;
const check = (name, cond) => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}`);
  if (!cond) failed += 1;
};

/* ── 造数据 ── */
state.data = {
  keyword_actions: [
    { regex: '(男娘|南梁|nn)', action: 'send_message', params: { message: '哪有男娘' } },
    { regex: '^img_fd', action: 'img_forward_create_task' },
    { regex: 'junjingtest_sex_filter', action: 'sex_filter_test', params: { filter: { type: 'group', data: ['1079845768'] } } },
  ],
  image_filter: {
    enable: true,
    audit_group_id: 1032125562,
    single_tag_mode: 'single',
    record_tag_mode: 'reply',
    filters: [
      { id: 'phoebe_bullshit', enable: true, params: { command: '菲比暴论', sender_ids: [3792417235], group_ids: [740726382] } },
      { id: 'nsfw', enable: true, params: { threshold: 0.5, model_path: 'D:/x.onnx' } },
    ],
  },
};
state.actions = ['send_message', 'img_forward_create_task', 'sex_filter_test', 'jm'];

/* ══ keywords ══ */
{
  const panel = new Elem('div');
  keywords.render(panel);

  let tiles = panel.querySelectorAll('.model-tile');
  check('关键词目录：3 条命令 + 1 个添加卡片', tiles.length === 4);

  // 卡片标题取 params.message
  const firstTitle = panel.querySelectorAll('.model-tile-name')[0];
  check('卡片标题取回复文本「哪有男娘」', firstTitle && firstTitle.textContent === '哪有男娘');

  // 进详情
  tiles[0].dispatch('click');
  check('详情页有返回按钮', !!panel.querySelector('.model-back'));
  check('详情页有 params 文本域', panel.querySelectorAll('textarea').length >= 1);

  // 返回目录
  panel.querySelector('.model-back').dispatch('click');
  tiles = panel.querySelectorAll('.model-tile');
  check('返回后回到目录', tiles.length === 4);

  // 添加命令 → 直接进详情
  tiles[tiles.length - 1].dispatch('click');   // add tile
  check('添加命令后进详情（共 4 条）', state.data.keyword_actions.length === 4);
  check('新命令 action 默认第一个注册 action', state.data.keyword_actions[3].action === 'send_message');
  panel.querySelector('.model-back').dispatch('click');

  // 搜索过滤
  const search = panel.querySelectorAll('.tester input')[1];
  search.value = 'sex_filter';
  search.dispatch('input');
  const visible = panel.querySelectorAll('.model-tile:not(.hidden)') || [];
  const hiddenCount = panel.querySelectorAll('.model-tile').filter((t) => t.classList.contains('hidden')).length;
  check('搜索「sex_filter」隐藏 3 张卡片', hiddenCount === 3);

  // 测试文本命中高亮
  const test = panel.querySelectorAll('.tester input')[0];
  test.value = '哪有男娘';
  test.dispatch('input');
  const hit = panel.querySelectorAll('.model-tile.hit');
  if (hit.length !== 1) {
    
    
  }
  check('测试文本命中 1 张（高亮）', hit.length === 1);

  // 就地写回：直接改 state 后 collect 应一致
  const collected = keywords.collectKeywordsFromDom();
  check('collect 与 state 一致且无错误', collected.errors.length === 0 && collected.rules.length === 4);

  /* ══ 批量设置触发范围 ══ */
  const checks = panel.querySelectorAll('.batch-check');
  check('每张命令卡片有勾选框', checks.length === 4);
  checks[0].checked = true; checks[0].dispatch('change');
  checks[1].checked = true; checks[1].dispatch('change');
  check('勾选后显示已选数', panel.textContent.includes('已选 2 条命令'));

  const setBtn = panel.querySelectorAll('button').find((b) => b.textContent === '批量设置触发范围');
  setBtn.dispatch('click');
  check('批量面板已展开', panel.textContent.includes('把范围套用到选中的 2 条命令'));

  // 选 ban 类型 + 填名单（往草稿列表加一项）
  const panelSel = panel.querySelectorAll('.batch-scope select')[0];
  panelSel.value = 'ban';
  panelSel.dispatch('change');
  const addRow = panel.querySelectorAll('.batch-scope .list-actions button').find((b) => b.textContent.includes('添加一项'));
  addRow.dispatch('click');
  const rowInput = panel.querySelectorAll('.batch-scope .list-input')[0];
  rowInput.value = '284840486';
  rowInput.dispatch('input');

  const applyBtn = panel.querySelectorAll('.batch-scope button').find((b) => b.textContent.includes('应用到选中的'));
  applyBtn.dispatch('click');
  const rules = state.data.keyword_actions;
  // 运行时语义：filter 在 params.filter 里（pattern_match 从 params 里取）
  check('应用后前两条命令都带上 ban 范围（params.filter）',
    rules[0].params?.filter?.type === 'ban' && rules[1].params?.filter?.type === 'ban'
    && JSON.stringify(rules[0].params.filter.data) === JSON.stringify([284840486]));
  check('应用后是各自独立副本（改一条不影响另一条）',
    rules[0].params.filter.data !== rules[1].params.filter.data);
  check('卡片角标显示范围', panel.textContent.includes('ban(1群)'));
  check('应用后勾选清空', !panel.textContent.includes('已选'));

  // 清除触发范围：重新勾两条 → 清除
  const checks2 = panel.querySelectorAll('.batch-check');
  checks2[0].checked = true; checks2[0].dispatch('change');
  checks2[1].checked = true; checks2[1].dispatch('change');
  const clearBtn = panel.querySelectorAll('button').find((b) => b.textContent === '清除触发范围');
  clearBtn.dispatch('click');
  check('清除后 params.filter 被删掉', rules[0].params?.filter === undefined && rules[1].params?.filter === undefined);

  /* ══ 详情页：单条 filter 结构化编辑也写 params.filter ══ */
  panel.querySelectorAll('.model-tile')[0].dispatch('click');   // 进第 1 条详情
  const detailChecks = panel.querySelectorAll('input').filter((i) => i.attributes.type === 'checkbox');
  const filterCheck = detailChecks[detailChecks.length - 1];    // 最后一个勾选框 = 限定触发范围
  filterCheck.checked = true; filterCheck.dispatch('change');
  check('详情勾选后 params.filter 生成', rules[0].params?.filter?.type === 'group');
  check('params 文本框同步刷新（含 filter）', JSON.stringify(rules[0].params).includes('"filter"')
    && panel.querySelectorAll('textarea').some((t) => t.value.includes('"filter"')));
  // 空名单先点「＋ 添加一项」，再填群号
  const addOneBtn = panel.querySelectorAll('.filter-box button').find((b) => b.textContent.includes('添加一项'));
  addOneBtn.dispatch('click');
  const detailInput = panel.querySelectorAll('.filter-box .list-input')[0];
  detailInput.value = '284840486';
  detailInput.dispatch('input');
  check('详情名单写入 params.filter.data', JSON.stringify(rules[0].params.filter.data) === JSON.stringify([284840486]));
  // 取消勾选 → 删除
  const filterCheck2 = panel.querySelectorAll('input').filter((i) => i.attributes.type === 'checkbox').slice(-1)[0];
  filterCheck2.checked = false; filterCheck2.dispatch('change');
  check('详情取消勾选后 params.filter 删除', rules[0].params?.filter === undefined);
}

/* ══ image_filter ══ */
{
  const panel = new Elem('div');
  imageFilter.render(panel);

  check('过滤门开关字段已渲染', !!panel.textContent.includes('启用图片过滤门'));
  let tiles = panel.querySelectorAll('.model-tile');
  check('过滤器目录：2 个过滤器卡片', tiles.length === 2);

  // 进详情（phoebe）
  tiles[0].dispatch('click');
  check('过滤器详情有返回按钮', !!panel.querySelector('.model-back'));
  check('详情展示内置参数说明', panel.textContent.includes('触发命令文本'));

  // 返回，点 nsfw 详情看阈值字段
  panel.querySelector('.model-back').dispatch('click');
  panel.querySelectorAll('.model-tile')[1].dispatch('click');
  check('nsfw 详情有阈值字段', panel.textContent.includes('命中阈值'));

  // 高级参数折叠
  const advBtn = panel.querySelectorAll('button').find((b) => b.textContent.includes('高级参数'));
  check('nsfw 有高级参数折叠', !!advBtn);

  // 返回目录 → 添加自定义过滤器
  panel.querySelector('.model-back').dispatch('click');
  const customInput = panel.querySelectorAll('.row input')[0];
  customInput.value = 'my_custom_filter';
  const addBtn = panel.querySelectorAll('.row button')[0];
  addBtn.dispatch('click');
  check('自定义过滤器已添加并进详情', state.data.image_filter.filters.length === 3
    && state.data.image_filter.filters[2].id === 'my_custom_filter'
    && !!panel.querySelector('.model-back'));
  check('自定义过滤器走 JSON 编辑', panel.textContent.includes('自定义过滤器的 params'));
  panel.querySelector('.model-back').dispatch('click');

  // 未知键保留：给 phoebe params 塞一个后端新键，重渲染后仍在
  state.data.image_filter.filters[0].params.future_key = 'kept';
  const phoebeTile = panel.querySelectorAll('.model-tile')[0];
  phoebeTile.dispatch('click');
  const extraArea = panel.querySelectorAll('textarea').find((t) => t.value.includes('future_key'));check('结构化编辑保留未知键', panel.textContent.includes('其它参数（JSON）') && !!extraArea);
  panel.querySelector('.model-back').dispatch('click');

  // 删除 nsfw
  const nsfwTile = panel.querySelectorAll('.model-tile').find((t) => t.textContent.includes('nsfw'));
  nsfwTile.dispatch('click');
  const delBtn = panel.querySelectorAll('button').find((b) => b.textContent === '删除此过滤器');
  delBtn.dispatch('click');   // confirm stub 返回 true
  check('删除后回到目录且剩 2 个', state.data.image_filter.filters.length === 2 && !!panel.querySelector('.model-grid'));

  // 未配置 image_filter 的引导路径
  delete state.data.image_filter;
  const panel2 = new Elem('div');
  imageFilter.render(panel2);
  check('无配置时显示创建引导', panel2.textContent.includes('创建图片过滤配置'));
}

/* ══ forward（机器人图片查重配置）══ */
{
  state.data.forward_image_dedup = { enable: true, keep_days: 30, image_keep_days: 2 };
  state.data.auto_forward = { auto_img_forward_from_bot: { enable: true, bot_list: [] } };
  const panel = new Elem('div');
  forward.render(panel);

  check('机器人图片分组有查重开关', panel.textContent.includes('启用机器人图片查重'));
  check('机器人图片分组有送达记录保留天数', panel.textContent.includes('图片送达记录保留天数'));
  check('机器人图片分组有内容身份保留天数', panel.textContent.includes('图片内容身份保留天数'));
}

/* ══ psychology（心理分析配置页）══ */
{
  state.data.agent = { models: [{ id: 'm1', name: '主力', model: 'deepseek-v4-flash', base_url: 'https://api.deepseek.com/v1' }] };
  state.data.psychology = {
    enable: true,
    keywords: ['分析心理'],
    image_mode: 'vision',
    model_id: 'm1',
    max_images: 6,
  };
  state.data.groupList = [];
  const panel = new Elem('div');
  psychology.render(panel);

  const cards = panel.querySelectorAll('.mode-card');
  check('心理分析：3 种回复形式 + 3 档图片策略', cards.length === 6);
  const activeCards = cards.filter((card) => card.classList.contains('active'));
  check(
    '心理分析：默认聊天记录 + 辅助视觉分析',
    activeCards.length === 2
      && activeCards[0].textContent.includes('聊天记录')
      && activeCards[1].textContent.includes('辅助视觉分析'),
  );
  check('心理分析：启用开关与唤醒词在页面上', panel.textContent.includes('启用心理分析') && panel.textContent.includes('唤醒词'));
  check('心理分析：能看到模型目录里的现成模型', panel.textContent.includes('主力'));

  // 切到「一段文字总结」：写回配置，逐条参数区仍在（只是不显示图像策略的联动）
  const textCard = cards.find((card) => card.textContent.includes('一段文字总结'));
  const replyRadio = textCard.querySelectorAll('input').find((node) => node.attributes.type === 'radio');
  replyRadio.checked = true;
  replyRadio.dispatch('change');
  check('心理分析：切到文字总结后写回 reply_mode', state.data.psychology.reply_mode === 'text');

  // 切到「仅 OCR」：写回配置，并且视觉模型卡片消失
  const ocrCard = panel.querySelectorAll('.mode-card').find((card) => card.textContent.includes('仅 OCR'));
  const radio = ocrCard.querySelectorAll('input').find((node) => node.attributes.type === 'radio');
  radio.checked = true;
  radio.dispatch('change');
  check('心理分析：切到仅 OCR 后写回 image_mode', state.data.psychology.image_mode === 'ocr');
  check('心理分析：仅 OCR 时不显示视觉模型配置', !panel.textContent.includes('视觉模型（可选）'));

  // 关键词是即时写回数组
  const keywordInput = panel.querySelectorAll('input').find((node) => node.attributes.type === 'text');
  keywordInput.value = '分析心理, 心理侧写';
  keywordInput.dispatch('input');
  check('心理分析：唤醒词写回数组', JSON.stringify(state.data.psychology.keywords) === JSON.stringify(['分析心理', '心理侧写']));
}

console.log(failed ? `\n${failed} 项失败` : '\n全部通过');
process.exit(failed ? 1 : 0);
