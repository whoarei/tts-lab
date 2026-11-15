"""Local-only HTTP API, durable runs, blind listening, and honest metrics."""
from __future__ import annotations

import asyncio
import csv
from contextlib import asynccontextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import uuid
import wave
import zipfile

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .catalog import public_catalog, validate_params
from .providers import ProviderError, synthesize
from . import credentials, local_models, reference_audio

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("TTS_LAB_DATA_DIR", ROOT.parent / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DESKTOP = os.getenv("TTS_LAB_DESKTOP") == "1"
DESKTOP_TOKEN = os.getenv("TTS_LAB_DESKTOP_TOKEN", "")
if DESKTOP and (len(DESKTOP_TOKEN) < 32 or not DESKTOP_TOKEN.isascii()):
    raise RuntimeError("Desktop authentication is not configured.")
TOKEN = secrets.token_urlsafe(32)
LIMIT = asyncio.Semaphore(3)
JOBS: dict[str, dict] = {}
TASKS: set[asyncio.Task] = set()
ID = re.compile(r"^[0-9a-f]{32}$")
HISTORY_LOCK = threading.RLock()
AUDIO_NAME = re.compile(r"audio\.(wav|mp3|ogg|opus|flac|aac)")
RUN_STATES = {'queued', 'running', 'completed', 'failed', 'interrupted'}

@asynccontextmanager
async def lifespan(application):
    try:
        cleanup_expired_history()
    except HTTPException:
        # Invalid settings must never fall back to a destructive default.
        # The settings endpoint reports the error and still permits correction.
        pass
    yield
    await local_models.shutdown()
    pending = list(TASKS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for run in JOBS.values():
        if run["status"] in {"queued", "running"}:
            run.update(status="interrupted", error="进程已停止。调用可能已计费，请检查后手动重试。")
            write_run(run)


app = FastAPI(title="TTS Lab", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


@app.middleware("http")
async def local_guard(request: Request, call_next):
    """Protect local credentials from cross-site requests and DNS rebinding."""
    if DESKTOP and not secrets.compare_digest(
        request.headers.get("x-desktop-token", "").encode("utf-8"), DESKTOP_TOKEN.encode("utf-8")
    ):
        return JSONResponse({"detail": "拒绝访问。"}, status_code=403)
    host = request.headers.get("host", "")
    try:
        parsed = urlsplit("http://" + host)
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            return JSONResponse({"detail": "仅允许本机访问。"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != "http://" + host:
            return JSONResponse({"detail": "拒绝跨站请求。"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "拒绝跨站请求。"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not secrets.compare_digest(request.headers.get("x-local-token", ""), TOKEN):
                return JSONResponse({"detail": "会话已变更，请刷新页面。"}, status_code=403)
            length = request.headers.get("content-length", "0")
            maximum = reference_audio.MAX_REFERENCE if request.method == 'POST' and request.url.path == '/api/references' else 100_000
            if int(length) > maximum:
                return JSONResponse({"detail": "请求过大。"}, status_code=413)
    except (ValueError, TypeError):
        return JSONResponse({"detail": "请求头无效。"}, status_code=400)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    )
    return response


class Variant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=60)
    params: dict = Field(default_factory=dict)


class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    variants: list[Variant] = Field(min_length=1, max_length=6)
    allow_cloud: bool = False


class Score(BaseModel):
    model_config = ConfigDict(extra="forbid")
    naturalness: int = Field(ge=1, le=5)
    accuracy: int = Field(ge=1, le=5)
    expression: int = Field(ge=1, le=5)
    usability: int = Field(ge=1, le=5)
    notes: str = Field(default="", max_length=2000)


class Preset(Variant):
    name: str = Field(min_length=1, max_length=80)


class AppSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    history_retention_days: int = Field(strict=True, ge=0, le=3650)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def path_for(run_id: str) -> Path:
    if not ID.fullmatch(run_id):
        raise HTTPException(404, "找不到记录。")
    return DATA_DIR / run_id


def write_run(run: dict):
    with HISTORY_LOCK:
        folder = path_for(run["id"])
        folder.mkdir(exist_ok=True)
        tmp = folder / "run.json.tmp"
        tmp.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(folder / "run.json")


def read_run(run_id: str) -> dict:
    with HISTORY_LOCK:
        try:
            return json.loads((path_for(run_id) / "run.json").read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            raise HTTPException(404, "找不到记录。") from None


def wav_duration(data: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(data)) as audio:
            return round(audio.getnframes() / audio.getframerate(), 3)
    except (wave.Error, EOFError, ZeroDivisionError):
        return None


def safe_params(params: dict) -> dict:
    """Catalog forbids unknown fields; never persist credential-like names."""
    return {k: v for k, v in params.items()
            if not any(x in k.lower() for x in ("key", "secret", "token", "password"))}


async def generate(run: dict):
    async with LIMIT:
        run["status"] = "running"
        run["started_at"] = now()
        write_run(run)
        start = time.perf_counter()
        try:
            # Global deadline includes AWS thread wait; no retry after timeouts.
            result = await asyncio.wait_for(
                synthesize(run["provider"], run["text"], run["params"]),
                timeout=1900 if run['provider'].startswith('local_') else 150)
            if not result.audio or len(result.audio) > 20_000_000:
                raise ProviderError("服务返回空音频或超过 20 MB 上限。")
            if result.extension not in {"wav", "mp3", "ogg", "opus", "flac", "aac"}:
                raise ProviderError("服务返回不支持的音频格式。")
            filename = "audio." + result.extension
            (path_for(run["id"]) / filename).write_bytes(result.audio)
            duration = wav_duration(result.audio) if result.extension == "wav" else None
            elapsed = round(time.perf_counter() - start, 3)
            run.update(status="completed", filename=filename,
                       mime_type=result.mime_type, bytes=len(result.audio),
                       elapsed_seconds=elapsed, duration_seconds=duration,
                       real_time_factor=round(elapsed / duration, 3) if duration else None,
                       metadata=result.metadata, error=None)
        except asyncio.TimeoutError:
            run.update(status="failed", error="超过 150 秒，本地停止等待。服务端仍可能完成并计费，请确认后再重试。")
        except ProviderError as exc:
            run.update(status="failed", error=str(exc)[:500])
        except Exception:
            # Never return raw SDK exceptions that may contain credentials.
            run.update(status="failed", error="生成失败。请检查配置、账号权限或服务状态；未自动重试。")
        finally:
            run["completed_at"] = now()
            run.setdefault("elapsed_seconds", round(time.perf_counter() - start, 3))
            write_run(run)


@app.get("/api/state")
def state():
    return {"token": TOKEN, "providers": public_catalog(), "references": reference_audio.list_references(), "version": "0.11.0", "desktop": DESKTOP}


@app.get('/api/activity')
def activity():
    """Small snapshot for the desktop's update gate; never return job content."""
    runs = max(sum(run.get('status') in {'queued', 'running'} for run in list(JOBS.values())),
               sum(not task.done() for task in list(TASKS)))
    installs = sum(not task.done() for task in list(local_models.TASKS.values()))
    return {'busy': bool(runs or installs), 'active_runs': runs, 'active_installs': installs}


@app.get('/api/credentials/{provider}')
async def credential_settings(provider: str):
    try:
        return credentials.describe(provider)
    except credentials.CredentialError as exc:
        raise HTTPException(404, str(exc)) from None


@app.put('/api/credentials/{provider}')
async def save_credentials(provider: str, request: Request):
    # Parse manually: Pydantic's input errors may echo submitted secrets.
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        raise HTTPException(422, '配置内容无效。') from None
    # No await between this gate and publishing the new environment. Queued
    # batches also count, so running requests never change credentials mid-run.
    if activity()['busy']:
        raise HTTPException(409, '请等待语音生成和模型安装完成后再保存凭证。')
    try:
        settings = credentials.save(provider, body, desktop=DESKTOP)
    except credentials.CredentialError as exc:
        raise HTTPException(422, str(exc)) from None
    return {'settings': settings, 'providers': public_catalog()}


@app.post('/api/references', status_code=201)
async def import_reference(request: Request, name: str = '参考录音.wav'):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > reference_audio.MAX_REFERENCE:
            raise HTTPException(413, '参考录音超过 2 MB。')
    try:
        return reference_audio.save(bytes(raw), name)
    except reference_audio.ReferenceError as exc:
        raise HTTPException(422, str(exc)) from None


@app.get('/api/references/{reference_id}/audio')
def reference_preview(reference_id: str):
    try:
        reference_audio.info(reference_id)
        return FileResponse(reference_audio.path_for(reference_id), media_type='audio/wav')
    except reference_audio.ReferenceError as exc:
        raise HTTPException(404, str(exc)) from None


class SavedVariant(Variant):
    uid: str = Field(pattern=r'^[a-zA-Z0-9-]{1,64}$')
    name: str = Field(min_length=1, max_length=60)


class VariantConfiguration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    variants: list[SavedVariant] = Field(min_length=1, max_length=6)
    selected_uid: str = Field(max_length=64)


@app.get('/api/variants')
def get_variants():
    path = DATA_DIR / 'variants.json'
    if not path.exists():
        return {'variants': [], 'selected_uid': ''}
    try:
        return VariantConfiguration.model_validate_json(path.read_text('utf-8')).model_dump()
    except (ValueError, OSError):
        raise HTTPException(500, '无法读取已保存方案，请检查数据目录中的 variants.json。') from None


@app.put('/api/variants')
def save_variants(configuration: VariantConfiguration):
    ids = [variant.uid for variant in configuration.variants]
    if len(set(ids)) != len(ids) or configuration.selected_uid not in ids:
        raise HTTPException(422, '方案编号必须唯一，且所选方案必须存在。')
    for variant in configuration.variants:
        variant.name = variant.name.strip()
        if not variant.name:
            raise HTTPException(422, '请输入方案名称。')
        try:
            variant.params = validate_params(variant.provider, variant.params)
        except (ValueError, ProviderError) as exc:
            raise HTTPException(422, f'{variant.name}：{exc}') from None
    payload = configuration.model_dump()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=DATA_DIR,
                                         prefix='.variants-', delete=False) as file:
            temporary = Path(file.name)
            json.dump(payload, file, ensure_ascii=False, indent=2)
        temporary.replace(DATA_DIR / 'variants.json')
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return payload


def preset_path(preset_id: str) -> Path:
    if not ID.fullmatch(preset_id):
        raise HTTPException(404, "找不到方案。")
    return DATA_DIR / "presets" / (preset_id + ".json")


@app.get("/api/presets")
def list_presets():
    migrate_legacy_presets()
    presets = []
    for file in (DATA_DIR / "presets").glob("*.json"):
        try:
            item = json.loads(file.read_text("utf-8"))
            Preset.model_validate({k: item[k] for k in ("name", "provider", "params")})
            if (ID.fullmatch(item["id"]) and item["id"] == file.stem
                    and isinstance(item["updated_at"], str)):
                presets.append(item)
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return {"presets": sorted(presets, key=lambda p: p["updated_at"], reverse=True)}


def write_preset(preset_id: str, preset: Preset) -> dict:
    name = preset.name.strip()
    if not name or any(ord(c) < 32 for c in name):
        raise HTTPException(422, "请输入有效的方案名称。")
    if preset.provider == "demo":
        raise HTTPException(422, "提示音不能保存为语音方案。")
    try:
        params = validate_params(preset.provider, preset.params)
    except (ValueError, ProviderError) as exc:
        raise HTTPException(422, str(exc)) from None
    item = {"id": preset_id, "name": name, "provider": preset.provider,
            "params": safe_params(params), "updated_at": now()}
    path = preset_path(preset_id)
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return item


def migrate_legacy_presets():
    # Keep variants.json intact for older installations. Stable IDs make the
    # upgrade idempotent and never overwrite edits made in the new editor.
    for variant in get_variants()['variants']:
        if variant['provider'] == 'demo':
            continue
        preset_id = uuid.uuid5(uuid.NAMESPACE_URL, 'tts-lab-variant:' + variant['uid']).hex
        if preset_path(preset_id).exists():
            continue
        try:
            write_preset(preset_id, Preset(name=variant['name'], provider=variant['provider'], params=variant['params']))
        except HTTPException:
            continue


@app.post("/api/presets", status_code=201)
async def create_preset(preset: Preset):
    return write_preset(uuid.uuid4().hex, preset)


@app.put("/api/presets/{preset_id}")
async def update_preset(preset_id: str, preset: Preset):
    if not preset_path(preset_id).is_file():
        raise HTTPException(404, "找不到方案。")
    return write_preset(preset_id, preset)


@app.post("/api/batches", status_code=202)
async def create_batch(batch: Batch):
    text = batch.text.strip()
    if not text:
        raise HTTPException(422, "请输入朗读文本。")
    catalog = {x["id"]: x for x in public_catalog()}
    if len(TASKS) + len(batch.variants) > 12:
        raise HTTPException(429, "还有较多任务在处理，请等待后再提交。")
    validated = []
    for variant in batch.variants:
        item = catalog.get(variant.provider)
        if not item:
            raise HTTPException(422, "未知服务。")
        if item["family"] not in {"demo", "local"} and not batch.allow_cloud:
            raise HTTPException(422, "请先确认将文本发送给所选服务，且可能产生费用。")
        if not item.get("ready"):
            raise HTTPException(422, f"{item['name']} 尚未配置，请先下载本地模型或按说明设置环境变量。")
        try:
            params = validate_params(variant.provider, variant.params, text)
            if item['family'] == 'local' and params['model'] not in item['installed_models']:
                raise ProviderError('所选本地模型尚未安装，请在“本地模型配置”中下载。')
            if 'reference_id' in params:
                reference_audio.info(params['reference_id'])
        except (ValueError, ProviderError) as exc:
            raise HTTPException(422, str(exc)) from None
        validated.append((variant.provider, item, params))
    batch_id = uuid.uuid4().hex
    runs = []
    for provider, item, params in validated:
        run = {"id": uuid.uuid4().hex, "batch_id": batch_id, "provider": provider,
               "provider_name": item["name"], "family": (
                   "generative" if provider == "polly" and params.get("engine") == "generative" else item["family"]),
               "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
               "params": safe_params(params), "status": "queued", "created_at": now(),
               "scores": None, "cloud_consent": batch.allow_cloud}
        with HISTORY_LOCK:
            write_run(run)
            JOBS[run["id"]] = run
        runs.append(run.copy())
        task = asyncio.create_task(generate(run))
        TASKS.add(task)
        task.add_done_callback(TASKS.discard)
    return {"batch_id": batch_id, "runs": runs}


class ModelInstall(BaseModel):
    model_config = ConfigDict(extra='forbid')
    device: str = 'cpu'


@app.get('/api/local-models')
def local_model_list():
    return {'models': local_models.list_models()}


@app.post('/api/local-models/{model_id}/install', status_code=202)
async def local_model_install(model_id: str, request: ModelInstall):
    try:
        return local_models.start_install(model_id, request.device)
    except local_models.LocalModelError as exc:
        raise HTTPException(409 if '已有模型' in str(exc) else 422, str(exc)) from None


@app.post('/api/local-models/{model_id}/cancel')
async def local_model_cancel(model_id: str):
    try:
        await local_models.cancel_install(model_id)
        return {'status': 'cancelled'}
    except local_models.LocalModelError as exc:
        raise HTTPException(422, str(exc)) from None


@app.delete('/api/local-models/{model_id}')
async def local_model_remove(model_id: str):
    try:
        await local_models.remove_model(model_id)
        return {'status': 'removed'}
    except local_models.LocalModelError as exc:
        raise HTTPException(409, str(exc)) from None


def is_history_link(path: Path) -> bool:
    """Junctions redirect Windows paths even though they are not symlinks."""
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def write_settings(settings: AppSettings) -> dict:
    with HISTORY_LOCK:
        path = DATA_DIR / 'settings.json'
        if is_history_link(path) or (path.exists() and not path.is_file()):
            raise HTTPException(500, '设置文件无法写入，请检查数据目录。')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=DATA_DIR,
                                             prefix='.settings-', delete=False) as file:
                temporary = Path(file.name)
                json.dump(settings.model_dump(), file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(path)
        except OSError:
            raise HTTPException(500, '设置保存失败，请重试。') from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return settings.model_dump()


def read_settings() -> AppSettings:
    with HISTORY_LOCK:
        path = DATA_DIR / 'settings.json'
        if is_history_link(path):
            raise HTTPException(500, '无法读取设置，请检查数据目录中的 settings.json。')
        if not path.exists():
            default = AppSettings(history_retention_days=30)
            write_settings(default)
            return default
        try:
            return AppSettings.model_validate_json(path.read_text('utf-8'))
        except (ValueError, OSError):
            raise HTTPException(500, '无法读取设置，请检查数据目录中的 settings.json。') from None


@app.get('/api/settings')
def get_settings():
    return read_settings().model_dump()


@app.post('/api/settings')
def save_settings(settings: AppSettings):
    with HISTORY_LOCK:
        payload = write_settings(settings)
        cleanup_expired_history(settings.history_retention_days)
        return payload


def history_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.removesuffix('Z') + ('+00:00' if value.endswith('Z') else ''))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def managed_run_files(folder: Path, run: dict) -> list[Path] | None:
    """Only declared audio plus run.json belong to retention cleanup."""
    if is_history_link(folder) or not folder.is_dir():
        return None
    allowed = {'run.json'}
    filename = run.get('filename')
    if filename is not None:
        if not isinstance(filename, str) or not AUDIO_NAME.fullmatch(filename):
            return None
        allowed.add(filename)
    try:
        files = list(folder.iterdir())
        if any(file.name not in allowed or is_history_link(file) or not file.is_file() for file in files):
            return None
        if not any(file.name == 'run.json' for file in files):
            return None
        return files
    except OSError:
        return None


def stored_run_entries(protected_batches: set[str] | None = None) -> list[tuple[Path, dict]]:
    entries = []
    for folder in DATA_DIR.iterdir():
        if not ID.fullmatch(folder.name) or is_history_link(folder) or not folder.is_dir():
            continue
        file = folder / 'run.json'
        if is_history_link(file) or not file.is_file():
            continue
        try:
            run = json.loads(file.read_text('utf-8'))
            if (not isinstance(run, dict) or run.get('id') != folder.name
                    or not isinstance(run.get('batch_id'), str) or not ID.fullmatch(run['batch_id'])
                    or not isinstance(run.get('status'), str) or run['status'] not in RUN_STATES
                    or not isinstance(run.get('created_at'), str)):
                if (protected_batches is not None and isinstance(run, dict)
                        and isinstance(run.get('batch_id'), str) and ID.fullmatch(run['batch_id'])):
                    protected_batches.add(run['batch_id'])
                continue
            if run['status'] in {'queued', 'running'} and run['id'] not in JOBS and managed_run_files(folder, run) is not None:
                run.update(status='interrupted', error='上次进程已停止。调用可能已计费，请检查后手动重试。')
                write_run(run)
            entries.append((folder, run))
        except (ValueError, TypeError, OSError):
            continue
    return entries


def remove_history_batch(entries: list[tuple[Path, dict]]) -> bool:
    # Stage every member before deleting. A failed rename rolls back the whole
    # group instead of leaving half a comparison visible in history.
    staged = []
    staging = None
    try:
        staging = Path(tempfile.mkdtemp(prefix='.expired-history-', dir=DATA_DIR))
        for folder, run in entries:
            target = staging / folder.name
            folder.rename(target)
            staged.append((folder, target, run))
        if any(managed_run_files(target, run) is None for _, target, run in staged):
            raise OSError('History changed during cleanup')
    except OSError:
        for original, target, _ in reversed(staged):
            try:
                if not original.exists():
                    target.rename(original)
            except OSError:
                pass
        if staging is not None:
            try:
                staging.rmdir()
            except OSError:
                pass
        return False
    # Never recursively delete unexpected files, even if another local process
    # changes a directory while cleanup is in progress.
    for _, target, run in staged:
        files = managed_run_files(target, run)
        if files is None:
            continue
        for file in files:
            try:
                file.unlink()
            except OSError:
                pass
        try:
            target.rmdir()
        except OSError:
            pass
    try:
        staging.rmdir()
    except OSError:
        pass
    return True


def cleanup_expired_history(retention_days: int | None = None, *, current_time: datetime | None = None,
                            entries: list[tuple[Path, dict]] | None = None,
                            protected_batches: set[str] | None = None) -> set[str]:
    with HISTORY_LOCK:
        days = read_settings().history_retention_days if retention_days is None else retention_days
        if days == 0:
            return set()
        cutoff = (current_time or datetime.now(timezone.utc)) - timedelta(days=days)
        groups = {}
        protected_batches = set() if protected_batches is None else protected_batches
        for folder, run in stored_run_entries(protected_batches) if entries is None else entries:
            groups.setdefault(run['batch_id'], []).append((folder, run))
        removed = set()
        for batch_id, batch in groups.items():
            if batch_id in protected_batches:
                continue
            times = [history_time(run['created_at']) for _, run in batch]
            if (any(value is None or value >= cutoff for value in times)
                    or any(run['status'] in {'queued', 'running'}
                           or JOBS.get(run['id'], {}).get('status') in {'queued', 'running'}
                           or managed_run_files(folder, run) is None for folder, run in batch)):
                continue
            if remove_history_batch(batch):
                removed.update(run['id'] for _, run in batch)
                for _, run in batch:
                    JOBS.pop(run['id'], None)
        return removed


def saved_runs(limit: int | None = 200):
    with HISTORY_LOCK:
        protected_batches = set()
        entries = stored_run_entries(protected_batches)
        removed = cleanup_expired_history(entries=entries, protected_batches=protected_batches)
        runs = [run for _, run in entries if run['id'] not in removed]
        return {'runs': sorted(runs, key=lambda run: run['created_at'], reverse=True)[:limit]}


@app.get('/api/history')
def list_history(page: int = 1, page_size: int = Query(default=10, ge=1, le=100)):
    groups = {}
    for run in saved_runs(None)['runs']:
        groups.setdefault(run['batch_id'], []).append(run)
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    batches = [{'batch_id': batch_id, 'runs': sorted(runs, key=lambda run: run['created_at'])}
               for batch_id, runs in groups.items()]
    batches.sort(key=lambda batch: (max(history_time(run['created_at']) or minimum for run in batch['runs']),
                                    batch['batch_id']), reverse=True)
    total = len(batches)
    total_pages = (total + page_size - 1) // page_size
    page = max(1, min(page, total_pages or 1))
    start = (page - 1) * page_size
    return {'batches': batches[start:start + page_size], 'page': page, 'page_size': page_size,
            'total': total, 'total_pages': total_pages}


@app.get("/api/runs")
def list_runs():
    return saved_runs(200)


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return read_run(run_id)


@app.post("/api/runs/{run_id}/score")
def save_score(run_id: str, score: Score):
    with HISTORY_LOCK:
        run = read_run(run_id)
        if run["status"] != "completed":
            raise HTTPException(409, "仅能评价已完成的音频。")
        if run["family"] == "demo":
            raise HTTPException(422, "演示提示音不是 TTS，不计入语音效果评分。")
        run["scores"] = score.model_dump()
        run["scored_at"] = now()
        write_run(run)
        if run_id in JOBS:
            JOBS[run_id].update(run)
        return run


@app.get("/api/runs/{run_id}/audio")
def get_audio(run_id: str):
    run = read_run(run_id)
    if run["status"] != "completed":
        raise HTTPException(404, "音频尚未完成。")
    filename = run.get("filename", "")
    if not re.fullmatch(r"audio\.(wav|mp3|ogg|opus|flac|aac)", filename):
        raise HTTPException(404)
    path = path_for(run_id) / filename
    if not path.is_file():
        raise HTTPException(404, "音频文件不存在。")
    return FileResponse(path, media_type=run["mime_type"], filename=f"{run['provider']}-{run_id[:8]}.{path.suffix[1:]}")


def csv_safe(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + value
    return value


def csv_bytes(runs: list[dict]) -> bytes:
    buf = io.StringIO(newline="")
    columns = ["id", "batch_id", "provider", "family", "status", "text", "text_sha256",
               "params", "elapsed_seconds", "duration_seconds", "real_time_factor", "bytes",
               "naturalness", "accuracy", "expression", "usability", "notes", "error", "created_at"]
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    for run in runs:
        values = dict(run, **(run.get("scores") or {}))
        writer.writerow({key: csv_safe(values.get(key, "")) for key in columns})
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


@app.get("/api/export.csv")
def export_csv():
    return Response(csv_bytes(list_runs()["runs"]), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=tts-results.csv"})


@app.get("/api/batches/{batch_id}/download")
def download_batch(batch_id: str):
    if not ID.fullmatch(batch_id):
        raise HTTPException(404)
    runs = [r for r in saved_runs(None)["runs"] if r["batch_id"] == batch_id]
    if not runs:
        raise HTTPException(404, "找不到这轮对比。")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("results.csv", csv_bytes(runs))
        archive.writestr("results.json", json.dumps(runs, ensure_ascii=False, indent=2))
        for run in runs:
            if run["status"] == "completed":
                filename = run.get("filename", "")
                if re.fullmatch(r"audio\.(wav|mp3|ogg|opus|flac|aac)", filename):
                    file = path_for(run["id"]) / filename
                    if file.is_file():
                        archive.write(file, f"{run['provider']}-{run['id'][:8]}/{filename}")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=tts-{batch_id[:8]}.zip"})


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
