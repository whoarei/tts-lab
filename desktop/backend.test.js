'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { startBackend } = require('./backend');
function fakeChild() {
  const child = new EventEmitter();
  child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
  child.kill = () => { child.emit('exit', 1); return true; };
  return child;
}
const base = { python: 'python', script: '/app/run.py', config: '/profile/credentials.env',
  dataDirectory: '/profile/data', token: 'x'.repeat(64), env: {} };

test('launcher validates split readiness and stops via stdin without shell', async () => {
  const child = fakeChild(); let options;
  const backend = startBackend({ ...base, spawnProcess: (_command, _args, opts) => { options = opts; return child; } });
  child.stdout.write('{"event":"ready","host":"127.0.0.1",');
  child.stdout.write('"port":54321}\n');
  assert.equal(await backend.ready, 'http://127.0.0.1:54321');
  assert.equal(options.shell, false);
  assert.equal(options.env.TTS_LAB_DESKTOP_TOKEN, base.token);
  child.stdin.on('data', chunk => { assert.equal(chunk.toString(), 'shutdown\n'); child.emit('exit', 0); });
  await backend.stop(); await backend.stop();
});

test('launcher rejects process failure without disclosing stderr', async () => {
  const child = fakeChild();
  const backend = startBackend({ ...base, spawnProcess: () => child });
  child.stderr.write('a-private-key-that-must-not-appear'); child.emit('exit', 1);
  await assert.rejects(backend.ready, error => !error.message.includes('a-private-key'));
  await backend.stop();
});

test('launcher rejects wrong-host readiness and times out', async () => {
  const child = fakeChild();
  const backend = startBackend({ ...base, spawnProcess: () => child, timeout: 20 });
  child.stdout.write('{"event":"ready","host":"0.0.0.0","port":12345}\n');
  await assert.rejects(backend.ready, /超时/); await backend.stop();
});

test('actual Python service launch, desktop auth, offline WAV and persistence', async () => {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'tts-node-test-'));
  const dataDirectory = path.join(profile, 'data'); fs.mkdirSync(dataDirectory);
  const config = path.join(profile, 'credentials.env'); fs.writeFileSync(config, '');
  const backend = startBackend({ ...base, python: process.env.TTS_LAB_PYTHON || 'python3',
    script: path.resolve(__dirname, '..', 'run.py'), config, dataDirectory, env: process.env });
  try {
    const origin = await backend.ready;
    const headers = { 'X-Desktop-Token': base.token };
    assert.equal((await fetch(origin + '/api/state')).status, 403);
    const state = await (await fetch(origin + '/api/state', { headers })).json();
    assert.ok(state.providers.some(item => item.id === 'demo'));
    assert.ok(state.providers.filter(item => item.family !== 'demo').every(item => !item.ready),
      'blank desktop profile must select offline demos even when boto3 is bundled');
    const response = await fetch(origin + '/api/batches', { method: 'POST',
      headers: { ...headers, 'X-Local-Token': state.token, 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: 'offline desktop test', variants: [{ provider: 'demo' }] }) });
    assert.equal(response.status, 202);
    const batch = await response.json(); const id = batch.runs[0].id;
    let run;
    for (let i = 0; i < 50; i++) {
      run = await (await fetch(origin + '/api/runs/' + id, { headers })).json();
      if (run.status === 'completed') break;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    assert.equal(run.status, 'completed');
    const audio = Buffer.from(await (await fetch(origin + '/api/runs/' + id + '/audio', { headers })).arrayBuffer());
    assert.equal(audio.toString('ascii', 0, 4), 'RIFF');
    assert.equal(fs.existsSync(path.join(dataDirectory, id, 'run.json')), true);
    await backend.stop();
    assert.equal(fs.existsSync(path.join(dataDirectory, id, 'audio.wav')), true);
  } finally { await backend.stop(); fs.rmSync(profile, { recursive: true, force: true }); }
});
