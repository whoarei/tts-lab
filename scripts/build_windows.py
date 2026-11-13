#!/usr/bin/env python3
"""Build a Windows x64 portable ZIP and optional per-user NSIS installer.

No target executables are run on a cross-build host. Runtime archives and every
wheel are SHA256-locked. The Tauri desktop executable is built separately with
Cargo. Requires Python 3.10+ and pip on the packaging host.
"""
from __future__ import annotations

import argparse
import email
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {'.env', '.venv', '__pycache__', '.git', 'node_modules'}


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def verify_hash(path, expected):
    actual = sha256(path)
    if actual != expected:
        raise ValueError(f'SHA256 mismatch: {path.name}: {actual} != {expected}')


def download(item, cache):
    target = cache / item['filename']
    if not target.exists():
        temporary = target.with_suffix(target.suffix + '.partial')
        print(f'Downloading {item["url"]}', flush=True)
        with urllib.request.urlopen(item['url'], timeout=180) as src, temporary.open('wb') as dst:
            shutil.copyfileobj(src, dst)
        verify_hash(temporary, item['sha256'])
        temporary.replace(target)
    verify_hash(target, item['sha256'])
    return target


def safe_members(archive):
    for item in archive.infolist():
        p = PurePosixPath(item.filename)
        if p.is_absolute() or '..' in p.parts or '\\' in item.filename or ':' in item.filename:
            raise ValueError(f'Unsafe archive member: {item.filename}')
        if (item.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f'Symlink in archive: {item.filename}')
        yield item


def extract(archive, target):
    with zipfile.ZipFile(archive) as z:
        z.extractall(target, members=list(safe_members(z)))


def install_wheel(wheel, site):
    # Wheel "installation" is extraction plus .data/{purelib,platlib} relocation.
    # Console scripts are not needed; Python starts run.py directly.
    with zipfile.ZipFile(wheel) as z:
        for member in safe_members(z):
            if member.is_dir():
                continue
            parts = PurePosixPath(member.filename).parts
            if parts[0].endswith('.data'):
                if parts[1] in ('purelib', 'platlib'):
                    parts = parts[2:]
                elif parts[1] in ('scripts', 'headers', 'data'):
                    # Preserve metadata/licenses and ancillary data without executing scripts.
                    parts = ('_wheel_data',) + parts
                else:
                    raise ValueError(f'Unknown wheel data category: {member.filename}')
            dest = site.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(member))


BACKEND_FILES = (
    '__init__.py', 'app.py', 'catalog.py', 'providers.py', 'local_models.py', 'local_worker.py', 'reference_audio.py',
    'static/app.js', 'static/desktop.js', 'static/index.html', 'static/style.css',
)


def copy_backend(source, destination):
    # Exact allowlist prevents private files, backups and local settings from
    # entering the bundle even if someone adds them inside the source package.
    for name in BACKEND_FILES:
        f = source / name
        if f.is_symlink() or source.is_symlink() or any((source / parent).is_symlink() for parent in Path(name).parents):
            raise ValueError(f'Backend symlink forbidden: {f}')
        if not f.is_file():
            raise ValueError(f'Missing backend source: {f}')
        out = destination / name
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, out)


def verify_pe(path):
    with path.open('rb') as f:
        if f.read(2) != b'MZ':
            raise ValueError(f'Missing MZ header: {path}')
        f.seek(0x3c)
        f.seek(struct.unpack('<I', f.read(4))[0])
        if f.read(4) != b'PE\0\0' or f.read(2) != b'\x64\x86':
            raise ValueError(f'Not an x64 PE: {path}')


