"""Installer path/cleanup boundaries and optional native NSIS compilation."""
from pathlib import Path, PurePosixPath, PureWindowsPath
import os
import subprocess
import tempfile
import unittest

from scripts.build_installer import (
    find_makensis, installer_script, nsis_quote, nsis_source_path,
    payload_commands, verify_installer,
)


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

    def test_generated_script_uses_native_windows_and_posix_payload_patterns(self):
        cases = (
            (PureWindowsPath(r'D:\a\测试 $TEMP with spaces'),
             r'D:\a\测试 $TEMP with spaces\bundle\*'),
            (PurePosixPath('/tmp/测试 $TEMP with spaces'),
             '/tmp/测试 $TEMP with spaces/bundle/*'),
        )
        for folder, expected in cases:
            with self.subTest(platform=type(folder).__name__):
                script = installer_script(folder / 'bundle', folder / 'setup.exe',
                                          folder / 'cleanup.nsh', '1.2.3', 1)
                self.assertIn(f'!define BUNDLE_FILES "{expected}"\n', script)
                self.assertIn(f'!define INSTALLER_PATH "{folder / "setup.exe"}"\n', script)
                self.assertIn(f'!define UNINSTALL_PAYLOAD "{folder / "cleanup.nsh"}"\n', script)

    def test_native_nsis_compiles_payload_with_spaces_unicode_and_dollars(self):
        try:
            compiler = find_makensis()
        except RuntimeError:
            self.skipTest('NSIS compiler is not installed')
        with tempfile.TemporaryDirectory(prefix='tts nsis 测试 $TEMP ') as temporary:
            folder = Path(temporary)
            bundle = folder / 'bundle'
            (bundle / 'resources/app').mkdir(parents=True)
            (bundle / 'TTS Lab.exe').write_bytes(b'fixture')
            (bundle / 'resources/app/测试 $file.txt').write_text('fixture', encoding='utf-8')
            cleanup, script, compiled = (folder / 'cleanup.nsh', folder / 'installer.nsi',
                                         folder / 'TTS-Lab-1.2.3-win-x64-setup.exe')
            cleanup.write_text(payload_commands(bundle), encoding='utf-8')
            script.write_text(installer_script(bundle, compiled, cleanup, '1.2.3', 1),
                              encoding='utf-8')
            result = subprocess.run([compiler, '-WX', '-V2', '-INPUTCHARSET', 'UTF8', str(script)],
                                    capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            verify_installer(compiled)


if __name__ == '__main__':
    unittest.main()
