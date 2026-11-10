"""Curated downloads, isolated runtimes, and offline subprocess inference."""
from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit, quote

import httpx


def model_root():
    data = Path(os.getenv('TTS_LAB_DATA_DIR', Path(__file__).resolve().parent.parent / 'data'))
    return Path(os.getenv('TTS_LAB_MODELS_DIR', data.parent / 'models')).resolve()


MODELS = {
    'kokoro-v1.1': dict(name='Kokoro v1.1 · 多语言 INT8', engine='kokoro',
        size='约 150 MB 模型文件；CPU 轻量运行', ram='建议 4 GB 以上内存',
        docs='https://github.com/k2-fsa/sherpa-onnx', license='Apache-2.0（模型）',
        repo=None, required=['model.int8.onnx', 'voices.bin', 'tokens.txt', 'lexicon-zh.txt',
                            'lexicon-us-en.txt', 'espeak-ng-data', 'phone-zh.fst', 'date-zh.fst']),
    'qwen3-0.6b': dict(name='Qwen3-TTS · 0.6B CustomVoice', engine='qwen3',
        size='数 GB 模型文件；另需 PyTorch 运行环境', ram='CPU 较慢，建议 8 GB 以上内存或 NVIDIA 显卡',
        docs='https://github.com/QwenLM/Qwen3-TTS', license='Apache-2.0',
        repo='Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice', required=['config.json']),
    'qwen3-1.7b': dict(name='Qwen3-TTS · 1.7B CustomVoice', engine='qwen3',
        size='数 GB 模型文件；另需 PyTorch 运行环境', ram='建议 16 GB 以上内存或 NVIDIA 显卡',
        docs='https://github.com/QwenLM/Qwen3-TTS', license='Apache-2.0',
        repo='Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice', required=['config.json']),
    'cosyvoice-sft': dict(name='CosyVoice · 300M-SFT 预置音色', engine='cosyvoice',
        size='数 GB 模型与运行环境；无需参考录音', ram='CPU 较慢，建议 8 GB 以上内存或 NVIDIA 显卡',
        docs='https://github.com/FunAudioLLM/CosyVoice', license='Apache-2.0',
        repo='FunAudioLLM/CosyVoice-300M-SFT',
        required=['cosyvoice.yaml', 'llm.pt', 'flow.pt', 'hift.pt', 'spk2info.pt',
                  'campplus.onnx', 'speech_tokenizer_v1.onnx']),
}
ASSETS = {
    'kokoro': ('https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-int8-multi-lang-v1_1.tar.bz2',
               '259c9c8bc906d6078e4e0570421885dabb68b539c2f970fcbb890a58eeaf3e9c'),
    'cosyvoice': ('https://github.com/FunAudioLLM/CosyVoice/archive/074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc.tar.gz',
                  '10c587f0853db27667a64a0a2dc0788b1c782cdab223682691db78702082a550'),
    'matcha': ('https://github.com/shivammehta25/Matcha-TTS/archive/dd9105b34bf2be2230f4aa1e4769fb586a3c824e.tar.gz',
               '59c43afae79110e4057eaa43f45b5558b556435da8fc85eab83f0c819ba3358d'),
}
PACKAGES = {
    'kokoro': ['sherpa-onnx==2.0.1', 'numpy==1.26.4', 'soundfile==0.12.1'],
    'qwen3': ['qwen-tts==0.1.1', 'numpy==1.26.4'],
    'cosyvoice': ['numpy==1.26.4', 'conformer==0.3.2', 'diffusers==0.29.0',
        'hydra-core==1.3.2', 'HyperPyYAML==1.2.3', 'inflect==7.3.1', 'librosa==0.10.2',
        'lightning==2.2.4', 'modelscope==1.20.0', 'onnxruntime==1.18.0',
        'openai-whisper==20231117', 'soundfile==0.12.1', 'transformers==4.51.3',
        'x-transformers==2.11.24', 'rich==13.7.1', 'wget==3.2', 'setuptools<81',
        'gdown==5.1.0', 'matplotlib==3.7.5'],
}
TASKS: dict[str, asyncio.Task] = {}
INFERENCE = asyncio.Lock()


