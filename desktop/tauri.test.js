'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../tts_lab/static/desktop.js'), 'utf8');
const id = 'a'.repeat(32);

function harness({native = true, origin = 'http://tts-api.localhost'} = {}) {
  const calls = [], errors = [], listeners = new Map();
  const subscriptions = new Map();
  let stopCount = 0, rejectCommand;
  const window = {
    __TTS_API_ORIGIN__: origin,
    dispatchEvent: event => errors.push(event.detail),
  };
  if (native) window.__TAURI__ = {
    core: {invoke: async (command, args) => {
      calls.push({command, args});
      if (command === rejectCommand) throw new Error('private backend error');
      return {maximized: false};
    }},
    event: {listen: async (name, callback) => {
      assert.ok(['desktop:window-state', 'desktop:update-state'].includes(name)); subscriptions.set(name, callback);
      return () => { stopCount++; };
    }},
  };
  vm.runInNewContext(source, {window, document: {addEventListener: (name, callback) => listeners.set(name, callback)},
    URL, CustomEvent: class { constructor(name, options) { this.type = name; this.detail = options.detail; } }});
  return {window, calls, errors, listeners,
    state: payload => subscriptions.get('desktop:window-state')({payload}),
    update: payload => subscriptions.get('desktop:update-state')({payload}), stops: () => stopCount,
    reject: name => { rejectCommand = name; },
    click: href => {
      let prevented = false;
      const link = {getAttribute: () => href};
      listeners.get('click')({target: {closest: () => link}, preventDefault: () => { prevented = true; }});
      return prevented;
    }};
}

test('ordinary browser mode adds no native bridge or document handlers', () => {
  const h = harness({native: false});
  assert.equal(h.window.ttsDesktop, undefined);
  assert.equal(h.listeners.size, 0);
});

test('native API URLs stay within the private protocol and reject path escapes', () => {
  for (const origin of ['http://tts-api.localhost', 'tts-api://localhost']) {
    const bridge = harness({origin}).window.ttsDesktop;
    assert.equal(bridge.apiUrl('/api/state'), origin + '/api/state');
    assert.equal(bridge.apiUrl('/api/references?name=test%2Ffile.wav'), origin + '/api/references?name=test%2Ffile.wav');
    for (const route of ['https://example.com/api/state', '//example.com/api/state', '/api/../secret',
      '/api/%2e%2e/secret', '/api/runs%2Faudio', '/api/\\secret', '/api/state#secret', '/api/state\n', '/static/app.js', null]) {
      assert.throws(() => bridge.apiUrl(route), /无效/);
    }
    assert.ok(Object.isFrozen(bridge));
  }
  assert.throws(() => harness({origin: 'https://example.com'}).window.ttsDesktop.apiUrl('/api/state'), /尚未准备好/);
});

test('bridge invokes only the fixed desktop control contract', async () => {
  const h = harness(), bridge = h.window.ttsDesktop;
  for (const name of ['minimize', 'toggleMaximize', 'close', 'getWindowState', 'restart', 'openDataDirectory']) {
    await bridge[name]();
    assert.equal(h.calls.at(-1).command, 'desktop_action');
    assert.equal(h.calls.at(-1).args.action, name);
  }
  assert.equal(bridge.invoke, undefined);
  assert.equal(bridge.editCredentials, undefined);
});

test('window state listeners sanitize payloads and dispose even before registration completes', async () => {
  const h = harness(); let state;
  const stop = h.window.ttsDesktop.onWindowState(value => { state = value; });
  h.state({maximized: 1, secret: 'not forwarded'});
  assert.equal(state.maximized, true); assert.equal(state.secret, undefined);
  stop();
  await Promise.resolve();
  assert.equal(h.stops(), 1);
  h.state({maximized: false});
  assert.equal(state.maximized, true);
});

