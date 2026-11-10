'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { isLocalUrl, isExternalDocumentation, requestHeaders, profileDirectory } = require('./security');
const origin = 'http://127.0.0.1:12345';

test('only the exact ephemeral localhost origin is trusted', () => {
  for (const url of [origin, origin + '/api/state', origin + '/#guide']) assert.equal(isLocalUrl(url, origin), true);
  for (const url of ['http://localhost:12345', 'http://127.0.0.1:12346', 'https://127.0.0.1:12345',
    'http://127.0.0.1.evil:12345', 'http://user:pass@127.0.0.1:12345', 'file:///etc/passwd',
    'javascript:alert(1)', 'https://example.com', 'invalid']) assert.equal(isLocalUrl(url, origin), false, url);
});

test('desktop secret is injected only locally and stripped from other requests', () => {
  assert.deepEqual(requestHeaders(origin + '/api/runs', origin, 'test-secret', { Accept: '*/*' }),
    { Accept: '*/*', 'X-Desktop-Token': 'test-secret' });
  assert.deepEqual(requestHeaders('https://example.com', origin, 'test-secret',
    { 'x-desktop-token': 'test-secret', 'X-Desktop-Token': 'test-secret', Accept: '*/*' }), { Accept: '*/*' });
});

test('external docs require plain https and no credentials', () => {
  assert.equal(isExternalDocumentation('https://platform.openai.com/docs'), true);
  for (const url of ['http://example.com', 'file:///C:/x', 'javascript:alert(1)', 'https://secret@example.com']) {
    assert.equal(isExternalDocumentation(url), false);
  }
});

test('persistent profile does not depend on installation directory', () => {
  assert.equal(profileDirectory('win32', '/users/me/local', '/fallback'), '/users/me/local/TTS Lab');
  assert.equal(profileDirectory('linux', null, '/home/me/.config'), '/home/me/.config/TTS Lab');
});
