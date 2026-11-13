"""Import the copied backend in a fresh interpreter to catch omitted modules."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.build_windows import ROOT, copy_backend


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
