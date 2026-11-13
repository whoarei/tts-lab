"""Reference import, private routing, and local worker contracts; no real voices sent."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import wave

import httpx
from tts_lab import app as application, local_models as models, reference_audio as refs, local_worker
from tts_lab.providers import _wav, validate_params, ProviderError, synthesize

WAV = _wav(b'\0\1' * (24000 * 3))


class ReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.patchers = [patch.object(models, 'model_root', return_value=self.folder / 'models'),
                         patch.object(application, 'DATA_DIR', self.folder),
                         patch.object(application, 'JOBS', {}), patch.object(application, 'TASKS', set())]
        for p in self.patchers:
            p.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app), base_url='http://127.0.0.1:8765')
        self.headers = {'X-Local-Token': application.TOKEN, 'Content-Type': 'audio/wav'}

    async def asyncTearDown(self):
        if application.TASKS:
            await asyncio.gather(*application.TASKS)
        await self.client.aclose()
        for p in reversed(self.patchers):
            p.stop()
        self.temp.cleanup()

    async def test_import_preview_and_state_keep_paths_private(self):
        result = await self.client.post('/api/references?name=C%3A%5Cprivate%5Cvoice.wav', content=WAV, headers=self.headers)
        self.assertEqual(result.status_code, 201, result.text)
        item = result.json()
        self.assertEqual(item['name'], 'voice.wav')
        self.assertEqual(item['duration_seconds'], 3)
        self.assertNotIn(str(self.folder), result.text)
        self.assertEqual((await self.client.get('/api/references/' + item['id'] + '/audio')).content, WAV)
        state = (await self.client.get('/api/state')).json()
        self.assertEqual(state['references'], [item])
        refs.path_for(item['id']).write_bytes(b'damaged')
        self.assertEqual((await self.client.get('/api/state')).json()['references'], [])
        self.assertEqual((await self.client.get('/api/references/' + item['id'] + '/audio')).status_code, 404)

    async def test_import_limits_token_and_wave_integrity(self):
        self.assertEqual((await self.client.post('/api/references', content=WAV)).status_code, 403)
        self.assertEqual((await self.client.post('/api/references', content=b'a' * (refs.MAX_REFERENCE + 1), headers=self.headers)).status_code, 413)
        for invalid in [b'not a wave', WAV[:100], _wav(b'\0\1' * 24000)]:
            result = await self.client.post('/api/references', content=invalid, headers=self.headers)
            self.assertEqual(result.status_code, 422, result.text)
        for invalid_id in ['../private', str(self.folder / 'private.wav'), 'a' * 32]:
            with self.assertRaises(refs.ReferenceError):
                refs.path_for(invalid_id)

    async def test_local_reference_validation_and_private_worker_request(self):
        ref = refs.save(WAV, 'voice.wav')
        for provider in ['local_indextts', 'local_glmtts']:
            with self.assertRaises(ProviderError):
                validate_params(provider, {}, 'hello')
            params = {'reference_id': ref['id']}
            if provider == 'local_glmtts':
                with self.assertRaises(ProviderError):
                    validate_params(provider, params, 'hello')
                params['reference_text'] = '你好，欢迎。'
            p = validate_params(provider, params, 'hello')
            with patch.object(models, 'command', new=AsyncMock()) as command:
                await models.run_worker(p['model'], 'cuda' if provider == 'local_glmtts' else 'cpu', p, 'hello', self.folder / 'out.wav')
            request = json.loads(command.call_args.kwargs['stdin'])
            self.assertEqual(Path(request['reference_audio']), refs.path_for(ref['id']))
            self.assertEqual(request['params']['reference_id'], ref['id'])
            if provider == 'local_glmtts':
                self.assertEqual(Path(request['model_dir']).parts[-2:], ('source', 'ckpt'))
            self.assertEqual(command.call_args.kwargs['env']['HF_HUB_OFFLINE'], '1')
            with patch.object(models, 'synthesize_local', new=AsyncMock(return_value=WAV)):
                result = await synthesize(provider, 'hello', p)
            self.assertEqual(result.metadata['reference_sha256'], ref['sha256'])

    async def test_local_reference_batch_requires_valid_reference_before_generation(self):
        spec = next(p for p in application.public_catalog() if p['id'] == 'local_indextts')
        spec.update(ready=True, installed_models=['indextts-2'])
        with patch.object(application, 'public_catalog', return_value=[spec]), \
             patch.object(application, 'synthesize', new=AsyncMock()) as generate:
            result = await self.client.post('/api/batches', headers={'X-Local-Token': application.TOKEN},
                json={'text': 'hello', 'variants': [{'provider': 'local_indextts', 'params': {'reference_id': 'a' * 32}}]})
            self.assertEqual(result.status_code, 422)
            generate.assert_not_called()

    async def test_missing_install_sample_fails_before_loading_model(self):
        with patch.object(models, 'command', new=AsyncMock()) as command:
            with self.assertRaises(models.LocalModelError):
                await models.run_worker('indextts-2', 'cpu', {}, 'hello', self.folder / 'out.wav', reference=self.folder / 'missing.wav')
            command.assert_not_called()

    def test_index_worker_uses_local_aux_paths_and_no_automatic_download(self):
        import types
        torch = types.ModuleType('torch')
        torch.manual_seed = MagicMock()
        module = types.ModuleType('indextts.infer_v2')
        module.IndexTTS2 = MagicMock()
        with patch.dict('sys.modules', {'torch': torch, 'indextts': types.ModuleType('indextts'), 'indextts.infer_v2': module}):
            local_worker.generate_index({'model_dir': str(self.folder), 'params': {'seed': 42, 'temperature': .8, 'top_p': .7},
                'device': 'cpu', 'reference_audio': 'private.wav', 'text': 'hello', 'output': 'out.wav'})
        args = module.IndexTTS2.call_args.kwargs
        self.assertFalse(args['use_qwen_emo'])
        self.assertFalse(args['use_cuda_kernel'])
        self.assertEqual(args['device'], 'cpu')
        self.assertTrue(all(str(self.folder / 'aux') in p for p in args['aux_paths'].values()))
        self.assertEqual(module.IndexTTS2.return_value.infer.call_args.kwargs['spk_audio_prompt'], 'private.wav')

    def test_glm_passes_reference_transcript_features_and_sample_rate(self):
        import types
        torch = types.ModuleType('torch')
        torch.tensor, torch.int32 = MagicMock(), object()
        glm = types.ModuleType('glmtts_inference')
        frontend, text_frontend, llm, flow, audio = [MagicMock() for _ in range(5)]
        text_frontend.text_normalize.side_effect = lambda text: text
        frontend._extract_speech_token.return_value.squeeze.return_value.tolist.return_value = [1, 2, 3]
        glm.load_models = MagicMock(return_value=(frontend, text_frontend, object(), llm, flow))
        glm.generate_long = MagicMock(return_value=(audio, None, None, None))
        glm.DEVICE = 'cuda'
        with patch.dict('sys.modules', {'torch': torch, 'glmtts_inference': glm}):
            local_worker.generate_glm({'reference_audio': 'private.wav', 'text': 'new text',
                'params': {'reference_text': 'reference transcript', 'seed': 42, 'sample_rate': '32000'}})
        glm.load_models.assert_called_once_with(use_phoneme=False, sample_rate=32000)
        frontend._extract_speech_feat.assert_called_once_with('private.wav', sample_rate=32000)
        call = glm.generate_long.call_args.kwargs
        self.assertEqual(call['cache']['cache_text'], ['reference transcript '])
        self.assertEqual(call['text_info'], ['tts-lab', 'new text'])
        self.assertEqual(call['seed'], 42)
        self.assertFalse(call['use_phoneme'])

    def test_windows_glm_normalizer_imports_are_portable(self):
        import sys
        import types
        module = types.ModuleType('wetext')
        module.Normalizer = MagicMock()
        with patch.dict(sys.modules, {'wetext': module}), patch.object(local_worker.sys, 'platform', 'win32'):
            local_worker.glm_windows_normalizers()
            from tn.chinese.normalizer import Normalizer as ChineseNormalizer
            from tn.english.normalizer import Normalizer as EnglishNormalizer
            ChineseNormalizer(remove_erhua=False, overwrite_cache=True).normalize('你好')
            EnglishNormalizer().normalize('hello')
            self.assertEqual(module.Normalizer.call_args_list[0].kwargs, {'lang': 'zh', 'operator': 'tn', 'remove_erhua': False})
            self.assertEqual(module.Normalizer.call_args_list[1].kwargs, {'lang': 'en', 'operator': 'tn'})
