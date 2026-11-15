'use strict';
const U = {view: null, pending: false};
const C = {provider: '', loading: false, saving: false, revision: 0};
function credentialControls() {
  $('#credential-save').disabled = C.loading || C.saving || !C.provider || S.busy || S.modelBusy || S.updating;
  $('#credential-save').textContent = C.saving ? '正在保存…' : '保存配置';
  $('#credential-close').disabled = C.saving;
  $('#credential-cancel').disabled = C.saving;
  for (const control of document.querySelectorAll('#credential-fields input, #credential-fields select, #credential-fields button')) {
    control.disabled = C.saving || S.updating || !!control.closest('[data-aws-mode][hidden]');
  }
}
function credentialError(message = '') {
  $('#credential-error').textContent = message;
  $('#credential-error').hidden = !message;
}
function closeCredentials() {
  if (C.saving) return;
  const id = C.provider;
  clearCredentials();
  $('#credential-dialog').close();
  // A successful save replaces the card nodes; focus the current card.
  queueMicrotask(() => $(`#cloud-services [data-provider="${id}"]`)?.focus());
}
function clearCredentials() {
  for (const input of $('#credential-dialog').querySelectorAll('input')) { input.value = ''; input.type = 'password'; }
  $('#credential-fields').replaceChildren(); credentialError();
  C.revision++; C.provider = ''; C.loading = false;
}
function setupCredentials() {
  const dialog = $('#credential-dialog');
  $('#credential-close').onclick = closeCredentials;
  $('#credential-cancel').onclick = closeCredentials;
  dialog.addEventListener('cancel', event => { event.preventDefault(); closeCredentials(); });
  dialog.addEventListener('close', () => {
    // Clear even revealed, unsaved inputs on every exit (including Escape).
    if (!dialog.open && C.provider) clearCredentials();
  });
  $('#credential-form').onsubmit = saveCredentials;
}
async function openCredentials(id) {
  if (S.updating || C.saving) return;
  C.provider = id; C.loading = true;
  const revision = ++C.revision;
  $('#credential-title').textContent = `${provider(id).name} 配置`;
  $('#credential-fields').replaceChildren(el('p', '正在读取配置状态…', 'hint'));
  credentialError(); credentialControls(); $('#credential-dialog').showModal();
  try {
    const settings = await api(`/api/credentials/${id}`);
    if (revision !== C.revision) return;
    renderCredentialFields(settings);
  } catch {
    if (revision !== C.revision) return;
    C.provider = ''; $('#credential-fields').replaceChildren();
    credentialError('无法读取配置状态，请关闭后重试。');
  } finally {
    if (revision === C.revision) { C.loading = false; credentialControls(); }
  }
}
function renderCredentialFields(settings) {
  const root = $('#credential-fields'); root.replaceChildren();
  if (settings.provider === 'polly') {
    const label = el('label', '认证方式', 'credential-field');
    const mode = el('select'); mode.id = 'credential-mode';
    mode.append(option('keys', 'Access Key / Secret Key'), option('profile', '本机 AWS Profile'));
    mode.value = settings.mode; label.append(mode); root.append(label);
    mode.onchange = () => {
      for (const row of root.querySelectorAll('[data-aws-mode]')) {
        const hidden = row.dataset.awsMode !== mode.value || (row.dataset.awsTemporary && !$('#credential-temporary')?.checked); row.hidden = hidden;
        for (const control of row.querySelectorAll('input, button')) control.disabled = hidden;
      }
    };
  }
  for (const field of settings.fields) {
    const row = el('div', undefined, 'credential-field');
    if (settings.provider === 'polly') {
      if (field.key === 'AWS_PROFILE') row.dataset.awsMode = 'profile';
      else if (['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'].includes(field.key)) row.dataset.awsMode = 'keys';
      if (field.key === 'AWS_SESSION_TOKEN') row.dataset.awsTemporary = '1';
    }
    const id = `credential-${field.key}`;
    const label = el('label', field.label); label.htmlFor = id;
    if (field.secret) label.append(el('span', field.configured ? '已配置' : '未配置', 'credential-state'));
    const wrap = el('div', undefined, 'credential-input');
    const input = el('input'); input.id = id; input.dataset.credential = field.key;
    input.type = field.secret ? 'password' : 'text'; input.maxLength = 4096;
    input.autocomplete = 'off'; input.spellcheck = false; input.setAttribute('autocapitalize', 'off');
    input.value = field.secret ? '' : field.value || '';
    input.placeholder = field.secret ? (field.configured ? '已配置，留空保留；输入新值替换' : '请输入凭证') : field.key;
    input.setAttribute('aria-describedby', 'credential-help'); wrap.append(input);
    if (field.secret) {
      const eye = el('button', undefined, 'credential-eye'); eye.type = 'button';
      eye.setAttribute('aria-label', `显示新输入的 ${field.label}`); eye.setAttribute('aria-pressed', 'false');
      // Constant SVG only; no API data is interpreted as markup.
      eye.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/><path class="eye-slash" d="m3 3 18 18"/></svg>';
      eye.onclick = () => {
        const shown = input.type === 'password'; input.type = shown ? 'text' : 'password';
        eye.setAttribute('aria-pressed', String(shown));
        eye.setAttribute('aria-label', `${shown ? '隐藏' : '显示'}新输入的 ${field.label}`);
      };
      wrap.append(eye);
    }
    row.append(label, wrap);
    if (field.help) row.append(el('p', field.help, 'credential-variable'));
    root.append(row);
  }
  if (settings.provider === 'polly') {
    const label = el('label', undefined, 'check credential-temporary');
    label.dataset.awsMode = 'keys';
    const check = el('input'); check.type = 'checkbox'; check.id = 'credential-temporary'; check.checked = settings.temporary;
    check.onchange = () => $('#credential-mode').onchange();
    label.append(check, document.createTextNode('使用临时凭证（需要 Session Token）；取消后保存会清除旧 Token'));
    root.append(label); $('#credential-mode').onchange();
  }
  const first = root.querySelector('input:not(:disabled), select'); first?.focus();
}
async function saveCredentials(event) {
  event.preventDefault();
  if (C.loading || C.saving || !C.provider || S.busy || S.modelBusy || S.updating) return;
  const values = {};
  for (const input of document.querySelectorAll('#credential-fields input[data-credential]:not(:disabled)')) values[input.dataset.credential] = input.value;
  const body = {values};
  if (C.provider === 'polly') { body.mode = $('#credential-mode').value; body.temporary = $('#credential-temporary').checked; }
  C.saving = true; credentialError(); lockControls();
  for (const input of document.querySelectorAll('#credential-fields input[data-credential]')) {
    if (input.closest('.credential-input').querySelector('.credential-eye')) input.type = 'password';
  }
  for (const eye of document.querySelectorAll('.credential-eye')) {
    eye.setAttribute('aria-pressed', 'false');
    eye.setAttribute('aria-label', eye.getAttribute('aria-label').replace('隐藏', '显示'));
  }
  try {
    const result = await api(`/api/credentials/${C.provider}`, {method: 'PUT', body: JSON.stringify(body)});
    S.providers = result.providers.filter(p => p.family !== 'demo');
    C.saving = false; closeCredentials(); renderServices();
    renderVariants();
  } catch (error) {
    credentialError(error.message || '保存失败，请稍后重试。');
  } finally { C.saving = false; lockControls(); }
}
function renderUpdateControls() {
  const view = U.view;
  if (!view) return;
  const active = U.pending || ['checking', 'downloading', 'installing'].includes(view.status);
  const development = view.distribution === 'development';
  $('#check-app-update').disabled = active;
  $('#check-app-update').textContent = view.status === 'checking' ? '正在检查…' : view.status === 'error' && !view.version ? '重试检查' : '检查更新';
  const download = $('#download-app-update'), install = $('#install-app-update');
  download.hidden = development || !view.version || !['available', 'error'].includes(view.status);
  download.disabled = active;
  download.textContent = view.status === 'error' ? '重试下载' : '下载更新';
  install.hidden = development || !['ready', 'installing'].includes(view.status);
  install.disabled = active || S.busy || S.saving || C.saving || S.modelBusy;
  install.textContent = view.status === 'installing' ? '正在安装…' : '安装并重启';
  $('#app-update-install-hint').textContent = development
    ? '开发环境仅检查版本，请从发布页下载安装。'
    : view.status === 'ready' && (S.busy || S.saving || S.modelBusy)
      ? '请等待语音生成、方案保存或模型安装完成，再安装更新。个人数据会保留。'
      : '安装会关闭应用、更新程序并重新启动；个人配置、本地模型和历史音频会保留。';
}
function renderUpdateView(view) {
  if (!view || typeof view.status !== 'string') return;
  U.view = view;
  const installing = view.status === 'installing';
  if (S.updating !== installing) { S.updating = installing; lockControls(); }
  const version = view.version ? `v${view.version}` : '';
  const labels = {
    idle: '尚未检查更新。', checking: '正在检查新版本…', current: '当前已是最新版本。',
    available: `发现新版本 ${version}，可下载后安装。`, downloading: `正在下载 ${version}，请保持应用打开。`,
    ready: `${version} 已下载并校验完成，可以安装。`, installing: '正在退出并安装更新，请稍候…',
    error: view.error || '更新未完成，请检查网络后重试。',
  };
  const status = $('#app-update-status');
  status.textContent = labels[view.status] || '无法读取更新状态，请重新检查。';
  status.classList.toggle('error', view.status === 'error');
  $('#app-update-version').textContent = `当前 ${view.current_version ? `v${view.current_version}` : $('#app-version').textContent} · ${{installed: '安装版', portable: '便携版', development: '开发环境'}[view.distribution] || '桌面版'}`;
  const downloaded = Math.max(0, Number(view.downloaded_bytes) || 0), total = Math.max(0, Number(view.total_bytes) || 0);
  const bytes = value => value >= 1048576 ? `${(value / 1048576).toFixed(1)} MiB` : value >= 1024 ? `${(value / 1024).toFixed(1)} KiB` : `${value} B`;
  $('#app-update-progress-wrap').hidden = !['downloading', 'ready'].includes(view.status) && !downloaded;
  const progress = $('#app-update-progress');
  if (total > 0) { progress.max = total; progress.value = Math.min(downloaded, total); }
  else progress.removeAttribute('value');
  $('#app-update-bytes').textContent = total > 0 ? `${bytes(downloaded)} / ${bytes(total)}` : `${bytes(downloaded)} 已下载`;
  $('#app-update-notes-wrap').hidden = !view.notes;
  $('#app-update-notes').textContent = typeof view.notes === 'string' ? view.notes : '';
  const shortcut = $('#updates-shortcut');
  shortcut.textContent = view.status === 'ready' ? '更新已就绪 ↗' : view.version && ['available', 'downloading'].includes(view.status) ? `新版本 ${version} ↗` : '应用更新 ↗';
  renderUpdateControls();
}
function updateStorage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* Storage can be disabled; manual checks remain available. */ }
  return null;
}
async function updateAction(action) {
  if (U.pending || ['checking', 'downloading', 'installing'].includes(U.view?.status)) return;
  if (action === 'installUpdate' && (U.view?.status !== 'ready' || S.busy || S.saving || C.saving || S.modelBusy)) return;
  U.pending = true;
  if (action === 'checkUpdate') updateStorage('ttsLab.updates.lastCheckAt', String(Date.now()));
  renderUpdateView({...U.view, status: {checkUpdate: 'checking', downloadUpdate: 'downloading', installUpdate: 'installing'}[action], error: ''});
  try {
    const view = await window.ttsDesktop[action]();
    if (view?.status) renderUpdateView(view);
    else renderUpdateView(await window.ttsDesktop.getUpdateState());
  } catch (error) {
    const message = typeof error === 'string' ? error : error?.message;
    renderUpdateView({...U.view, status: 'error', error: message || '更新未完成，请检查网络后重试。'});
  } finally { U.pending = false; renderUpdateControls(); }
}
async function setupUpdates() {
  const desktop = window.ttsDesktop;
  if (!desktop?.getUpdateState) return;
  $('#app-update-panel').hidden = false; $('#updates-shortcut').hidden = false;
  $('#updates-shortcut').onclick = () => { showView('models'); $('#app-update-panel').scrollIntoView({block: 'start', behavior: 'smooth'}); $('#app-update-title').focus({preventScroll: true}); };
  const auto = $('#app-update-auto');
  auto.checked = updateStorage('ttsLab.updates.autoCheck') !== '0';
  auto.onchange = () => updateStorage('ttsLab.updates.autoCheck', auto.checked ? '1' : '0');
  $('#check-app-update').onclick = () => updateAction('checkUpdate');
  $('#download-app-update').onclick = () => updateAction('downloadUpdate');
  $('#install-app-update').onclick = () => updateAction('installUpdate');
  renderUpdateView({status: 'idle', downloaded_bytes: 0, total_bytes: 0});
  let revision = 0;
  desktop.onUpdateState(view => { revision++; renderUpdateView(view); });
  try {
    const initialRevision = revision;
    const view = await desktop.getUpdateState();
    if (revision === initialRevision && !U.pending) renderUpdateView(view);
    const now = Date.now(), last = Number(updateStorage('ttsLab.updates.lastCheckAt')) || 0;
    if (window.__TTS_UPDATES_AUTO__ === true && auto.checked && ['idle', 'current'].includes(U.view.status) &&
        (last > now || now - last >= 86400000)) void updateAction('checkUpdate');
  } catch (error) {
    renderUpdateView({...U.view, status: 'error', error: '无法读取更新状态，请重新检查更新。'});
  }
}
async function setupDesktop() {
  if (!window.ttsDesktop) return;
  $('#window-controls').hidden = false; $('#desktop-settings').hidden = false;
  const desktopAction = action => async () => { try { await action(); } catch { notice('桌面操作未完成，请稍后重试。', true); } };
  $('#window-minimize').onclick = desktopAction(() => window.ttsDesktop.minimize());
  $('#window-maximize').onclick = desktopAction(() => window.ttsDesktop.toggleMaximize());
  $('#window-close').onclick = desktopAction(() => window.ttsDesktop.close());
  $('#open-data-directory').onclick = desktopAction(() => window.ttsDesktop.openDataDirectory());
  const updateWindowState = state => {
    const button = $('#window-maximize'), label = state.maximized ? '还原' : '最大化';
    button.setAttribute('aria-label', label); button.title = label;
    button.classList.toggle('maximized', state.maximized);
  };
  window.ttsDesktop.onWindowState(updateWindowState);
  updateWindowState(await window.ttsDesktop.getWindowState());
}

