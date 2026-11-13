'use strict';
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { Menu } = require('electron');

async function runSmoke({ window, origin, token, profile, downloads }) {
  fs.mkdirSync(path.join(profile, 'exports'), { recursive: true });
  const evaluate = code => window.webContents.executeJavaScript(code, true);
  async function waitFor(expression, timeout = 20000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (await evaluate(expression)) return;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('Timed out waiting for desktop UI: ' + expression);
  }
  async function api(route) {
    const response = await fetch(origin + route, { headers: { 'X-Desktop-Token': token }, signal: AbortSignal.timeout(5000) });
    assert.equal(response.status, 200, route); return response;
  }
  window.webContents.setAudioMuted(true);
  await waitFor("document.querySelectorAll('.variant').length === 2 && !document.querySelector('#test-variant').disabled");
  assert.equal(Menu.getApplicationMenu(), null);
  assert.equal(await evaluate("typeof require === 'undefined' && typeof process === 'undefined'"), true);
  assert.equal(await evaluate("!document.querySelector('#window-controls').hidden && typeof window.ttsDesktop.close === 'function'"), true);
  assert.equal(await evaluate("Array.from(document.querySelectorAll('.provider')).every(x => x.value === 'demo')"), true);
  assert.equal((await fetch(origin + '/api/state')).status, 403);
  // Exercise the custom titlebar through the isolated preload bridge.
  window.show();
  await evaluate("document.querySelector('#window-maximize').click()");
  await waitFor("document.querySelector('#window-maximize').getAttribute('aria-label') === '还原'");
  assert.equal(window.isMaximized(), true);
  await evaluate("document.querySelector('#window-maximize').click()");
  await waitFor("document.querySelector('#window-maximize').getAttribute('aria-label') === '最大化'");
  await evaluate("document.querySelector('#window-minimize').click()");
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.equal(window.isMinimized(), true);
  window.restore();
  // Text test submits exactly one saved scheme, including its edited parameters.
  await evaluate("document.querySelector('[data-page=compare]').click(); document.querySelectorAll('.variant-name')[1].value='高频测试'; document.querySelectorAll('.variant-name')[1].dispatchEvent(new Event('input')); document.querySelector('#save-variants').click()");
  await waitFor("document.querySelector('#save-state').textContent === '已保存到本机' && !document.querySelector('#test-variant').disabled");
  await evaluate("document.querySelector('[data-page=test]').click(); const select=document.querySelector('#test-variant'); select.selectedIndex=1; select.dispatchEvent(new Event('change'))");
  await waitFor("!document.querySelector('#test-variant').disabled");
  await evaluate("document.querySelector('#test-generate').click()");
  await waitFor("document.querySelectorAll('#test-results-slot audio').length === 1 && !document.querySelector('#test-generate').disabled");
  const firstHistory = await (await api('/api/runs')).json();
  assert.equal(firstHistory.runs.length, 1); assert.equal(firstHistory.runs[0].params.frequency, 660);
  await evaluate("document.querySelector('[data-page=compare]').click(); document.querySelector('#compare-generate').click()");
  await waitFor("document.querySelectorAll('#compare-results-slot audio').length === 2 && !document.querySelector('#compare-generate').disabled");
  await waitFor("Array.from(document.querySelectorAll('audio')).every(x => x.readyState >= 1 && x.duration === 1.5)");
  await evaluate("document.querySelector('#compare-results-slot audio').play()");
  await waitFor("document.querySelector('#compare-results-slot audio').currentTime > 0");
  await evaluate("document.querySelector('#compare-results-slot audio').pause(); document.querySelector('#compare-results-slot .blind').click()");
  assert.equal(await evaluate("document.querySelectorAll('#compare-results-slot .download').length"), 2);
  await evaluate("document.querySelector('#page-compare .history-button').click()");
  await waitFor("document.querySelectorAll('.history-row').length === 2");
  await evaluate("document.querySelector('#close-history').click(); document.querySelector('#add-variant').click()");
  assert.equal(await evaluate("document.querySelector('#history-panel').hidden"), true);
  assert.equal(await evaluate("document.querySelectorAll('.variant').length"), 3);
  await evaluate("Array.from(document.querySelectorAll('.remove-variant')).at(-1).click()");
  assert.equal(await evaluate("document.querySelectorAll('.variant').length"), 2);
  await evaluate("document.querySelector('[data-page=models]').click()");
  await waitFor("document.querySelectorAll('.model-card').length === 4");
  assert.equal(await evaluate("!document.querySelector('#desktop-settings').hidden"), true);
  await evaluate("document.querySelector('[data-page=compare]').click()");
  const { runs } = await (await api('/api/runs')).json();
  assert.equal(runs.length, 3);
  assert.ok(runs.every(run => run.provider === 'demo' && run.status === 'completed'));
  const audio = Buffer.from(await (await api(`/api/runs/${runs[0].id}/audio`)).arrayBuffer());
  assert.equal(audio.toString('ascii', 0, 4), 'RIFF');
  assert.match(await (await api('/api/export.csv')).text(), /demo/);
  await evaluate("document.querySelector('#compare-results-slot .download-batch').click()");
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline && !downloads.some(item => item.state === 'completed')) await new Promise(resolve => setTimeout(resolve, 100));
  const download = downloads.find(item => item.state === 'completed' && item.file.endsWith('.zip'));
  assert.ok(download, 'Native ZIP download completed');
  const savedZip = fs.readFileSync(download.file);
  assert.ok(savedZip.length > 100); assert.equal(savedZip.readUInt32LE(), 0x04034b50);
  assert.ok(savedZip.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06])) >= 0, 'ZIP central directory completed');
  await evaluate("document.querySelector('#compare-text').value = ''; document.querySelector('#compare-generate').click()");
  assert.match(await evaluate("document.querySelector('#notice').textContent"), /输入/);
  const saved = await (await api('/api/variants')).json(); assert.equal(saved.variants[1].name, '高频测试');
  // Reopening the same backend restores the saved scheme, not the unsaved draft.
  await new Promise(resolve => { window.webContents.once('did-finish-load', resolve); window.reload(); });
  await waitFor("!document.querySelector('#test-variant').disabled && document.querySelector('#test-variant').selectedOptions[0].textContent === '高频测试'");
  const image = await window.webContents.capturePage();
  fs.writeFileSync(path.join(profile, 'desktop-smoke.png'), image.toPNG());
  // The frameless window must remain operable even if saved configuration is corrupt.
  const variantsPath = path.join(profile, 'data', 'variants.json');
  const originalVariants = fs.readFileSync(variantsPath);
  try {
    fs.writeFileSync(variantsPath, '{broken');
    await new Promise(resolve => { window.webContents.once('did-finish-load', resolve); window.reload(); });
    await waitFor("document.querySelector('#notice').textContent.includes('variants.json')");
    assert.equal(await evaluate("!document.querySelector('#window-controls').hidden && !document.querySelector('#desktop-settings').hidden"), true);
    assert.equal(await evaluate("(async () => typeof (await window.ttsDesktop.getWindowState()).maximized)()"), 'boolean');
  } finally { fs.writeFileSync(variantsPath, originalVariants); }
  return { status: 'passed', platform: process.platform, arch: process.arch, electron: process.versions.electron,
    checks: ['frameless titlebar maximize/restore/minimize', 'no menu', 'sandboxed bridge', 'session authentication',
      'window controls survive configuration errors', 'three pages', 'saved scheme reload', 'single-scheme generation', 'two-scheme comparison', 'muted audio playback',
      'blind reveal', 'history', 'add/remove scheme', 'native ZIP download', 'CSV export', 'empty input'], cloudRequests: 0 };
}
module.exports = { runSmoke };
