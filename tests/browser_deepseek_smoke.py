"""DeepSeek UI/API integration with mock PCM; never contacts a paid service.

Run with the Python that has Playwright. TTS_LAB_TEST_PYTHON may select the
backend interpreter (for example .venv/bin/python). CHROMIUM_PATH and
TTS_LAB_SCREENSHOTS follow the other browser smoke tests.
"""
import csv
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import wave
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def serve_mock_pcm():
    sys.path.insert(0, str(ROOT))
    import math
    import struct
    import httpx
    import uvicorn
    from run import DesktopServer
    from tts_lab import providers
    from tts_lab.catalog import CATALOG

    # The disposable process never inherits provider credentials or AWS profiles.
    for key in {key for provider in CATALOG for key in provider['env']} | {key for key in os.environ if key.startswith('AWS_')}:
        os.environ.pop(key, None)
    os.environ.update(DEEPSEEK_API_KEY='browser-test-dummy-not-a-real-key',
                      AWS_EC2_METADATA_DISABLED='true', AWS_CONFIG_FILE=os.devnull,
                      AWS_SHARED_CREDENTIALS_FILE=os.devnull)
    pcm = b''.join(struct.pack('<h', int(4000 * math.sin(2 * math.pi * 440 * frame / 24000))) for frame in range(12000))

    def handle(request):
        assert request.method == 'POST' and str(request.url) == 'https://api.deepseek.com/v1/tools/tts'
        payload = json.loads(request.content)
        assert set(payload) == {'text', 'voice_id', 'format', 'filter_markdown'}
        assert payload['format'] == 'pcm' and type(payload['filter_markdown']) is bool
        with open(os.environ['TTS_LAB_MOCK_REQUESTS'], 'a', encoding='utf-8') as log:
            log.write(json.dumps(payload, ensure_ascii=False) + '\n')
        return httpx.Response(200, content=pcm, headers={'Content-Type': 'audio/pcm'})

    original_client = httpx.AsyncClient

    def mock_client(**kwargs):
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    providers.httpx.AsyncClient = mock_client
    server = DesktopServer(uvicorn.Config('tts_lab.app:app', host='127.0.0.1', port=0,
                                         access_log=False, log_level='error'))
    server.run()


def check_wav(data):
    with wave.open(io.BytesIO(data), 'rb') as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getnframes()) == (24000, 1, 2, 12000)
        assert any(audio.readframes(12000))


def check_zip(path, expected_params):
    with zipfile.ZipFile(path) as archive:
        runs = json.loads(archive.read('results.json'))
        assert len(runs) == len(expected_params)
        assert sorted((run['params']['voice'], run['params']['filter_markdown']) for run in runs) == sorted(expected_params)
        assert all(run['provider'] == 'deepseek' and set(run['params']) == {'voice', 'filter_markdown'} for run in runs)
        audio_names = [name for name in archive.namelist() if name.endswith('/audio.wav')]
        assert len(audio_names) == len(expected_params)
        for name in audio_names:
            check_wav(archive.read(name))


