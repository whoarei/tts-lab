'use strict';
const { app, BrowserWindow, Menu, dialog, session, shell, ipcMain } = require('electron');
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

async function restartApplication() {
  const result = await dialog.showMessageBox(window, {
    type: 'question', title: '重新载入凭证', buttons: ['取消', '重启应用'], defaultId: 0, cancelId: 0,
    message: '保存 credentials.env 后，重启应用使配置生效。',
    detail: '重启会中断正在生成的任务，云端请求仍可能完成并计费。配置和历史记录会保留。',
  });
  if (result.response !== 1 || quitting || closePromise) return;
  quitting = true;
  await backend.stop();
  app.relaunch(); allowQuit = true; app.exit(0);
}

async function editCredentials() {
  if (process.platform === 'win32') {
    const editor = spawn(path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'notepad.exe'),
      [configPath], { shell: false, detached: false, stdio: 'ignore' });
    editor.on('error', () => dialog.showErrorBox('无法打开记事本', `请手动编辑：${configPath}`));
    editor.unref();
  } else {
    const error = await shell.openPath(configPath);
    if (error) throw new Error('无法打开凭证文件');
  }
}

function installDesktopControls() {
  // A narrow bridge: only this window's local main frame may invoke fixed actions.
  const actions = {
    minimize: () => window.minimize(),
    toggleMaximize: () => { if (window.isMaximized()) window.unmaximize(); else window.maximize(); },
    close: () => { void requestQuit(); },
    getWindowState: () => ({ maximized: window.isMaximized() }),
    editCredentials,
    restart: restartApplication,
    openDataDirectory: async () => { if (await shell.openPath(profile)) throw new Error('无法打开配置目录'); },
  };
  ipcMain.handle('desktop:action', async (event, action) => {
    if (!window || window.isDestroyed() || event.sender !== window.webContents ||
      event.senderFrame !== window.webContents.mainFrame || !isLocalUrl(event.senderFrame.url, origin) ||
      !Object.hasOwn(actions, action)) throw new Error('拒绝桌面操作');
    return actions[action]();
  });
  const update = () => { if (!window.isDestroyed()) window.webContents.send('desktop:window-state', { maximized: window.isMaximized() }); };
  window.on('maximize', update); window.on('unmaximize', update);
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
    width: 1360, height: 920, minWidth: 880, minHeight: 640, show: false, frame: false,
    title: 'TTS Lab', backgroundColor: '#f6f8fa',
    webPreferences: { preload: path.join(__dirname, 'preload.js'), nodeIntegration: false, contextIsolation: true, sandbox: true,
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
  Menu.setApplicationMenu(null);
  window.setMenu(null);
  installDesktopControls();
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
