"""Download integrity, lifecycle, consent, and local inference routing; no model downloads."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from tts_lab import app as application
from tts_lab import local_models as m
from tts_lab.catalog import public_catalog
from tts_lab.providers import _wav, synthesize, validate_params, ProviderError

WAV = _wav(b'\x00\x01' * 2400)


class LocalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patchers = [patch.object(m, 'model_root', return_value=self.root),
                         patch.object(m, 'TASKS', {}), patch.object(m, 'INFERENCE', asyncio.Lock())]
        for p in self.patchers:
            p.start()

    async def asyncTearDown(self):
        await m.shutdown()
        for p in reversed(self.patchers):
            p.stop()
        self.temp.cleanup()

    def ready(self, model='kokoro-v1.1', device='cpu'):
        folder = self.root / model
        m.write_json(folder / 'model' / 'config.json', {'test': True})
        python = m.runtime_python(m.MODELS[model]['engine'], device)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.touch()
        m.write_json(python.parent.parent / 'ready.json', {'schema': 1,
                     'signature':m.runtime_signature(m.MODELS[model]['engine'],device)})
        m.write_json(folder / 'ready.json', {'device': device, 'files': {'model/config.json': (folder / 'model/config.json').stat().st_size}})
        return folder

    async def test_catalog_requires_complete_files_and_runtime(self):
        self.assertFalse(m.status('kokoro-v1.1')['ready'])
        folder = self.ready()
        self.assertTrue(m.status('kokoro-v1.1')['ready'])
        provider = next(p for p in public_catalog() if p['id'] == 'local_kokoro')
        self.assertTrue(provider['ready'])
        self.assertEqual(provider['installed_models'], ['kokoro-v1.1'])
        (folder / 'model/config.json').write_text('truncated')
        self.assertFalse(m.status('kokoro-v1.1')['ready'])

    async def test_readiness_does_not_fall_back_to_another_model(self):
        self.ready('qwen3-1.7b')
        provider = next(p for p in public_catalog() if p['id'] == 'local_qwen3')
        self.assertEqual(provider['fields'][0]['default'], 'qwen3-1.7b')
        with self.assertRaises(m.LocalModelError):
            await m.synthesize_local('qwen3', 'hello', {'model':'qwen3-0.6b'})
        with self.assertRaises(m.LocalModelError):
            await m.synthesize_local('kokoro', 'hello', {'model':'qwen3-1.7b'})

    async def test_download_hash_and_retry_reuse(self):
        requests=[]
        def respond(request):
            requests.append(request)
            return httpx.Response(200, content=b'valid model')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
            path = self.root / 'file.bin'
            digest = hashlib.sha256(b'valid model').hexdigest()
            for _ in range(2):
                await m.download(c, 'https://huggingface.co/model/file', path, digest, 'kokoro-v1.1')
            self.assertEqual(len(requests), 1)
            path.write_bytes(b'damaged')
            await m.download(c, 'https://huggingface.co/model/file', path, digest, 'kokoro-v1.1')
            self.assertEqual(len(requests), 2)
            self.assertEqual(path.read_bytes(), b'valid model')

    async def test_corrupt_or_oversized_download_never_replaces_file(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'damaged'))) as c:
            target = self.root / 'file.bin'
            for options in [{}, {'expected_size':2}]:
                with self.assertRaises(m.LocalModelError):
                    await m.download(c, 'https://huggingface.co/model/file', target, '0'*64, 'kokoro-v1.1', **options)
                self.assertFalse(target.exists())
                self.assertFalse(target.with_name('file.bin.partial').exists())

    async def test_redirects_never_reach_local_or_untrusted_hosts(self):
        requests=[]
        def redirect(request):
            requests.append(request)
            return httpx.Response(302, headers={'location':'http://127.0.0.1/private'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as c:
            with self.assertRaises(m.LocalModelError):
                await m.response(c, 'https://huggingface.co/model/file')
        self.assertEqual(len(requests), 1)
        for url in ['http://huggingface.co/a', 'https://huggingface.co.evil/a',
                    'https://user:secret@huggingface.co/a', 'https://127.0.0.1/a']:
            self.assertFalse(m.allowed_download(url))

    async def test_hub_snapshot_pins_commit_and_verifies_git_and_lfs(self):
        revision = 'a'*40
        text, weight = b'{}', b'weights'
        meta = {'sha':revision,'siblings':[
            {'rfilename':'config.json','size':len(text),'blobId':hashlib.sha1(b'blob 2\0'+text).hexdigest()},
            {'rfilename':'model.safetensors','size':len(weight),
             'lfs':{'size':len(weight),'sha256':hashlib.sha256(weight).hexdigest()}}]}
        requests=[]
        def respond(request):
            requests.append(request)
            if '/api/models/' in request.url.path:
                return httpx.Response(200,json=meta)
            self.assertIn('/resolve/'+revision+'/', request.url.path)
            return httpx.Response(200,content=text if request.url.path.endswith('config.json') else weight)
        with patch.object(m.shutil, 'disk_usage', return_value=type('Disk',(),{'free':30*1024**3})()):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
                await m.download_hub(c,'qwen3-0.6b',self.root/'qwen3-0.6b/model')
                await m.download_hub(c,'qwen3-0.6b',self.root/'qwen3-0.6b/model')
        self.assertEqual(len(requests),3)
        self.assertEqual(m.read_json(self.root/'qwen3-0.6b/model/config.json'),{})
        self.assertEqual(m.read_json(self.root/'qwen3-0.6b/model/snapshot.json')['revision'],revision)

    async def test_archive_rejects_traversal_symlink_and_windows_paths(self):
        for name, kind in [('root/../../escape',tarfile.REGTYPE),('root/link',tarfile.SYMTYPE),('C:/escape',tarfile.REGTYPE)]:
            archive=self.root/'test.tar'
            with tarfile.open(archive,'w') as t:
                info=tarfile.TarInfo(name); info.type=kind; info.linkname='/tmp/escape'
                t.addfile(info)
            with self.assertRaises(m.LocalModelError):
                m.extract_archive(archive,self.root/'extracted')

    async def test_auxiliary_download_allowlist_excludes_unneeded_weights(self):
        content = b'codec'
        revision = 'a' * 40
        metadata = {'sha': revision, 'siblings': [
            {'rfilename': name, 'size': len(content), 'lfs': {'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)}}
            for name in ['semantic_codec/model.safetensors', 'huge_other_model.pth']]}
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=metadata) if '/api/models/' in request.url.path else httpx.Response(200, content=content)
        with patch.object(m.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 30 * 1024**3})()):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                await m.download_hub(client, 'indextts-2', self.root / 'aux', repo='amphion/MaskGCT', filenames=['semantic_codec/model.safetensors'])
        self.assertEqual(len(requests), 2)
        self.assertTrue(requests[-1].url.path.endswith('/semantic_codec/model.safetensors'))
        self.assertFalse((self.root / 'aux' / 'huge_other_model.pth').exists())

    async def test_new_weight_formats_and_only_documented_tokenizer_code_download(self):
        names = ['weights.pth', 'generator.ckpt', 'vq32k-phoneme-tokenizer/tokenization_chatglm.py', 'unrequested.py']
        metadata = {'sha': 'a' * 40, 'siblings': [
            {'rfilename': name, 'size': 1, 'blobId': hashlib.sha1(b'blob 1\0x').hexdigest()} for name in names]}
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=metadata) if '/api/models/' in request.url.path else httpx.Response(200, content=b'x')
        with patch.object(m.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 30 * 1024**3})()):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                await m.download_hub(client, 'glm-tts', self.root / 'weights')
        self.assertEqual(len(requests), 4)
        self.assertFalse((self.root / 'weights' / 'unrequested.py').exists())
        for name in names[:-1]:
            self.assertTrue((self.root / 'weights' / name).is_file())

    async def test_restarted_install_and_missing_runtime_not_ready(self):
        m._update('kokoro-v1.1',state='downloading',message='old')
        self.assertEqual(m.status('kokoro-v1.1')['state'],'interrupted')
        self.ready()
        m.runtime_python('kokoro','cpu').unlink()
        self.assertFalse(m.status('kokoro-v1.1')['ready'])

    async def test_cancel_install_releases_single_download_slot(self):
        entered=asyncio.Event()
        async def install(*args):
            entered.set()
            await asyncio.sleep(60)
        with patch.object(m,'install',side_effect=install):
            m.start_install('kokoro-v1.1','cpu')
            await entered.wait()
            with self.assertRaises(m.LocalModelError): m.start_install('qwen3-0.6b','cpu')
            await m.cancel_install('kokoro-v1.1')
            self.assertTrue(m.TASKS['kokoro-v1.1'].cancelled())
            m.start_install('qwen3-0.6b','cpu')
            await m.cancel_install('qwen3-0.6b')

    async def test_invalid_model_and_device_do_not_start_downloads(self):
        for model,device in [('../private','cpu'),('kokoro-v1.1','cuda'),('qwen3-0.6b','invalid'),('glm-tts','cpu')]:
            with self.assertRaises(m.LocalModelError): m.start_install(model,device)
        self.assertFalse(m.TASKS)

    async def test_remove_preserves_history_and_shared_runtime(self):
        self.ready()
        history=self.root/'history.wav'; history.write_bytes(WAV)
        await m.remove_model('kokoro-v1.1')
        self.assertFalse((self.root/'kokoro-v1.1').exists())
        self.assertTrue(m.runtime_python('kokoro','cpu').exists())
        self.assertEqual(history.read_bytes(),WAV)

    async def test_offline_subprocess_environment_does_not_inherit_provider_keys(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY':'private','AZURE_SPEECH_KEY':'private','AWS_PROFILE':'private','GH_TOKEN':'private'}):
            env=m.command_env(offline=True)
        for name in ['OPENAI_API_KEY','AZURE_SPEECH_KEY','AWS_PROFILE','GH_TOKEN']: self.assertNotIn(name,env)
        self.assertEqual(env['HF_HUB_OFFLINE'],'1')
        self.assertEqual(env['CUDA_VISIBLE_DEVICES'],'')

    async def test_embedded_windows_uv_executable_discovery(self):
        executable=self.root/'Lib/site-packages/_wheel_data/uv-0.9.9.data/scripts/uv.exe'
        executable.parent.mkdir(parents=True)
        executable.touch()
        with patch.object(m.sys,'executable',str(self.root/'python.exe')), \
             patch('uv.find_uv_bin',side_effect=FileNotFoundError):
            self.assertEqual(m.uv_binary(),str(executable))

    async def test_process_cancel_terminates_worker(self):
        pid=self.root/'worker.pid'
        code='import os,time;from pathlib import Path;Path('+repr(str(pid))+').write_text(str(os.getpid()));time.sleep(60)'
        task=asyncio.create_task(m.command([sys.executable,'-c',code],env=m.command_env()))
        for _ in range(100):
            if pid.exists(): break
            await asyncio.sleep(.02)
        self.assertTrue(pid.exists())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        if os.name != 'nt':
            with self.assertRaises(ProcessLookupError): os.kill(int(pid.read_text()),0)

    async def test_provider_adapters_route_all_local_engines_without_http(self):
        for engine in ['kokoro','qwen3','cosyvoice']:
            with patch.object(m,'synthesize_local',new=AsyncMock(return_value=WAV)) as worker:
                result=await synthesize('local_'+engine,'你好',{})
            self.assertEqual(result.audio,WAV)
            self.assertTrue(result.metadata['is_local'])
            self.assertEqual(worker.call_args.args[:2],(engine,'你好'))

    async def test_qwen_small_rejects_unsupported_instructions(self):
        with self.assertRaises(ProviderError):
            validate_params('local_qwen3',{'instructions':'cheerful'},'hello')
        for engine in ['kokoro','qwen3','cosyvoice']:
            with self.assertRaises(ProviderError):
                validate_params('local_'+engine,{},'中'*301)

    async def test_install_requires_successful_offline_smoke(self):
        spec=m.MODELS['kokoro-v1.1']
        folder=self.ready()
        python=m.runtime_python('kokoro','cpu')
        async def download(*args,**kwargs): pass
        async def extract(*args,**kwargs):
            for name in spec['required']:
                p=folder/'model'/name
                if name=='espeak-ng-data': p.mkdir(exist_ok=True)
                else: p.write_bytes(b'model')
        async def worker(model,device,params,text,output,python):
            self.assertFalse((folder/'ready.json').exists())
            output.write_bytes(WAV)
        with patch.object(m,'prepare_runtime',new=AsyncMock(return_value=python)), \
             patch.object(m,'download',side_effect=download), patch.object(m,'extract',side_effect=extract), \
             patch.object(m,'run_worker',side_effect=worker), \
             patch.object(m.shutil,'disk_usage',return_value=type('Disk',(),{'free':30*1024**3})()):
            await m.install('kokoro-v1.1','cpu')
            self.assertTrue(m.status('kokoro-v1.1')['ready'])
            with patch.object(m,'run_worker',new=AsyncMock(side_effect=m.LocalModelError('推理失败'))):
                await m.install('kokoro-v1.1','cpu')
            self.assertFalse(m.status('kokoro-v1.1')['ready'])
            self.assertEqual(m.status('kokoro-v1.1')['state'],'failed')

    async def test_local_batch_requires_installed_selection_but_not_cloud_consent(self):
        self.ready()
        with patch.object(application,'DATA_DIR',self.root), patch.object(application,'TASKS',set()), \
             patch.object(application,'JOBS',{}), patch.object(application,'LIMIT',asyncio.Semaphore(3)), \
             patch.object(m,'synthesize_local',new=AsyncMock(return_value=WAV)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app),base_url='http://127.0.0.1:8765') as c:
                headers={'X-Local-Token':application.TOKEN}
                body={'text':'hello','variants':[{'provider':'local_kokoro'}]}
                result=await c.post('/api/batches',json=body,headers=headers)
                self.assertEqual(result.status_code,202,result.text)
                await asyncio.gather(*list(application.TASKS))
                run=await c.get('/api/runs/'+result.json()['runs'][0]['id'])
                self.assertEqual(run.json()['status'],'completed')
                self.assertFalse(run.json()['cloud_consent'])
                self.ready('qwen3-1.7b')
                body['variants']=[{'provider':'local_qwen3','params':{'model':'qwen3-0.6b'}}]
                result=await c.post('/api/batches',json=body,headers=headers)
                self.assertEqual(result.status_code,422)
                self.assertIn('尚未安装',result.text)

    async def test_model_routes_enforce_local_token_and_hide_internal_paths(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app),base_url='http://127.0.0.1:8765') as c:
            result=await c.get('/api/local-models')
            self.assertEqual(len(result.json()['models']),6)
            self.assertNotIn(str(self.root),result.text)
            self.assertEqual((await c.post('/api/local-models/kokoro-v1.1/install',json={'device':'cpu'})).status_code,403)
            self.assertEqual((await c.delete('/api/local-models/kokoro-v1.1')).status_code,403)
            result=await c.post('/api/local-models/unknown/install',json={'device':'cpu'},headers={'X-Local-Token':application.TOKEN})
            self.assertEqual(result.status_code,422)
