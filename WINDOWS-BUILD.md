# Windows x64 portable build

Build host: Python 3.10+ with pip, network access to official Python/Electron releases and PyPI. Node/npm are not required for this packaging script. The recipient needs neither Python nor Node.js installed.

```sh
python scripts/build_windows.py
```

Output: `dist/TTS-Lab-<version>-win-x64.zip` and its SHA256 sidecar. Extract the entire ZIP before opening `TTS Lab.exe`. Target: Windows 10/11 x64. This is a portable application, not an installer. App code is not separately signed; Windows may display a reputation warning. Follow your normal security policy rather than bypassing warnings automatically.

## Locked inputs

- Electron 45.2.3 official Windows x64 archive. Its SHA256 was checked against the release's `SHASUMS256.txt`.
- Python 3.13.17 official embedded Windows x64 runtime. Its SHA256 was checked against the python.org release page.
- All Python runtime dependencies, including AWS boto3, are pinned in `requirements-desktop.lock`, with exact wheel filenames and SHA256 values also in `runtime-lock.json`. Only binary Windows cp313 or universal wheels are accepted. Colorama is explicit to avoid cross-platform marker omissions.

`runtime-lock.json` links the official checksum sources. Cached archives and wheels are checked on every build; a mismatch fails closed. No fetched installer or target executable is executed by the packaging script. Dependencies retain their bundled metadata and licenses; Electron and Python licenses remain alongside their runtime files.

## Contents and isolation

- `TTS Lab.exe` and Electron runtime files
- `resources/app/main.js` and desktop JavaScript files
- `resources/backend/run.py`, `resources/backend/tts_lab/`
- `resources/python/python.exe`, Python standard library and `Lib/site-packages/`
- `.env.example` configuration template, not credentials
- `BUILD-MANIFEST.json` with hashes for every payload file except the manifest itself

The backend source allowlist is exactly `run.py`, `tts_lab/{__init__,app,catalog,providers}.py`, and `tts_lab/static/{app.js,index.html,style.css}`. The build excludes `.env`, user `data`, caches, virtual environments and repository metadata. `botocore/data` is SDK service metadata and must remain included. The Python `_pth` isolates imports from globally installed Python packages. Build caches remain under `.build-cache/`, outside the release ZIP.

## Verification

```sh
python scripts/build_windows.py --verify dist/TTS-Lab-<version>-win-x64
```

Checks: locked archive/wheel SHA256, final ZIP CRC, x64 PE executable/native-extension headers, required application files, all installed dependency versions, absence of excluded private paths, and payload manifest hashes.

Cross-building does **not** test native Windows launch, SmartScreen behavior, desktop interaction, audio playback or paid provider APIs. Test those on a Windows x64 machine before broad distribution. To update dependencies, deliberately regenerate both lock files for Windows CPython 3.13, inspect conditional dependencies for Windows, and retest; do not relax hash checks.

## GitHub Actions

The workflow runs only manually (`workflow_dispatch`) or when pushing a `v*` tag. Ordinary commits and pull requests do not trigger it. It performs the packaging script's integrity checks but no automated application tests or native smoke test, as requested.

Manual runs upload the portable ZIP and SHA256 as an Actions artifact (30-day retention); they do not publish a Release. Tag runs require the tag to match `v` plus `package.json` version, then build and publish both files as a GitHub Release. Re-running an existing tag replaces its release assets. Only the release job receives `contents: write`; no provider secrets are used. This file describes the configured workflow, not evidence that a remote run has passed.
