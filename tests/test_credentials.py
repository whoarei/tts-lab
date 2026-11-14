"""Credential dialog contracts with disposable files and synthetic keys only."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dotenv import dotenv_values
import httpx

import run as launcher
from tts_lab import app as module, credentials


class CredentialsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'credentials.env'
        self.path.write_text('# Keep this comment\nOPENAI_API_KEY=other-service\n', encoding='utf-8-sig')
        self.patches = [patch.dict(os.environ, {}, clear=True),
                        patch.object(credentials, 'CONFIG_PATH', self.path),
                        patch.object(module, 'JOBS', {}), patch.object(module, 'TASKS', set()),
                        patch.object(module.local_models, 'TASKS', {})]
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url='http://127.0.0.1:8765')
        self.headers = {'X-Local-Token': module.TOKEN}

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def save(self, provider='deepseek', body=None, headers=None):
        return await self.client.put('/api/credentials/' + provider,
                                     json=body or {'values': {'DEEPSEEK_API_KEY': 'synthetic-key'}},
                                     headers=self.headers if headers is None else headers)

    async def test_save_immediate_state_blank_keep_replacement_and_restart(self):
        secret = "synthetic-'quoted'\\path\\'${HOME}#value"
        with patch.object(module, 'synthesize', side_effect=AssertionError('No provider calls on save')):
            response = await self.save(body={'values': {'DEEPSEEK_API_KEY': secret}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(secret, response.text)
        self.assertTrue(next(p for p in response.json()['providers'] if p['id'] == 'deepseek')['ready'])
        self.assertEqual(os.environ['DEEPSEEK_API_KEY'], secret)
        self.assertEqual(dotenv_values(self.path, interpolate=False)['DEEPSEEK_API_KEY'], secret)
        self.assertIn('# Keep this comment', self.path.read_text())
        self.assertEqual(dotenv_values(self.path)['OPENAI_API_KEY'], 'other-service')
        self.assertEqual((await self.save(body={'values': {'DEEPSEEK_API_KEY': ''}})).status_code, 200)
        self.assertEqual(os.environ['DEEPSEEK_API_KEY'], secret)
        view = (await self.client.get('/api/credentials/deepseek')).json()
        self.assertTrue(view['fields'][0]['configured'])
        self.assertNotIn('value', view['fields'][0])
        self.assertEqual((await self.save()).status_code, 200)
        os.environ.update(TTS_LAB_DESKTOP_TOKEN='t' * 40, TTS_LAB_DATA_DIR=self.temp.name)
        launcher.configure(True, self.path)
        self.assertEqual(os.environ['DEEPSEEK_API_KEY'], 'synthetic-key')
        if os.name != 'nt':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_provider_schemas_public_values_and_secret_presence_only(self):
        os.environ.update(AZURE_SPEECH_KEY='hidden-azure', AZURE_SPEECH_REGION='eastasia',
                          GOOGLE_CLOUD_ACCESS_TOKEN='hidden-google', GOOGLE_CLOUD_PROJECT='my-project',
                          AWS_ACCESS_KEY_ID='hidden-access-id', AWS_SECRET_ACCESS_KEY='hidden-secret')
        for provider in credentials.FIELDS:
            response = await self.client.get('/api/credentials/' + provider)
            self.assertEqual(response.status_code, 200)
            fields = response.json()['fields']
            self.assertEqual({f['key'] for f in fields}, set(credentials.FIELDS[provider]))
            for field in fields:
                if field['secret']:
                    self.assertNotIn('value', field)
            self.assertNotIn('hidden-', response.text)
        azure = (await self.client.get('/api/credentials/azure')).json()
        self.assertEqual(azure['fields'][1]['value'], 'eastasia')
        for provider in ('local_kokoro', 'demo', 'unknown'):
            self.assertEqual((await self.client.get('/api/credentials/' + provider)).status_code, 404)

    async def test_multi_field_required_and_atomic_failure(self):
        response = await self.save('azure', {'values': {'AZURE_SPEECH_KEY': 'hidden-azure'}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('hidden-azure', response.text)
        self.assertNotIn('AZURE_SPEECH_KEY', os.environ)
        response = await self.save('azure', {'values': {'AZURE_SPEECH_KEY': 'hidden-azure', 'AZURE_SPEECH_REGION': 'westus'}})
        self.assertEqual(response.status_code, 200)
        original = self.path.read_bytes()
        with patch.object(credentials.os, 'replace', side_effect=OSError('private path')):
            response = await self.save('azure', {'values': {'AZURE_SPEECH_KEY': 'replacement', 'AZURE_SPEECH_REGION': 'eastasia'}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('private path', response.text)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(os.environ['AZURE_SPEECH_KEY'], 'hidden-azure')
        self.assertEqual(os.environ['AZURE_SPEECH_REGION'], 'westus')
        self.assertEqual(list(self.path.parent.glob('.credentials-*.tmp')), [])

    async def test_auth_origin_busy_and_invalid_input_never_echo_credentials(self):
        self.assertEqual((await self.save(headers={})).status_code, 403)
        self.assertEqual((await self.save(headers={**self.headers, 'Origin': 'https://evil.invalid'})).status_code, 403)
        with patch.object(module, 'DESKTOP', True), patch.object(module, 'DESKTOP_TOKEN', 't' * 40):
            self.assertEqual((await self.client.get('/api/credentials/deepseek')).status_code, 403)
        module.JOBS['queued'] = {'status': 'queued'}
        self.assertEqual((await self.save()).status_code, 409)
        module.JOBS.clear()
        installing = asyncio.create_task(asyncio.sleep(60))
        try:
            module.local_models.TASKS['installing'] = installing
            self.assertEqual((await self.save()).status_code, 409)
        finally:
            installing.cancel()
            await asyncio.gather(installing, return_exceptions=True)
            module.local_models.TASKS.clear()
        original = self.path.read_bytes()
        for body in [[], {'values': 'private-secret'}, {'values': {'DEEPSEEK_API_KEY': 'private-secret\nINJECT=1'}},
                     {'values': {'OPENAI_API_KEY': 'private-secret'}}, {'values': {'DEEPSEEK_API_KEY': ['private-secret']}},
                     {'values': {'DEEPSEEK_API_KEY': 'private-secret'}, 'path': 'private-secret'},
                     {'values': {'DEEPSEEK_API_KEY': 'private-secret' * 400}}]:
            response = await self.client.put('/api/credentials/deepseek', json=body, headers=self.headers)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn('private-secret', response.text)
        response = await self.client.put('/api/credentials/deepseek', content='{"values":"private-secret', headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('private-secret', response.text)
        self.assertEqual(self.path.read_bytes(), original)

    async def test_duplicate_definitions_and_invalid_file_preservation(self):
        self.path.write_text('DEEPSEEK_API_KEY=old\n# retained\nDEEPSEEK_API_KEY=duplicate\n')
        self.assertEqual((await self.save()).status_code, 200)
        self.assertEqual(self.path.read_text().count('DEEPSEEK_API_KEY='), 1)
        self.path.write_text('DEEPSEEK_API_KEY="unterminated\n')
        original = self.path.read_bytes()
        self.assertEqual((await self.save()).status_code, 422)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(os.environ['DEEPSEEK_API_KEY'], 'synthetic-key')

    async def test_aws_auth_mode_switch_clears_incompatible_cached_credentials(self):
        with patch.object(module, 'DESKTOP', True), patch.object(module, 'DESKTOP_TOKEN', 't' * 40):
            self.headers['X-Desktop-Token'] = 't' * 40
            response = await self.save('polly', {'mode': 'keys', 'temporary': True, 'values': {
                'AWS_ACCESS_KEY_ID': 'synthetic-id', 'AWS_SECRET_ACCESS_KEY': 'synthetic-secret', 'AWS_SESSION_TOKEN': 'synthetic-token'}})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(os.environ['AWS_SHARED_CREDENTIALS_FILE'], os.devnull)
            self.assertNotIn('synthetic-', response.text)
            self.assertEqual((await self.save('polly', {'mode': 'profile', 'values': {'AWS_PROFILE': 'tts-test'}})).status_code, 200)
            self.assertNotIn('AWS_SHARED_CREDENTIALS_FILE', os.environ)
            self.assertNotIn('AWS_ACCESS_KEY_ID', os.environ)
            self.assertNotIn('AWS_SESSION_TOKEN', os.environ)
            self.assertEqual((await self.save('polly', {'mode': 'keys', 'values': {}})).status_code, 422)
            self.assertEqual((await self.save('polly', {'mode': 'keys', 'values': {
                'AWS_ACCESS_KEY_ID': 'new-id', 'AWS_SECRET_ACCESS_KEY': 'new-secret'}})).status_code, 200)
            self.assertNotIn('AWS_PROFILE', os.environ)
            self.assertNotIn('AWS_SESSION_TOKEN', os.environ)
            self.assertEqual(os.environ['AWS_SHARED_CREDENTIALS_FILE'], os.devnull)
            self.assertEqual((await self.save('polly', {'mode': [], 'values': {}})).status_code, 422)
            os.environ.update(TTS_LAB_DESKTOP_TOKEN='t' * 40, TTS_LAB_DATA_DIR=self.temp.name)
            launcher.configure(True, self.path)
            self.assertNotIn('AWS_PROFILE', os.environ)
            self.assertNotIn('AWS_SESSION_TOKEN', os.environ)
            try:
                import boto3
            except ImportError:
                pass
            else:
                # Resolve local direct credentials only; no service request.
                resolved = boto3.Session().get_credentials()
                self.assertEqual(resolved.access_key, 'new-id')
                self.assertEqual(resolved.secret_key, 'new-secret')


if __name__ == '__main__':
    unittest.main()
