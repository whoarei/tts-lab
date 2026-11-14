"""Disposable browser/backend integration; synthetic credentials, no cloud calls."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

from dotenv import dotenv_values
import httpx
from playwright.sync_api import sync_playwright

# Import only the public schema; never inspect the user's credential file.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tts_lab.credentials import PROVIDER_ENV

root = Path(__file__).resolve().parents[1]
output = Path(os.environ.get('TTS_LAB_SCREENSHOTS', '/tmp'))
output.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as folder:
    config = Path(folder) / 'credentials.env'
    config.write_text('DEEPSEEK_API_KEY=previous-synthetic-key\n')
    env = {key: value for key, value in os.environ.items() if key not in PROVIDER_ENV and not key.startswith('AWS_')}
    env.update(TTS_LAB_DESKTOP='0', TTS_LAB_DATA_DIR=folder + '/data', TTS_LAB_MODELS_DIR=folder + '/models')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    with (Path(folder) / 'backend.log').open('w') as log:
        process = subprocess.Popen([sys.executable, str(root / 'run.py'), '--port', str(port), '--config', str(config)],
                                   cwd=folder, env=env, stdout=log, stderr=log)
        try:
            with httpx.Client(base_url=base, trust_env=False) as client:
                for _ in range(150):
                    try:
                        if client.get('/api/state').is_success:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(.1)
                else:
                    raise AssertionError('Disposable backend did not start')
            with sync_playwright() as playwright:
                launch = {'headless': True}
                if os.getenv('CHROMIUM_PATH'):
                    launch['executable_path'] = os.environ['CHROMIUM_PATH']
                browser = playwright.chromium.launch(**launch)
                page = browser.new_page(viewport={'width': 1360, 'height': 960})
                errors, batches = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda req: batches.append(req.url) if req.url.endswith('/api/batches') else None)
                page.goto(base)
                page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
                page.click('[data-page=models]')
                assert page.locator('#edit-credentials, #restart-app').count() == 0
                card = page.locator('[data-provider=deepseek]')
                assert '已配置' in card.inner_text()
                card.focus(); page.keyboard.press('Enter')
                key = page.locator('#credential-DEEPSEEK_API_KEY')
                key.wait_for()
                assert key.input_value() == ''
                assert key.get_attribute('type') == 'password'
                assert '已配置' in page.locator('#credential-fields').inner_text()
                key.fill('unsaved-synthetic-key')
                page.get_by_role('button', name='显示新输入的 API Key', exact=True).click()
                assert key.get_attribute('type') == 'text'
                page.get_by_role('button', name='隐藏新输入的 API Key', exact=True).click()
                assert key.get_attribute('type') == 'password'
                page.keyboard.press('Escape')
                assert page.locator('#credential-dialog').is_hidden()
                assert page.locator('#credential-fields input').count() == 0
                assert card.evaluate('(el) => el === document.activeElement')
                assert dotenv_values(config)['DEEPSEEK_API_KEY'] == 'previous-synthetic-key'
                card.click(); key.wait_for()
                # Empty secret preserves the configured key without reading it back.
                page.click('#credential-save')
                page.wait_for_function("() => !document.querySelector('#credential-dialog').open")
                assert dotenv_values(config)['DEEPSEEK_API_KEY'] == 'previous-synthetic-key'
                card.click(); key.wait_for(); key.fill('replacement-synthetic-key')
                with page.expect_response(lambda res: res.url.endswith('/api/credentials/deepseek') and res.request.method == 'PUT') as saved:
                    page.click('#credential-save')
                assert 'replacement-synthetic-key' not in saved.value.text()
                assert 'previous-synthetic-key' not in saved.value.text()
                page.wait_for_function("() => !document.querySelector('#credential-dialog').open")
                assert dotenv_values(config)['DEEPSEEK_API_KEY'] == 'replacement-synthetic-key'
                card.click(); key.wait_for()
                assert key.input_value() == '' and key.get_attribute('type') == 'password'
                # Simulate a disk error: dialog stays open and original file is kept.
                def fail_save(route):
                    if route.request.method == 'PUT':
                        route.fulfill(status=422, content_type='application/json', body='{"detail":"模拟保存失败，请重试。"}')
                    else:
                        route.continue_()
                page.route('**/api/credentials/deepseek', fail_save)
                key.fill('failed-synthetic-key'); page.click('#credential-save')
                page.wait_for_function("() => !document.querySelector('#credential-error').hidden")
                assert page.locator('#credential-dialog').is_visible()
                assert dotenv_values(config)['DEEPSEEK_API_KEY'] == 'replacement-synthetic-key'
                page.click('#credential-cancel')
                page.unroute('**/api/credentials/deepseek', fail_save)
                # Configure a service that needs both a key and a region.
                page.click('[data-provider=azure]')
                page.locator('#credential-AZURE_SPEECH_KEY').fill('azure-synthetic-key')
                page.locator('#credential-AZURE_SPEECH_REGION').fill('westus')
                page.click('#credential-save')
                page.wait_for_function("() => !document.querySelector('#credential-dialog').open")
                assert '已配置' in page.locator('[data-provider=azure]').inner_text()
                assert dotenv_values(config)['AZURE_SPEECH_REGION'] == 'westus'
                # Optional AWS token can be cleared when switching back to long-term keys.
                page.click('[data-provider=polly]')
                page.locator('#credential-mode').select_option('profile')
                assert page.locator('#credential-AWS_ACCESS_KEY_ID').is_hidden()
                page.locator('#credential-AWS_PROFILE').fill('test-profile')
                page.click('#credential-save')
                page.wait_for_function("() => !document.querySelector('#credential-dialog').open")
                assert dotenv_values(config)['AWS_PROFILE'] == 'test-profile'
                page.click('[data-provider=polly]')
                assert page.locator('#credential-mode').input_value() == 'profile'
                page.click('#credential-close')
                page.click('[data-provider=deepseek]'); key.wait_for()
                page.screenshot(path=str(output / 'tts-lab-credentials-dialog.png'))
                page.set_viewport_size({'width': 390, 'height': 760})
                assert page.locator('#credential-dialog').evaluate('(el) => el.scrollWidth <= el.clientWidth')
                assert page.locator('#credential-save').is_visible()
                page.screenshot(path=str(output / 'tts-lab-credentials-narrow.png'))
                page.click('#credential-close')
                page.reload(); page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
                page.click('[data-page=models]'); card.click(); key.wait_for()
                assert key.input_value() == ''
                assert not errors, errors
                assert not batches, 'Saving credentials must not submit synthesis requests'
                browser.close()
            print('PASS: credential dialog keyboard, masking, preserve/replace, cancel, errors, Azure/AWS, reload, narrow layout; no synthesis calls')
        finally:
            process.terminate()
            process.wait(timeout=15)
