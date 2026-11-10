'use strict';
const { app, BrowserWindow, Menu, dialog, session, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { startBackend } = require('./backend');
const { isLocalUrl, isExternalDocumentation, requestHeaders, profileDirectory } = require('./security');

app.setName('TTS Lab');
const smokeMode = process.argv.includes('--smoke-test');
// Self-tests always get a new blank profile and never read the user's credentials.
const profile = smokeMode ? fs.mkdtempSync(path.join(os.tmpdir(), 'tts-lab-smoke-'))
  : profileDirectory(process.platform, process.env.LOCALAPPDATA, app.getPath('appData'));
fs.mkdirSync(profile, { recursive: true, mode: 0o700 });
app.setPath('userData', profile);
const configPath = path.join(profile, 'credentials.env');
const dataDirectory = path.join(profile, 'data');
const desktopToken = crypto.randomBytes(32).toString('hex');
let window;
let backend;
let origin;
let quitting = false;
let allowQuit = false;
let closePromise;
let exitCode = 0;
const smokeDownloads = [];

const resources = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '..');
const backendPath = app.isPackaged ? path.join(resources, 'backend', 'run.py') : path.join(resources, 'run.py');
const python = app.isPackaged ? path.join(resources, 'python', 'python.exe')
  : (process.env.TTS_LAB_PYTHON || (process.platform === 'win32' ? 'python' : 'python3'));
const templatePath = app.isPackaged ? path.join(resources, 'app', 'credentials.env.example')
  : path.join(resources, 'desktop', 'credentials.env.example');

async function localRequest(route) {
  return fetch(origin + route, {
    headers: { 'X-Desktop-Token': desktopToken }, signal: AbortSignal.timeout(2500),
  });
}

async function requestQuit(force = false) {
  if (closePromise) return closePromise;
  closePromise = (async () => {
    if (!force && !smokeMode && origin && window && !window.isDestroyed()) {
      try {
        const response = await localRequest('/api/runs');
        const { runs } = await response.json();
        if (runs.some(run => ['queued', 'running'].includes(run.status))) {
          const answer = await dialog.showMessageBox(window, {
            type: 'warning', title: '仍有任务在生成', buttons: ['留在窗口', '仍然退出'],
            defaultId: 0, cancelId: 0,
            message: '退出会中断本机等待，云服务仍可能完成并计费。',
            detail: '再次打开后可查看历史。请确认服务状态后再手动重试，程序不会自动重发。',
          });
          if (answer.response !== 1) { closePromise = null; return; }
        }
      } catch { /* A failed local service must never prevent closing the app. */ }
    }
    quitting = true;
    if (backend) await backend.stop();
    allowQuit = true;
    app.exit(exitCode);
  })();
  return closePromise;
}

function fatal(message) {
  if (quitting) return;
  exitCode = 1;
  if (!smokeMode) dialog.showErrorBox('TTS Lab 无法继续运行', message + '\n不会自动重试云端请求。');
  void requestQuit(true);
}

async function openDocumentation(url) {
  if (!isExternalDocumentation(url) || !window || window.isDestroyed()) return;
  const result = await dialog.showMessageBox(window, {
    type: 'question', title: '打开外部网页', buttons: ['取消', '用浏览器打开'],
    defaultId: 0, cancelId: 0, message: '在默认浏览器中打开此文档？', detail: url,
  });
  if (result.response === 1) await shell.openExternal(url).catch(() => {});
}

function installMenu() {
  const restart = async () => {
    const result = await dialog.showMessageBox(window, {
      type: 'question', title: '重新载入凭证', buttons: ['取消', '重启应用'], defaultId: 0,
      message: '保存 credentials.env 后，重启应用使配置生效。',
      detail: '如果有正在生成的任务，请先等它结束。重启不撤销云端已经收到的请求。',
    });
    if (result.response !== 1) return;
    // Do not relaunch until the child is fully stopped; the single-instance lock
    // would otherwise race with the replacement instance.
    quitting = true;
    await backend.stop();
    app.relaunch(); allowQuit = true; app.exit(0);
  };
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: '文件', submenu: [
      { label: '编辑 API 凭证…', click: async () => {
        if (process.platform === 'win32') {
          const editor = spawn(path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'notepad.exe'), [configPath], { shell: false, detached: false, stdio: 'ignore' });
          editor.on('error', () => dialog.showErrorBox('无法打开记事本', `请手动编辑：${configPath}`));
          editor.unref();
        } else await shell.openPath(configPath);
      } },
      { label: '重启并载入凭证…', click: restart },
      { type: 'separator' },
      { label: '打开数据和配置目录', click: () => shell.openPath(profile) },
      { type: 'separator' }, { label: '退出', accelerator: 'Alt+F4', click: () => requestQuit() },
    ] },
    { label: '编辑', submenu: [{ role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' },
      { type: 'separator' }, { role: 'cut', label: '剪切' }, { role: 'copy', label: '复制' },
      { role: 'paste', label: '粘贴' }, { role: 'selectAll', label: '全选' }] },
    { label: '视图', submenu: [{ role: 'resetZoom', label: '原始大小' },
      { role: 'zoomIn', label: '放大' }, { role: 'zoomOut', label: '缩小' },
      { role: 'togglefullscreen', label: '全屏' }] },
    { label: '帮助', submenu: [{ label: '关于与配置说明', click: () => dialog.showMessageBox(window, {
      type: 'info', title: 'TTS Lab 0.2.1', message: 'TTS Lab · 本地语音对比工作台',
      detail: '已内置 Electron、Python 和服务依赖，无需另装 Python。\n\n首次可直接使用离线提示音。真实语音需要你自己的服务凭证和网络，可能收费。\n\n通过“文件 → 编辑 API 凭证”配置，保存后重启。凭证以本机明文文件保存，不在网页中显示，也不会包含在音频导出中。请勿分享配置目录。\n\n数据位置：' + profile + '\n\n此便携版未作应用代码签名；不自动更新或上传数据。',
    }) }] },
  ]));
}