class LocalModelError(Exception):
    pass


def spec_for(model_id):
    if model_id not in MODELS:
        raise LocalModelError('未知本地模型。')
    return MODELS[model_id]


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def runtime_python(engine, device):
    return model_root() / 'runtimes' / f'{engine}-{device}' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def runtime_signature(engine, device):
    return hashlib.sha256(json.dumps([PACKAGES[engine], device, 1]).encode()).hexdigest()


def status(model_id):
    spec = spec_for(model_id)
    folder = model_root() / model_id
    result = read_json(folder / 'status.json')
    ready = read_json(folder / 'ready.json')
    device = ready.get('device', result.get('device', 'cpu'))
    runtime = runtime_python(spec['engine'], device)
    files = ready.get('files', {})
    complete = bool(files) and all((folder / name).is_file() and (folder / name).stat().st_size == size
                                   for name, size in files.items())
    marker = read_json(runtime.parent.parent / 'ready.json')
    installed = complete and runtime.is_file() and marker.get('signature') == runtime_signature(spec['engine'], device)
    active = model_id in TASKS and not TASKS[model_id].done()
    if installed:
        result.update(state='ready', message='已安装，可离线生成真实人声。')
    elif not active and result.get('state') in {'installing', 'downloading', 'verifying'}:
        result.update(state='interrupted', message='上次安装已中断，可重试。')
    elif not result:
        result.update(state='not_installed', message='尚未下载。')
    elif result.get('state') == 'ready':
        result.update(state='incomplete', message='文件或运行环境不完整，请重新安装。')
    return {'id': model_id, **{k: spec[k] for k in ('name', 'engine', 'size', 'ram', 'docs', 'license')},
            **result, 'ready': installed, 'device': device, 'busy': active}


def list_models():
    return [status(model_id) for model_id in MODELS]


def _update(model_id, **values):
    path = model_root() / model_id / 'status.json'
    current = read_json(path)
    current.update(values)
    write_json(path, current)