function renderServices() {
  const root = $('#cloud-services'); root.replaceChildren();
  for (const item of S.providers.filter(item => item.family !== 'local')) {
    const card = el('button', undefined, 'cloud-model');
    card.type = 'button'; card.dataset.provider = item.id;
    card.setAttribute('aria-haspopup', 'dialog'); card.setAttribute('aria-controls', 'credential-dialog');
    card.setAttribute('aria-label', `配置 ${item.name}`);
    card.disabled = S.updating || C.saving;
    card.append(el('div', item.name), el('span', item.credentials_configured ? '已配置' : '待配置', item.credentials_configured ? 'available' : ''));
    card.onclick = () => openCredentials(item.id); root.append(card);
  }
}
function freezeUpdateControls() {
  for (const control of document.querySelectorAll('#generate, #add-variant, .variant input, .variant select, .variant textarea, .variant button, #text, .samples button, #clear-text, #local-device, #local-model-list button, #cloud-services button, .score-grid select, .result-notes, .score-actions button, #preset-name, #confirm-save')) {
    if (S.updating) {
      if (!control.hasAttribute('data-update-disabled')) control.dataset.updateDisabled = control.disabled ? '1' : '0';
      control.disabled = true;
    } else if (control.hasAttribute('data-update-disabled')) {
      control.disabled = control.dataset.updateDisabled === '1'; delete control.dataset.updateDisabled;
    }
  }
}
function lockControls() {
  freezeUpdateControls(); renderVariants(); credentialControls(); renderUpdateControls();
}
async function setupSettings() {
  setupCredentials();
  window.addEventListener('tts-desktop-error', event => notice(event.detail, true));
  try { await setupDesktop(); } catch { notice('无法读取窗口状态，请稍后重试。', true); }
  void setupUpdates();
}
