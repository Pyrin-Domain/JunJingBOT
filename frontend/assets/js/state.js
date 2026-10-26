/**
 * 全局状态 + 字段定义。
 *
 * 这里只放「数据」，不放渲染逻辑；DOM 工具在 dom.js，网络在 api.js。
 * 所有标签页共享同一个 state 对象（配置、脏标记、群列表缓存）。
 */

export const state = {
  data: null,          // 当前配置（与表单双向绑定）
  meta: null,          // 配置文件信息
  actions: [],         // 可用 action 名（来自后端 action_registry）
  imgsDir: '',         // 图片目录（{imgs_dir} 替换目标）
  dirty: false,        // 有未保存修改
  ws: null,            // OneBot 连接设置（config/config.json）
  paths: null,         // 路径映射设置（config/environment.json）
  groupList: [],       // NapCat 群列表（群号列表编辑器 / 下拉框用）
  groupNames: {},      // 群号 -> 群名
  groupsError: '',     // 群列表没取到时的原因
  listEditors: [],     // 已渲染的列表编辑器（群列表刷新后重画名字与下拉框）
  tab: 'status',
  statusTimer: null,   // 运行状态自动刷新
};

/** 标签页顺序与中文标题（与旧面板一致）。 */
export const TABS = [
  { id: 'status', label: '运行状态' },
  { id: 'connection', label: '连接方式' },
  { id: 'paths', label: '路径映射' },
  { id: 'basic', label: '基础参数' },
  { id: 'agent', label: 'AI Agent' },
  { id: 'interject', label: '群聊插话' },
  { id: 'meme', label: '表情包' },
  { id: 'forward', label: '搬运与去重' },
  { id: 'text', label: '提示词与回复' },
  { id: 'keywords', label: '关键词 / 命令' },
  { id: 'raw', label: '原始 JSON' },
];

/** 关键词 action 的参数说明（没有 action_registry 时也能给出提示）。 */
export const ACTION_HINTS = {
  send_message: 'params: {"message": "回复文本"}；也可 {"message": ["A", "B"]}（随机挑一条）；可选 {"filter": {"type": "ban", "data": ["群号"]}} 屏蔽某些群',
  send_img: 'params: {"img_addr": "{imgs_dir}/文件名.gif", "summary": "图片说明"}',
  get_long_history_test: '无参数：调试用，打印长历史拉取结果',
  set_tokenizer: '无参数：切换「发送前分词」开关',
  ocr: '无参数：对「被回复的那条消息」里的图片做 OCR',
  export: '无参数：export -g 群号 / export -u QQ号（配合回复一条消息，把区间内历史搬过去）',
  fabric: '无参数：按 [qq:xxx][name:xxx][context:xxx] 造一条转发节点发出去',
  get_music: '无参数：点歌，取消息里 [歌名] 作为搜索词',
  img_forward_create_task: '无参数：img_fd -g 群 / -u QQ / -a，再用 -b / -e 标出区间',
};

