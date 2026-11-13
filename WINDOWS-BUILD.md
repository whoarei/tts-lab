# Windows x64 installer and portable build

Build host: Python 3.10+ with pip, network access to official Python/Electron releases and PyPI. Building the installer additionally needs NSIS 3 (`makensis`; tested with 3.11). Node/npm are not required for these packaging scripts. The recipient needs neither Python nor Node.js installed.

```sh
python scripts/build_windows.py --installer
```

Outputs:

- `dist/TTS-Lab-<version>-win-x64-setup.exe` and `.exe.sha256`: installer for the current user. The default directory is `%LOCALAPPDATA%\Programs\TTS Lab`; no administrator access is requested. It creates a Start menu entry, an optional desktop shortcut, and a Windows uninstall entry.
- `dist/TTS-Lab-<version>-win-x64.zip` and `.zip.sha256`: portable folder. Extract the entire ZIP before opening `TTS Lab.exe`.

Target: Windows 10/11 x64. App code is not separately signed. The installer and application keep credentials, saved schemes, audio history, and downloaded models in `%LOCALAPPDATA%\TTS Lab`, outside the installed program. Upgrading or uninstalling does not delete this user data. Close TTS Lab before installing an update; the installer checks whether its executable is in use and does not force-terminate it.

For a ZIP alone, omit `--installer`. If NSIS is not on PATH, pass `--makensis /path/to/makensis`. To wrap an already-built, unchanged bundle without downloading or rebuilding it:

```sh
python scripts/build_installer.py --bundle dist/TTS-Lab-<version>-win-x64
```

## Locked inputs

- Electron 45.2.3 official Windows x64 archive. Its SHA256 was checked against the release's `SHASUMS256.txt`.
- Python 3.13.17 official embedded Windows x64 runtime. Its SHA256 was checked against the python.org release page.
- All Python runtime dependencies (including the uv model-runtime installer), including AWS boto3, are pinned in `requirements-desktop.lock`, with exact wheel filenames and SHA256 values also in `runtime-lock.json`. Only binary Windows cp313 or universal wheels are accepted. Colorama is explicit to avoid cross-platform marker omissions.

`runtime-lock.json` links the official checksum sources. Cached archives and wheels are checked on every build; a mismatch fails closed. No fetched installer or target executable is executed by the packaging script. Dependencies retain their bundled metadata and licenses; Electron and Python licenses remain alongside their runtime files.

## Contents and isolation

- `TTS Lab.exe` and Electron runtime files
- `resources/app/main.js`, the sandboxed `preload.js` bridge, and desktop JavaScript files
- `resources/backend/run.py`, `resources/backend/tts_lab/`
- `resources/python/python.exe`, Python standard library and `Lib/site-packages/`
- `.env.example` configuration template, not credentials
- `BUILD-MANIFEST.json` with hashes for every payload file except the manifest itself

The backend source allowlist is exactly `run.py`, `tts_lab/{__init__,app,catalog,providers,local_models,local_worker}.py`, and `tts_lab/static/{app.js,index.html,style.css}`. The build excludes `.env`, user `data`, caches, virtual environments and repository metadata. `botocore/data` is SDK service metadata and must remain included. The Python `_pth` isolates imports from globally installed Python packages. Build caches remain under `.build-cache/`, outside the release ZIP. Downloaded local models are also excluded; users install them through the app configuration after extraction. The uv Windows executable is retained under `Lib/site-packages/_wheel_data/uv-0.9.9.data/scripts/uv.exe` and is located explicitly by the local model manager.

## Verification

```sh
python scripts/build_windows.py --verify dist/TTS-Lab-<version>-win-x64
```

Checks: locked archive/wheel SHA256, final ZIP CRC, x64 PE executable/native-extension headers, required application files, all installed dependency versions, absence of excluded private paths, and payload manifest hashes.

The installer builder verifies the bundle before compiling it, checks the output PE header, and writes a SHA256 sidecar. Its uninstaller is generated from the exact payload file list: it deletes those files and removes empty application directories, without recursively deleting user-created files. For an upgrade in the existing install location, the previous uninstaller removes its known payload before the new files are copied. The NSIS bootstrap is an x86 PE that checks for an x64 Windows 10/11 host; the bundled application remains x64.

Cross-building does **not** test native Windows install/uninstall, upgrade, launch, SmartScreen behavior, desktop interaction, audio playback or paid provider APIs. Validate those on Windows x64 before broad distribution. In particular, check installation paths containing spaces/Chinese text, shortcuts, upgrading with saved schemes/history, refusing to replace a running app, and retaining `%LOCALAPPDATA%\TTS Lab` after uninstall. To update dependencies, deliberately regenerate both lock files for Windows CPython 3.13, inspect conditional dependencies for Windows, and retest; do not relax hash checks.

## GitHub Actions

The workflow runs only manually (`workflow_dispatch`) or when pushing a `v*` tag. Ordinary commits and pull requests do not trigger it. It installs NSIS 3.11 and builds both distributions. It performs the packaging script's integrity checks; application tests and native smoke tests are run separately.

Manual runs upload the setup EXE, portable ZIP, and both SHA256 files as an Actions artifact (30-day retention); they do not publish a Release. Tag runs require the tag to match `v` plus `package.json` version, then publish these four files as a GitHub Release. Re-running an existing tag replaces its release assets. Only the release job receives `contents: write`; no provider secrets are used. This file describes the configured workflow, not evidence that a remote run has passed.