def safe_path(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:
        raise LocalModelError('模型包含不安全文件路径。')
    return p


def extract_archive(archive, destination, include=None):
    """Strip the archive root, rejecting links and path traversal on Windows too."""
    total = 0
    with tarfile.open(archive) as tar:
        for member in tar:
            parts = safe_path(member.name).parts[1:]
            if not parts:
                continue
            relative = '/'.join(parts)
            if include and not any(relative == p or relative.startswith(p + '/') for p in include):
                continue
            if not member.isfile() and not member.isdir():
                raise LocalModelError('模型压缩包不能包含链接或特殊文件。')
            target = destination.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += member.size
            if total > 3 * 1024**3:
                raise LocalModelError('压缩包解压后超出大小限制。')
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, target.open('wb') as dst:
                shutil.copyfileobj(src, dst)


async def extract(archive, destination, include=None):
    task = asyncio.create_task(asyncio.to_thread(extract_archive, archive, destination, include))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # Wait for file writes to finish before allowing a delete or a retry.
        await task
        raise


def allowed_download(url):
    u = urlsplit(str(url))
    host = u.hostname or ''
    return (u.scheme == 'https' and not u.username and not u.password and u.port in (None, 443)
            and (host in {'github.com', 'codeload.github.com', 'release-assets.githubusercontent.com',
                          'huggingface.co', 'hf.co'} or host.endswith(('.huggingface.co', '.hf.co'))))


async def response(client, url):
    for _ in range(8):
        if not allowed_download(url):
            raise LocalModelError('下载地址不在受支持的模型源内。')
        request = client.build_request('GET', url)
        result = await client.send(request, stream=True)
        if result.status_code in {301, 302, 303, 307, 308}:
            next_url = result.url.join(result.headers.get('location', ''))
            await result.aclose()
            url = str(next_url)
            continue
        if result.status_code != 200:
            code = result.status_code
            await result.aclose()
            raise LocalModelError(f'模型下载 HTTP {code}，请检查网络访问后重试。')
        return result
    raise LocalModelError('模型下载重定向过多。')


def file_hash(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    if algorithm == 'sha1':
        h.update(f'blob {path.stat().st_size}\0'.encode())
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


async def download(client, url, target, digest, model_id, algorithm='sha256', expected_size=None):
    if target.is_file() and await asyncio.to_thread(file_hash, target, algorithm) == digest:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.partial')
    r = await response(client, url)
    limit = expected_size if expected_size is not None else 2 * 1024**3
    size = 0
    try:
        with partial.open('wb') as out:
            async for chunk in r.aiter_bytes(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise LocalModelError('下载文件超出预期大小。')
                out.write(chunk)
                _update(model_id, file=target.name, downloaded_bytes=size,
                        total_bytes=expected_size or int(r.headers.get('content-length', 0)))
        if expected_size is not None and size != expected_size:
            raise LocalModelError('模型文件下载不完整。')
        if await asyncio.to_thread(file_hash, partial, algorithm) != digest:
            raise LocalModelError('模型文件完整性校验失败，请重试。')
        partial.replace(target)
    finally:
        await r.aclose()
        partial.unlink(missing_ok=True)


def command_env(device='cpu', offline=False):
    env = dict(os.environ)
    for name in list(env):
        if name.endswith(('_KEY', '_TOKEN')) or name.startswith('AWS_'):
            env.pop(name, None)
    env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               UV_PYTHON_INSTALL_DIR=str(model_root() / 'python'),
               UV_CACHE_DIR=str(model_root() / 'cache'), UV_NATIVE_TLS='true',
               HF_HOME=str(model_root() / 'hub-cache'), DO_NOT_TRACK='1',
               MPLCONFIGDIR=str(model_root() / 'cache' / 'matplotlib'),
               HF_HUB_DISABLE_TELEMETRY='1', TOKENIZERS_PARALLELISM='false')
    if device == 'cpu':
        env['CUDA_VISIBLE_DEVICES'] = ''
    if offline:
        env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_DATASETS_OFFLINE='1')
    return env


async def command(args, *, env, stdin=None, timeout=1800):
    process = await asyncio.create_subprocess_exec(
        *map(str, args), stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
        cwd=model_root(), **({'creationflags': 0x08000200} if os.name == 'nt' else {'start_new_session': True}))
    lines = deque(maxlen=40)

    async def communicate():
        if stdin is not None:
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.close()
        while chunk := await process.stdout.read(2048):
            lines.append(chunk)
        return await process.wait()

    try:
        code = await asyncio.wait_for(communicate(), timeout)
        if code:
            # Bounded installer output is retained locally for diagnosis, not returned to the UI.
            (model_root() / 'last-error.log').write_bytes(b''.join(lines))
            raise LocalModelError('本地运行环境或推理失败；请检查磁盘、设备与网络后重试。')
    finally:
        if process.returncode is None:
            if os.name == 'nt':
                killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(process.pid), '/T', '/F',
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    creationflags=0x08000000)
                await killer.wait()
                if process.returncode is None:
                    process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await process.wait()


def uv_binary():
    try:
        from uv import find_uv_bin
        return find_uv_bin()
    except (ImportError, FileNotFoundError):
        # Windows embedded Python wheels preserve their script payload in this directory.
        data = Path(sys.executable).parent / 'Lib' / 'site-packages' / '_wheel_data'
        candidates = list(data.glob('uv-*.data/scripts/uv.exe'))
        if len(candidates) == 1 and candidates[0].is_file():
            return str(candidates[0])
        raise LocalModelError('缺少模型环境安装器 uv，请安装项目依赖或使用完整桌面发行包。') from None


async def prepare_runtime(model_id, device):
    engine = MODELS[model_id]['engine']
    python = runtime_python(engine, device)
    marker = python.parent.parent / 'ready.json'
    if read_json(marker).get('signature') == runtime_signature(engine, device) and python.is_file():
        return python
    uv = uv_binary()
    env = command_env(device)
    _update(model_id, state='installing', message='正在安装独立 Python 与推理依赖，首次可能需要较长时间。')
    if not python.is_file():
        await command([uv, 'venv', '--managed-python', '--python', '3.11.14', python.parent.parent], env=env)
    if engine != 'kokoro':
        version = '2.6.0' if engine == 'qwen3' else '2.3.1'
        index = 'https://download.pytorch.org/whl/' + ('cpu' if device == 'cpu' else 'cu121' if engine == 'cosyvoice' else 'cu124')
        await command([uv, 'pip', 'install', '--python', python, '--index-url', index,
                       f'torch=={version}', f'torchaudio=={version}'], env=env)
    constraints = python.parent.parent / 'build-constraints.txt'
    constraints.write_text('setuptools<81\n', encoding='utf-8')
    await command([uv, 'pip', 'install', '--python', python, '--build-constraints', constraints,
                   *PACKAGES[engine]], env=env)
    await command([uv, 'pip', 'check', '--python', python], env=env)
    write_json(marker, {'engine': engine, 'device': device, 'schema': 1,
                        'signature': runtime_signature(engine, device)})
    return python


