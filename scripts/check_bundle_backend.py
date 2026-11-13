#!/usr/bin/env python3
"""Check the packaged backend with its embedded Python, without cloud traffic.

Run from any directory using the bundle's resources/python/python.exe. Imports
must resolve through the bundle's isolated ._pth; this script never inserts the
checkout or a different backend into sys.path. No provider credential is used.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
from unittest import mock
import wave


def check(bundle: Path, version: str) -> dict:
    backend = bundle.resolve() / 'resources' / 'backend'
    import run as runner
    assert Path(runner.__file__).resolve() == backend / 'run.py', 'Launcher imported outside the bundle'
    # Match desktop isolation: inherited provider keys, SDK profiles and metadata
    # discovery must not influence a credential-free smoke check.
    for name in list(os.environ):
        if name in runner.PROVIDER_ENV or name.startswith('AWS_'):
            os.environ.pop(name, None)
    os.environ.update(AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull,
                      AWS_EC2_METADATA_DISABLED='true', TTS_LAB_DESKTOP='0')
    with tempfile.TemporaryDirectory(prefix='tts-lab-bundle-backend-') as temporary:
        isolated = Path(temporary).resolve()
        assert not isolated.is_relative_to(bundle.resolve()), 'Smoke data must remain outside the payload'
        os.environ['TTS_LAB_DATA_DIR'] = str(isolated / 'data')
        os.environ['TTS_LAB_MODELS_DIR'] = str(isolated / 'models')
        from tts_lab import app, providers, reference_audio
        import httpx

        assert app.ROOT.resolve() == backend / 'tts_lab', 'Application imported outside the bundle'
        state = app.state()
        assert state['version'] == version, 'Packaged API version does not match the release'
        catalog = {item['id']: item for item in state['providers']}
        assert {'mimo', 'doubao', 'deepseek', 'stepfun', 'fish', 'local_indextts', 'local_glmtts'} <= catalog.keys()
        deepseek = catalog['deepseek']
        assert not deepseek['ready'] and deepseek['missing_env'] == ['DEEPSEEK_API_KEY']
        fields = {field['key']: field for field in deepseek['fields']}
        assert fields['voice']['options'] == ['stella', 'tide'] and fields['voice']['default'] == 'stella'
        assert fields['filter_markdown']['type'] == 'checkbox' and fields['filter_markdown']['default'] is True
        assert state['references'] == [], 'Smoke profile must start without user recordings'

        audio = asyncio.run(providers.synthesize('demo', 'offline Windows backend check', {}))
        with wave.open(io.BytesIO(audio.audio), 'rb') as reader:
            assert reader.getnframes() > 0 and reader.getsampwidth() == 2
        ref = reference_audio.save(providers._wav(b'\x00\x01' * 72000), 'reference.wav')
        assert reference_audio.info(ref['id'])['sha256'] == ref['sha256']

        fake_key = 'tts-lab-offline-deepseek-key'
        text = '这是包内接口回归，不发送真实云请求。'
        pcm = b'\x00\x01' * 2400
        requests = []
        expected = [('stella', True), ('tide', False)]
        real_client = httpx.AsyncClient

        def respond(request):
            index = len(requests)
            assert index < len(expected), 'Unexpected extra request or retry'
            voice, markdown = expected[index]
            assert request.method == 'POST'
            assert str(request.url) == 'https://api.deepseek.com/v1/tools/tts'
            assert request.headers['Authorization'] == 'Bearer ' + fake_key
            assert request.headers['content-type'].startswith('application/json')
            assert json.loads(request.content) == {
                'text': text, 'voice_id': voice, 'format': 'pcm', 'filter_markdown': markdown,
            }
            requests.append((voice, markdown))
            return httpx.Response(200, headers={'content-type': 'application/octet-stream'}, content=pcm)

        def client(**options):
            assert options['follow_redirects'] is False and options['trust_env'] is False
            return real_client(transport=httpx.MockTransport(respond), **options)

        async def deepseek_contract():
            for voice, markdown in expected:
                result = await providers.synthesize('deepseek', text, {'voice': voice, 'filter_markdown': markdown})
                assert result.extension == 'wav' and result.mime_type == 'audio/wav'
                assert result.metadata['provider'] == 'deepseek' and result.metadata['sample_rate'] == 24000
                with wave.open(io.BytesIO(result.audio), 'rb') as reader:
                    assert reader.getnchannels() == 1 and reader.getsampwidth() == 2
                    assert reader.getframerate() == 24000 and reader.getnframes() == 2400
                    assert reader.readframes(2400) == pcm

        os.environ['DEEPSEEK_API_KEY'] = fake_key
        try:
            configured = next(item for item in app.state()['providers'] if item['id'] == 'deepseek')
            assert configured['ready'] and fake_key not in json.dumps(configured)
            # A mistakenly constructed default transport must fail locally, not
            # escape to a real endpoint. MockTransport bypasses these methods.
            with (mock.patch.object(httpx.AsyncHTTPTransport, 'handle_async_request', side_effect=AssertionError('Real HTTP is forbidden')),
                 mock.patch.object(httpx.HTTPTransport, 'handle_request', side_effect=AssertionError('Real HTTP is forbidden')),
                 mock.patch.object(providers.httpx, 'AsyncClient', side_effect=client)):
                asyncio.run(deepseek_contract())
        finally:
            os.environ.pop('DEEPSEEK_API_KEY', None)
        assert requests == expected
        return {'status': 'passed', 'version': version, 'packaged_backend': True,
                'demo_wav': True, 'reference_storage': True, 'deepseek_missing_credentials': True,
                'deepseek_mock_voices': [voice for voice, _ in requests],
                'deepseek_pcm_wav': {'sample_rate': 24000, 'channels': 1, 'sample_width': 2},
                'real_cloud_requests': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.bundle, args.version), ensure_ascii=True, sort_keys=True))


if __name__ == '__main__':
    main()
