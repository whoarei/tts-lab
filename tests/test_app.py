"""API and end-to-end offline tests; no cloud calls and no real secrets."""
import asyncio
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import wave
import zipfile

import httpx
from tts_lab import app as module
from tts_lab.providers import ProviderError, _audio, _wav


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = module.DATA_DIR
        module.DATA_DIR = Path(self.temp.name)
        module.LIMIT = asyncio.Semaphore(3)
        module.JOBS.clear()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://127.0.0.1:8765")
        self.headers = {"X-Local-Token": module.TOKEN}

    async def asyncTearDown(self):
        if module.TASKS:
            await asyncio.gather(*list(module.TASKS), return_exceptions=True)
        await self.client.aclose()
        module.DATA_DIR = self.old_data
        self.temp.cleanup()

    async def send(self, body):
        response = await self.client.post("/api/batches", json=body, headers=self.headers)
        if module.TASKS:
            await asyncio.gather(*list(module.TASKS))
        return response

    async def test_home_and_catalog_no_secrets(self):
        home = await self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("TTS LAB", home.text)
        self.assertIn("frame-ancestors 'none'", home.headers["content-security-policy"])
        with patch.dict("os.environ", {"OPENAI_API_KEY": "never-return-this-secret"}):
            response = await self.client.get("/api/state")
        self.assertNotIn("never-return-this-secret", response.text)
        ids = {p['id'] for p in response.json()['providers']}
        self.assertEqual(len(ids), 19)
        self.assertTrue({'deepseek', 'mimo', 'doubao', 'stepfun', 'fish', 'local_indextts', 'local_glmtts', 'local_kokoro', 'local_qwen3', 'local_cosyvoice'}.issubset(ids))

    async def test_update_activity_counts_work_without_returning_history_or_text(self):
        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        waiting = asyncio.create_task(asyncio.sleep(60))
        try:
            with patch.object(module, 'JOBS', {'old': {'status': 'completed', 'text': 'private text'},
                                             'active': {'status': 'running', 'text': 'private text'}}), \
                 patch.object(module.local_models, 'TASKS', {'working': waiting, 'finished': finished}):
                response = await self.client.get('/api/activity')
                self.assertEqual(response.json(), {'busy': True, 'active_runs': 1, 'active_installs': 1})
                self.assertNotIn('private text', response.text)
            with patch.object(module, 'JOBS', {'old': {'status': 'failed'}}), \
                 patch.object(module.local_models, 'TASKS', {'finished': finished}):
                self.assertEqual((await self.client.get('/api/activity')).json(),
                                 {'busy': False, 'active_runs': 0, 'active_installs': 0})
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def test_demo_end_to_end_audio_zip_csv_history(self):
        response = await self.send({"text": "你好 world", "variants": [
            {"provider": "demo", "params": {"frequency": 440}},
            {"provider": "demo", "params": {"frequency": 660}}]})
        self.assertEqual(response.status_code, 202, response.text)
        ids = [r["id"] for r in response.json()["runs"]]
        self.assertNotEqual(*ids)
        for run_id in ids:
            run = (await self.client.get("/api/runs/" + run_id)).json()
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["duration_seconds"], 1.5)
            audio = await self.client.get(f"/api/runs/{run_id}/audio")
            self.assertEqual(audio.status_code, 200)
            with wave.open(io.BytesIO(audio.content)) as wav:
                self.assertEqual(wav.getframerate(), 24000)
                self.assertEqual(wav.getnframes(), 36000)
            score = await self.client.post(f"/api/runs/{run_id}/score", headers=self.headers,
                json={"naturalness": 5, "accuracy": 5, "expression": 5, "usability": 5})
            self.assertEqual(score.status_code, 422)
        history = (await self.client.get("/api/runs")).json()
        self.assertEqual(len(history["runs"]), 2)
        bundle = await self.client.get(f"/api/batches/{response.json()['batch_id']}/download")
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            self.assertEqual(len(archive.namelist()), 4)
            self.assertIn("results.json", archive.namelist())
        csv_response = await self.client.get("/api/export.csv")
        rows = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)

    async def test_consent_and_credential_checks(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake"}):
            r = await self.send({"text": "hello", "variants": [{"provider": "openai"}]})
            self.assertEqual(r.status_code, 422)
            self.assertIn("可能产生费用", r.text)
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            r = await self.send({"text": "hello", "allow_cloud": True, "variants": [{"provider": "openai"}]})
            self.assertEqual(r.status_code, 422)
            self.assertIn("尚未配置", r.text)

    async def test_request_validation(self):
        invalid = [
            {"text": " ", "variants": [{"provider": "demo"}]},
            {"text": "a"*2001, "variants": [{"provider": "demo"}]},
            {"text": "hello", "variants": []},
            {"text": "hello", "variants": [{"provider": "demo"}]*7},
            {"text": "hello", "variants": [{"provider": "unknown"}]},
            {"text": "hello", "variants": [{"provider": "demo", "params": {"frequency": -1}}]},
            {"text": "hello", "variants": [{"provider": "demo", "params": {"key": "oops"}}]},
        ]
        for body in invalid:
            self.assertEqual((await self.send(body)).status_code, 422, body)
        self.assertEqual(list(module.DATA_DIR.iterdir()), [])

    async def test_named_variants_persist_without_requiring_provider_credentials(self):
        self.assertEqual((await self.client.get('/api/variants')).json()['variants'], [])
        configuration = {'selected_uid': 'voice-b', 'variants': [
            {'uid': 'voice-a', 'name': '日常播报', 'provider': 'azure', 'params': {}},
            {'uid': 'voice-b', 'name': '试听音', 'provider': 'demo', 'params': {'frequency': 660}},
        ]}
        response = await self.client.put('/api/variants', json=configuration, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        stored = json.loads((module.DATA_DIR / 'variants.json').read_text('utf-8'))
        self.assertEqual(stored, (await self.client.get('/api/variants')).json())
        self.assertEqual(stored['selected_uid'], 'voice-b')
        self.assertEqual(stored['variants'][0]['params']['voice'], 'zh-CN-XiaoxiaoNeural')
        self.assertEqual(stored['variants'][1]['params']['frequency'], 660)
        self.assertEqual(list(module.DATA_DIR.glob('*/run.json')), [])

    async def test_variant_save_validation_and_local_guard_preserve_previous_configuration(self):
        valid = {'selected_uid': 'a', 'variants': [
            {'uid': 'a', 'name': '原方案', 'provider': 'demo', 'params': {}}]}
        response = await self.client.put('/api/variants', json=valid, headers=self.headers)
        expected = response.json()
        self.assertEqual((await self.client.put('/api/variants', json=valid)).status_code, 403)
        invalid = [
            {**valid, 'selected_uid': 'missing'},
            {**valid, 'variants': valid['variants'] * 2},
            {**valid, 'variants': []},
            {**valid, 'variants': [{**valid['variants'][0], 'uid': '../escape'}]},
            {**valid, 'variants': [{**valid['variants'][0], 'name': ' '}]},
            {**valid, 'variants': [{**valid['variants'][0], 'provider': 'unknown'}]},
            {**valid, 'variants': [{**valid['variants'][0], 'params': {'api_key': 'must-not-persist'}}]},
            {**valid, 'variants': [{**valid['variants'][0], 'params': {'frequency': -1}}]},
        ]
        for payload in invalid:
            response = await self.client.put('/api/variants', json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual((await self.client.get('/api/variants')).json(), expected)

    async def test_corrupt_saved_variants_report_an_error_without_overwriting(self):
        path = module.DATA_DIR / 'variants.json'
        path.write_text('{broken', encoding='utf-8')
        self.assertEqual((await self.client.get('/api/variants')).status_code, 500)
        self.assertEqual(path.read_text('utf-8'), '{broken')

    async def test_cross_site_and_token_protection(self):
        body = {"text": "hello", "variants": [{"provider": "demo"}]}
        self.assertEqual((await self.client.post("/api/batches", json=body)).status_code, 403)
        self.assertEqual((await self.client.post("/api/batches", json=body, headers={**self.headers, "Origin":"https://evil.example"})).status_code, 403)
        self.assertEqual((await self.client.get("/api/state", headers={"Host":"evil.example"})).status_code, 403)
        self.assertEqual((await self.client.get("/api/state", headers={"Sec-Fetch-Site":"cross-site"})).status_code, 403)

    async def test_one_failure_does_not_cancel_others(self):
        async def fake(provider, text, params):
            if params["frequency"] == 440:
                raise ProviderError("模拟供应商失败")
            return _audio(_wav(b"\0\0"*24000))
        with patch.object(module, "synthesize", fake):
            r = await self.send({"text":"hello", "variants":[{"provider":"demo"}, {"provider":"demo","params":{"frequency":660}}]})
        runs = [(await self.client.get("/api/runs/"+x["id"])).json() for x in r.json()["runs"]]
        self.assertEqual([r["status"] for r in runs], ["failed", "completed"])

    async def test_score_persist_and_csv_formula_escape(self):
        r = await self.send({"text":"=SUM(1,2)","variants":[{"provider":"demo"}]})
        run_id = r.json()["runs"][0]["id"]
        run = module.read_run(run_id)
        run["family"] = "neural"  # Synthetic fixture for score API only.
        module.write_run(run)
        score = {"naturalness": 5, "accuracy": 4, "expression": 3, "usability": 4, "notes":"@formula"}
        response = await self.client.post(f"/api/runs/{run_id}/score", json=score, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(module.read_run(run_id)["scores"], score)
        response = await self.client.get("/api/export.csv")
        self.assertIn("'=SUM", response.text)
        self.assertIn("'@formula", response.text)

    async def test_restarted_pending_run_marked_interrupted(self):
        run = {"id":"a"*32,"batch_id":"b"*32,"status":"running","created_at":"2026-01-01"}
        module.write_run(run)
        result = (await self.client.get("/api/runs")).json()
        self.assertEqual(result["runs"][0]["status"], "interrupted")

    async def test_bad_audio_and_traversal(self):
        self.assertEqual((await self.client.get("/api/runs/not-an-id/audio")).status_code, 404)
        self.assertEqual((await self.client.get("/api/batches/not-an-id/download")).status_code, 404)
        with self.assertRaises(HTTPExceptionIfAvailable()):
            module.path_for("../../etc/passwd")

    async def test_batch_export_older_than_history_limit(self):
        batch_id = "b"*32
        for i in range(201):
            module.write_run({"id":f"{i:032x}", "batch_id":batch_id if i < 2 else "c"*32,
                              "status":"failed", "created_at":f"2026-01-{i:04d}",
                              "provider":"demo", "family":"demo", "text":"older text"})
        history = (await self.client.get("/api/runs")).json()
        self.assertEqual(len(history["runs"]), 200)
        bundle = await self.client.get(f"/api/batches/{batch_id}/download")
        self.assertEqual(bundle.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            runs = json.loads(archive.read("results.json"))
            self.assertEqual(len(runs), 2)


def HTTPExceptionIfAvailable():
    from fastapi import HTTPException
    return HTTPException


if __name__ == "__main__":
    unittest.main()