async def download_hub(client, model_id, folder):
    spec = MODELS[model_id]
    meta = read_json(folder / 'snapshot.json')
    if meta.get('repo') != spec['repo'] or not meta.get('siblings'):
        r = await response(client, f'https://huggingface.co/api/models/{spec["repo"]}/revision/main?blobs=true')
        try:
            raw = bytearray()
            async for chunk in r.aiter_bytes(64 * 1024):
                raw.extend(chunk)
                if len(raw) > 8 * 1024**2:
                    raise LocalModelError('模型清单过大。')
            meta = json.loads(raw)
        finally:
            await r.aclose()
    revision = meta.get('sha', '')
    if len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise LocalModelError('无法确定模型快照版本。')
    meta['repo'] = spec['repo']
    meta['revision'] = revision
    # Freeze metadata before downloading, so retrying never mixes two revisions.
    write_json(folder / 'snapshot.json', meta)
    files = [f for f in meta['siblings'] if PurePosixPath(f['rfilename']).suffix.lower()
             in {'.json', '.safetensors', '.bin', '.pt', '.onnx', '.yaml', '.model', '.tiktoken', '.txt', '.npy', '.npz', '.md'}]
    total = sum(f.get('lfs', {}).get('size', f.get('size', 0)) for f in files)
    if total > 20 * 1024**3 or shutil.disk_usage(model_root()).free < total + 3 * 1024**3:
        raise LocalModelError('磁盘空间不足，或模型超过 20 GB 上限。')
    for i, file in enumerate(files):
        name = file['rfilename']
        safe_path(name)
        lfs = file.get('lfs')
        digest = lfs.get('sha256') if lfs else file.get('blobId')
        if not digest:
            raise LocalModelError('模型源缺少文件校验信息。')
        _update(model_id, message=f'正在下载模型文件 {i + 1}/{len(files)}', state='downloading')
        await download(client, f'https://huggingface.co/{spec["repo"]}/resolve/{revision}/{quote(name, safe="/")}',
                       folder / name, digest, model_id, 'sha256' if lfs else 'sha1',
                       file.get('size', lfs.get('size') if lfs else None))