/** 普通字段定义：基础 / 插话 / 搬运 / 提示词。 */
export const FIELDS = {
  basic: [
    { path: 'master_qq', label: '主人 QQ', type: 'intlist', item: 'qq', full: true, hint: '这些 QQ 的用户会被当作「主人」写进 AI 上下文' },
    { path: 'heartbeat.min_minutes', label: '心跳最短间隔（分钟）', type: 'number', hint: '在这两个数之间随机取值，防止掉线' },
    { path: 'heartbeat.max_minutes', label: '心跳最长间隔（分钟）', type: 'number' },
    { path: 'message.extraconfig.tokenizer', label: '发送前分词（tokenizer）', type: 'bool' },
    { path: 'message.extraconfig.recent', label: '@ 时带上最近聊天记录', type: 'bool', hint: '关掉可以省 token' },
  ],
  interject: [
    { path: 'interject.enable', label: '启用群聊插话', type: 'bool', hint: '关闭后只在被 @ 时说话' },
    { path: 'interject.frequent', label: '累计多少条消息触发判断', type: 'number', step: 1, hint: '群里攒够这么多条新消息，就让模型判断一次要不要接话' },
    { path: 'interject.extra_history', label: '额外多扒几条上下文', type: 'number', step: 1, hint: '触发时除了上面那个数，再多取几条给模型当语境' },
    { path: 'interject.idle_minutes', label: '安静多久检查一次（分钟）', type: 'number', hint: '群里有零星几条消息、但一直凑不够条数时，靠这个定时器兜底；没有新消息就只重新倒计时' },
    { path: 'interject.cooldown_minutes', label: '两次插话最小间隔（分钟）', type: 'number', hint: '防止刷屏；Bot 刚回过话也会重置这个冷却' },
    { path: 'interject.hot_minutes', label: 'Bot 说话后的敏感期（分钟）', type: 'number', hint: '这段时间内，有人回复 Bot 刚说的那句会直接交给 AI（不用 @）；过期就恢复普通模式' },
    { path: 'interject.hot_frequent', label: '敏感期内多少条就触发插话', type: 'number', step: 1, hint: '比普通门槛低，聊起来时接得更勤' },
    { path: 'interject.hot_idle_minutes', label: '敏感期内的检查间隔（分钟）', type: 'number' },
    { path: 'interject.groups', label: '只在这些群插话（留空 = 所有群）', type: 'intlist', item: 'group', full: true },
    { path: 'interject.blacklist_groups', label: '这些群永不插话', type: 'intlist', item: 'group', full: true },
    { path: 'interject.aliases', label: '机器人别名', type: 'strlist', full: true, hint: '群里喊名字但没 @ 也算在叫它（如「君景、独角兽」）。句中只是提到名字只当成「在说我」，不会强制回复；单独喊名字、或带「在吗 / 你觉得」这类词才算召唤' },
    { path: 'interject.interests.high', label: '明显在意的话题词', type: 'strlist', full: true, hint: '命中只会让它在差不多的局面里更倾向接这个话题，不构成开口的理由' },
    { path: 'interject.interests.low', label: '只是略懂的话题词', type: 'strlist', full: true, hint: '命中会在上下文里标成（略懂），不额外加分' },
  ],
  meme: [
    { path: 'meme.enable', label: '启用表情包', type: 'bool', hint: '关闭后模型不会写 <meme>，已经有 <meme> 的消息里的表情会被丢掉' },
    { path: 'meme.path', label: '表情包 JSON 路径', type: 'text', full: true, hint: '相对路径按项目根目录解析，默认 data/memes.json；结构与 Meme.json 一致（faces / mfaces / stickers）' },
    { path: 'meme.prompt_limit', label: '写进提示词的表情条数', type: 'number', step: 1, hint: '0 = 全部。表情很多时可以只给前 N 条，省 token；没列出来的名字模型不会用' },
  ],
  forward: [
    { path: 'auto_forward.enable', label: '启用自动搬运', type: 'bool' },
    { path: 'auto_forward.dedup', label: '启用内容去重（搬过就不再搬）', type: 'bool', hint: '关闭后同一内容会被反复搬运' },
    { path: 'auto_forward.groupid_list', label: '监听的来源群', type: 'intlist', item: 'group', full: true, hint: '只有这些群里发的转发卡片才会被搬运；可用「从群列表添加」按群名选' },
    { path: 'auto_forward.target_groupid', label: '搬运的目标群', type: 'intlist', item: 'group', full: true, hint: '会跳过消息原本所在的群' },
    { path: 'auto_forward.userid_list', label: '私聊来源 QQ（userid_list）', type: 'intlist', item: 'qq', full: true, hint: '私聊消息只有这些 QQ 会被搬运；留空 = 不搬任何私聊。也认 user_id_list 这种写法' },
    { path: 'auto_forward.task_userid', label: '另外私聊转发的好友（task_userid）', type: 'intlist', item: 'qq', full: true, hint: '搬运时除了目标群，还会私聊转发给这些好友' },
    { path: 'auto_forward.auto_img_forward_from_bot.enable', label: '机器人发的图片也自动搬运', type: 'bool' },
    {
      path: 'auto_forward.auto_img_forward_from_bot.bot_list',
      label: '视为机器人的 QQ（可限定生效范围）',
      type: 'objlist',
      full: true,
      idKey: 'bot_id',
      hint: '每个机器人一条：group = 只在指定群生效（群号留空 = 所有群聊）、private = 只私聊、all = 群聊与私聊都算。这些 QQ 发的图片会被自动搬运',
      empty: () => ({ bot_id: '', type: 'group', group_id: [] }),
      subFields: [
        { key: 'bot_id', label: '机器人 QQ', kind: 'id' },
        {
          key: 'type', label: '生效范围', kind: 'select',
          options: [['group', 'group（只看群聊）'], ['private', 'private（只看私聊）'], ['all', 'all（群聊 + 私聊）']],
        },
        // 只有群聊需要指定范围：私聊的发信人就是这个机器人自己，没什么可填的
        { key: 'group_id', label: '生效群号（留空 = 所有群聊）', kind: 'intlist', item: 'group', showWhen: (o) => o.type !== 'private' },
      ],
    },
    { path: 'auto_forward.strip_tail_ads.enable', label: '搬运时掐掉末尾广告', type: 'bool', full: true, hint: '开启后不再整卡转发：把聊天记录取回来，扫末尾几条、删掉二维码海报 / 群推荐卡片 / 纯链接，再重建成一份新的聊天记录' },
    { path: 'auto_forward.strip_tail_ads.scan_tail', label: '只扫末尾几条', type: 'number', step: 1, hint: '广告一般就挂在最后几条；0 = 不扫（只做追加）' },
    { path: 'auto_forward.strip_tail_ads.drop_qr', label: '删带二维码的图片', type: 'bool', hint: '用 OpenCV 识别，顺手把进群海报一起带走（需要 opencv-python，缺了这项自动跳过）' },
    { path: 'auto_forward.strip_tail_ads.drop_group_card', label: '删群推荐卡片', type: 'bool', hint: 'com.tencent.tuwen.lua / 群公告 / 落地页是 qun.qq.com、m.q.qq.com/a/p/ 的卡片' },
    { path: 'auto_forward.strip_tail_ads.drop_link', label: '删纯链接消息', type: 'bool', hint: '只删「整条就是一个链接」的；正文里顺带提一句网址不算' },
    { path: 'auto_forward.strip_tail_ads.image_timeout', label: '二维码识别超时（秒）', type: 'number', step: 1 },
    { path: 'auto_forward.strip_tail_ads.append.enable', label: '末尾追加自己的消息', type: 'bool', full: true, hint: '像别人的广告一样，在搬过去的聊天记录最后加一条（或多条）自己的签名' },
    {
      path: 'auto_forward.strip_tail_ads.append.items',
      label: '追加的消息（按顺序排在末尾）',
      type: 'objlist',
      full: true,
      idKey: 'uin',
      addLabel: '＋ 添加一条消息',
      blankNote: '条没填 QQ 号',
      dupNote: '条 QQ 号重复',
      hint: 'content 直接写字就是文本消息；要发图片就填消息段数组，例如 [{"type":"image","data":{"file":"D:/图片/a.png"}}]（本地路径会自动转 base64，NapCat 在 WSL / 容器里也发得出去）',
      empty: () => ({ uin: '', name: '', content: '' }),
      subFields: [
        { key: 'uin', label: 'QQ 号', kind: 'id', placeholder: '10004' },
        { key: 'name', label: '昵称', kind: 'text', placeholder: '显示在聊天记录里的名字' },
        { key: 'content', label: '内容', kind: 'textarea', placeholder: '文本，或消息段数组' },
      ],
    },
  ],
  text: [
    { path: 'prompts.group_context', label: '群聊补充说明（可选）', type: 'textarea', full: true, hint: '群号/发言人/时间/历史已经自动结构化，这里只写模型看不出来的背景（如「这是大连理工的校园群」）；留空即可' },
    { path: 'prompts.private_context', label: '私聊补充说明（可选）', type: 'textarea', hint: '同上，留空即可' },
    { path: 'prompts.history_command_regex', label: '「历史」指令前缀正则', type: 'text' },
    { path: 'replies.forwarded_already', label: '「搬过了」图片说明', type: 'text' },
    { path: 'replies.network_upgrade', label: '校园网升级提示文案', type: 'text' },
  ],
};

