/**
 * 后端 API 封装。
 *
 * - token：`?token=` > localStorage > sessionStorage（兼容旧面板）
 * - API 地址：`?api=http://127.0.0.1:8390` > localStorage > 同源
 * - 统一 JSON 解析；网络错误 / 401 自动弹提示条
 */

import { setBanner } from './dom.js';

const params = new URLSearchParams(window.location.search);

const TOKEN_KEY = 'panel_token';
const API_KEY = 'panel_api';

function readStorage(key) {
  try {
    return window.localStorage.getItem(key) || window.sessionStorage.getItem(key) || '';
  } catch (err) {
    return '';
  }
}

function writeStorage(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (err) {
    try { window.sessionStorage.setItem(key, value); } catch (ignored) { /* 隐私模式 */ }
  }
}

function normalizeBase(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  return raw.replace(/\/+$/, '');
}

/* 只在第一次加载时读；页面内不会改 token。 */
export const token = (() => {
  const fromQuery = params.get('token');
  const value = fromQuery || readStorage(TOKEN_KEY);
  if (fromQuery) writeStorage(TOKEN_KEY, fromQuery);
  return value;
})();

export function getApiBase() {
  const fromQuery = params.get('api');
  if (fromQuery) {
    const base = normalizeBase(fromQuery);
    writeStorage(API_KEY, base);
    return base;
  }
  return normalizeBase(readStorage(API_KEY));
}

/** 把 `/api/status` 拼成实际请求地址。 */
export function apiUrl(path) {
  if (/^https?:\/\//i.test(path)) return path;
  const base = getApiBase();
  return base ? `${base}${path.startsWith('/') ? '' : '/'}${path}` : path;
}

function authHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
    headers['X-Panel-Token'] = token;   // 旧后端只认这个
  }
  return headers;
}

export async function api(path, { method = 'GET', body = null } = {}) {
  let res;
  try {
    res = await fetch(apiUrl(path), {
      method,
      headers: authHeaders(),
      body: body === null ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    const message = `网络错误：${err && err.message ? err.message : err}`;
    setBanner('err', '连接不上面板后端', [message, `请求地址：${apiUrl(path)}`]);
    return { status: 0, body: { ok: false, errors: [message] } };
  }

  let payload;
  try {
    payload = await res.json();
  } catch (err) {
    payload = { ok: false, errors: [`返回内容不是 JSON（HTTP ${res.status}）`] };
  }

  if (res.status === 401) {
    setBanner('err', '访问被拒绝（token 不正确或缺失）', [
      '请用 http://主机:端口/?token=xxx 打开面板',
      ...((payload && payload.errors) || []),
    ]);
  }
  return { status: res.status, body: payload };
}

export { setBanner };