async def install(model_id, device):
    spec = MODELS[model_id]
    folder = model_root() / model_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'ready.json').unlink(missing_ok=True)
    _update(model_id, state='installing', message='准备下载。', device=device, downloaded_bytes=0, total_bytes=0)
    try:
        if shutil.disk_usage(model_root()).free < 3 * 1024**3:
            raise LocalModelError('至少需要 3 GB 可用磁盘空间；大型模型需要更多空间。')
        python = await prepare_runtime(model_id, device)
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=20), follow_redirects=False) as client:
            if spec['engine'] == 'kokoro':
                _update(model_id, state='downloading', message='正在下载 Kokoro 多语言模型。')
                archive = model_root() / 'downloads' / 'kokoro.tar.bz2'
                await download(client, ASSETS['kokoro'][0], archive, ASSETS['kokoro'][1], model_id)
                await extract(archive, folder / 'model')
            else:
                await download_hub(client, model_id, folder / 'model')
            if spec['engine'] == 'cosyvoice':
                for asset, dest in [('cosyvoice', folder / 'source'), ('matcha', folder / 'source' / 'third_party' / 'Matcha-TTS')]:
                    archive = model_root() / 'downloads' / f'{asset}.tar.gz'
                    await download(client, ASSETS[asset][0], archive, ASSETS[asset][1], model_id)
                    include = ['cosyvoice', 'LICENSE', 'NOTICE'] if asset == 'cosyvoice' else ['matcha', 'LICENSE']
                    await extract(archive, dest, include)
        if not all((folder / 'model' / name).exists() for name in spec['required']):
            raise LocalModelError('模型文件不完整，请重新下载。')
        _update(model_id, state='verifying', message='正在离线加载模型并试生成，首次 CPU 加载可能需要几分钟。')
        from .catalog import CATALOG
        fields = next(p for p in CATALOG if p['id'] == 'local_' + spec['engine'])['fields']
        params = {f['key']: f['default'] for f in fields}
        output = folder / 'smoke.wav'
        async with INFERENCE:
            await run_worker(model_id, device, params, '你好。', output, python)
        from .providers import _audio
        _audio(output.read_bytes())
        output.unlink()
        files = {f.relative_to(folder).as_posix(): f.stat().st_size for parent in ['model', 'source']
                 for f in (folder / parent).rglob('*') if f.is_file() and '__pycache__' not in f.parts}
        write_json(folder / 'ready.json', {'device': device, 'files': files})
        _update(model_id, state='ready', message='已安装，可离线生成真实人声。', file='', downloaded_bytes=0, total_bytes=0)
    except asyncio.CancelledError:
        _update(model_id, state='interrupted', message='安装已取消，可稍后重试。')
        raise
    except Exception as exc:
        message = str(exc) if isinstance(exc, LocalModelError) else '安装失败，请检查网络、可用空间和运行环境后重试。'
        _update(model_id, state='failed', message=message)


def start_install(model_id, device):
    spec = spec_for(model_id)
    if device not in {'cpu', 'cuda'} or (spec['engine'] == 'kokoro' and device != 'cpu'):
        raise LocalModelError('该模型不支持所选运行设备。')
    if any(not task.done() for task in TASKS.values()):
        raise LocalModelError('已有模型正在安装，请等待完成或取消后再操作。')
    if INFERENCE.locked():
        raise LocalModelError('正在使用本地模型，请等待生成或删除完成后再安装。')
    task = asyncio.create_task(install(model_id, device))
    TASKS[model_id] = task
    return {'id': model_id, 'state': 'installing'}


async def cancel_install(model_id):
    spec_for(model_id)
    task = TASKS.get(model_id)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def shutdown():
    for model_id in list(TASKS):
        await cancel_install(model_id)


async def remove_model(model_id):
    spec_for(model_id)
    if any(not task.done() for task in TASKS.values()) or INFERENCE.locked():
        raise LocalModelError('正在安装或生成，请完成后再删除模型。')
    async with INFERENCE:
        folder = model_root() / model_id
        if folder.exists():
            await asyncio.to_thread(shutil.rmtree, folder)


async def run_worker(model_id, device, params, text, output, python=None):
    spec = spec_for(model_id)
    folder = model_root() / model_id
    request = dict(engine=spec['engine'], model_dir=str(folder / 'model'),
                   source_dir=str(folder / 'source'), text=text, params=params,
                   device=device, output=str(output))
    await command([python or runtime_python(spec['engine'], device), Path(__file__).with_name('local_worker.py')],
                  env=command_env(device, offline=True), stdin=json.dumps(request, ensure_ascii=False).encode(), timeout=1800)


async def synthesize_local(engine, text, params):
    model_id = params['model']
    spec = spec_for(model_id)
    if spec['engine'] != engine:
        raise LocalModelError('所选模型不属于该本地引擎。')
    async with INFERENCE:
        current = status(model_id)
        if not current['ready']:
            raise LocalModelError('请先在“本地模型配置”中下载并安装所选模型。')
        with tempfile.TemporaryDirectory(dir=model_root()) as temporary:
            output = Path(temporary) / 'audio.wav'
            try:
                await run_worker(model_id, current['device'], params, text, output)
            except asyncio.TimeoutError:
                raise LocalModelError('本地生成超时，请缩短文本或使用更快的运行设备。') from None
            if output.stat().st_size > 20_000_000:
                raise LocalModelError('生成音频超过大小上限，请缩短文本。')
            return output.read_bytes()
