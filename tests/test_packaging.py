"""Import the copied backend in a fresh interpreter to catch omitted modules."""
import os
import json
import shutil
import struct
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.build_windows import ROOT, BACKEND_FILES, copy_backend, find_desktop_exe, verify_pe


class PackagingTests(unittest.TestCase):
    def test_packaged_backend_imports_without_checkout_on_python_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            backend = folder / 'backend'
            copy_backend(ROOT / 'tts_lab', backend / 'tts_lab')
            env = dict(os.environ, TTS_LAB_DATA_DIR=str(folder / 'data'),
                       TTS_LAB_MODELS_DIR=str(folder / 'models'), TTS_LAB_DESKTOP='0')
            code = ('import sys; sys.path.insert(0, ' + repr(str(backend)) + '); '
                    'from tts_lab import app, reference_audio; '
                    'assert app.ROOT.parent == __import__("pathlib").Path(' + repr(str(backend)) + '); '
                    'assert app.state()["references"] == []')
            result = subprocess.run([sys.executable, '-I', '-c', code], cwd=folder,
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_explicit_desktop_binary_must_exist_and_be_windows_x64(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'tts-lab.exe'
            with self.assertRaises(ValueError):
                find_desktop_exe(executable)
            # This is an invalid non-Windows executable, not a usable Tauri bundle.
            executable.write_bytes(b'not a Windows application')
            self.assertEqual(find_desktop_exe(executable), executable.resolve())
            with self.assertRaises(ValueError):
                verify_pe(executable)
            binary = bytearray(256)
            binary[:2] = b'MZ'
            struct.pack_into('<I', binary, 0x3c, 128)
            binary[128:134] = b'PE\0\0\x4c\x01'
            executable.write_bytes(binary)
            with self.assertRaises(ValueError):
                verify_pe(executable)
            binary[132:134] = b'\x64\x86'
            executable.write_bytes(binary)
            verify_pe(executable)

    def test_backend_allowlist_includes_desktop_bridge_and_excludes_private_files(self):
        self.assertIn('static/desktop.js', BACKEND_FILES)
        with tempfile.TemporaryDirectory() as temporary:
            source, target = Path(temporary) / 'source', Path(temporary) / 'target'
            for name in BACKEND_FILES:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture')
            (source / 'credentials.env').write_text('private')
            (source / 'local.backup.py').write_text('private')
            copy_backend(source, target)
            self.assertEqual({p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()},
                             set(BACKEND_FILES))

    def test_packaged_smoke_checks_deepseek_contract_without_real_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            backend = bundle / 'resources/backend'
            copy_backend(ROOT / 'tts_lab', backend / 'tts_lab')
            shutil.copy2(ROOT / 'run.py', backend / 'run.py')
            script = ROOT / 'scripts/check_bundle_backend.py'
            version = json.loads((ROOT / 'package.json').read_text())['version']
            # Simulate the embedded runtime's isolated _pth in a fresh process.
            # Neither the working directory nor Python path contains the checkout.
            code = ('import sys, runpy; sys.path.insert(0, ' + repr(str(backend)) + '); '
                    'sys.argv = ' + repr([str(script), '--bundle', str(bundle), '--version', version]) + '; '
                    'runpy.run_path(' + repr(str(script)) + ', run_name="__main__")')
            env = dict(os.environ, DEEPSEEK_API_KEY='inherited-value-must-be-cleared')
            result = subprocess.run([sys.executable, '-I', '-c', code], cwd=bundle,
                                    env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['status'], 'passed')
            self.assertEqual(report['deepseek_mock_voices'], ['stella', 'tide'])
            self.assertEqual(report['real_cloud_requests'], 0)
            self.assertNotIn('inherited-value-must-be-cleared', result.stdout + result.stderr)