test('updater commands are fixed and never forward caller-supplied URLs or paths', async () => {
  const h = harness(), bridge = h.window.ttsDesktop;
  for (const [method, command] of [['getUpdateState', 'get_update_state'], ['checkUpdate', 'check_update'],
    ['downloadUpdate', 'download_update'], ['installUpdate', 'install_update']]) {
    await bridge[method]({url: 'https://example.com/unsigned.exe', path: '/private'});
    assert.equal(h.calls.at(-1).command, command);
    assert.equal(h.calls.at(-1).args, undefined);
  }
  assert.equal(bridge.update, undefined);
});

test('updater events expose only display fields and support early disposal', async () => {
  const h = harness(); let view;
  const stop = h.window.ttsDesktop.onUpdateState(value => { view = value; });
  h.update({status: 'downloading', current_version: '0.6.0', version: '0.7.0', notes: '<b>plain text</b>',
    downloaded_bytes: 1024, total_bytes: 4096, distribution: 'portable',
    url: 'https://example.com/unsigned.exe', path: '/private', secret: 'not forwarded'});
  assert.equal(view.status, 'downloading'); assert.equal(view.downloaded_bytes, 1024);
  assert.equal(view.notes, '<b>plain text</b>'); assert.equal(view.url, undefined);
  assert.equal(view.path, undefined); assert.equal(view.secret, undefined);
  h.update({status: 'error', error: '请等待模型安装完成', downloaded_bytes: -1, total_bytes: NaN});
  assert.equal(view.error, '请等待模型安装完成'); assert.equal(view.downloaded_bytes, 0); assert.equal(view.total_bytes, 0);
  stop(); await Promise.resolve();
  assert.equal(h.stops(), 1);
  h.update({status: 'installing'});
  assert.equal(view.status, 'error');
});

test('native downloads pass original export routes to Save As without navigating', async () => {
  const h = harness();
  for (const route of [`/api/runs/${id}/audio`, `/api/batches/${id}/download`, '/api/export.csv']) {
    assert.ok(h.click(route));
    assert.equal(h.calls.at(-1).command, 'save_export');
    assert.equal(h.calls.at(-1).args.route, route);
  }
  const count = h.calls.length;
  for (const route of ['/api/state', `/api/runs/${id}/audio?path=C:/private`, '/api/runs/../secret/audio']) assert.ok(h.click(route));
  assert.equal(h.calls.length, count);
  assert.equal(h.errors.length, 3);
  assert.equal(h.click('#main'), false);
});

test('documentation opens through native confirmation and rejects non-HTTPS or credential-bearing links', async () => {
  const h = harness();
  assert.ok(h.click('https://platform.openai.com/docs'));
  assert.equal(h.calls[0].command, 'open_documentation');
  assert.equal(h.calls[0].args.url, 'https://platform.openai.com/docs');
  for (const href of ['http://example.com', 'file:///secret', 'javascript:alert(1)', 'https://key@example.com/']) assert.ok(h.click(href));
  assert.equal(h.calls.length, 1);
  assert.equal(h.errors.length, 4);
});

test('native operation failures show a generic message without disclosing backend errors', async () => {
  const h = harness(); h.reject('save_export');
  h.click('/api/export.csv');
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(h.errors, ['桌面操作未完成，请稍后重试。']);
});

test('titlebar dragging never consumes button clicks or non-left mouse input', async () => {
  const h = harness(); let prevented = 0;
  const move = (button, detail, interactive = false) => h.listeners.get('mousedown')({button, detail,
    target: {closest: selector => selector === '.titlebar' ? {} : interactive ? {} : null},
    preventDefault: () => { prevented++; }});
  move(0, 1); move(0, 2); move(0, 1, true); move(2, 1);
  assert.equal(h.calls.length, 2); assert.equal(prevented, 2);
  assert.equal(h.calls[0].args.action, 'startDragging');
  assert.equal(h.calls[1].args.action, 'toggleMaximize');
});
