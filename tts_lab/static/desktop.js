'use strict';

// The web application also runs in an ordinary browser. Native access exists
// only inside the Tauri webview, and never exposes filesystem paths or secrets.
(() => {
  const tauri = window.__TAURI__;
  if (!tauri?.core?.invoke) return;

  const origins = new Set(['http://tts-api.localhost', 'tts-api://localhost']);
  const origin = window.__TTS_API_ORIGIN__;
  const report = message => window.dispatchEvent(new CustomEvent('tts-desktop-error', {detail: message}));
  const action = name => tauri.core.invoke('desktop_action', {action: name});

  function subscribe(name, callback, normalize, errorMessage) {
    if (typeof callback !== 'function') return () => {};
    let disposed = false, unlisten;
    tauri.event.listen(name, event => {
      if (!disposed) callback(normalize(event.payload));
    }).then(stop => { if (disposed) stop(); else unlisten = stop; })
      .catch(() => report(errorMessage));
    return () => { disposed = true; unlisten?.(); };
  }

  function updateView(value = {}) {
    const view = {};
    for (const key of ['status', 'current_version', 'version', 'notes', 'error', 'distribution']) {
      if (typeof value?.[key] === 'string') view[key] = value[key];
    }
    for (const key of ['downloaded_bytes', 'total_bytes']) {
      view[key] = Number.isFinite(value?.[key]) && value[key] >= 0 ? value[key] : 0;
    }
    return view;
  }

  function apiRoute(value) {
    if (typeof value !== 'string' || !value.startsWith('/api/') || /[\\\x00-\x20\x7f]/.test(value)) throw new Error('无效的本机服务地址。');
    const parsed = new URL(value, 'https://tts.invalid');
    const pathname = value.split('?')[0];
    if (parsed.origin !== 'https://tts.invalid' || parsed.hash || parsed.pathname !== pathname || /%(?:2e|2f|5c)/i.test(pathname)) throw new Error('无效的本机服务地址。');
    return parsed.pathname + parsed.search;
  }

  const bridge = {
    apiUrl: route => {
      if (!origins.has(origin)) throw new Error('本机服务通信尚未准备好，请重新打开应用。');
      return origin + apiRoute(route);
    },
    minimize: () => action('minimize'),
    toggleMaximize: () => action('toggleMaximize'),
    close: () => action('close'),
    getWindowState: () => action('getWindowState'),
    restart: () => action('restart'),
    openDataDirectory: () => action('openDataDirectory'),
    getUpdateState: () => tauri.core.invoke('get_update_state'),
    checkUpdate: () => tauri.core.invoke('check_update'),
    downloadUpdate: () => tauri.core.invoke('download_update'),
    installUpdate: () => tauri.core.invoke('install_update'),
    onWindowState: callback => subscribe('desktop:window-state', callback,
      value => ({maximized: !!value?.maximized}), '无法更新窗口状态，请稍后重试。'),
    onUpdateState: callback => subscribe('desktop:update-state', callback,
      updateView, '无法读取更新进度，请重新检查更新。'),
  };
  Object.defineProperty(window, 'ttsDesktop', {value: Object.freeze(bridge)});

  // Only fixed export routes reach the native Save As dialog. The Rust side
  // checks the same boundary and chooses the filename from the API response.
  const exportRoute = /^\/api\/(?:export\.csv|runs\/[a-f0-9]{32}\/audio|batches\/[a-f0-9]{32}\/download)$/;
  document.addEventListener('click', event => {
    const link = event.target.closest?.('a[href]');
    if (!link) return;
    const href = link.getAttribute('href');
    if (!href || href.startsWith('#')) return;
    event.preventDefault();
    let request;
    try {
      if (href.startsWith('/api/')) {
        const route = apiRoute(href);
        if (!exportRoute.test(route)) throw new Error('不支持的导出地址。');
        request = tauri.core.invoke('save_export', {route});
      } else {
        const url = new URL(href);
        if (url.protocol !== 'https:' || url.username || url.password) throw new Error('仅支持打开 HTTPS 官方文档。');
        request = tauri.core.invoke('open_documentation', {url: url.href});
      }
      Promise.resolve(request).catch(() => report('桌面操作未完成，请稍后重试。'));
    } catch (error) { report(error.message); }
  });

  document.addEventListener('mousedown', event => {
    if (event.button !== 0 || !event.target.closest?.('.titlebar') ||
        event.target.closest('button, a, input, select, textarea, .window-controls')) return;
    event.preventDefault();
    action(event.detail === 2 ? 'toggleMaximize' : 'startDragging')
      .catch(() => report('无法移动窗口，请稍后重试。'));
  });
})();
