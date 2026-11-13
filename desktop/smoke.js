// Injected only by --smoke-test into a fresh, credential-free native profile.
window.addEventListener('DOMContentLoaded', async () => {
  const checks = [];
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const wait = async (predicate, message) => {
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      if (await predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error(message);
  };
  const api = async route => {
    const response = await fetch(window.ttsDesktop.apiUrl(route));
    assert(response.ok, 'Native API request failed'); return response;
  };
  try {
    await wait(() => document.querySelectorAll('.variant').length === 2 &&
      !document.querySelector('#test-variant').disabled, 'App initialization timed out');
    assert(typeof require === 'undefined' && typeof process === 'undefined', 'Unexpected Node access');
    assert(!document.querySelector('#window-controls').hidden, 'Desktop controls missing');
    const state = await (await api('/api/state')).json();
    assert(state.desktop && state.providers.filter(p => p.family !== 'demo').every(p => !p.ready), 'Profile must be isolated');
    checks.push('bundled frontend', 'private native API proxy', 'isolated profile');
    await window.ttsDesktop.toggleMaximize();
    await wait(async () => (await window.ttsDesktop.getWindowState()).maximized, 'Maximize failed');
    await window.ttsDesktop.toggleMaximize();
    checks.push('native window controls');
    document.querySelector('#test-generate').click();
    await wait(() => document.querySelectorAll('#test-results-slot audio').length === 1 &&
      !document.querySelector('#test-generate').disabled, 'Single generation failed');
    document.querySelector('[data-page=compare]').click();
    document.querySelector('#compare-generate').click();
    await wait(() => document.querySelectorAll('#compare-results-slot audio').length === 2 &&
      !document.querySelector('#compare-generate').disabled, 'Comparison failed');
    const audio = document.querySelector('#compare-results-slot audio');
    audio.muted = true;
    await wait(() => audio.readyState >= 1 && audio.duration === 1.5, 'Audio protocol metadata failed');
    await audio.play();
    await wait(() => audio.currentTime > 0, 'Muted audio playback failed'); audio.pause();
    const runs = (await (await api('/api/runs')).json()).runs;
    assert(runs.length === 3 && runs.every(r => r.provider === 'demo' && r.status === 'completed'), 'Offline history mismatch');
    checks.push('single scheme generation', 'two scheme comparison', 'audio metadata and muted playback', 'persistent history');
    const invoke = window.__TAURI__.core.invoke;
    const batchPath = document.querySelector('#compare-results-slot .download-batch').getAttribute('href');
    assert(await invoke('save_export', {route: batchPath}), 'ZIP export failed');
    assert(await invoke('save_export', {route: '/api/export.csv'}), 'CSV export failed');
    assert(await invoke('save_export', {route: `/api/runs/${runs[0].id}/audio`}), 'Audio export failed');
    for (const [command, args] of [['desktop_action', {action: 'execute'}], ['save_export', {route: 'https://example.com'}]]) {
      let rejected = false; try { await invoke(command, args); } catch { rejected = true; }
      assert(rejected, 'Native boundary accepted invalid request');
    }
    document.querySelector('[data-page=models]').click();
    await wait(() => document.querySelectorAll('.model-card').length === 6, 'Model page failed');
    checks.push('native ZIP CSV and audio exports', 'native command allowlists', 'model configuration page');
    await invoke('smoke_report', {report: {status: 'passed', checks, cloudRequests: 0}});
  } catch (error) {
    await window.__TAURI__.core.invoke('smoke_report', {report: {status: 'failed', error: String(error.message).slice(0, 300), checks}});
  }
});
