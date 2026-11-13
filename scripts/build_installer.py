#!/usr/bin/env python3
"""Wrap a verified Windows bundle in a per-user NSIS installer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile

try:
    from .build_windows import ROOT, sha256, verify_bundle
except ImportError:
    from build_windows import ROOT, sha256, verify_bundle


def nsis_quote(value):
    """Escape runtime NSIS strings, including dollar expansion."""
    value = str(value)
    if any(c in value for c in '\r\n\0') or '${' in value:
        raise ValueError('Unsupported control character or NSIS expression in installer path')
    return value.replace('$', '$$').replace('"', '$\\"')


def nsis_source_path(value):
    """Compile-time File/OutFile/include paths do not expand runtime dollars."""
    value = str(value)
    if any(c in value for c in '\r\n\0"') or '${' in value:
        raise ValueError('Unsupported character in NSIS source path')
    return value


def installer_script(bundle, compiled, cleanup, version, kilobytes):
    definitions = {
        'APP_VERSION': version,
        'WINDOWS_VERSION': version + '.0',
        # NSIS on Windows does not accept a wildcard appended with a POSIX slash
        # to an otherwise native path. Join before converting the path to text.
        'BUNDLE_FILES': bundle / '*',
        'INSTALLER_PATH': compiled,
        'UNINSTALL_PAYLOAD': cleanup,
        'INSTALLED_KB': kilobytes,
    }
    return ('\n'.join(f'!define {key} "{nsis_source_path(value)}"'
                      for key, value in definitions.items()) + '\n' +
            f'!include "{nsis_source_path(ROOT / "scripts/windows-installer.nsi")}"\n')


def payload_commands(bundle):
    """Uninstall only the exact packaged files, never arbitrary user files."""
    files, directories = [], []
    for item in sorted(bundle.rglob('*')):
        if item.is_symlink():
            raise ValueError(f'Installer payload symlink: {item}')
        relative = item.relative_to(bundle)
        if any('"' in part or any(c in part for c in '*?\r\n') for part in relative.parts):
            raise ValueError(f'Unsupported Windows payload path: {relative}')
        target = nsis_quote(str(relative).replace('/', '\\'))
        if item.is_file():
            files.append(f'  Delete "$INSTDIR\\{target}"')
        elif item.is_dir():
            directories.append((len(relative.parts), f'  RMDir "$INSTDIR\\{target}"'))
    return '\n'.join(files + [line for _, line in sorted(directories, reverse=True)]) + '\n'


def find_makensis(command=None):
    if command:
        compiler = shutil.which(str(command))
        if compiler:
            return compiler
    else:
        compiler = shutil.which('makensis')
        if compiler:
            return compiler
        # NSIS is installed here by the standard Windows installer/Chocolatey.
        import os
        for variable in ('ProgramFiles(x86)', 'ProgramFiles'):
            if os.environ.get(variable):
                candidate = Path(os.environ[variable]) / 'NSIS' / 'makensis.exe'
                if candidate.is_file():
                    return str(candidate)
    raise RuntimeError('NSIS makensis was not found. Install NSIS 3, or pass --makensis /path/to/makensis.')


def verify_installer(path):
    # NSIS uses an x86 bootstrap executable even for x64 payloads.
    with path.open('rb') as file:
        if file.read(2) != b'MZ':
            raise ValueError('Installer is not a Windows executable')
        file.seek(0x3c)
        offset = struct.unpack('<I', file.read(4))[0]
        file.seek(offset)
        if file.read(4) != b'PE\0\0' or file.read(2) not in (b'\x4c\x01', b'\x64\x86'):
            raise ValueError('Invalid installer PE header')
    if path.stat().st_size < 32768:
        raise ValueError('Installer output is unexpectedly small')


def build_installer(bundle, output, compiler=None):
    bundle, output = Path(bundle).resolve(), Path(output).resolve()
    if not (bundle / 'BUILD-MANIFEST.json').is_file():
        raise ValueError('Installer requires a bundle with BUILD-MANIFEST.json; run build_windows.py first')
    lock = json.loads((ROOT / 'runtime-lock.json').read_text())
    verify_bundle(bundle, lock)
    package = json.loads((bundle / 'resources/app/package.json').read_text())
    version = package['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Installer requires a three-part numeric package version')
    compiler = find_makensis(compiler)
    output.mkdir(parents=True, exist_ok=True)
    installer = output / f'TTS-Lab-{version}-win-x64-setup.exe'
    kilobytes = sum(p.stat().st_size for p in bundle.rglob('*') if p.is_file()) // 1024
    with tempfile.TemporaryDirectory(prefix='.tts-lab-installer-', dir=output) as temporary:
        folder = Path(temporary)
        compiled = folder / installer.name
        cleanup = folder / 'uninstall-payload.nsh'
        cleanup.write_text(payload_commands(bundle), encoding='utf-8')
        script = folder / 'installer.nsi'
        script.write_text(installer_script(bundle, compiled, cleanup, version, kilobytes),
                          encoding='utf-8')
        subprocess.run([compiler, '-WX', '-V2', '-INPUTCHARSET', 'UTF8', str(script)], check=True)
        verify_installer(compiled)
        compiled.replace(installer)
    installer.with_suffix('.exe.sha256').write_text(f'{sha256(installer)}  {installer.name}\n')
    print(f'Built {installer} ({installer.stat().st_size:,} bytes).')
    print('Per-user install/uninstall and upgrade still require native Windows validation.')
    return installer


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True, help='Verified extracted Windows bundle')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--makensis', help='NSIS compiler executable')
    arguments = parser.parse_args()
    build_installer(arguments.bundle, arguments.output, arguments.makensis)
