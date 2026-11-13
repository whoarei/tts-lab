"""Desktop launcher integration/security tests using no provider calls."""
import asyncio
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
import run as launcher
from tts_lab import app as module


class ConfigurationTests(unittest.TestCase):
    def test_explicit_config_ignores_environment_and_interpolation(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "OPENAI_API_KEY": "inherited-secret", "GEMINI_API_KEY": "inherited-gemini",
            "AWS_PROFILE": "inherited-profile", "AWS_CONTAINER_CREDENTIALS_FULL_URI": "http://example.invalid",
            "TTS_LAB_DESKTOP_TOKEN": "t" * 40, "TTS_LAB_DATA_DIR": folder,
        }):
            config = Path(folder) / "credentials.env"
            config.write_text('OPENAI_API_KEY=explicit-secret\nGEMINI_API_KEY=${OPENAI_API_KEY}\nTTS_LAB_DATA_DIR=bad\n', encoding="utf-8")
            launcher.configure(True, config)
            self.assertEqual(os.environ["OPENAI_API_KEY"], "explicit-secret")
            self.assertEqual(os.environ["GEMINI_API_KEY"], "${OPENAI_API_KEY}")
            self.assertEqual(os.environ["TTS_LAB_DATA_DIR"], folder)
            self.assertNotIn("AWS_PROFILE", os.environ)
            self.assertNotIn("AWS_CONTAINER_CREDENTIALS_FULL_URI", os.environ)
            self.assertEqual(os.environ["AWS_SHARED_CREDENTIALS_FILE"], os.devnull)

    def test_explicit_aws_profile_is_supported(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "TTS_LAB_DESKTOP_TOKEN": "t" * 40, "TTS_LAB_DATA_DIR": folder,
        }):
            config = Path(folder) / "credentials.env"
            config.write_text('AWS_PROFILE=my-tts-profile\n', encoding="utf-8")
            launcher.configure(True, config)
            self.assertEqual(os.environ["AWS_PROFILE"], "my-tts-profile")
            self.assertNotIn("AWS_SHARED_CREDENTIALS_FILE", os.environ)
            self.assertEqual(os.environ["AWS_EC2_METADATA_DISABLED"], "true")

    def test_new_tts_keys_only_load_from_explicit_desktop_config(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            'MIMO_API_KEY': 'inherited-mimo', 'VOLCENGINE_TTS_API_KEY': 'inherited-volc',
            'STEP_API_KEY': 'inherited-step', 'FISH_AUDIO_API_KEY': 'inherited-fish',
            'TTS_LAB_DESKTOP_TOKEN': 't' * 40, 'TTS_LAB_DATA_DIR': folder,
        }):
            config = Path(folder) / 'credentials.env'
            config.write_text('', encoding='utf-8')
            launcher.configure(True, config)
            self.assertNotIn('MIMO_API_KEY', os.environ)
            self.assertNotIn('VOLCENGINE_TTS_API_KEY', os.environ)
            self.assertNotIn('STEP_API_KEY', os.environ)
            self.assertNotIn('FISH_AUDIO_API_KEY', os.environ)
            config.write_text('MIMO_API_KEY=explicit-mimo\nVOLCENGINE_TTS_API_KEY=explicit-volc\nSTEP_API_KEY=explicit-step\nFISH_AUDIO_API_KEY=explicit-fish\n', encoding='utf-8')
            launcher.configure(True, config)
            self.assertEqual(os.environ['MIMO_API_KEY'], 'explicit-mimo')
            self.assertEqual(os.environ['VOLCENGINE_TTS_API_KEY'], 'explicit-volc')
            self.assertEqual(os.environ['STEP_API_KEY'], 'explicit-step')
            self.assertEqual(os.environ['FISH_AUDIO_API_KEY'], 'explicit-fish')

    def test_deepseek_key_requires_explicit_desktop_config_and_is_cleared_on_removal(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            'DEEPSEEK_API_KEY': 'inherited-deepseek',
            'TTS_LAB_DESKTOP_TOKEN': 't' * 40, 'TTS_LAB_DATA_DIR': folder,
        }):
            config = Path(folder) / 'credentials.env'
            config.write_text('', encoding='utf-8')
            launcher.configure(True, config)
            self.assertNotIn('DEEPSEEK_API_KEY', os.environ)
            config.write_text('DEEPSEEK_API_KEY=explicit-deepseek\n', encoding='utf-8-sig')
            launcher.configure(True, config)
            self.assertEqual(os.environ['DEEPSEEK_API_KEY'], 'explicit-deepseek')
            config.write_text('', encoding='utf-8')
            launcher.configure(True, config)
            self.assertNotIn('DEEPSEEK_API_KEY', os.environ)

    def test_bundled_polly_requires_explicit_config_for_desktop(self):
        from tts_lab.catalog import public_catalog
        with patch.dict(os.environ, {"TTS_LAB_DESKTOP": "1", "AWS_PROFILE": "",
                                     "AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": ""}), \
                patch("tts_lab.catalog.importlib.util.find_spec", return_value=object()):
            polly = next(item for item in public_catalog() if item["id"] == "polly")
            self.assertFalse(polly["ready"])
            with patch.dict(os.environ, {"AWS_PROFILE": "explicit-profile"}):
                polly = next(item for item in public_catalog() if item["id"] == "polly")
                self.assertTrue(polly["ready"])
            with patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test"}):
                polly = next(item for item in public_catalog() if item["id"] == "polly")
                self.assertTrue(polly["ready"])

    def test_missing_token_and_config_fail_closed(self):
        with patch.dict(os.environ, {"TTS_LAB_DESKTOP_TOKEN": ""}):
            with self.assertRaises(ValueError):
                launcher.configure(True, None)


class DesktopGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_route_requires_desktop_token(self):
        with patch.object(module, "DESKTOP", True), patch.object(module, "DESKTOP_TOKEN", "t" * 40):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://127.0.0.1:8765") as client:
                for route in ["/", "/static/app.js", "/api/state", "/api/activity", "/api/runs", "/api/local-models", "/api/export.csv", "/missing"]:
                    self.assertEqual((await client.get(route)).status_code, 403, route)
                    self.assertEqual((await client.get(route, headers={"X-Desktop-Token": "wrong"})).status_code, 403)
                state = await client.get("/api/state", headers={"X-Desktop-Token": "t" * 40})
                self.assertEqual(state.status_code, 200)
                self.assertNotIn("t" * 40, state.text)
                response = await client.post("/api/batches", headers={"X-Desktop-Token": "t" * 40}, json={})
                self.assertEqual(response.status_code, 403)  # existing write CSRF guard remains

    async def test_shutdown_marks_pending_jobs_interrupted(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(module, "DATA_DIR", Path(folder)), patch.object(module, "JOBS", {}), patch.object(module, "TASKS", set()):
            run = {"id": "a" * 32, "status": "queued"}
            module.JOBS[run["id"]] = run
            task = asyncio.create_task(asyncio.sleep(60))
            module.TASKS.add(task)
            async with module.lifespan(module.app):
                pass
            self.assertTrue(task.cancelled())
            self.assertEqual(module.read_run(run["id"])["status"], "interrupted")


class DesktopProcessTests(unittest.TestCase):
    def test_ephemeral_listener_static_data_and_graceful_shutdown(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "persistent-data"
            config = Path(folder) / "credentials.env"
            config.write_text("OPENAI_API_KEY=do-not-expose-config-secret\n", encoding="utf-8")
            env = dict(os.environ, TTS_LAB_DESKTOP_TOKEN="t" * 40, TTS_LAB_DATA_DIR=str(data))
            process = subprocess.Popen([sys.executable, str(root / "run.py"), "--desktop", "--port", "0", "--config", str(config)], cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            lines = queue.Queue()
            threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
            try:
                ready_line = lines.get(timeout=15)
                ready = json.loads(ready_line)
                self.assertEqual(ready["event"], "ready")
                self.assertEqual(ready["host"], "127.0.0.1")
                self.assertGreater(ready["port"], 0)
                with httpx.Client(base_url=f'http://127.0.0.1:{ready["port"]}', trust_env=False) as client:
                    self.assertEqual(client.get("/").status_code, 403)
                    client.headers["X-Desktop-Token"] = "t" * 40
                    self.assertEqual(client.get("/").status_code, 200)
                    self.assertEqual(client.get("/static/app.js").status_code, 200)
                    state = client.get("/api/state")
                    self.assertNotIn("do-not-expose-config-secret", state.text)
                    client.headers["X-Local-Token"] = state.json()["token"]
                    batch = client.post("/api/batches", json={"text": "offline desktop test", "variants": [{"provider": "demo"}]})
                    self.assertEqual(batch.status_code, 202, batch.text)
                    run_id = batch.json()["runs"][0]["id"]
                    self.assertTrue((data / run_id / "run.json").is_file())
                stdout, stderr = process.communicate("shutdown\n", timeout=15)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertNotIn("do-not-expose-config-secret", ready_line + stdout + stderr)
                self.assertNotIn("t" * 40, ready_line + stdout + stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
