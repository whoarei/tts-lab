"""Installer cleanup boundaries and literal-path handling; no Windows execution."""
from pathlib import Path
import os
import tempfile
import unittest

from scripts.build_installer import nsis_quote, nsis_source_path, payload_commands


class InstallerTests(unittest.TestCase):
    def test_cleanup_only_removes_the_packaged_files_and_empty_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder)
            (bundle / 'resources/app').mkdir(parents=True)
            (bundle / 'TTS Lab.exe').write_bytes(b'fixture')
            (bundle / 'resources/app/preload.js').write_text('fixture')
            commands = payload_commands(bundle)
        self.assertIn('Delete "$INSTDIR\\TTS Lab.exe"', commands)
        self.assertIn('Delete "$INSTDIR\\resources\\app\\preload.js"', commands)
        self.assertLess(commands.index('RMDir "$INSTDIR\\resources\\app"'),
                        commands.index('RMDir "$INSTDIR\\resources"'))
        self.assertNotIn('/r', commands)
        self.assertNotIn('$LOCALAPPDATA', commands)
        self.assertNotIn('*', commands)

    def test_nsis_literals_cannot_expand_variables_or_inject_commands(self):
        self.assertEqual(nsis_quote('C:/build/$TEMP/"file"'), 'C:/build/$$TEMP/$\\"file$\\"')
        for invalid in ('x\nFile x', 'x\rFile x', 'x\0y', '${APP_VERSION}'):
            with self.assertRaises(ValueError):
                nsis_quote(invalid)

    def test_payload_symlinks_and_wildcards_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder)
            # Windows rejects wildcard filenames before they reach the builder.
            if os.name != 'nt':
                (bundle / 'bad*name').write_text('fixture')
                with self.assertRaises(ValueError):
                    payload_commands(bundle)
                (bundle / 'bad*name').unlink()
            try:
                (bundle / 'linked').symlink_to(bundle / 'outside')
            except OSError:
                self.skipTest('Symlinks are unavailable')
            with self.assertRaises(ValueError):
                payload_commands(bundle)

    def test_compile_time_paths_preserve_spaces_unicode_and_literal_dollars(self):
        value = 'C:/Users/测试/$TEMP build/output'
        self.assertEqual(nsis_source_path(value), value)
        for invalid in ('x\nFile x', 'x\rFile x', 'x\0y', 'x"y', '${APP_VERSION}'):
            with self.assertRaises(ValueError):
                nsis_source_path(invalid)


if __name__ == '__main__':
    unittest.main()
