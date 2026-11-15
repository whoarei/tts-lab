"""Isolated browser regression, with synthetic audio instead of model/cloud calls.

pip install playwright; playwright install chromium
CHROMIUM_PATH=/usr/bin/chromium python tests/browser_smoke.py
The temporary backend never reads .env or downloads models. Its Kokoro adapter
returns a silent WAV fixture; these checks verify UI behavior, not TTS quality.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

import httpx

ROOT = Path(__file__).resolve().parents[1]


def fixture_server(port):
    sys.path.insert(0, str(ROOT))
    import uvicorn
    from tts_lab import app as module
    from tts_lab import credentials
    from tts_lab.providers import _audio, _wav

    for key in list(os.environ):
        if key in credentials.PROVIDER_ENV or key.startswith('AWS_'):
            os.environ.pop(key, None)
    credentials.CONFIG_PATH = Path(os.environ['TTS_LAB_DATA_DIR']) / 'credentials.env'
    catalog = module.public_catalog
    installed = {'local_kokoro': ['kokoro-v1.1'], 'local_qwen3': ['qwen3-1.7b']}

    def fixture_catalog():
        providers = catalog()
        for provider in providers:
            if provider['id'] in installed:
                provider['ready'] = bool(installed[provider['id']])
                provider['missing_env'] = [] if provider['ready'] else ['fixture model unavailable']
                provider['installed_models'] = installed[provider['id']][:]
            elif provider['id'] == 'polly':
                # The source catalog may detect an SDK without credentials.
                provider.update(ready=True, credentials_configured=False)
            elif provider['id'] == 'openai':
                provider.update(ready=False, credentials_configured=True)
            elif provider['id'] != 'deepseek':
                provider['ready'] = False
        return providers

    @module.app.post('/fixture/availability')
    async def fixture_availability(models: dict):
        for key, values in models.items():
            assert key in installed and isinstance(values, list)
            installed[key] = values
        return {'status': 'updated'}

    async def fixture_audio(provider, text, params):
        assert provider in installed, 'Browser tests must never call cloud providers'
        return _audio(_wav(b'\0\0' * 36000), {'fixture': True})

    module.public_catalog = fixture_catalog
    module.synthesize = fixture_audio
    uvicorn.run(module.app, host='127.0.0.1', port=port, log_level='error')


def check_ui(origin, data_dir):
    from playwright.sync_api import sync_playwright
    output = Path('/tmp/tts-lab-preview')
    output.mkdir(exist_ok=True)
    with sync_playwright() as p:
        launch = {'headless': True, 'args': ['--no-sandbox']}
        if os.getenv('CHROMIUM_PATH'):
            launch['executable_path'] = os.environ['CHROMIUM_PATH']
        browser = p.chromium.launch(**launch)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def page_history(direction):
            previous = page.locator('#history-page').inner_text()
            page.click('#history-' + direction)
            page.wait_for_function('(previous) => document.querySelector("#history-page").textContent !== previous', arg=previous)

        def open_history_batch(batch_id):
            while page.locator('#history-prev').is_enabled():
                page_history('prev')
            for _ in range(10):
                row = page.locator(f'.history-row[data-batch="{batch_id}"]')
                if row.count():
                    row.locator('button').click()
                    return
                assert page.locator('#history-next').is_enabled(), f'Missing historical batch {batch_id}'
                page_history('next')
            raise AssertionError(f'Historical batch was not reachable: {batch_id}')
        page.goto(origin)
        page.wait_for_selector('body[data-ready="true"]')
        assert page.locator('.variant').count() == 1
        assert page.locator('.provider option[value="demo"]').count() == 0
        assert page.locator('.preset').input_value() == ''
        assert page.locator('.brand-mark').get_attribute('src') == '/static/icon.svg'
        assert page.locator('.provider').input_value() == 'local_kokoro'
        assert page.locator('.provider option:enabled').evaluate_all('(nodes) => nodes.map(n => n.value)') == ['local_kokoro', 'local_qwen3']
        assert page.locator('#cloud-consent').count() == 0
        # Every provider's defaults must pass browser constraints, including
        # fractional ranges whose minimum is not aligned to their suggested step.
        for service in page.locator('.provider option').evaluate_all('(nodes) => nodes.map(n => n.value)'):
            page.locator('.provider').select_option(service)
            invalid = page.locator('.variant input, .variant select, .variant textarea').evaluate_all(
                '(nodes) => nodes.filter(n => !n.checkValidity()).map(n => n.dataset.param)')
            assert not invalid, (service, invalid)
            aligned = page.evaluate('''() => {
                const text = document.querySelector('#text').getBoundingClientRect();
                const params = document.querySelector('.provider-info').getBoundingClientRect();
                return Math.abs(text.bottom - params.bottom) < 1;
            }''')
            assert aligned, service
            if service == 'local_qwen3':
                assert page.locator('[data-param="model"] option:enabled').evaluate_all('(nodes) => nodes.map(n => n.value)') == ['qwen3-1.7b']
                assert page.locator('[data-param="model"]').input_value() == 'qwen3-1.7b'
        page.locator('.provider').select_option('local_kokoro')
        speed = page.locator('[data-param="speed"]')
        voice = page.locator('[data-param="voice"]')
        voice.fill('19')
        speed.fill('1.1')
        page.click('.save-preset')
        page.fill('#preset-name', '日常朗读')
        page.click('#confirm-save')
        page.wait_for_selector('#save-dialog', state='hidden')
        original_id = page.locator('.preset').input_value()
        assert original_id
        assert page.locator('.preset-status').inner_text() == '已保存'
        speed.fill('1.2')
        assert '未保存' in page.locator('.preset-status').inner_text()
        # Generation uses the current draft without overwriting or creating presets.
        page.click('#generate')
        page.wait_for_selector('audio')
        page.wait_for_function('() => !document.querySelector("#generate").disabled')
        presets = page.request.get(origin + '/api/presets').json()['presets']
        assert len(presets) == 1 and presets[0]['params']['speed'] == 1.1
        runs = page.request.get(origin + '/api/runs').json()['runs']
        original_run = runs[0]
        assert runs[0]['params']['speed'] == 1.2
        assert runs[0]['params']['voice'] == 19
        # The single action synthesizes if needed, then starts playback itself.
        page.wait_for_function('() => document.querySelector("audio").currentTime > 0')
        original_audio = page.locator('audio').get_attribute('src')
        page.click('#generate')
        page.wait_for_function('() => !document.querySelector("#generate").disabled')
        assert page.locator('audio').get_attribute('src') == original_audio
        assert len(page.request.get(origin + '/api/runs').json()['runs']) == 1
        # A changed parameter needs new audio; reverting reuses the older file.
        speed.fill('1.3')
        page.click('#generate')
        page.wait_for_function('() => !document.querySelector("#generate").disabled')
        assert page.locator('audio').get_attribute('src') != original_audio
        assert len(page.request.get(origin + '/api/runs').json()['runs']) == 2
        speed.fill('1.2')
        page.click('#generate')
        page.wait_for_function('() => !document.querySelector("#generate").disabled')
        assert page.locator('audio').get_attribute('src') == original_audio
        assert len(page.request.get(origin + '/api/runs').json()['runs']) == 2
        # Completed results expose just playback and the batch ZIP action.
        assert page.locator('.result-card[data-status="completed"]').count() == 1
        assert page.locator('.result-card h3, .result-card .status, .result-card pre, .rating, .score-grid, .result-notes, .score-actions, #source-panel, .download').count() == 0
        assert page.locator('#download-batch').inner_text() == '下载音频'
        assert 'secondary' in page.locator('#download-batch').get_attribute('class').split()
        assert page.locator('#exports a, #exports button').count() == 1
        assert page.locator('a[href="/api/export.csv"]').count() == 0
        page.click('[data-view="compare"]')
        assert page.locator('.variant').count() == 2
        assert page.locator('audio').count() == 0
        page.click('[data-view="test"]')
        assert speed.input_value() == '1.2'
        assert voice.input_value() == '19'
        assert page.locator('audio').count() == 1
        # Save updates the selected preset; save-as creates an independent copy.
        page.click('.save-preset')
        page.wait_for_function('() => document.querySelector(".preset-status").textContent === "已保存"')
        assert page.locator('.preset').input_value() == original_id
        voice.fill('20')
        page.click('.save-as')
        page.fill('#preset-name', '新的音色')
        page.click('#confirm-save')
        page.wait_for_selector('#save-dialog', state='hidden')
        copy_id = page.locator('.preset').input_value()
        assert copy_id != original_id
        presets = {x['id']: x for x in page.request.get(origin + '/api/presets').json()['presets']}
        assert presets[original_id]['params']['voice'] == 19
        assert presets[original_id]['params']['speed'] == 1.2
        assert presets[copy_id]['params']['voice'] == 20
        page.locator('.preset').select_option('')
        assert speed.input_value() == '1'
        assert page.locator('.preset-status').inner_text() == ''
        page.fill('#text', '这段文字在切换页面后仍然保留。')
        page.click('[data-view="models"]')
        page.wait_for_selector('.model-card')
        assert page.locator('.model-card').count() == 6
        assert page.locator('.cloud-model').count() == 13
        assert '点击配置' not in page.locator('body').inner_text()
        page.select_option('#local-device', 'cuda')
        page.select_option('#local-device', 'cpu')
        page.screenshot(path=str(output / 'models.png'), full_page=True)
        page.click('[data-view="about"]')
        assert page.locator('#about-panel').is_visible()
        assert page.locator('#app-version').inner_text() == 'v' + page.request.get(origin + '/api/state').json()['version']
        assert page.locator('#app-update-panel').is_hidden()
        assert page.locator('#browser-update-links').is_visible()
        assert page.locator('.app-sidebar #app-version').count() == 0
        page.click('[data-view="test"]')
        assert page.locator('#text').input_value() == '这段文字在切换页面后仍然保留。'
        page.fill('#text', '')
        page.click('#generate')
        assert '输入' in page.locator('#notice').inner_text()
        page.click('[data-view="settings"]')
        assert page.locator('#settings-panel').is_visible()
        page.wait_for_function('() => document.querySelector("#history-retention-days").value === "30"')
        assert page.request.get(origin + '/api/settings').json()['history_retention_days'] == 30
        assert page.locator('#preferences-expiry-hint').is_visible()
        page.select_option('#history-retention-days', '0')
        assert page.locator('#preferences-expiry-hint').is_hidden()
        page.click('#save-preferences')
        page.wait_for_function('() => document.querySelector("#preferences-status").textContent === "已保存"')
        assert page.request.get(origin + '/api/settings').json()['history_retention_days'] == 0
        page.reload(); page.wait_for_selector('body[data-ready="true"]')
        page.click('[data-view="settings"]')
        page.wait_for_function('() => document.querySelector("#history-retention-days").value === "0"')
        assert page.locator('#preferences-expiry-hint').is_hidden()
        page.screenshot(path=str(output / 'settings.png'), full_page=True)
        page.click('[data-view="test"]')
        page.reload()
        page.wait_for_selector('body[data-ready="true"]')
        assert page.locator('.preset option').count() == 3
        page.locator('.preset').select_option(original_id)
        assert speed.input_value() == '1.2'
        assert voice.input_value() == '19'
        # Saved audio also survives a page reload, and text changes invalidate it.
        page.click('#generate')
        page.wait_for_function('() => !document.querySelector("#generate").disabled')
        assert page.locator('audio').get_attribute('src') == original_audio
        assert len(page.request.get(origin + '/api/runs').json()['runs']) == 2
        page.fill('#text', '这是一段新的试听文本。')
        page.click('#generate')
        page.wait_for_function('() => !document.querySelector("#generate").disabled')
        assert page.locator('audio').get_attribute('src') != original_audio
        assert len(page.request.get(origin + '/api/runs').json()['runs']) == 3
        page.screenshot(path=str(output / 'text-test.png'), full_page=True)
        # Comparison remains independent and keeps its drafts across navigation.
        page.click('[data-view="compare"]')
        page.locator('.variant .preset').first.select_option(copy_id)
        page.locator('.variant').first.locator('.icon-button').first.click()
        assert page.locator('.variant').count() == 3
        page.locator('.variant').last.locator('.icon-button').last.click()
        assert page.locator('.variant').count() == 2
        page.click('[data-view="models"]')
        page.click('[data-view="compare"]')
        assert page.locator('.variant .preset').first.input_value() == copy_id
        comparison_expected = page.locator('.variant').evaluate_all('''(cards) => cards.map(card => ({
            provider: card.querySelector('.provider').value,
            params: Object.fromEntries(Array.from(card.querySelectorAll('[data-param]')).map(input =>
                [input.dataset.param, input.type === 'checkbox' ? input.checked :
                    input.type === 'number' ? Number(input.value) : input.value]))
        }))''')
        page.click('#generate')
        page.wait_for_function('() => document.querySelectorAll("audio").length === 2 && !document.querySelector("#generate").disabled')
        assert page.locator('#results h3').all_text_contents() == ['声音 A', '声音 B']
        assert 'Kokoro' not in page.locator('#results').inner_text()
        assert page.locator('.download, .rating, .score-grid, .result-notes, .score-actions, #source-panel, .result-card pre').count() == 0
        page.uncheck('#blind')
        assert sorted(page.locator('#results h3').all_text_contents()) == ['方案 1', '方案 2']
        assert 'Kokoro' not in page.locator('#results').inner_text()
        assert page.locator('.result-card[data-status="completed"] .status').count() == 0
        assert page.locator('#download-batch').inner_text() == '下载音频'
        assert page.locator('#exports a, #exports button').count() == 1
        with page.expect_download() as download:
            page.click('#download-batch')
        batch_file = output / 'batch.zip'
        download.value.save_as(str(batch_file))
        with zipfile.ZipFile(batch_file) as archive:
            records = json.loads(archive.read('results.json'))
            assert len(records) == 2
            assert sorted(json.dumps({key: run[key] for key in ('provider', 'params')}, sort_keys=True) for run in records) == sorted(json.dumps(item, sort_keys=True) for item in comparison_expected)
            audio_names = [name for name in archive.namelist() if name.endswith('/audio.wav')]
            assert len(audio_names) == 2
            assert all(archive.read(name).startswith(b'RIFF') for name in audio_names)
        # Reordering the two editor configurations must reuse their existing
        # audio while relabelling each result to its current editor position.
        labels_before = {card.get_attribute('data-run'): card.locator('h3').inner_text()
                         for card in page.locator('.result-card').all()}
        count_before_swap = len(page.request.get(origin + '/api/runs').json()['runs'])
        page.locator('.variant').first.locator('.provider').select_option(comparison_expected[1]['provider'])
        page.locator('.variant').nth(1).locator('.preset').select_option(copy_id)
        comparison_expected.reverse()
        page.click('#generate')
        page.wait_for_function('() => !document.querySelector("#generate").disabled')
        assert len(page.request.get(origin + '/api/runs').json()['runs']) == count_before_swap
        expected_positions = {json.dumps(item, sort_keys=True): index + 1
                              for index, item in enumerate(comparison_expected)}
        expected_by_run = {run['id']: expected_positions[json.dumps(
            {key: run[key] for key in ('provider', 'params')}, sort_keys=True)] for run in records}
        assert {card.get_attribute('data-run') for card in page.locator('.result-card').all()} == set(expected_by_run)
        for card in page.locator('.result-card').all():
            run_id = card.get_attribute('data-run')
            scheme = expected_by_run[run_id]
            assert card.get_attribute('data-scheme') == str(scheme)
            assert card.locator('h3').inner_text() == f'方案 {scheme}'
            assert card.locator('audio').get_attribute('aria-label') == f'方案 {scheme}'
            assert card.locator('h3').inner_text() != labels_before[run_id]
        page.screenshot(path=str(output / 'results.png'), full_page=True)
        recent = page.request.get(origin + '/api/runs').json()['runs']
        comparison_runs = sorted((run for run in recent if run['batch_id'] == recent[0]['batch_id']),
                                 key=lambda run: run['created_at'])
        presets_before_history = page.request.get(origin + '/api/presets').json()['presets']
        token = page.request.get(origin + '/api/state').json()['token']
        headers = {'X-Local-Token': token}
        seeded_batches = []
        for number in range(10):
            response = page.request.post(origin + '/api/batches', headers=headers, data={
                'text': f'历史分页样例 {number + 1}',
                'variants': [{'provider': 'local_kokoro', 'params': {'voice': number}}]})
            assert response.status == 202, response.text()
            seeded_batches.append(response.json()['batch_id'])
        for _ in range(50):
            if all(run['status'] == 'completed' for run in page.request.get(origin + '/api/runs').json()['runs']):
                break
            time.sleep(.1)
        else:
            raise AssertionError('Synthetic pagination fixtures did not finish')
        page.click('[data-view="compare"]')
        page.wait_for_function('() => document.querySelectorAll(".history-row").length === 10')
        assert page.locator('#history-panel').evaluate('(node) => node.tagName') == 'SECTION'
        assert page.locator('#history-panel').is_visible()
        assert page.locator('#history-panel > summary').count() == 0
        first_page_batches = page.locator('.history-row').evaluate_all('(rows) => rows.map(row => row.dataset.batch)')
        assert set(first_page_batches) == set(seeded_batches)
        assert page.locator('#history-prev').is_disabled() and page.locator('#history-next').is_enabled()
        page_history('next')
        page.wait_for_function('() => document.querySelectorAll(".history-row").length === 4')
        second_page_batches = page.locator('.history-row').evaluate_all('(rows) => rows.map(row => row.dataset.batch)')
        assert not set(first_page_batches) & set(second_page_batches)
        assert comparison_runs[0]['batch_id'] in second_page_batches
        assert page.locator('#history-prev').is_enabled() and page.locator('#history-next').is_disabled()
        second_page = page.request.get(origin + '/api/history?page=2&page_size=10').json()
        assert second_page['page'] == 2 and second_page['page_size'] == 10
        assert second_page['total'] == 14 and second_page['total_pages'] == 2
        full_comparison = next(batch['runs'] for batch in second_page['batches'] if batch['batch_id'] == comparison_runs[0]['batch_id'])
        assert {run['id'] for run in full_comparison} == {run['id'] for run in comparison_runs}
        page.screenshot(path=str(output / 'history-page-2.png'), full_page=True)
        # Opening history replaces the current text and editor settings, without
        # attaching the restored draft to a saved preset or changing its files.
        page.fill('#text', '这些是尚未保存的编辑内容。')
        page.locator('.variant .provider').first.select_option('local_qwen3')
        open_history_batch(original_run['batch_id'])
        assert page.locator('#page-title').inner_text() == '文本测试'
        assert page.locator('audio').count() == 1
        assert page.locator('#text').input_value() == original_run['text']
        assert page.locator('.provider').input_value() == original_run['provider']
        assert speed.input_value() == str(original_run['params']['speed'])
        assert voice.input_value() == str(original_run['params']['voice'])
        assert page.locator('.preset').input_value() == ''
        speed.fill('1.7')
        page.click('[data-view="about"]')
        page.click('[data-view="test"]')
        assert page.locator('.result-card').first.get_attribute('data-run') == original_run['id']
        assert page.locator('.result-card pre').count() == 0
        assert page.request.get(origin + '/api/runs/' + original_run['id']).json()['params'] == original_run['params']
        page.click('[data-view="compare"]')
        assert page.locator('#text').input_value() == '这些是尚未保存的编辑内容。'
        open_history_batch(comparison_runs[0]['batch_id'])
        assert page.locator('#page-title').inner_text() == '方案对比'
        assert page.locator('#text').input_value() == comparison_runs[0]['text']
        assert page.locator('.variant .provider').evaluate_all('(nodes) => nodes.map(n => n.value)') == [run['provider'] for run in comparison_runs]
        restored = page.locator('.variant').evaluate_all('''(cards) => cards.map(card =>
            Object.fromEntries(Array.from(card.querySelectorAll('[data-param]')).map(input =>
                [input.dataset.param, input.type === 'checkbox' ? input.checked :
                    input.type === 'number' ? Number(input.value) : input.value])))''')
        assert restored == [run['params'] for run in comparison_runs]
        assert page.locator('.preset').evaluate_all('(nodes) => nodes.every(n => n.value === "")')
        assert page.request.get(origin + '/api/presets').json()['presets'] == presets_before_history
        for _ in range(4):
            page.click('#add-variant')
        assert page.locator('.variant').count() == 6
        assert page.locator('#add-variant').is_hidden()
        assert all(b.is_disabled() for b in page.locator('.variant-header .icon-button:first-child').all())
        page.screenshot(path=str(output / 'compare.png'), full_page=True)
        # Historical presets retain unavailable values as context, never options
        # that can silently invoke an unconfigured service or an absent model.
        page.click('[data-view="test"]')
        token = page.request.get(origin + '/api/state').json()['token']
        headers = {'X-Local-Token': token}
        legacy_model = page.request.post(origin + '/api/presets', headers=headers, data={
            'name': '旧的本地模型', 'provider': 'local_qwen3', 'params': {'model': 'qwen3-0.6b'}}).json()
        legacy_cloud = page.request.post(origin + '/api/presets', headers=headers, data={
            'name': '旧的云服务', 'provider': 'azure', 'params': {}}).json()
        page.reload(); page.wait_for_selector('body[data-ready="true"]')
        page.locator('.preset').select_option(legacy_model['id'])
        model = page.locator('[data-param="model"]')
        assert model.input_value() == 'qwen3-0.6b'
        assert model.locator('option:checked').evaluate('(node) => node.disabled'), model.evaluate('(node) => node.outerHTML')
        assert model.locator('option:enabled').evaluate_all('(nodes) => nodes.map(n => n.value)') == ['qwen3-1.7b']
        assert page.locator('#generate').is_disabled() and page.locator('.save-preset').is_disabled()
        model.select_option('qwen3-1.7b')
        assert page.locator('#generate').is_enabled()
        assert next(p for p in page.request.get(origin + '/api/presets').json()['presets'] if p['id'] == legacy_model['id'])['params']['model'] == 'qwen3-0.6b'
        page.locator('.preset').select_option(legacy_cloud['id'])
        assert page.locator('.provider').input_value() == 'azure'
        assert page.locator('.provider option:checked').evaluate('(node) => node.disabled')
        assert page.locator('.provider option[value="azure"]:enabled').count() == 0
        assert page.locator('#generate').is_disabled() and page.locator('.save-as').is_disabled()
        before = len(page.request.get(origin + '/api/runs').json()['runs'])
        page.locator('#generate').dispatch_event('click')
        assert len(page.request.get(origin + '/api/runs').json()['runs']) == before
        for width, height in [(1280, 800), (880, 700), (390, 844)]:
            page.set_viewport_size({'width': width, 'height': height})
            for view in ['test', 'compare', 'models', 'settings', 'about']:
                page.click(f'[data-view="{view}"]')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, view)
                if width == 390:
                    page.screenshot(path=str(output / f'mobile-{view}.png'), full_page=True)
        assert len(list((data_dir / 'presets').glob('*.json'))) == 4
        # Removing a model does not prevent playing matching completed history.
        response = page.request.post(origin + '/fixture/availability', headers=headers, data={'local_kokoro': []})
        assert response.ok
        page.reload(); page.wait_for_selector('body[data-ready="true"]')
        assert page.locator('.provider').input_value() == 'local_qwen3'
        open_history_batch(original_run['batch_id'])
        assert page.locator('.provider').input_value() == 'local_kokoro'
        assert page.locator('.provider option:checked').evaluate('(node) => node.disabled')
        assert page.locator('[data-param="model"]').input_value() == 'kokoro-v1.1'
        assert page.locator('[data-param="model"] option:checked').evaluate('(node) => node.disabled')
        assert page.locator('#generate').is_enabled()
        page.click('#generate')
        page.wait_for_function('() => !document.querySelector("#generate").disabled')
        assert page.locator('#results audio').get_attribute('src') == original_audio
        assert len(page.request.get(origin + '/api/runs').json()['runs']) == before
        page.fill('#text', '当前模型已经删除，这段新文本不能重新生成。')
        assert page.locator('#generate').is_disabled()
        assert page.locator('.provider').input_value() == 'local_kokoro'
        # No ready service means an empty editor, while About, configuration and
        # historical audio remain available. Configuring a key enables its option.
        response = page.request.post(origin + '/fixture/availability', headers=headers, data={'local_qwen3': []})
        assert response.ok
        page.reload(); page.wait_for_selector('body[data-ready="true"]')
        assert page.locator('.provider').input_value() == '' and page.locator('.provider').is_disabled()
        assert page.locator('[data-param]').count() == 0
        assert page.locator('#generate').is_disabled() and page.locator('.save-preset').is_disabled()
        page.click('[data-view="about"]'); assert page.locator('#about-panel').is_visible()
        page.click('[data-view="models"]'); page.wait_for_selector('.cloud-model')
        assert page.locator('.cloud-model').count() == 13
        page.click('[data-provider="deepseek"]')
        page.fill('#credential-DEEPSEEK_API_KEY', 'browser-fixture-key-not-a-real-key')
        page.click('#credential-save'); page.wait_for_selector('#credential-dialog', state='hidden')
        page.click('[data-view="test"]')
        assert page.locator('.provider').input_value() == ''
        assert page.locator('.provider').is_enabled()
        assert page.locator('.provider option:enabled').evaluate_all('(nodes) => nodes.map(n => n.value)') == ['deepseek']
        page.locator('.provider').select_option('deepseek')
        assert page.locator('#generate').is_enabled() and page.locator('.save-preset').is_enabled()
        # A direct click on a cloud service must submit without a checkbox.
        # Intercept both readiness and submission so this never makes a cloud call.
        def cloud_state(route):
            response = route.fetch()
            state = response.json()
            for service in state['providers']:
                if service['id'] == 'openai':
                    service.update(ready=True, credentials_configured=True, missing_env=[])
            route.fulfill(response=response, json=state)

        submissions = []

        def cloud_submit(route):
            submissions.append(route.request.post_data_json)
            route.fulfill(status=503, json={'detail': '模拟云端请求已接收'})

        page.route('**/api/state', cloud_state)
        page.route('**/api/batches', cloud_submit)
        page.reload()
        page.wait_for_selector('body[data-ready="true"]')
        page.click('#generate')
        page.wait_for_selector('#notice:not([hidden])')
        assert len(submissions) == 1
        assert submissions[0]['allow_cloud'] is True
        assert submissions[0]['variants'][0]['provider'] == 'openai'
        assert '模拟云端请求已接收' in page.locator('#notice').inner_text()
        assert not errors, errors
        print('UI passed: optional presets, save/update/save-as, draft-only synthesis, automatic playback, '
              'cache reuse after edits/reload/reordering, editor-linked comparison labels, minimal playback results, view isolation, blind reveal, parameter ZIP, paginated history, retention settings, six-variant limit, '
              'ready service/model filtering, unavailable history replay, empty/configured states, desktop/mobile; no JS errors or cloud/model calls.')
        browser.close()


def main():
    with tempfile.TemporaryDirectory(prefix='tts-lab-browser-') as temp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, TTS_LAB_DATA_DIR=temp, TTS_LAB_MODELS_DIR=temp + '/models')
        # This child imports app directly, never run.configure() or a .env file.
        env.pop('TTS_LAB_DESKTOP', None)
        server = subprocess.Popen([sys.executable, __file__, '--fixture-server', str(port)], cwd=ROOT, env=env)
        origin = f'http://127.0.0.1:{port}'
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError('Fixture backend stopped')
                try:
                    if httpx.get(origin + '/api/state', timeout=1).status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError('Fixture backend did not start')
            check_ui(origin, Path(temp))
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--fixture-server':
        fixture_server(int(sys.argv[2]))
    else:
        main()
