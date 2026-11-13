"""Offline UI integration: run.py on :8765; CHROMIUM_PATH selects system Chromium.

Uses only the demo provider. Screenshots/downloads go to TTS_LAB_SCREENSHOTS or /tmp.
"""
import json
import io
import os
from pathlib import Path
import wave
import zipfile
from playwright.sync_api import sync_playwright

output = Path(os.environ.get('TTS_LAB_SCREENSHOTS', '/tmp'))
output.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    launch = {'headless': True}
    if os.getenv('CHROMIUM_PATH'):
        launch['executable_path'] = os.environ['CHROMIUM_PATH']
    browser = p.chromium.launch(**launch)
    page = browser.new_page(viewport={'width': 1360, 'height': 960})
    errors, submissions = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: submissions.append(request.post_data_json)
            if request.method == 'POST' and request.url.endswith('/api/batches') else None)
    page.goto('http://127.0.0.1:8765')
    page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
    assert page.locator('#page-test').is_visible()
    assert not page.locator('#page-compare').is_visible()
    assert not page.locator('#page-models').is_visible()
    assert not page.locator('#window-controls').is_visible()
    page.click('[data-page=compare]')
    first = page.locator('.variant').first
    for provider_id, credential in [('mimo', 'MIMO_API_KEY'), ('doubao', 'VOLCENGINE_TTS_API_KEY'),
                                     ('stepfun', 'STEP_API_KEY'), ('fish', 'FISH_AUDIO_API_KEY'),
                                     ('deepseek', 'DEEPSEEK_API_KEY')]:
        first.locator('.provider').select_option(provider_id)
        assert credential in first.inner_text()
    first.locator('.provider').select_option('local_indextts')
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(24000)
        writer.writeframes(b'\0\1' * 72000)
    first.get_by_label('导入参考录音', exact=True).set_input_files({
        'name': 'reference-test.wav', 'mimeType': 'audio/wav', 'buffer': buffer.getvalue()})
    page.wait_for_function("() => document.querySelector('.variant [data-param=reference_id]').value.length === 32")
    reference_id = first.locator('[data-param=reference_id]').input_value()
    assert first.locator('audio').count() == 1
    first.locator('.provider').select_option('local_glmtts')
    first.locator('[data-param=reference_id]').select_option(reference_id)
    first.locator('[data-param=reference_text]').fill('这是参考录音的逐字稿。')
    page.locator('#coverage-note summary').click()
    assert 'DeepSeek' in page.locator('#coverage-note').inner_text()
    page.click('#save-variants')
    page.wait_for_function("() => document.querySelector('#save-state').textContent === '已保存到本机'")
    page.reload()
    page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
    page.click('[data-page=compare]')
    assert first.locator('[data-param=reference_id]').input_value() == reference_id
    assert first.locator('[data-param=reference_text]').input_value() == '这是参考录音的逐字稿。'
    while page.locator('.variant').count() < 2:
        page.click('#add-variant')
    while page.locator('.variant').count() > 2:
        page.locator('.remove-variant').last.click()
    for index, variant in enumerate(page.locator('.variant').all()):
        variant.locator('.provider').select_option('demo')
        variant.locator('.variant-name').fill(['低频测试', '高频测试'][index])
        variant.locator('[data-param=frequency]').fill(str([440, 660][index]))
    page.click('#save-variants')
    page.wait_for_function("() => document.querySelector('#save-state').textContent === '已保存到本机'")
    page.click('[data-page=test]')
    page.select_option('#test-variant', label='高频测试')
    page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
    selected_uid = page.locator('#test-variant').input_value()
    assert '660' in page.locator('#selected-variant').inner_text()
    page.reload()
    page.wait_for_function("() => !document.querySelector('#test-variant').disabled")
    assert page.locator('#test-variant').input_value() == selected_uid
    page.screenshot(path=str(output / 'tts-lab-text-test.png'), full_page=True)
    page.fill('#text', '')
    page.click('#test-generate')
    assert '输入' in page.locator('#notice').inner_text()
    page.fill('#text', '同一段文字，选择一个已保存方案进行测试。')
    page.click('#test-generate')
    page.click('[data-page=compare]')
    assert page.locator('#compare-generate').is_disabled()
    page.click('[data-page=test]')
    page.wait_for_function("() => document.querySelectorAll('#test-results-slot audio').length === 1 && !document.querySelector('#test-generate').disabled")
    assert len(submissions[-1]['variants']) == 1
    assert submissions[-1]['variants'][0]['params']['frequency'] == 660
    assert not submissions[-1]['allow_cloud']
    audio = page.locator('#test-results-slot audio')
    audio.evaluate('(audio) => audio.play()')
    page.wait_for_function("() => document.querySelector('#test-results-slot audio').currentTime > 0")
    audio.evaluate('(audio) => audio.pause()')
    with page.expect_download() as info:
        page.click('#test-results-slot .download-batch')
    archive = output / 'tts-browser-batch.zip'
    info.value.save_as(str(archive))
    with zipfile.ZipFile(archive) as bundle:
        runs = json.loads(bundle.read('results.json'))
        assert len(runs) == 1 and runs[0]['params']['frequency'] == 660
    page.click('#page-test .history-button')
    page.wait_for_selector('.history-row')
    page.locator('.history-row .secondary').first.click()
    assert page.locator('#history-panel').is_hidden()
    page.click('[data-page=compare]')
    assert page.locator('#compare-text').input_value() == '同一段文字，选择一个已保存方案进行测试。'
    page.click('#compare-generate')
    page.wait_for_function("() => document.querySelectorAll('#compare-results-slot audio').length === 2 && !document.querySelector('#compare-generate').disabled")
    assert len(submissions[-1]['variants']) == 2
    assert '声音 A' in page.locator('#compare-results-slot').inner_text()
    page.uncheck('#compare-results-slot .blind')
    assert page.locator('#compare-results-slot .download').count() == 2
    assert page.locator('#test-results-slot audio').count() == 1
    page.click('#add-variant')
    assert page.locator('.variant').count() == 3
    page.locator('.remove-variant').last.click()
    assert page.locator('.variant').count() == 2
    # Unsaved edits stay out of the saved single-scheme test.
    page.locator('.variant [data-param=frequency]').last.fill('770')
    page.click('[data-page=test]')
    assert '660' in page.locator('#selected-variant').inner_text()
    page.click('[data-page=compare]')
    page.locator('.variant [data-param=frequency]').last.fill('660')
    page.click('#save-variants')
    page.wait_for_function("() => document.querySelector('#save-state').textContent === '已保存到本机'")
    page.screenshot(path=str(output / 'tts-lab-comparison.png'), full_page=True)
    page.locator('.variant .provider').first.select_option('openai')
    assert page.locator('#compare-consent-row').is_visible()
    page.locator('.variant .provider').first.select_option('demo')
    assert page.locator('#compare-consent-row').is_hidden()
    page.click('[data-page=models]')
    page.wait_for_selector('.model-card')
    assert page.locator('.model-card').count() == 6
    assert page.locator('[data-model="glm-tts"] button').first.is_disabled()
    page.select_option('#local-device', 'cuda')
    page.select_option('#local-device', 'cpu')
    page.screenshot(path=str(output / 'tts-lab-models.png'), full_page=True)
    # Every navigation target must fit a narrow browser and the minimum desktop width.
    for width in [880, 390]:
        page.set_viewport_size({'width': width, 'height': 844})
        for target in ['test', 'compare', 'models']:
            page.click(f'[data-page={target}]')
            assert page.locator(f'#page-{target}').is_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, target)
    page.click('[data-page=test]')
    page.screenshot(path=str(output / 'tts-lab-mobile.png'), full_page=True)
    page.click('[data-page=compare]')
    page.locator('.variant .provider').first.select_option('local_indextts')
    page.locator('.variant [data-param=reference_id]').first.select_option(reference_id)
    page.set_viewport_size({'width': 320, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors
    print('UI passed: three pages, saved schemes/reload, single-vs-multi payloads, independent results, audio, ZIP, history, consent, responsive layout; no JS errors.')
    browser.close()
