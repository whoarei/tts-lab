'use strict';
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

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
    const response = await fetch(origin + route, { headers: { 'X-Desktop-Token': token },
      signal: AbortSignal.timeout(5000) });
    assert.equal(response.status, 200, route);
    return response;
  }
  window.webContents.setAudioMuted(true);
  await waitFor("document.querySelectorAll('.variant').length === 2");
  assert.equal(await evaluate("typeof require === 'undefined' && typeof process === 'undefined'"), true);
  assert.equal(await evaluate("Array.from(document.querySelectorAll('.provider')).every(x => x.value === 'demo')"), true);
  assert.equal((await fetch(origin + '/api/state')).status, 403);
  await evaluate("document.querySelector('#generate').click()");
  await waitFor("document.querySelectorAll('audio').length === 2 && !document.querySelector('#generate').disabled");
  await waitFor("Array.from(document.querySelectorAll('audio')).every(x => x.readyState >= 1 && x.duration === 1.5)");
  await evaluate("document.querySelector('audio').play()");
  await waitFor("document.querySelector('audio').currentTime > 0");
  await evaluate("document.querySelector('audio').pause(); document.querySelector('#blind').click()");
  assert.equal(await evaluate("document.querySelectorAll('.download').length"), 2);
  await evaluate("document.querySelector('#refresh-history').click()");
  await waitFor("document.querySelectorAll('.history-row').length === 1");
  await evaluate("document.querySelector('#close-history').click(); document.querySelector('#add-variant').click()");
  assert.equal(await evaluate("document.querySelector('#history-panel').hidden"), true);
  assert.equal(await evaluate("document.querySelectorAll('.variant').length"), 3);
  await evaluate("Array.from(document.querySelectorAll('.variant .icon-button')).at(-1).click()");
  assert.equal(await evaluate("document.querySelectorAll('.variant').length"), 2);
  const { runs } = await (await api('/api/runs')).json();
  assert.equal(runs.length, 2);
  assert.ok(runs.every(run => run.provider === 'demo' && run.status === 'completed'));
  const audio = Buffer.from(await (await api(`/api/runs/${runs[0].id}/audio`)).arrayBuffer());
  assert.equal(audio.toString('ascii', 0, 4), 'RIFF');
  const archive = Buffer.from(await (await api(`/api/batches/${runs[0].batch_id}/download`)).arrayBuffer());
  assert.equal(archive.readUInt32LE(), 0x04034b50);
  assert.match(await (await api('/api/export.csv')).text(), /demo/);
  await evaluate("document.querySelector('#download-batch').click()");
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline && !downloads.some(item => item.state === 'completed')) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const download = downloads.find(item => item.state === 'completed' && item.file.endsWith('.zip'));
  assert.ok(download, 'Native ZIP download completed');
  const savedZip = fs.readFileSync(download.file);
  assert.ok(savedZip.length > 100, 'Saved ZIP is nonempty');
  assert.equal(savedZip.readUInt32LE(), 0x04034b50);
  assert.ok(savedZip.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06])) >= 0, 'ZIP central directory completed');
  await evaluate("document.querySelector('#text').value = ''; document.querySelector('#generate').click()");
  assert.match(await evaluate("document.querySelector('#notice').textContent"), /输入/);
  const image = await window.webContents.capturePage();
  fs.writeFileSync(path.join(profile, 'desktop-smoke.png'), image.toPNG());
  return { status: 'passed', platform: process.platform, arch: process.arch,
    electron: process.versions.electron,
    checks: ['sandboxed UI', 'session authentication', 'two offline WAV files', 'muted audio playback',
      'blind reveal', 'history open/close', 'add/remove variant', 'native ZIP download', 'CSV export', 'empty input'],
    cloudRequests: 0 };
}
module.exports = { runSmoke };
