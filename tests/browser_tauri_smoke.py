"""Frontend transport integration with a mocked Tauri bridge (not a native GUI test).

Run a disposable web-mode backend on :8765 first. The Windows custom-protocol
origin is intercepted here; the native Rust proxy and Save As dialog are tested
separately. No cloud services or user credentials are used.
"""
import io
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import wave

from playwright.sync_api import sync_playwright

backend = 'http://127.0.0.1:8765'
api_origin = 'http://tts-api.localhost'
output = Path(os.environ.get('TTS_LAB_SCREENSHOTS', '/tmp'))
output.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    launch = {'headless': True}
    if os.getenv('CHROMIUM_PATH'):
        launch['executable_path'] = os.environ['CHROMIUM_PATH']
    browser = p.chromium.launch(**launch)
    ordinary = browser.new_page()
    ordinary.goto(backend)
    ordinary.wait_for_function("() => !document.querySelector('#test-variant').disabled")
    assert ordinary.locator('#app-update-panel').is_hidden()
    assert ordinary.locator('#updates-shortcut').is_hidden()
    assert ordinary.evaluate('window.ttsDesktop === undefined')
    ordinary.close()
    context = browser.new_context(viewport={'width': 1360, 'height': 960})
    page = context.new_page()
    errors, requests = [], []
    hold_generations = False
    page.on('pageerror', lambda error: errors.append(str(error)))

    page.add_init_script("""window.__TTS_API_ORIGIN__ = 'http://tts-api.localhost';
      window.__NATIVE_CALLS__ = [];
      window.__NATIVE_EVENTS__ = {};
      window.__UPDATE_VIEW__ = {status: 'idle', current_version: '0.6.0', downloaded_bytes: 0, total_bytes: 0, distribution: 'portable'};
      window.__EMIT_UPDATE__ = (patch) => {
        window.__UPDATE_VIEW__ = {...window.__UPDATE_VIEW__, ...patch};
        window.__NATIVE_EVENTS__['desktop:update-state']?.({payload: {...window.__UPDATE_VIEW__}});
        return {...window.__UPDATE_VIEW__};
      };
      const auto = localStorage.getItem('test.updates.autoEnabled');
      if (auto !== null) window.__TTS_UPDATES_AUTO__ = auto === '1';
      window.__TAURI__ = {
        core: {invoke: async (command, args) => {
          window.__NATIVE_CALLS__.push([command, args]);
          if (command === 'desktop_action' && args.action === 'getWindowState') return {maximized: false};
          if (command === 'get_update_state') return {...window.__UPDATE_VIEW__};
          if (command === 'check_update') {
            if (window.__CHECK_CURRENT__) return window.__EMIT_UPDATE__({status: 'current', version: '', notes: '', error: ''});
            if (window.__CHECK_ERROR__) throw '网络连接失败，请稍后重试。';
            return window.__EMIT_UPDATE__({status: 'available', version: '0.7.0', notes: '<img src=x onerror=alert(1)>\\n新增在线更新', error: '', downloaded_bytes: 0, total_bytes: 4096});
          }
          if (command === 'download_update') {
            window.__EMIT_UPDATE__({status: 'downloading', downloaded_bytes: 128, total_bytes: 4096, error: ''});
            return new Promise(resolve => { window.__FINISH_UPDATE_DOWNLOAD__ = () => resolve(window.__EMIT_UPDATE__({status: 'ready', downloaded_bytes: 4096, total_bytes: 4096})); });
          }
          if (command === 'install_update') {
            if (window.__INSTALL_BLOCKED__) return new Promise((_resolve, reject) => { window.__FAIL_INSTALL__ = () => reject('请等待模型安装完成后重试。'); });
            return window.__EMIT_UPDATE__({status: 'installing'});
          }
        }},
        event: {listen: async (name, callback) => { window.__NATIVE_EVENTS__[name] = callback; return () => { delete window.__NATIVE_EVENTS__[name]; }; }}
      };""")

    def document_route(route):
        response = route.fetch()
        headers = dict(response.headers)
        headers['content-security-policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self' http://tts-api.localhost; "
            "media-src 'self' http://tts-api.localhost; img-src 'self' data:")
        route.fulfill(response=response, headers=headers)

    def protocol_route(route):
        request = route.request
        parsed = urlsplit(request.url)
        requests.append((request.method, parsed.path, request.post_data_buffer))
        cors = {'access-control-allow-origin': backend,
                'access-control-allow-headers': 'Content-Type, X-Local-Token, Range',
                'access-control-allow-methods': 'GET, POST, PUT, DELETE, OPTIONS'}
        if request.method == 'OPTIONS':
            route.fulfill(status=204, headers=cors)
            return
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() not in {'host', 'origin', 'sec-fetch-site'}}
        response = route.fetch(url=backend + parsed.path + ('?' + parsed.query if parsed.query else ''),
                               headers=headers)
        if hold_generations and request.method == 'GET' and re.fullmatch(r'/api/runs/[a-f0-9]{32}', parsed.path):
            data = response.json(); data['status'] = 'running'
            response_headers = {key: value for key, value in response.headers.items()
                                if key.lower() not in {'content-length', 'content-encoding'}}
            route.fulfill(status=response.status, body=json.dumps(data), headers={**response_headers, **cors})
            return
        route.fulfill(response=response, headers={**response.headers, **cors})

    context.route(backend + '/', document_route)
    context.route(api_origin + '/**', protocol_route)
    page.goto(backend)
    page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
    assert page.locator('#window-controls').is_visible()
    page.click('[data-page=models]')
    assert page.locator('#desktop-settings').is_visible()
    assert page.locator('#app-update-panel').is_visible()
    assert page.locator('#app-update-auto').is_checked()
    assert '尚未检查' in page.locator('#app-update-status').inner_text()
    assert not page.evaluate("window.__NATIVE_CALLS__.some(call => call[0] === 'check_update')")
    page.click('#edit-credentials')
    page.click('#open-data-directory')
    page.click('[data-page=compare]')
    first = page.locator('.variant').first
    first.locator('.provider').select_option('local_indextts')
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(24000)
        writer.writeframes(b'\0\1' * 72000)
    first.get_by_label('导入参考录音', exact=True).set_input_files({
        'name': 'tauri-reference.wav', 'mimeType': 'audio/wav', 'buffer': buffer.getvalue()})
    page.wait_for_function("() => document.querySelector('.variant [data-param=reference_id]').value.length === 32")
    assert first.locator('audio').get_attribute('src').startswith(api_origin + '/api/references/')
    assert any(method == 'POST' and path == '/api/references' and body == buffer.getvalue()
               for method, path, body in requests)
    first.locator('.provider').select_option('demo')
    for variant in page.locator('.variant').all():
        variant.locator('.provider').select_option('demo')
    page.click('#save-variants')
    page.wait_for_function("() => document.querySelector('#save-state').textContent === '已保存到本机'")
    page.click('[data-page=test]')
    page.fill('#text', 'Tauri 桌面前端测试，只生成离线提示音。')
    page.click('#test-generate')
    page.wait_for_function("() => document.querySelectorAll('#test-results-slot audio').length === 1 && !document.querySelector('#test-generate').disabled")
    audio = page.locator('#test-results-slot audio')
    assert audio.get_attribute('src').startswith(api_origin + '/api/runs/')
    audio.evaluate('(node) => node.play()')
    page.wait_for_function("() => document.querySelector('#test-results-slot audio').currentTime > 0")
    audio.evaluate('(node) => node.pause()')
    original_url = page.url
    page.click('#test-results-slot .download')
    page.click('#test-results-slot .download-batch')
    page.click('#test-results-slot a[href="/api/export.csv"]')
    page.wait_for_function("() => window.__NATIVE_CALLS__.filter(call => call[0] === 'save_export').length === 3")
    assert page.url == original_url
    page.click('[data-page=compare]')
    first.locator('.provider').select_option('openai')
    first.locator('.doc-link').click()
    page.wait_for_function("() => window.__NATIVE_CALLS__.some(call => call[0] === 'open_documentation')")
    calls = page.evaluate('window.__NATIVE_CALLS__')
    assert any(command == 'open_documentation' and args['url'].startswith('https://') for command, args in calls)
    assert page.url == original_url
    page.locator('.app-caption').dispatch_event('mousedown', {'button': 0, 'detail': 1})
    page.wait_for_function("() => window.__NATIVE_CALLS__.some(call => call[1]?.action === 'startDragging')")
    calls = page.evaluate('window.__NATIVE_CALLS__')
    assert any(command == 'desktop_action' and args['action'] == 'startDragging' for command, args in calls)
    # Update checks and downloads are separate, and release notes remain text.
    page.click('#updates-shortcut')
    page.click('#check-app-update')
    page.wait_for_function("() => document.querySelector('#app-update-status').textContent.includes('发现新版本')")
    assert 'v0.7.0' in page.locator('#app-update-status').inner_text()
    assert '<img src=x onerror=alert(1)>' in page.locator('#app-update-notes').text_content()
    assert page.locator('#app-update-notes img').count() == 0
    assert not page.evaluate("window.__NATIVE_CALLS__.some(call => ['download_update', 'install_update'].includes(call[0]))")
    page.click('#download-app-update')
    page.wait_for_function("() => document.querySelector('#app-update-progress').value === 128")
    assert page.locator('#check-app-update').is_disabled()
    assert not page.locator('#test-generate').is_disabled()
    assert page.locator('#install-app-update').is_hidden()
    page.evaluate("window.__EMIT_UPDATE__({status: 'downloading', downloaded_bytes: 2048, total_bytes: 4096})")
    assert page.locator('#app-update-progress').get_attribute('value') == '2048'
    assert page.locator('#app-update-bytes').inner_text() == '2.0 KiB / 4.0 KiB'
    page.evaluate('window.__FINISH_UPDATE_DOWNLOAD__()')
    page.wait_for_function("() => !document.querySelector('#install-app-update').disabled")
    assert '关闭应用' in page.locator('#app-update-install-hint').inner_text()
    page.evaluate("window.__EMIT_UPDATE__({distribution: 'installed'})")
    assert '安装版' in page.locator('#app-update-version').inner_text()
    assert page.locator('#install-app-update').is_visible()
    page.evaluate("window.__EMIT_UPDATE__({distribution: 'portable'})")
    assert '便携版' in page.locator('#app-update-version').inner_text()
    page.locator('#app-update-notes-wrap').evaluate('(node) => { node.open = true; }')
    page.locator('#app-update-panel').screenshot(path=str(output / 'tts-tauri-update-panel.png'))
    assert not page.evaluate("window.__NATIVE_CALLS__.some(call => call[0] === 'install_update')")
    # Native state can become ready while generation is active. Even a scripted
    # click must not dispatch install until the active work finishes.
    page.click('[data-page=test]')
    hold_generations = True
    page.click('#test-generate')
    page.click('[data-page=models]')
    assert page.locator('#install-app-update').is_disabled()
    assert '请等待' in page.locator('#app-update-install-hint').inner_text()
    page.locator('#install-app-update').dispatch_event('click')
    assert not page.evaluate("window.__NATIVE_CALLS__.some(call => call[0] === 'install_update')")
    hold_generations = False
    page.wait_for_function("() => !document.querySelector('#test-generate').disabled && !document.querySelector('#install-app-update').disabled")
    page.evaluate("window.__EMIT_UPDATE__({status: 'error', error: '下载中断，请重试。'})")
    assert page.locator('#download-app-update').inner_text() == '重试下载'
    assert page.locator('#install-app-update').is_hidden()
    page.click('#download-app-update')
    page.wait_for_function("() => document.querySelector('#app-update-progress').value === 128")
    page.evaluate('window.__FINISH_UPDATE_DOWNLOAD__()')
    page.wait_for_function("() => !document.querySelector('#install-app-update').disabled")
    page.evaluate('window.__CHECK_CURRENT__ = true')
    page.click('#check-app-update')
    page.wait_for_function("() => document.querySelector('#app-update-status').textContent.includes('最新版本')")
    assert page.locator('#download-app-update').is_hidden() and page.locator('#install-app-update').is_hidden()
    page.evaluate('window.__CHECK_CURRENT__ = false; window.__CHECK_ERROR__ = true')
    page.click('#check-app-update')
    page.wait_for_function("() => document.querySelector('#check-app-update').textContent === '重试检查'")
    assert '网络连接失败' in page.locator('#app-update-status').inner_text()
    assert not page.evaluate("window.__NATIVE_CALLS__.some(call => call[0] === 'install_update')")
    page.evaluate('window.__CHECK_ERROR__ = false')
    page.click('#check-app-update')
    page.wait_for_function("() => document.querySelector('#app-update-status').textContent.includes('发现新版本')")
    page.evaluate("window.__EMIT_UPDATE__({status: 'available', distribution: 'development'})")
    assert page.locator('#download-app-update').is_hidden()
    page.evaluate("window.__EMIT_UPDATE__({status: 'ready', distribution: 'development'})")
    assert page.locator('#install-app-update').is_hidden()
    assert '发布页' in page.locator('#app-update-install-hint').inner_text()
    page.evaluate("window.__EMIT_UPDATE__({status: 'ready', distribution: 'portable'}); window.__INSTALL_BLOCKED__ = true")
    page.click('#install-app-update')
    page.wait_for_function("() => typeof window.__FAIL_INSTALL__ === 'function'")
    assert page.locator('#test-generate').is_disabled() and page.locator('#compare-generate').is_disabled()
    assert page.locator('#test-variant').is_disabled() and page.locator('#add-variant').is_disabled()
    assert page.locator('#save-variants').is_disabled() and page.locator('#local-device').is_disabled()
    assert page.locator('#edit-credentials').is_disabled() and page.locator('#restart-app').is_disabled()
    assert page.locator('.variant input:enabled, .variant select:enabled, .variant button:enabled').count() == 0
    assert page.locator('#local-model-list button:enabled').count() == 0
    writes_before_install = len([request for request in requests if request[0] in {'POST', 'PUT', 'DELETE'}])
    # Script-dispatched clicks bypass the browser's disabled-button filtering;
    # the action guards must still stop work while the install helper starts.
    page.locator('#test-generate').dispatch_event('click')
    page.locator('#compare-generate').dispatch_event('click')
    page.locator('#save-variants').dispatch_event('click')
    page.locator('#local-model-list button').first.dispatch_event('click')
    assert len([request for request in requests if request[0] in {'POST', 'PUT', 'DELETE'}]) == writes_before_install
    page.evaluate('window.__FAIL_INSTALL__()')
    page.wait_for_function("() => document.querySelector('#app-update-status').textContent.includes('请等待模型安装')")
    assert not page.locator('#test-generate').is_disabled() and not page.locator('#local-device').is_disabled()
    assert not page.locator('#local-model-list button').first.is_disabled()
    assert page.locator('[data-model="glm-tts"] button').first.is_disabled()
    assert not page.evaluate("window.__NATIVE_CALLS__.some(call => call[0] === 'desktop_action' && call[1]?.action === 'close')")
    page.evaluate("window.__INSTALL_BLOCKED__ = false; window.__EMIT_UPDATE__({status: 'ready'})")
    page.click('#install-app-update')
    page.wait_for_function("() => document.querySelector('#app-update-status').textContent.includes('正在退出并安装')")
    assert page.locator('#install-app-update').is_disabled()
    assert page.evaluate("window.__NATIVE_CALLS__.filter(call => call[0] === 'install_update').length") == 2
    for width in [880, 390, 320]:
        page.set_viewport_size({'width': width, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
    page.screenshot(path=str(output / 'tts-tauri-updates.png'), full_page=True)
    # Daily startup checking is gated by the native production flag and a user
    # preference. Reloading within 24 hours never repeats the automatic check.
    page.evaluate("localStorage.setItem('test.updates.autoEnabled', '0'); localStorage.removeItem('ttsLab.updates.lastCheckAt'); localStorage.removeItem('ttsLab.updates.autoCheck')")
    page.reload(); page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
    assert not page.evaluate("window.__NATIVE_CALLS__.some(call => call[0] === 'check_update')")
    page.evaluate("localStorage.setItem('test.updates.autoEnabled', '1'); localStorage.setItem('ttsLab.updates.autoCheck', '0')")
    page.reload(); page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
    assert not page.evaluate("window.__NATIVE_CALLS__.some(call => call[0] === 'check_update')")
    page.click('[data-page=models]')
    assert not page.locator('#app-update-auto').is_checked()
    page.check('#app-update-auto')
    page.reload(); page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
    page.wait_for_function("() => window.__NATIVE_CALLS__.filter(call => call[0] === 'check_update').length === 1")
    assert not page.evaluate("window.__NATIVE_CALLS__.some(call => ['download_update', 'install_update'].includes(call[0]))")
    page.reload(); page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
    assert not page.evaluate("window.__NATIVE_CALLS__.some(call => call[0] === 'check_update')")
    page.click('[data-page=models]')
    page.click('#check-app-update')
    page.wait_for_function("() => window.__NATIVE_CALLS__.filter(call => call[0] === 'check_update').length === 1")
    assert not errors, errors
    calls = page.evaluate('window.__NATIVE_CALLS__')
    page.screenshot(path=str(output / 'tts-tauri-frontend.png'), full_page=True)
    print(json.dumps({'status': 'passed', 'native_bridge': 'mocked',
                      'api_requests': len(requests), 'native_calls': calls,
                      'checks': ['private API URLs', 'raw reference WAV upload', 'audio playback',
                                 'WAV/ZIP/CSV native exports', 'documentation', 'window controls', 'titlebar drag',
                                 'update states/progress/retry', 'busy install guard', 'explicit install', 'daily startup check opt-out']},
                     ensure_ascii=False))
    browser.close()
