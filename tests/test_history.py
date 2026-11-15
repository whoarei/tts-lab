"""History pagination and conservative retention; no external services or models."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from tts_lab import app as module


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = module.DATA_DIR
        module.DATA_DIR = Path(self.temp.name) / 'data'
        module.DATA_DIR.mkdir()
        module.JOBS.clear()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                        base_url='http://127.0.0.1:8765')
        self.headers = {'X-Local-Token': module.TOKEN}
        self.sequence = 0
        self.clock = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)

    async def asyncTearDown(self):
        if module.TASKS:
            await asyncio.gather(*list(module.TASKS), return_exceptions=True)
        await self.client.aclose()
        module.JOBS.clear()
        module.DATA_DIR = self.old_data
        self.temp.cleanup()

    def add_run(self, *, batch_id=None, created_at=None, status='completed', audio=True, **extra):
        self.sequence += 1
        run_id = f'{self.sequence:08x}{self.sequence:024x}'
        run = {'id': run_id, 'batch_id': batch_id or f'{10000 + self.sequence:032x}',
               'created_at': created_at if created_at is not None else self.clock.isoformat(),
               'status': status, 'provider': 'openai', 'provider_name': 'OpenAI Speech',
               'family': 'cloud', 'text': '保留原文。', 'params': {'model': 'gpt-4o-mini-tts',
               'voice': 'coral', 'speed': 1.1, 'instructions': '自然朗读'},
               'scores': None, 'metadata': {'nested': ['unchanged', 1]}}
        if audio:
            run.update(filename='audio.wav', mime_type='audio/wav', bytes=5)
        run.update(extra)
        module.write_run(run)
        if audio:
            (module.DATA_DIR / run_id / 'audio.wav').write_bytes(b'audio')
        return run

    def folder(self, run):
        return module.DATA_DIR / run['id']

    def settings(self, days):
        module.write_settings(module.AppSettings(history_retention_days=days))

    async def test_default_settings_are_persisted_and_changes_survive_read(self):
        expected = {'history_retention_days': 30}
        self.assertEqual((await self.client.get('/api/settings')).json(), expected)
        settings_file = module.DATA_DIR / 'settings.json'
        self.assertEqual(json.loads(settings_file.read_text('utf-8')), expected)
        for days in (0, 7, 3650):
            expected = {'history_retention_days': days}
            response = await self.client.post('/api/settings', json=expected, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), expected)
            self.assertEqual(module.read_settings().model_dump(), expected)
            self.assertEqual((await self.client.get('/api/settings')).json(), expected)
            self.assertEqual(json.loads(settings_file.read_text('utf-8')), expected)
        self.assertEqual(list(module.DATA_DIR.glob('.settings-*')), [])

    async def test_settings_validate_strict_integer_and_local_auth(self):
        self.settings(0)
        invalid = [{}, {'history_retention_days': '30'}, {'history_retention_days': True},
                   {'history_retention_days': 30.0}, {'history_retention_days': None},
                   {'history_retention_days': -1}, {'history_retention_days': 3651},
                   {'history_retention_days': 30, 'unexpected': 1}]
        for payload in invalid:
            response = await self.client.post('/api/settings', json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 422, payload)
        for headers in ({}, {**self.headers, 'Origin': 'https://example.org'},
                        {**self.headers, 'Sec-Fetch-Site': 'cross-site'},
                        {**self.headers, 'Host': 'attacker.example'}):
            response = await self.client.post('/api/settings', json={'history_retention_days': 30}, headers=headers)
            self.assertEqual(response.status_code, 403, headers)
        for endpoint in ('/api/settings', '/api/history'):
            self.assertEqual((await self.client.get(endpoint, headers={'Origin': 'https://example.org'})).status_code, 403)
        self.assertEqual(module.read_settings().history_retention_days, 0)

    async def test_failed_atomic_save_keeps_old_settings_and_history(self):
        self.settings(0)
        run = self.add_run(created_at=(datetime.now(timezone.utc) - timedelta(days=60)).isoformat())
        with patch.object(Path, 'replace', side_effect=OSError('disk unavailable')):
            response = await self.client.post('/api/settings', json={'history_retention_days': 1}, headers=self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('disk unavailable', response.text)
        self.assertEqual(module.read_settings().history_retention_days, 0)
        self.assertTrue(self.folder(run).is_dir())
        self.assertEqual(list(module.DATA_DIR.glob('.settings-*')), [])

    async def test_corrupt_settings_report_error_without_deleting_and_allow_correction(self):
        run = self.add_run(created_at=(datetime.now(timezone.utc) - timedelta(days=100)).isoformat())
        path = module.DATA_DIR / 'settings.json'
        for contents in ('{broken', '{"history_retention_days": "30"}', '{"history_retention_days": -1}'):
            path.write_text(contents, encoding='utf-8')
            for endpoint in ('/api/settings', '/api/history', '/api/runs'):
                self.assertEqual((await self.client.get(endpoint)).status_code, 500, endpoint)
            with patch.object(module.local_models, 'shutdown', new_callable=AsyncMock):
                async with module.app.router.lifespan_context(module.app):
                    self.assertTrue(self.folder(run).is_dir())
            self.assertEqual(path.read_text('utf-8'), contents)
        response = await self.client.post('/api/settings', json={'history_retention_days': 0}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.client.get('/api/history')).json()['total'], 1)

    async def test_retention_boundary_and_whole_batch_use_latest_member(self):
        self.settings(30)
        old = self.add_run(created_at=(self.clock - timedelta(days=31)).isoformat())
        exact = self.add_run(created_at=(self.clock - timedelta(days=30)).isoformat())
        recent = self.add_run(created_at=(self.clock - timedelta(days=29)).isoformat())
        mixed_old = self.add_run(created_at=(self.clock - timedelta(days=60)).isoformat())
        mixed_boundary = self.add_run(batch_id=mixed_old['batch_id'],
                                      created_at=(self.clock - timedelta(days=30)).isoformat())
        grouped_old = self.add_run(created_at=(self.clock - timedelta(days=32)).isoformat())
        grouped_old_2 = self.add_run(batch_id=grouped_old['batch_id'],
                                    created_at=(self.clock - timedelta(days=31, seconds=1)).isoformat())
        removed = module.cleanup_expired_history(current_time=self.clock)
        self.assertEqual(removed, {old['id'], grouped_old['id'], grouped_old_2['id']})
        for run in (old, grouped_old, grouped_old_2):
            self.assertFalse(self.folder(run).exists())
        for run in (exact, recent, mixed_old, mixed_boundary):
            self.assertEqual((self.folder(run) / 'audio.wav').read_bytes(), b'audio')
        self.assertEqual(list(module.DATA_DIR.glob('.expired-history-*')), [])

    async def test_timezone_offsets_are_compared_as_instants(self):
        self.settings(30)
        expired = self.add_run(created_at='2026-10-16T14:59:59+03:00')
        boundary = self.add_run(created_at='2026-10-16T15:00:00+03:00')
        zulu = self.add_run(created_at='2026-10-16T11:59:59Z')
        self.assertEqual(module.cleanup_expired_history(current_time=self.clock), {expired['id'], zulu['id']})
        self.assertTrue(self.folder(boundary).is_dir())

    async def test_active_jobs_and_invalid_timestamps_protect_full_batch(self):
        self.settings(30)
        old = (self.clock - timedelta(days=60)).isoformat()
        protected = []
        for status in ('queued', 'running'):
            active = self.add_run(created_at=old, status=status, audio=False)
            sibling = self.add_run(created_at=old, batch_id=active['batch_id'])
            module.JOBS[active['id']] = active.copy()
            protected.extend((active, sibling))
        # Even a stale completed disk snapshot cannot override the active job.
        active_memory = self.add_run(created_at=old)
        module.JOBS[active_memory['id']] = {**active_memory, 'status': 'running'}
        protected.append(active_memory)
        for timestamp in ('not a date', '2026-01-01', '2026-01-01T00:00:00', '99999-01-01T00:00:00Z'):
            invalid = self.add_run(created_at=timestamp)
            sibling = self.add_run(created_at=old, batch_id=invalid['batch_id'])
            protected.extend((invalid, sibling))
        self.assertEqual(module.cleanup_expired_history(current_time=self.clock), set())
        for run in protected:
            self.assertTrue(self.folder(run).is_dir(), run)

    async def test_unknown_files_and_invalid_metadata_protect_full_batch(self):
        self.settings(30)
        old = (self.clock - timedelta(days=60)).isoformat()
        protected = []
        for problem in ('extra-file', 'subdirectory', 'pending-write', 'unsafe-filename',
                        'invalid-id', 'invalid-created-at', 'invalid-status'):
            run = self.add_run(created_at=old)
            sibling = self.add_run(created_at=old, batch_id=run['batch_id'])
            folder = self.folder(run)
            if problem == 'extra-file':
                (folder / 'personal.txt').write_text('leave me', encoding='utf-8')
            elif problem == 'subdirectory':
                (folder / 'unknown').mkdir()
            elif problem == 'pending-write':
                (folder / 'run.json.tmp').write_text('{}', encoding='utf-8')
            else:
                altered = dict(run)
                if problem == 'unsafe-filename':
                    altered['filename'] = '../personal.txt'
                elif problem == 'invalid-id':
                    altered['id'] = 'different-id'
                elif problem == 'invalid-status':
                    altered['status'] = ['completed']
                else:
                    altered['created_at'] = 123
                (folder / 'run.json').write_text(json.dumps(altered), encoding='utf-8')
            protected.extend((run, sibling))
        for name in ('presets', 'models', 'references', 'not-a-run'):
            folder = module.DATA_DIR / name
            folder.mkdir()
            (folder / 'sentinel').write_text('keep', encoding='utf-8')
        self.assertEqual(module.cleanup_expired_history(current_time=self.clock), set())
        for run in protected:
            self.assertTrue(self.folder(run).is_dir())
        for name in ('presets', 'models', 'references', 'not-a-run'):
            self.assertEqual((module.DATA_DIR / name / 'sentinel').read_text('utf-8'), 'keep')

    async def test_symlinks_are_never_followed_or_deleted(self):
        self.settings(30)
        old = (self.clock - timedelta(days=60)).isoformat()
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        sentinel = outside / 'sentinel'
        sentinel.write_bytes(b'keep')
        folder_link = module.DATA_DIR / ('f' * 32)
        try:
            folder_link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Creating symlinks is not permitted on this system')
        linked_audio = self.add_run(created_at=old)
        (self.folder(linked_audio) / 'audio.wav').unlink()
        (self.folder(linked_audio) / 'audio.wav').symlink_to(sentinel)
        sibling = self.add_run(created_at=old, batch_id=linked_audio['batch_id'])
        linked_metadata = self.add_run(created_at=old)
        metadata = self.folder(linked_metadata) / 'run.json'
        external_metadata = outside / 'run.json'
        external_metadata.write_text(metadata.read_text('utf-8'), encoding='utf-8')
        metadata.unlink()
        metadata.symlink_to(external_metadata)
        self.assertEqual(module.cleanup_expired_history(current_time=self.clock), set())
        self.assertTrue(folder_link.is_symlink())
        self.assertTrue(metadata.is_symlink())
        self.assertTrue(self.folder(sibling).is_dir())
        self.assertEqual(sentinel.read_bytes(), b'keep')
        settings_file = module.DATA_DIR / 'settings.json'
        settings_file.unlink()
        settings_file.symlink_to(sentinel)
        self.assertEqual((await self.client.get('/api/settings')).status_code, 500)
        response = await self.client.post('/api/settings', json={'history_retention_days': 0}, headers=self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertTrue(settings_file.is_symlink())
        self.assertEqual(sentinel.read_bytes(), b'keep')

    async def test_windows_junctions_are_skipped_without_platform_privileges(self):
        self.settings(30)
        old = (self.clock - timedelta(days=60)).isoformat()
        linked_folder = self.add_run(created_at=old)
        linked_metadata = self.add_run(created_at=old)
        linked_audio = self.add_run(created_at=old)
        sibling = self.add_run(created_at=old, batch_id=linked_audio['batch_id'])
        ordinary = self.add_run(created_at=old)
        junctions = {self.folder(linked_folder), self.folder(linked_metadata) / 'run.json',
                     self.folder(linked_audio) / 'audio.wav'}
        with patch.object(Path, 'is_junction', lambda path: path in junctions, create=True):
            self.assertEqual(module.cleanup_expired_history(current_time=self.clock), {ordinary['id']})
            for run in (linked_folder, linked_metadata, linked_audio, sibling):
                self.assertEqual((self.folder(run) / 'audio.wav').read_bytes(), b'audio')
            self.assertFalse(self.folder(ordinary).exists())
            settings_file = module.DATA_DIR / 'settings.json'
            original_settings = settings_file.read_bytes()
            junctions.add(settings_file)
            self.assertEqual((await self.client.get('/api/settings')).status_code, 500)
            response = await self.client.post('/api/settings', json={'history_retention_days': 0}, headers=self.headers)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(settings_file.read_bytes(), original_settings)

    async def test_failed_batch_staging_rolls_back_every_member(self):
        self.settings(30)
        old = (self.clock - timedelta(days=60)).isoformat()
        first = self.add_run(created_at=old)
        second = self.add_run(created_at=old, batch_id=first['batch_id'])
        original_rename = Path.rename
        calls = []

        def rename(path, target):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('file busy')
            return original_rename(path, target)

        with patch.object(Path, 'rename', rename):
            self.assertEqual(module.cleanup_expired_history(current_time=self.clock), set())
        for run in (first, second):
            self.assertEqual((self.folder(run) / 'audio.wav').read_bytes(), b'audio')
        self.assertEqual(list(module.DATA_DIR.glob('.expired-history-*')), [])

    async def test_startup_listing_and_setting_changes_apply_retention(self):
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        self.settings(0)
        forever = self.add_run(created_at=old)
        self.assertEqual((await self.client.get('/api/history')).json()['total'], 1)
        response = await self.client.post('/api/settings', json={'history_retention_days': 30}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.folder(forever).exists())
        startup = self.add_run(created_at=old)
        with patch.object(module.local_models, 'shutdown', new_callable=AsyncMock):
            async with module.app.router.lifespan_context(module.app):
                self.assertFalse(self.folder(startup).exists())
        for endpoint in ('/api/history', '/api/runs'):
            run = self.add_run(created_at=old)
            self.assertEqual((await self.client.get(endpoint)).status_code, 200)
            self.assertFalse(self.folder(run).exists())

    async def test_pagination_counts_batches_preserves_raw_runs_and_legacy_limit(self):
        self.settings(0)
        batches = []
        for index in range(41):
            timestamp = (self.clock + timedelta(seconds=index)).isoformat()
            first = self.add_run(created_at=timestamp)
            runs = [first] + [self.add_run(batch_id=first['batch_id'], created_at=timestamp) for _ in range(5)]
            batches.append({'batch_id': first['batch_id'], 'runs': runs})
        first_page = (await self.client.get('/api/history')).json()
        self.assertEqual({key: first_page[key] for key in ('page', 'page_size', 'total', 'total_pages')},
                         {'page': 1, 'page_size': 10, 'total': 41, 'total_pages': 5})
        self.assertEqual([batch['batch_id'] for batch in first_page['batches']],
                         [batch['batch_id'] for batch in reversed(batches[-10:])])
        all_returned = []
        for page in range(1, 6):
            payload = (await self.client.get(f'/api/history?page={page}')).json()
            for batch in payload['batches']:
                self.assertEqual(len(batch['runs']), 6)
                all_returned.extend(batch['runs'])
        self.assertEqual(len(all_returned), 246)
        self.assertEqual({run['id'] for run in all_returned}, {run['id'] for batch in batches for run in batch['runs']})
        self.assertEqual(next(run for run in all_returned if run['id'] == batches[0]['runs'][0]['id']), batches[0]['runs'][0])
        self.assertEqual(len((await self.client.get('/api/runs')).json()['runs']), 200)
        # Existing export and score APIs remain available even after UI removal.
        self.assertEqual((await self.client.get('/api/export.csv')).status_code, 200)
        self.assertEqual((await self.client.get(f"/api/batches/{batches[0]['batch_id']}/download")).status_code, 200)
        scored = await self.client.post(f"/api/runs/{batches[0]['runs'][0]['id']}/score", headers=self.headers,
                                        json={'naturalness': 4, 'accuracy': 5, 'expression': 3, 'usability': 4})
        self.assertEqual(scored.status_code, 200, scored.text)

    async def test_empty_pages_clamping_and_query_validation(self):
        expected = {'batches': [], 'page': 1, 'page_size': 10, 'total': 0, 'total_pages': 0}
        self.assertEqual((await self.client.get('/api/history?page=999')).json(), expected)
        self.settings(0)
        for index in range(3):
            self.add_run(created_at=(self.clock + timedelta(seconds=index)).isoformat())
        for requested, actual in ((999, 2), (0, 1), (-10, 1)):
            payload = (await self.client.get(f'/api/history?page={requested}&page_size=2')).json()
            self.assertEqual(payload['page'], actual)
            self.assertEqual(payload['total_pages'], 2)
            self.assertEqual(len(payload['batches']), 1 if actual == 2 else 2)
        for query in ('page=bad', 'page_size=0', 'page_size=101', 'page_size=bad', 'page_size=1.5'):
            self.assertEqual((await self.client.get('/api/history?' + query)).status_code, 422, query)

    async def test_scoring_cannot_recreate_a_batch_during_cleanup(self):
        self.settings(30)
        old = (self.clock - timedelta(days=60)).isoformat()
        run = self.add_run(created_at=old)
        original_read = module.read_run
        score_read = threading.Event()
        allow_score = threading.Event()
        cleanup_started = threading.Event()
        cleanup_finished = threading.Event()

        def paused_read(run_id):
            value = original_read(run_id)
            score_read.set()
            if not allow_score.wait(5):
                raise AssertionError('Score synchronization timed out')
            return value

        def cleanup():
            cleanup_started.set()
            value = module.cleanup_expired_history(current_time=self.clock)
            cleanup_finished.set()
            return value

        def exercise():
            with patch.object(module, 'read_run', paused_read), ThreadPoolExecutor(max_workers=2) as pool:
                scoring = pool.submit(module.save_score, run['id'], module.Score(
                    naturalness=4, accuracy=5, expression=3, usability=4))
                try:
                    self.assertTrue(score_read.wait(5))
                    cleaning = pool.submit(cleanup)
                    self.assertTrue(cleanup_started.wait(5))
                    self.assertFalse(cleanup_finished.wait(0.1))
                finally:
                    allow_score.set()
                self.assertEqual(scoring.result(timeout=5)['scores']['accuracy'], 5)
                self.assertEqual(cleaning.result(timeout=5), {run['id']})
        await asyncio.to_thread(exercise)
        self.assertFalse(self.folder(run).exists())
        self.assertEqual((await self.client.get('/api/runs/' + run['id'])).status_code, 404)


if __name__ == '__main__':
    unittest.main()