/* AI 模型配置的数据模型：每条配置 = 服务地址 + 密钥 + 模型名。
 *   agent.models          [{ id, name, base_url, api_key, api_key_env, model, temperature?, max_tokens? }]
 *   agent.active_model    使用中的配置 id
 *   agent.judge_model     插话判断器用的配置 id（空 = 跟随主模型）
 *   agent.fallback_models 主模型 429 时按顺序降级的配置 id 列表
 */
export const MODEL_KEYS = ['base_url', 'api_key', 'api_key_env', 'model'];
export const MODEL_ENTRY_KEYS = [...MODEL_KEYS, 'temperature', 'max_tokens'];

/** app.js 注册、raw 标签页调用的钩子，避免模块循环 import。 */
export const hooks = {
  renderAll: () => {},
  setBanner: () => {},
};

/* ══════════════════════ 脏标记 ══════════════════════ */
export function markDirty() {
  state.dirty = true;
  const badge = document.querySelector('#stateBadge');
  if (!badge) return;
  badge.className = 'state dirty';
  badge.textContent = '未保存';
}

export function markClean(text = '已保存') {
  state.dirty = false;
  const badge = document.querySelector('#stateBadge');
  if (!badge) return;
  badge.className = 'state saved';
  badge.textContent = text;
}