def main():
    from playwright.sync_api import sync_playwright

    output = Path(os.environ.get('TTS_LAB_SCREENSHOTS', '/tmp'))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tts-deepseek-browser-') as temporary:
        fixture = Path(temporary)
        calls_file = fixture / 'http-calls.jsonl'
        env = dict(os.environ, TTS_LAB_DATA_DIR=str(fixture / 'data'),
                   TTS_LAB_MODELS_DIR=str(fixture / 'models'), TTS_LAB_DESKTOP='0',
                   TTS_LAB_MOCK_REQUESTS=str(calls_file), PYTHONUTF8='1', PYTHONUNBUFFERED='1')
        process = subprocess.Popen([os.getenv('TTS_LAB_TEST_PYTHON', sys.executable), str(Path(__file__).resolve()), '--serve'],
                                   cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            ready = queue.Queue()
            threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
            line = ready.get(timeout=30)
            assert line, 'Mock backend failed to start: ' + process.stderr.read()
            address = json.loads(line)
            assert address['event'] == 'ready' and address['host'] == '127.0.0.1'
            origin = f"http://127.0.0.1:{address['port']}"
            with sync_playwright() as playwright:
                launch = {'headless': True}
                if os.getenv('CHROMIUM_PATH'):
                    launch['executable_path'] = os.environ['CHROMIUM_PATH']
                browser = playwright.chromium.launch(**launch)
                page = browser.new_page(viewport={'width': 1360, 'height': 960})
                errors, submissions = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: submissions.append(request.post_data_json)
                        if request.method == 'POST' and request.url.endswith('/api/batches') else None)
                page.goto(origin)
                page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
                page.click('[data-page=compare]')
                page.locator('#coverage-note summary').click()
                assert page.locator('#coverage-note a[href="https://api-docs.deepseek.com/guides/tts/"]').count() == 1
                assert '未找到可直接接入' not in page.locator('#coverage-note').inner_text()
                first = page.locator('.variant').first
                first.locator('.provider').select_option('deepseek')
                assert first.locator('[data-param=voice]').input_value() == 'stella'
                assert first.locator('[data-param=filter_markdown]').is_checked()
                assert first.locator('[data-param]').count() == 2 and first.locator('input[type=file]').count() == 0
                first.locator('.variant-name').fill('DeepSeek tide · 保留 Markdown')
                first.locator('[data-param=voice]').select_option('tide')
                first.locator('[data-param=filter_markdown]').uncheck()
                first.locator('.clone-variant').click()
                second = page.locator('.variant').nth(1)
                second.locator('.variant-name').fill('DeepSeek stella · 过滤 Markdown')
                second.locator('[data-param=voice]').select_option('stella')
                second.locator('[data-param=filter_markdown]').check()
                page.click('#save-variants')
                page.wait_for_function("() => document.querySelector('#save-state').textContent === '已保存到本机'")
                page.reload()
                page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
                page.click('[data-page=compare]')
                assert first.locator('[data-param=voice]').input_value() == 'tide'
                assert not first.locator('[data-param=filter_markdown]').is_checked()
                assert second.locator('[data-param=voice]').input_value() == 'stella'
                assert second.locator('[data-param=filter_markdown]').is_checked()
                page.click('[data-page=test]')
                page.select_option('#test-variant', label='DeepSeek tide · 保留 Markdown')
                page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
                page.fill('#text', 'DeepSeek **Markdown** parameter UI test.')
                assert page.locator('#test-consent-row').is_visible()
                page.click('#test-generate')
                page.wait_for_function("() => document.querySelector('#notice').textContent.includes('请先确认')")
                assert not calls_file.exists(), 'No provider request is allowed without consent'
                page.check('#test-cloud-consent')
                page.click('#test-generate')
                page.wait_for_function("() => document.querySelectorAll('#test-results-slot audio').length === 1 && !document.querySelector('#test-generate').disabled")
                assert submissions[-1]['variants'] == [{'provider': 'deepseek', 'params': {'voice': 'tide', 'filter_markdown': False}}]
                assert submissions[-1]['allow_cloud'] is True
                audio = page.locator('#test-results-slot audio')
                audio.evaluate('(node) => node.play()')
                page.wait_for_function("() => document.querySelector('#test-results-slot audio').currentTime > 0")
                audio.evaluate('(node) => node.pause()')
                with page.expect_download() as download:
                    page.click('#test-results-slot .download')
                wav = output / 'tts-deepseek-mock.wav'
                download.value.save_as(str(wav)); check_wav(wav.read_bytes())
                with page.expect_download() as download:
                    page.click('#test-results-slot .download-batch')
                single = output / 'tts-deepseek-single.zip'
                download.value.save_as(str(single)); check_zip(single, [('tide', False)])
                page.click('[data-page=compare]')
                page.check('#compare-cloud-consent')
                page.click('#compare-generate')
                page.wait_for_function("() => document.querySelectorAll('#compare-results-slot audio').length === 2 && !document.querySelector('#compare-generate').disabled")
                assert len(submissions[-1]['variants']) == 2 and submissions[-1]['allow_cloud'] is True
                page.uncheck('#compare-results-slot .blind')
                assert page.locator('#compare-results-slot .download').count() == 2
                with page.expect_download() as download:
                    page.click('#compare-results-slot .download-batch')
                compare = output / 'tts-deepseek-compare.zip'
                download.value.save_as(str(compare)); check_zip(compare, [('tide', False), ('stella', True)])
                with page.expect_download() as download:
                    page.click('#compare-results-slot a[href="/api/export.csv"]')
                csv_file = output / 'tts-deepseek-results.csv'
                download.value.save_as(str(csv_file))
                rows = list(csv.DictReader(io.StringIO(csv_file.read_text(encoding='utf-8-sig'))))
                assert len(rows) == 3 and all(row['provider'] == 'deepseek' for row in rows)
                requests = [json.loads(line) for line in calls_file.read_text().splitlines()]
                assert len(requests) == 3 and sorted((item['voice_id'], item['filter_markdown']) for item in requests) == [('stella', True), ('tide', False), ('tide', False)]
                assert not errors, errors
                page.screenshot(path=str(output / 'tts-deepseek-comparison.png'), full_page=True)
                browser.close()
                print('DeepSeek UI passed: two-field defaults, voice/checkbox changes, save/reload, consent, single and comparison generation, WAV playback/download, ZIP/CSV; 3 mocked PCM requests, no real cloud calls.')
        finally:
            if process.poll() is None:
                try:
                    process.communicate('shutdown\n', timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.communicate(timeout=5)


if __name__ == '__main__':
    if sys.argv[1:] == ['--serve']:
        serve_mock_pcm()
    else:
        main()
