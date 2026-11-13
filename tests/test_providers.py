"""Offline contracts only: these tests never contact paid APIs."""
import base64
import io
import json
import os
import sys
import types
import unittest
import wave
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

import httpx
from tts_lab.catalog import CATALOG, public_catalog, validate_params
from tts_lab.providers import ProviderError, _audio, _wav, synthesize

MP3 = b'ID3' + b'\0' * 32
PCM = b'\0\0' * 100
WAV = _wav(PCM)
ENV = {key: 'test-secret-not-for-browser' for c in CATALOG for key in c['env']}
ENV['AZURE_SPEECH_REGION'] = 'eastus'


class FragmentedResponse(httpx.AsyncByteStream):
    async def __aiter__(self):
        # Deliberately split JSON and base64 across transport chunks.
        raw = b'\n'.join(json.dumps(item).encode() for item in [
            {'code': 0, 'data': base64.b64encode(MP3[:7]).decode()},
            {'code': 0, 'data': None, 'sentence': {'text': 'hello'}},
            {'code': 0, 'data': base64.b64encode(MP3[7:]).decode()},
            {'code': 20000000, 'data': None}])
        for i in range(0, len(raw), 5):
            yield raw[i:i+5]


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.requests = []
        self.response_override = None
        real_client = httpx.AsyncClient
        def make_client(**kwargs):
            return real_client(transport=httpx.MockTransport(self.handle), **kwargs)
        self.client_patch = patch('tts_lab.providers.httpx.AsyncClient', make_client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def handle(self, request):
        self.requests.append(request)
        if self.response_override:
            return self.response_override(request)
        host = request.url.host
        if host == 'generativelanguage.googleapis.com':
            return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'inlineData': {'mimeType': 'audio/L16;codec=pcm;rate=24000', 'data': base64.b64encode(PCM).decode()}}]}}]})
        if host.startswith('dashscope'):
            return httpx.Response(200, json={'output': {'audio': {'url': 'https://test.oss-cn-beijing.aliyuncs.com/audio?signature=private'}}})
        if host == 'test.oss-cn-beijing.aliyuncs.com':
            return httpx.Response(200, content=WAV)
        if host == 'api.minimax.io':
            return httpx.Response(200, json={'base_resp': {'status_code': 0}, 'data': {'audio': MP3.hex()}})
        if host == 'api.xiaomimimo.com':
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'audio': {'data': base64.b64encode(WAV).decode()}}}]})
        if host == 'openspeech.bytedance.com':
            return httpx.Response(200, stream=FragmentedResponse())
        if host in {'api.stepfun.com', 'api.stepfun.ai', 'api.fish.audio'}:
            return httpx.Response(200, content=WAV)
        if host == 'texttospeech.googleapis.com':
            return httpx.Response(200, json={'audioContent': base64.b64encode(MP3).decode()})
        if host.endswith('speech.microsoft.com'):
            return httpx.Response(200, content=WAV)
        return httpx.Response(200, content=MP3)

    def payload(self, index=0):
        return json.loads(self.requests[index].content)

    async def test_openai_payload_and_header(self):
        result = await synthesize('openai', '同一文本', {'instructions': '自然平静', 'speed': 1.2})
        body = self.payload()
        self.assertEqual(body['input'], '同一文本')
        self.assertEqual(body['instructions'], '自然平静')
        self.assertEqual(body['response_format'], 'mp3')
        self.assertEqual(body['speed'], 1.2)
        self.assertTrue(self.requests[0].headers['Authorization'].startswith('Bearer '))
        self.assertEqual(result.extension, 'mp3')
        self.assertEqual(result.metadata['latency_kind'], 'request_completion_not_first_audio')

    async def test_gemini_pcm_wav(self):
        result = await synthesize('gemini', 'Hello', {})
        self.assertEqual(self.payload()['generationConfig']['responseModalities'], ['AUDIO'])
        self.assertEqual(self.payload()['generationConfig']['speechConfig']['voiceConfig']['prebuiltVoiceConfig']['voiceName'], 'Kore')
        self.assertIn('x-goog-api-key', self.requests[0].headers)
        self.assertNotIn('key=', str(self.requests[0].url))
        with wave.open(io.BytesIO(result.audio)) as r:
            self.assertEqual((r.getframerate(), r.getnchannels(), r.getsampwidth()), (24000, 1, 2))
            self.assertEqual(r.readframes(r.getnframes()), PCM)

    async def test_gemini_empty_audio_rejected(self):
        self.response_override = lambda r: httpx.Response(200, json={'candidates': []})
        with self.assertRaises(ProviderError):
            await synthesize('gemini', 'Hello', {})

    async def test_qwen_download_without_auth(self):
        result = await synthesize('qwen', '你好', {})
        self.assertEqual(self.payload()['input'], {'text': '你好', 'voice': 'Cherry', 'language_type': 'Auto'})
        self.assertIn('authorization', self.requests[0].headers)
        self.assertNotIn('authorization', self.requests[1].headers)
        self.assertEqual(self.requests[1].method, 'GET')
        self.assertNotIn('signature', json.dumps(result.metadata))
        self.assertEqual(result.extension, 'wav')

    async def test_qwen_unsafe_urls_rejected(self):
        for url in ['http://test.aliyuncs.com/a', 'https://evil.example/a', 'https://aliyuncs.com.evil.example/a', 'https://user:password@a.aliyuncs.com/a', 'https://a.aliyuncs.com:8443/a', 'https://127.0.0.1/a']:
            with self.subTest(url=url):
                self.requests.clear()
                self.response_override = lambda r, u=url: httpx.Response(200, json={'output': {'audio': {'url': u}}})
                with self.assertRaises(ProviderError):
                    await synthesize('qwen', 'hello', {})
                self.assertEqual(len(self.requests), 1)

    async def test_qwen_download_redirect_not_followed(self):
        def respond(r):
            if r.url.host.startswith('dashscope'):
                return httpx.Response(200, json={'output': {'audio': {'url': 'https://a.aliyuncs.com/audio'}}})
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        self.response_override = respond
        with self.assertRaises(ProviderError):
            await synthesize('qwen', 'hello', {})
        self.assertEqual(len(self.requests), 2)

    async def test_minimax_hex_payload(self):
        result = await synthesize('minimax', '你好', {'emotion': 'happy', 'pitch': 2})
        body = self.payload()
        self.assertEqual(body['output_format'], 'hex')
        self.assertFalse(body['stream'])
        self.assertEqual(body['voice_setting']['pitch'], 2)
        self.assertEqual(body['voice_setting']['emotion'], 'happy')
        self.assertEqual(body['audio_setting']['format'], 'mp3')
        self.assertEqual(result.audio, MP3)

    async def test_minimax_embedded_error_sanitized(self):
        self.response_override = lambda r: httpx.Response(200, json={'base_resp': {'status_code': 1008, 'status_msg': 'secret-token-should-not-leak'}})
        with self.assertRaises(ProviderError) as caught:
            await synthesize('minimax', 'hello', {})
        self.assertIn('1008', str(caught.exception))
        self.assertNotIn('secret-token', str(caught.exception))

    async def test_mimo_roles_format_and_audio(self):
        result = await synthesize('mimo', '只朗读这句话', {'instructions': '平静、慢速'})
        self.assertEqual(self.payload()['messages'], [
            {'role': 'user', 'content': '平静、慢速'}, {'role': 'assistant', 'content': '只朗读这句话'}])
        self.assertEqual(self.payload()['audio'], {'format': 'wav', 'voice': '冰糖'})
        self.assertFalse(self.payload()['stream'])
        self.assertEqual(str(self.requests[0].url), 'https://api.xiaomimimo.com/v1/chat/completions')
        self.assertEqual(self.requests[0].headers['api-key'], ENV['MIMO_API_KEY'])
        self.assertEqual(result.audio, WAV)

    async def test_mimo_voice_design_omits_preset_voice(self):
        await synthesize('mimo', '你好', {'model': 'mimo-v2.5-tts-voicedesign', 'instructions': '温柔的青年女声'})
        self.assertNotIn('voice', self.payload()['audio'])
        self.assertEqual(self.payload()['messages'][0]['content'], '温柔的青年女声')
        await synthesize('mimo', '你好', {})
        self.assertEqual(self.payload(1)['messages'], [{'role': 'assistant', 'content': '你好'}])

    async def test_mimo_missing_invalid_and_truncated_audio_rejected(self):
        for response in [{'error': {'message': 'private-secret'}}, {'choices': []},
                         {'choices': [{'finish_reason': 'stop', 'message': {}}]},
                         {'choices': [{'finish_reason': 'stop', 'message': {'audio': {'data': 'invalid!'}}}]},
                         {'choices': [{'finish_reason': 'length', 'message': {'audio': {'data': base64.b64encode(WAV).decode()}}}]}]:
            with self.subTest(response=response):
                self.requests.clear()
                self.response_override = lambda r, body=response: httpx.Response(200, json=body)
                with self.assertRaises(ProviderError) as caught:
                    await synthesize('mimo', '你好', {})
                self.assertNotIn('private-secret', str(caught.exception))
                self.assertEqual(len(self.requests), 1)

    async def test_doubao_chunk_assembly_and_voice_controls(self):
        result = await synthesize('doubao', 'hello', {'instructions': '自然、轻快', 'speech_rate': 20, 'loudness_rate': -10})
        req = self.payload()['req_params']
        self.assertEqual(req['text'], 'hello')
        self.assertEqual(req['speaker'], 'zh_female_vv_uranus_bigtts')
        self.assertEqual(req['audio_params'], {'format': 'mp3', 'sample_rate': 24000, 'speech_rate': 20, 'loudness_rate': -10})
        self.assertEqual(json.loads(req['additions']), {'context_texts': ['自然、轻快']})
        request = self.requests[0]
        self.assertEqual(str(request.url), 'https://openspeech.bytedance.com/api/v3/tts/unidirectional')
        self.assertEqual(request.headers['X-Api-Resource-Id'], 'seed-tts-2.0')
        self.assertEqual(request.headers['X-Api-Key'], ENV['VOLCENGINE_TTS_API_KEY'])
        self.assertTrue(request.headers['X-Api-Request-Id'])
        self.assertEqual(result.audio, MP3)

    async def test_doubao_existing_cloned_voice(self):
        await synthesize('doubao', 'hello', {'resource_id': 'seed-icl-2.0', 'voice': 'S_existing_voice'})
        self.assertEqual(self.requests[0].headers['X-Api-Resource-Id'], 'seed-icl-2.0')
        self.assertEqual(self.payload()['req_params']['speaker'], 'S_existing_voice')
        self.assertNotIn('additions', self.payload()['req_params'])

    async def test_doubao_partial_error_and_invalid_streams_rejected(self):
        audio = json.dumps({'code': 0, 'data': base64.b64encode(MP3).decode()})
        terminal = json.dumps({'code': 20000000})
        for response in [audio, terminal, audio + '\n' + json.dumps({'code': 45000000, 'message': 'private-secret'}),
                         'not-json', '[]', json.dumps({'code': True}),
                         json.dumps({'code': 0, 'data': 'bad!'}) + '\n' + terminal,
                         audio + '\n' + terminal + '\n' + audio]:
            with self.subTest(response=response):
                self.requests.clear()
                self.response_override = lambda r, body=response: httpx.Response(200, content=body)
                with self.assertRaises(ProviderError) as caught:
                    await synthesize('doubao', 'hello', {})
                self.assertNotIn('private-secret', str(caught.exception))
                self.assertEqual(len(self.requests), 1)

    async def test_elevenlabs_settings_optional(self):
        await synthesize('elevenlabs', 'hello', {'stability': .3})
        self.assertEqual(self.payload()['voice_settings']['stability'], .3)
        self.assertIn('xi-api-key', self.requests[0].headers)
        self.assertEqual(self.requests[0].url.params['output_format'], 'mp3_44100_128')
        await synthesize('elevenlabs', 'hello', {'use_voice_settings': False})
        self.assertNotIn('voice_settings', self.payload(1))

    async def test_stepfun_official_payload_regions_and_audio(self):
        result = await synthesize('stepfun', '重庆的银行', {'instructions': '轻快', 'speed': .8, 'volume': 1.2})
        body = self.payload()
        self.assertEqual(body['input'], '重庆的银行')
        self.assertEqual(body['instruction'], '轻快')
        self.assertEqual(body['model'], 'stepaudio-3-tts')
        self.assertEqual(body['language'], 'zh')
        self.assertEqual(body['response_format'], 'wav')
        self.assertFalse(body['return_url'])
        self.assertEqual(body['speed'], .8)
        self.assertEqual(body['volume'], 1.2)
        self.assertEqual(self.requests[0].url.host, 'api.stepfun.com')
        self.assertEqual(self.requests[0].headers['Authorization'], 'Bearer ' + ENV['STEP_API_KEY'])
        self.assertEqual(result.audio, WAV)
        await synthesize('stepfun', 'hello', {'region': 'international', 'model': 'stepaudio-2.5-tts'})
        self.assertEqual(self.requests[1].url.host, 'api.stepfun.ai')
        self.assertNotIn('instruction', self.payload(1))

    async def test_fish_header_selects_model_and_existing_voice(self):
        result = await synthesize('fish', '你好', {'voice': 'my-existing-voice', 'model': 's2-pro', 'normalize': False})
        self.assertEqual(str(self.requests[0].url), 'https://api.fish.audio/v1/tts')
        self.assertEqual(self.requests[0].headers['model'], 's2-pro')
        self.assertEqual(self.requests[0].headers['Authorization'], 'Bearer ' + ENV['FISH_AUDIO_API_KEY'])
        self.assertEqual(self.payload(), {'text': '你好', 'reference_id': 'my-existing-voice', 'format': 'wav',
                                         'temperature': .7, 'top_p': .7, 'normalize': False})
        self.assertEqual(result.audio, WAV)

    async def test_new_cloud_json_failures_never_become_audio_or_retry(self):
        self.response_override = lambda r: httpx.Response(200, json={'message': 'private-secret'})
        for provider, params in [('stepfun', {}), ('fish', {'voice': 'existing-voice'})]:
            self.requests.clear()
            with self.assertRaises(ProviderError) as caught:
                await synthesize(provider, 'hello', params)
            self.assertNotIn('private-secret', str(caught.exception))
            self.assertEqual(len(self.requests), 1)

    async def test_azure_safe_ssml(self):
        text = '<voice name="evil"> & 你好'
        await synthesize('azure', text, {'style': 'cheerful', 'rate': 20, 'pitch': -5})
        request = self.requests[0]
        root = ElementTree.fromstring(request.content)
        self.assertEqual(''.join(root.itertext()), text)
        self.assertIn('eastus.tts.speech.microsoft.com', str(request.url))
        self.assertEqual(request.headers['X-Microsoft-OutputFormat'], 'riff-24khz-16bit-mono-pcm')
        self.assertIn(b'rate="+20%"', request.content)
        self.assertIn(b'pitch="-5%"', request.content)

    async def test_google_cloud_payload(self):
        result = await synthesize('google', 'hello', {'pitch': 3, 'speaking_rate': 1.1})
        self.assertEqual(self.payload()['audioConfig']['pitch'], 3)
        self.assertEqual(self.payload()['audioConfig']['speakingRate'], 1.1)
        self.assertIn('x-goog-user-project', self.requests[0].headers)
        self.assertEqual(result.audio, MP3)

    async def test_http_errors_not_retried_or_leaked(self):
        self.response_override = lambda r: httpx.Response(401, text='private key from vendor')
        with self.assertRaises(ProviderError) as caught:
            await synthesize('openai', 'hello', {})
        self.assertIn('401', str(caught.exception))
        self.assertNotIn('private key', str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    async def test_json_not_accepted_as_audio(self):
        self.response_override = lambda r: httpx.Response(200, json={'error': 'do not play this'})
        with self.assertRaises(ProviderError):
            await synthesize('openai', 'hello', {})

    async def test_demo_valid_wav_without_network(self):
        result = await synthesize('demo', 'not spoken', {'duration': .5})
        self.assertTrue(result.metadata['is_demo'])
        with wave.open(io.BytesIO(result.audio)) as r:
            self.assertEqual(r.getnframes(), 12000)
        self.assertEqual(self.requests, [])

    async def test_polly_mocked_sdk(self):
        stream = MagicMock()
        stream.read.return_value = MP3
        client = MagicMock()
        client.synthesize_speech.return_value = {'AudioStream': stream}
        boto = types.ModuleType('boto3')
        boto.client = MagicMock(return_value=client)
        config_module = types.ModuleType('botocore.config')
        config_module.Config = MagicMock()
        with patch.dict(sys.modules, {'boto3': boto, 'botocore': types.ModuleType('botocore'), 'botocore.config': config_module}):
            result = await synthesize('polly', 'hello', {})
        self.assertEqual(result.audio, MP3)
        client.synthesize_speech.assert_called_once_with(Text='hello', TextType='text', OutputFormat='mp3', VoiceId='Joanna', Engine='neural', SampleRate='24000')
        self.assertEqual(config_module.Config.call_args.kwargs['retries'], {'total_max_attempts': 1})
        stream.close.assert_called_once()
        client.close.assert_called_once()


class ValidationTests(unittest.TestCase):
    def test_defaults_for_all(self):
        for c in CATALOG:
            self.assertEqual(set(validate_params(c['id'], {})), {f['key'] for f in c['fields']})

    def test_new_cloud_limits_and_model_fallback_prevention(self):
        for provider, params, text in [('stepfun', {}, '中' * 1001),
                 ('stepfun', {'instructions': 'a' * 501}, 'hello'),
                 ('stepfun', {'model': 'stepaudio-2.5-tts', 'instructions': 'a' * 201}, 'hello'),
                 ('fish', {}, 'hello'), ('fish', {'model': 'unknown', 'voice': 'voice'}, 'hello')]:
            with self.subTest(provider=provider, params=params), self.assertRaises(ProviderError):
                validate_params(provider, params, text)

    def test_unknown_range_and_boolean_rejected(self):
        for provider, params in [('demo', {'frequency': True}), ('demo', {'frequency': 9999}), ('demo', {'frequency': float('nan')}), ('demo', {'unknown': 1}), ('elevenlabs', {'speaker_boost': 1}), ('openai', {'format': 'invalid'}), ('minimax', {'pitch': 1.5}), ('openai', {'voice': ''})]:
            with self.subTest(provider=provider, params=params), self.assertRaises(ProviderError):
                validate_params(provider, params, 'hello')

    def test_text_limits(self):
        for provider, text in [('demo', ''), ('demo', 'a' * 2001), ('qwen', '中' * 601), ('google', '中' * 1800), ('azure', '\x00')]:
            with self.subTest(provider=provider), self.assertRaises(ProviderError):
                validate_params(provider, {}, text)

    def test_mimo_design_and_doubao_model_voice_compatibility(self):
        for provider, params in [('mimo', {'model': 'mimo-v2.5-tts-voicedesign'}),
                                 ('mimo', {'model': 'mimo-v2.5-tts-voiceclone'}),
                                 ('doubao', {'resource_id': 'seed-icl-2.0'}),
                                 ('doubao', {'resource_id': 'seed-icl-2.0', 'voice': 'S_example', 'instructions': '开心'}),
                                 ('doubao', {'speech_rate': 1.5})]:
            with self.subTest(provider=provider, params=params), self.assertRaises(ProviderError):
                validate_params(provider, params, 'hello')

    def test_public_catalog_no_credentials_and_warning(self):
        with patch.dict(os.environ, ENV):
            catalog = public_catalog()
        self.assertNotIn('test-secret-not-for-browser', json.dumps(catalog))
        self.assertTrue(next(c for c in catalog if c['id'] == 'demo')['ready'])
        self.assertIn('2027-01-06', next(c for c in catalog if c['id'] == 'openai')['warning'])

    def test_audio_size_limit(self):
        with self.assertRaises(ProviderError):
            _audio(b'ID3' + b'\0' * (20 * 1024 * 1024))


if __name__ == '__main__':
    unittest.main()
