'use strict';
const path = require('node:path');

function isLocalUrl(candidate, origin) {
  try {
    const parsed = new URL(candidate);
    return parsed.origin === origin && parsed.protocol === 'http:' &&
      parsed.hostname === '127.0.0.1' && !parsed.username && !parsed.password;
  } catch { return false; }
}

function isExternalDocumentation(candidate) {
  try {
    const url = new URL(candidate);
    return url.protocol === 'https:' && !url.username && !url.password;
  } catch { return false; }
}

function requestHeaders(candidate, origin, token, headers = {}) {
  const clean = Object.fromEntries(Object.entries(headers).filter(([key]) =>
    key.toLowerCase() !== 'x-desktop-token'));
  if (isLocalUrl(candidate, origin)) clean['X-Desktop-Token'] = token;
  return clean;
}

function profileDirectory(platform, localAppData, fallback) {
  return platform === 'win32' && localAppData
    ? path.join(localAppData, 'TTS Lab') : path.join(fallback, 'TTS Lab');
}

module.exports = { isLocalUrl, isExternalDocumentation, requestHeaders, profileDirectory };
