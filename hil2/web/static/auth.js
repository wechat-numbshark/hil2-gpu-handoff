/* 登录与会话。
 *
 * 三条路径，按环境自动选：
 *   1. 在飞书客户端里打开  → JSAPI 免登，用户无感知
 *   2. 浏览器 + 已开 SSO   → 跳飞书 OAuth 授权
 *   3. 未开 SSO（内网只读）→ 自报姓名，仅供内网演示
 *
 * 令牌存在 localStorage，所有写操作都带 Authorization 头。
 */
window.HIL2Auth = (() => {
  'use strict';

  const TOKEN_KEY = 'hil2-token';
  const NAME_KEY = 'hil2-identity';

  let session = null;
  const listeners = new Set();

  const store = {
    get token() { try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; } },
    set token(v) { try { v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY); } catch { /* 隐私模式 */ } },
    get name() { try { return localStorage.getItem(NAME_KEY) || ''; } catch { return ''; } },
    set name(v) { try { localStorage.setItem(NAME_KEY, v); } catch { /* 隐私模式 */ } }
  };

  function headers(extra = {}) {
    const h = { ...extra };
    if (store.token) h.Authorization = `Bearer ${store.token}`;
    return h;
  }

  /** 带登录态的 fetch。401 时清掉过期令牌并刷新会话。 */
  async function fetchAuthed(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: headers({ Accept: 'application/json', ...(options.headers || {}) })
    });
    if (response.status === 401 && store.token) {
      store.token = '';
      await refresh();
    }
    return response;
  }

  const inFeishu = () =>
    typeof window !== 'undefined' &&
    (!!window.h5sdk || /lark|feishu/i.test(navigator.userAgent || ''));

  async function refresh() {
    try {
      const response = await fetch('/api/auth/session', { headers: headers() });
      session = await response.json();
    } catch {
      session = { sso_enabled: false, authenticated: false, user: null, is_admin: false };
    }
    // 免登模式下用自报姓名兜底，让预约页仍然可用
    if (session.open_mode && !session.user && store.name) {
      session.user = { open_id: localOpenId(store.name), name: store.name, avatar_url: '' };
    }
    listeners.forEach((fn) => { try { fn(session); } catch { /* 单个订阅出错不影响其它 */ } });
    return session;
  }

  /** 免登模式下由姓名派生一个稳定 id；开了 SSO 后端会忽略它。 */
  function localOpenId(name) {
    let hash = 0;
    for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) | 0;
    return `local_${Math.abs(hash).toString(36)}`;
  }

  function setLocalName(name) {
    store.name = name;
    if (session?.open_mode) {
      session.user = name ? { open_id: localOpenId(name), name, avatar_url: '' } : null;
      listeners.forEach((fn) => fn(session));
    }
  }

  /** 飞书客户端内免登：JSAPI 鉴权 → 取临时码 → 换本系统令牌。 */
  async function loginInFeishu() {
    const url = window.location.href.split('#')[0];
    const params = await fetch(`/api/auth/feishu-auth-params?url=${encodeURIComponent(url)}`)
      .then((r) => { if (!r.ok) throw new Error('获取鉴权参数失败'); return r.json(); })
      .then((body) => body.data);

    await new Promise((resolve, reject) => {
      window.h5sdk.config({
        appId: params.appId,
        timestamp: params.timestamp,
        nonceStr: params.nonceStr,
        signature: params.signature,
        jsApiList: ['tt.requestAuthCode'],
        onSuccess: resolve,
        onFail: (err) => reject(new Error(err?.errorMessage || 'JSAPI 鉴权失败'))
      });
    });

    const code = await new Promise((resolve, reject) => {
      window.h5sdk.ready(() => {
        window.tt.requestAuthCode({
          appId: params.appId,
          success: (res) => resolve(res.code),
          fail: (err) => reject(new Error(err?.errMsg || '获取授权码失败'))
        });
      });
    });

    return exchange(code);
  }

  /** 浏览器 OAuth：跳飞书授权页，回来时带 code。 */
  async function loginInBrowser() {
    const back = window.location.pathname + window.location.search;
    try { sessionStorage.setItem('hil2-return-to', back); } catch { /* 忽略 */ }
    const response = await fetch('/api/auth/feishu-oauth-url');
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || '未配置飞书登录，请联系管理员');
    }
    window.location.href = (await response.json()).data.url;
  }

  async function exchange(code, redirectUri) {
    const response = await fetch('/api/auth/feishu-login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(redirectUri ? { code, redirect_uri: redirectUri } : { code })
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || '登录失败');
    }
    const data = (await response.json()).data;
    store.token = data.token;
    if (data.user?.name) store.name = data.user.name;
    await refresh();
    return data;
  }

  async function login() {
    if (!session) await refresh();
    if (!session.sso_enabled) throw new Error('当前是免登模式，直接填写姓名即可预约');
    return inFeishu() ? loginInFeishu() : loginInBrowser();
  }

  function logout() {
    store.token = '';
    return refresh();
  }

  return {
    get session() { return session; },
    get user() { return session?.user || null; },
    get isAdmin() { return !!session?.is_admin; },
    get openMode() { return !!session?.open_mode; },
    get localName() { return store.name; },
    onChange(fn) { listeners.add(fn); if (session) fn(session); return () => listeners.delete(fn); },
    refresh, login, logout, exchange, setLocalName, fetchAuthed, headers, inFeishu
  };
})();
