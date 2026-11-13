# Windows x64 Tauri installer and portable build

The desktop source uses Tauri 2. The package version remains 0.5.0 during development; the published v0.5.0 assets use Electron. The [Windows candidate workflow](https://github.com/whoarei/tts-lab/actions/runs/43363388939) passed release compilation, bundled-backend checks, native WebView2 desktop smoke, and silent install/upgrade/uninstall checks for commit `5bc1bd448986a7d4bc7c397ab36576e1f81ea1c6`. These instructions do not announce a new release; remaining interactive/runtime-provisioning limits are recorded below and in `VERIFICATION.json`.

Build on Windows with Python 3.10+ and pip, Node.js 22+, the Rust MSVC toolchain, Visual Studio C++ build tools, and WebView2. Building the installer also needs NSIS 3 (`makensis`; tested with 3.11). Dependency downloads need access to npm, crates.io, official Python/Microsoft distributions, and PyPI. The recipient does not need Python, Node.js, Rust, or development tools installed.

```powershell
npm ci
python scripts/prepare_frontend.py
cargo build --manifest-path src-tauri/Cargo.toml --release --locked --features custom-protocol --target x86_64-pc-windows-msvc
python scripts/third_party_notices.py
python scripts/build_windows.py --desktop-exe src-tauri/target/x86_64-pc-windows-msvc/release/tts-lab.exe --installer
```

The packaging script accepts an existing compiled x64 desktop executable; it does not compile Rust or download Electron. If `--desktop-exe` is omitted, it looks for the Windows target executable above, then `src-tauri/target/release/tts-lab.exe`. The compiled executable must come from the same source and version as the Python payload. `scripts/prepare_frontend.py` stages only the frontend assets before compilation; the Tauri executable embeds them.

Outputs:

- `dist/TTS-Lab-<version>-win-x64-setup.exe` and `.exe.sha256`: current-user installer. The default directory remains `%LOCALAPPDATA%\Programs\TTS Lab`; no administrator access is requested. It creates a Start menu entry, an optional desktop shortcut, and a Windows uninstall entry.
- `dist/TTS-Lab-<version>-win-x64.zip` and `.zip.sha256`: portable folder. Extract the entire ZIP before opening `TTS Lab.exe`. The host must already have Microsoft Edge WebView2 Runtime installed.

Target: Windows 10/11 x64. The app is not code signed. Python and the application dependencies remain bundled; local voice models and their separate inference environments are downloaded through the app when requested. Removing Electron does not remove Python or model requirements, and it does not directly accelerate synthesis.

The Windows candidate above produced a 55,836,422-byte portable ZIP (53.25 MiB) and a 44,507,027-byte setup EXE (42.45 MiB), compared with 212,120,731 and 154,417,010 bytes for the published Electron v0.5.0 assets. Those measured reductions are approximately 74% and 71%. They apply to that exact candidate artifact; later changes to packaged documentation can change byte counts. Locked-input and payload SHA256 checks and portable ZIP CRC ran inside CI, and both SHA256 sidecars were uploaded. Independent candidate-artifact download verification was blocked locally by the environment proxy with HTTP 403; it has not been claimed as completed.

## WebView2 installation

Tauri uses the system Microsoft Edge WebView2 Runtime instead of bundling Chromium. The setup includes a small Microsoft bootstrapper, not the full offline browser runtime. It checks the current-user and machine EdgeUpdate client registry in both 32-bit and 64-bit views. If a runtime exists, setup skips the bootstrapper and does not need a runtime download. If it is missing, setup runs the bootstrapper with `/silent /install` and checks the registry again. This path needs internet access; an offline host must provision WebView2 beforehand.

The bootstrapper is fetched from an immutable official Microsoft CDN URL and its SHA256 is locked in `runtime-lock.json`. It is included only in the setup executable. Runtime detection/installation happens before uninstalling the existing TTS Lab payload, so failure stops the upgrade without first removing the previous application. Microsoft maintains the shared Evergreen runtime; uninstalling TTS Lab does not remove it.

For a ZIP alone, omit `--installer`. If NSIS is not on PATH, pass `--makensis /path/to/makensis`. To wrap an already-built, unchanged bundle:

```sh
python scripts/build_installer.py --bundle dist/TTS-Lab-<version>-win-x64
```

## Dependencies and payload

- Tauri and Rust dependencies are resolved by `src-tauri/Cargo.lock`; Windows builds use `--locked`.
- JavaScript development tooling is pinned in `package-lock.json`. No frontend framework or remote CDN is required by the interface.
- Python 3.13.17 is the official embedded Windows x64 runtime, with its archive SHA256 checked against the Python release source.
- All 27 Python runtime distributions, including boto3 and the uv model-environment installer, retain their existing versions and hashes in `requirements-desktop.lock` and `runtime-lock.json`. Only Windows cp313 or universal binary wheels are accepted. Colorama remains explicit to avoid platform-marker omissions.

Cached archives, the WebView2 bootstrapper, and wheels are hash checked before use. The packaging script does not execute fetched Windows installers or target executables. Python and dependency license/metadata files remain included. `scripts/third_party_notices.py` collects license texts for the locked Windows Rust dependency graph and the selected Rust standard library; missing notices stop generation. The generated `THIRD-PARTY-NOTICES.txt` is required by packaging and accompanies the executable. Regenerate it with the toolchain used for the release.

The portable payload includes:

- `TTS Lab.exe`, the native Tauri executable with embedded frontend assets
- `resources/app/package.json` version metadata and `credentials.env.example`
- `resources/backend/run.py` and the allowlisted `resources/backend/tts_lab/` source
- `resources/python/python.exe`, Python standard library, and `Lib/site-packages/`
- usage/configuration templates and `BUILD-MANIFEST.json`, which records every payload file hash except the manifest itself
- `THIRD-PARTY-NOTICES.txt` with Rust dependency and standard-library notices

The backend allowlist includes `run.py`, `tts_lab/{__init__,app,catalog,providers,local_models,local_worker,reference_audio}.py`, and the application static files including the Tauri desktop adapter. A fresh-interpreter import regression test checks the copied backend outside the checkout. The build excludes actual credentials, user data, downloaded models, caches, virtual environments, and repository metadata. `botocore/data` is SDK service metadata and remains included. Python `_pth` isolates imports from globally installed packages. Build caches stay in `.build-cache/`, outside the ZIP. uv remains under `Lib/site-packages/_wheel_data/uv-0.9.9.data/scripts/uv.exe` and is located explicitly by the model manager.

## Application data and upgrades

Both distributions retain `%LOCALAPPDATA%\TTS Lab` independently of the install location and Tauri's application identifier:

- `credentials.env`: private user-owned plaintext provider configuration
- `data/variants.json`: saved schemes and selection
- `data/`: historical runs, audio, and scores
- `models/`: downloaded voice models, inference environments, and reference recordings

The native layer starts Python on an ephemeral loopback port and privately adds its per-launch desktop token to backend requests. A custom protocol serves API/media responses to the embedded frontend; pages do not receive the desktop token. Export, credential editing, restart, and directory-opening actions use fixed native commands. Browser-source mode remains available with `python run.py`.

Close TTS Lab before upgrading. The installer retains the previous uninstall identity and detects an executable in use instead of terminating it. Upgrading in place first removes the old installer's exact known payload after WebView2 is available, then copies the new payload. The uninstaller deletes only packaged files and removes empty application directories; it does not recursively delete user-created files or touch `%LOCALAPPDATA%\TTS Lab`. The NSIS bootstrap is an x86 PE that checks for an x64 Windows 10/11 host; the bundled application is x64.

## Verification

```sh
python scripts/build_windows.py --verify dist/TTS-Lab-<version>-win-x64
```

The build checks locked input SHA256 values, final ZIP CRC, x64 PE executable/native-extension headers, backend file completeness, all installed dependency versions and Windows dependency closure, excluded private paths, and manifest hashes. The installer builder verifies its bundle before compiling, checks the output PE header, and writes a SHA256 sidecar.

A successful cross-build does not establish native installation, upgrade/uninstall, shortcuts, SmartScreen behavior, audio playback, GUI behavior, or paid provider API correctness. Windows checks should cover first launch with/without WebView2, installer runtime-download failure, paths containing spaces/Chinese text, an upgrade from Electron with saved schemes and history, refusal to replace a running app, and retained user data after uninstall. Do not relax hash checks when changing Python dependencies; deliberately regenerate both Windows CPython 3.13 lock files and verify the dependency closure.

The successful Windows candidate used an existing WebView2 runtime. Its automated checks exercised fresh silent setup, reinstall, upgrade from the hash-verified published Electron v0.5.0 installer, obsolete Electron payload removal, uninstall, and sentinel-file preservation in both the user profile and install directory. Native desktop smoke exercised binary `File` reference upload/preview, window maximize/restore, offline single/comparison generation, audio metadata and muted playback, native WAV/ZIP/CSV exports, native-command boundaries, and the model page. First-time WebView2 installation/download failure, interactive wizard and Save As choices, actual speaker output, SmartScreen, and replacement while the app is running remain unverified. Real paid cloud calls and full GPU inference were not part of this migration validation.

## GitHub Actions

The Windows workflow is available through `workflow_dispatch` and `v*` tags. It installs Rust/MSVC, JavaScript tooling, and NSIS; prepares the embedded frontend; compiles the Tauri executable with the Rust lockfile; and builds setup and portable distributions. The bundled Windows Python checks backend imports, provider catalog, offline demo WAV synthesis, and reference storage in temporary user directories without provider credentials. Native desktop smoke checks are reported separately from backend checks; see the current workflow and `VERIFICATION.json` for executed coverage.

Manual runs upload the setup EXE, ZIP, and SHA256 sidecars as an Actions artifact without publishing a Release. A tag must match `v` plus the package version before release publication. Only the release job receives `contents: write`; no provider secrets are used. Workflow configuration is not evidence that a remote run has passed. The Tauri migration has not been released merely because development retains the previous package version.