async function start() {
  fs.mkdirSync(dataDirectory, { recursive: true, mode: 0o700 });
  if (!fs.existsSync(configPath)) fs.copyFileSync(templatePath, configPath, fs.constants.COPYFILE_EXCL);
  if (process.platform !== 'win32') fs.chmodSync(configPath, 0o600);
  backend = startBackend({ python, script: backendPath, config: configPath, dataDirectory,
    token: desktopToken, onUnexpectedExit: () => fatal('本机语音服务意外停止。请重新打开应用后检查历史记录。') });
  origin = await backend.ready;
  const localSession = session.fromPartition('tts-lab-' + crypto.randomUUID());
  localSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  localSession.setPermissionCheckHandler(() => false);
  localSession.setDevicePermissionHandler(() => false);
  localSession.webRequest.onBeforeRequest({ urls: ['<all_urls>'] }, (details, callback) => {
    callback({ cancel: !isLocalUrl(details.url, origin) });
  });
  localSession.webRequest.onBeforeSendHeaders({ urls: ['<all_urls>'] }, (details, callback) => {
    callback({ requestHeaders: requestHeaders(details.url, origin, desktopToken, details.requestHeaders) });
  });
  localSession.on('will-download', (_event, item) => {
    if (smokeMode) {
      const folder = path.join(profile, 'exports');
      fs.mkdirSync(folder, { recursive: true });
      const download = { file: path.join(folder, path.basename(item.getFilename())), state: 'progressing' };
      smokeDownloads.push(download);
      item.setSavePath(download.file);
      item.once('done', (_event, state) => { download.state = state; });
      return;
    }
    // Native Save As lets the user choose the export destination each time.
    item.setSaveDialogOptions({ title: '保存 TTS Lab 导出文件', defaultPath: path.join(app.getPath('downloads'), item.getFilename()) });
  });
  window = new BrowserWindow({
    width: 1440, height: 980, minWidth: 880, minHeight: 640, show: false,
    title: 'TTS Lab', backgroundColor: '#f5f3ed',
    webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true,
      webSecurity: true, allowRunningInsecureContent: false, webviewTag: false,
      navigateOnDragDrop: false, spellcheck: false, devTools: !app.isPackaged,
      session: localSession },
  });
  window.webContents.setWindowOpenHandler(({ url }) => { void openDocumentation(url); return { action: 'deny' }; });
  window.webContents.on('will-navigate', (event, url) => { if (!isLocalUrl(url, origin)) event.preventDefault(); });
  window.webContents.on('will-redirect', (event, url) => { if (!isLocalUrl(url, origin)) event.preventDefault(); });
  window.webContents.on('will-attach-webview', event => event.preventDefault());
  window.webContents.on('render-process-gone', () => fatal('桌面界面意外停止。请重新打开应用。'));
  window.on('close', event => { if (!allowQuit) { event.preventDefault(); void requestQuit(); } });
  window.once('ready-to-show', () => { if (!smokeMode) window.show(); });
  installMenu();
  await window.loadURL(origin + '/');
  if (smokeMode) {
    try {
      const result = await require('./smoke').runSmoke({ window, origin, token: desktopToken, profile, downloads: smokeDownloads });
      fs.writeFileSync(path.join(profile, 'smoke-result.json'), JSON.stringify(result, null, 2));
      // Windows GUI executables may have no stdout handle; report through this file.
    } catch (error) {
      exitCode = 1;
      const result = { status: 'failed', error: String(error.message).slice(0, 500), profile };
      fs.writeFileSync(path.join(profile, 'smoke-result.json'), JSON.stringify(result, null, 2));
    }
    await requestQuit(true);
  }
}

if (!app.requestSingleInstanceLock()) app.exit(0);
else {
  app.on('second-instance', () => { if (window) { if (window.isMinimized()) window.restore(); window.show(); window.focus(); } });
  app.on('before-quit', event => { if (!allowQuit) { event.preventDefault(); void requestQuit(); } });
  app.whenReady().then(start).catch(() => fatal('启动失败。请确认发行包已完整解压，且本机用户目录可写。'));
}
