"""Frontend transport integration with a mocked Tauri bridge (not a native GUI test).

Run a disposable web-mode backend on :8765 first. The Windows custom-protocol
origin is intercepted here; the native Rust proxy and Save As dialog are tested
separately. No cloud services or user credentials are used.
"""
import io
import json
import os
from pathlib import Path
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
    context = browser.new_context(viewport={'width': 1360, 'height': 960})
    page = context.new_page()
    errors, requests = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    page.add_init_script("""window.__TTS_API_ORIGIN__ = 'http://tts-api.localhost';
      window.__NATIVE_CALLS__ = [];
      window.__TAURI__ = {
        core: {invoke: async (command, args) => {
          window.__NATIVE_CALLS__.push([command, args]);
          if (command === 'desktop_action' && args.action === 'getWindowState') return {maximized: false};
        }},
        event: {listen: async () => () => {}}
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
        route.fulfill(response=response, headers={**response.headers, **cors})

    context.route(backend + '/', document_route)
    context.route(api_origin + '/**', protocol_route)
    page.goto(backend)
    page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
    assert page.locator('#window-controls').is_visible()
    page.click('[data-page=models]')
    assert page.locator('#desktop-settings').is_visible()
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
    page.wait_for_function("() => window.__NATIVE_CALLS__.some(call => call[1].action === 'startDragging')")
    calls = page.evaluate('window.__NATIVE_CALLS__')
    assert any(command == 'desktop_action' and args['action'] == 'startDragging' for command, args in calls)
    assert not errors, errors
    page.screenshot(path=str(output / 'tts-tauri-frontend.png'), full_page=True)
    print(json.dumps({'status': 'passed', 'native_bridge': 'mocked',
                      'api_requests': len(requests), 'native_calls': calls,
                      'checks': ['private API URLs', 'raw reference WAV upload', 'audio playback',
                                 'WAV/ZIP/CSV native exports', 'documentation', 'window controls', 'titlebar drag']},
                     ensure_ascii=False))
    browser.close()