def verify_bundle(bundle, lock):
    required = ['TTS Lab.exe', 'resources/python/python.exe',
                'resources/python/python313.dll', 'resources/python/python313._pth',
                'resources/app/package.json', 'resources/app/credentials.env.example',
                'resources/backend/run.py', 'resources/backend/tts_lab/app.py',
                'resources/python/Lib/site-packages/_wheel_data/uv-0.9.9.data/scripts/uv.exe',
                '.env.example', 'README.md', 'WINDOWS-BUILD.md', 'api_contracts.md', 'THIRD-PARTY-NOTICES.txt']
    for name in required:
        if not (bundle / name).is_file():
            raise ValueError(f'Missing bundle member: {name}')
    for name in ('resources/app/main.js', 'resources/app/preload.js', 'chrome_100_percent.pak',
                 'resources.pak', 'icudtl.dat', 'LICENSE.electron.txt'):
        if (bundle / name).exists():
            raise ValueError(f'Unexpected Electron payload: {name}')
    package = json.loads((bundle / 'resources/app/package.json').read_text())
    if package.get('desktop') != 'tauri':
        raise ValueError('Desktop metadata must identify the Tauri application')
    backend_root = bundle / 'resources/backend'
    actual_backend = {f.relative_to(backend_root).as_posix() for f in backend_root.rglob('*') if f.is_file()}
    expected_backend = {'run.py'} | {f'tts_lab/{name}' for name in BACKEND_FILES}
    if actual_backend != expected_backend:
        raise ValueError('Backend payload differs from exact source allowlist')
    for f in bundle.rglob('*'):
        if f.is_symlink():
            raise ValueError(f'Bundle symlink: {f}')
        rel = f.relative_to(bundle)
        if any(p in FORBIDDEN for p in rel.parts) or f.suffix == '.pyc':
            raise ValueError(f'Forbidden private/cache member: {rel}')
        # botocore/data is required SDK service metadata, not user data.
        if rel.parts[0] == 'data' or rel.parts[:3] == ('resources', 'backend', 'data'):
            raise ValueError(f'User data in bundle: {rel}')
    for executable in ['TTS Lab.exe', 'resources/python/python.exe',
                       'resources/python/Lib/site-packages/_wheel_data/uv-0.9.9.data/scripts/uv.exe']:
        verify_pe(bundle / executable)
    site = bundle / 'resources/python/Lib/site-packages'
    installed = {}
    for metadata in site.glob('*.dist-info/METADATA'):
        m = email.message_from_bytes(metadata.read_bytes())
        installed[m['Name'].lower().replace('_', '-')] = m['Version']
    for wheel in lock['wheels']:
        if installed.get(wheel['name'].lower().replace('_', '-')) != wheel['version']:
            raise ValueError(f'Missing/wrong dependency: {wheel["name"]}')
    for name in ['fastapi', 'uvicorn', 'httpx', 'dotenv', 'boto3', 'botocore', 'pydantic_core']:
        if not (site / name).is_dir():
            raise ValueError(f'Missing import package: {name}')
    for native in site.rglob('*.pyd'):
        verify_pe(native)
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.markers import default_environment
    target_env = dict(default_environment(), sys_platform='win32', os_name='nt',
                      platform_system='Windows', platform_machine='AMD64',
                      python_version='3.13', python_full_version=lock['python']['version'],
                      implementation_name='cpython', platform_python_implementation='CPython', extra='')
    for metadata in site.glob('*.dist-info/METADATA'):
        m = email.message_from_bytes(metadata.read_bytes())
        for value in m.get_all('Requires-Dist', []):
            req = Requirement(value)
            if req.marker and not req.marker.evaluate(target_env):
                continue
            version = installed.get(req.name.lower().replace('_', '-'))
            if version is None or version not in req.specifier:
                raise ValueError(f'Unsatisfied Windows dependency: {m["Name"]}: {req}')
    manifest = bundle / 'BUILD-MANIFEST.json'
    if manifest.exists():
        expected = json.loads(manifest.read_text())['files']
        actual_names = {f.relative_to(bundle).as_posix() for f in bundle.rglob('*') if f.is_file() and f != manifest}
        if actual_names != {item['path'] for item in expected}:
            raise ValueError('Payload files differ from build manifest')
        for item in expected:
            verify_hash(bundle / item['path'], item['sha256'])
    return len(installed)


def find_desktop_exe(explicit=None):
    candidates = [Path(explicit)] if explicit else [
        ROOT / 'src-tauri/target/x86_64-pc-windows-msvc/release/tts-lab.exe',
        ROOT / 'src-tauri/target/release/tts-lab.exe',
    ]
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.resolve()
    raise ValueError('Tauri release executable is missing. Run cargo build --release --locked '
                     '--features custom-protocol --target x86_64-pc-windows-msvc in src-tauri first, or pass --desktop-exe.')


def build(args):
    lock = json.loads((ROOT / 'runtime-lock.json').read_text())
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    desktop_exe = find_desktop_exe(args.desktop_exe)
    verify_pe(desktop_exe)
    python_archive = download(lock['python'], cache)
    wheels = cache / 'wheels'
    wheels.mkdir(exist_ok=True)
    if any(not (wheels / w['filename']).exists() for w in lock['wheels']):
        subprocess.run([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:',
                        '--platform', 'win_amd64', '--python-version', '313',
                        '--implementation', 'cp', '--abi', 'cp313', '--no-deps',
                        '--require-hashes', '-r', str(ROOT / 'requirements-desktop.lock'),
                        '-d', str(wheels)], check=True)
    for wheel in lock['wheels']:
        verify_hash(wheels / wheel['filename'], wheel['sha256'])
    package = json.loads((ROOT / 'package.json').read_text())
    version = package['version']
    bundle = args.output.resolve() / f'TTS-Lab-{version}-win-x64'
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    shutil.copy2(desktop_exe, bundle / 'TTS Lab.exe')
    python = bundle / 'resources/python'
    extract(python_archive, python)
    site = python / 'Lib/site-packages'
    site.mkdir(parents=True)
    for wheel in lock['wheels']:
        install_wheel(wheels / wheel['filename'], site)
    (python / 'python313._pth').write_text('python313.zip\n.\nLib/site-packages\n../backend\nimport site\n', encoding='utf-8')
    backend = bundle / 'resources/backend'
    backend.mkdir(parents=True)
    shutil.copy2(ROOT / 'run.py', backend / 'run.py')
    copy_backend(ROOT / 'tts_lab', backend / 'tts_lab')
    app = bundle / 'resources/app'
    app.mkdir(parents=True)
    shutil.copy2(ROOT / 'desktop/credentials.env.example', app / 'credentials.env.example')
    (app / 'package.json').write_text(json.dumps({'name': package['name'], 'version': version,
                                                 'desktop': 'tauri', 'private': True}, indent=2) + '\n')
    shutil.copy2(ROOT / '.env.example', bundle / '.env.example')
    for name in ['README.md', 'WINDOWS-BUILD.md', 'api_contracts.md', 'WINDOWS-README.txt', '先看这里.txt', 'VERIFICATION.json', 'runtime-lock.json', 'THIRD-PARTY-NOTICES.txt']:
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, bundle / name)
    count = verify_bundle(bundle, lock)
    manifest = {'target': 'win32-x64', 'desktop': 'tauri', 'version': version, 'runtime': lock,
                'verification': {'dependency_distributions': count, 'pe_x64_headers': True,
                                 'native_windows_launch': 'not run by cross-platform build'},
                'files': [{'path': f.relative_to(bundle).as_posix(), 'sha256': sha256(f)}
                          for f in sorted(bundle.rglob('*')) if f.is_file()]}
    (bundle / 'BUILD-MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    verify_bundle(bundle, lock)
    archive = bundle.with_name(bundle.name + '.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(bundle.rglob('*')):
            if f.is_file():
                z.write(f, f.relative_to(bundle.parent))
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise ValueError('Final ZIP CRC check failed')
    archive.with_suffix('.zip.sha256').write_text(f'{sha256(archive)}  {archive.name}\n')
    print(f'Built {archive} ({archive.stat().st_size:,} bytes); {count} dependency distributions verified.')
    if args.installer:
        try:
            from .build_installer import build_installer
        except ImportError:
            from build_installer import build_installer
        build_installer(bundle, args.output, args.makensis, cache=args.cache)
    print('Windows launch, GUI behavior and live provider calls require separate native testing.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop-exe', type=Path, help='Release-mode Tauri Windows x64 executable')
    parser.add_argument('--cache', type=Path, default=ROOT / '.build-cache')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--installer', action='store_true', help='Also build a per-user setup EXE (requires NSIS 3)')
    parser.add_argument('--makensis', help='NSIS compiler executable for --installer')
    parser.add_argument('--verify', type=Path, help='Verify an existing extracted bundle without rebuilding')
    args = parser.parse_args()
    if args.verify:
        print(f'Verified {verify_bundle(args.verify, json.loads((ROOT / "runtime-lock.json").read_text()))} dependency distributions')
    else:
        build(args)
