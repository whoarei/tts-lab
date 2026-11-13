---
name: release
description: 准备和发布 TTS Lab Windows x64 安装版与便携版；用于版本升级、发布前检查、打标签和核验 GitHub Release。
---

# TTS Lab 版本发布

沿用仓库的 `.github/workflows/windows-build.yml`，先阅读 `WINDOWS-BUILD.md`。
当前桌面壳为 Tauri 2，Windows 复用系统 WebView2；Python 与锁定依赖仍随包提供。已发布的 Electron 资产保持原样，源码迁移本身不等于发布新版。
所有命令从仓库根目录执行。使用当前 checkout，保留用户已有修改。
只要求添加 skill 或准备版本时，不执行远程发布；明确要求发布时，完成检查后继续发布和核验，无需重复确认。

## 1. 确定版本与提交

- 检查 `git status --short`、当前分支和 origin，明确要发布的提交。
- 用户未确定版本号时，先检查 `package.json`、已发布标签及上次发布以来的改动，提出一个明确的推荐版本号，并说明推荐原因；不要静默沿用当前版本或自行跳过推荐。
- 推荐依据：兼容性修复通常升补丁版本，兼容的新功能通常升次版本，破坏性变更按项目约定升级并明确兼容性影响。对于 `0.x` 阶段，遵循已有版本策略，不仅因有破坏性变更就自动推荐 `1.0.0`。
- 推荐说明包含“当前/上次发布版本 → 推荐版本”和对应改动，例如：`0.2.0 → 0.2.1`，因为本次仅修复问题，没有新增功能或改变兼容性。检查推荐标签尚未被使用；若当前清单已包含未发布的版本升级，应结合改动评估，不重复递增。
- 用户已指定版本号时沿用其要求；最终将确定的版本同步到 `package.json`，标签必须是 `v<version>`。
- 若要求升级版本，使用 `npm version <version> --no-git-tag-version` 同步 `package.json` 与 `package-lock.json`；同时更新 `src-tauri/Cargo.toml` 的应用版本、`src-tauri/tauri.conf.json` 的版本、`tts_lab/app.py` 中 `/api/state` 返回的版本，并同步 `src-tauri/Cargo.lock` 中本地 `tts-lab` 包版本。更新 Cargo.lock 时检查差异只含应用自身版本，不顺带更新依赖。检查桌面界面、说明文档和 `VERIFICATION.json` 的版本文字，按需更新。
- 构建前确认 npm 两个清单、Rust 应用及 Cargo.lock、Tauri 配置、后端 API 版本全部相同；Windows 工作流也会逐项核对。NSIS 安装器要求三段纯数字版本。
- 不顺带升级 Tauri、Rust 工具链、Python、WebView2 bootstrapper 或依赖锁定版本。运行时升级须另行验证；修改应用自身版本不需要重新解析依赖图。
- 发布前所有预期变更应已提交。不要擅自提交用户无关改动，也不要覆盖已有本地或远程标签。

## 2. 发布前检查

激活项目 Python 环境后执行：

```bash
. .venv/bin/activate
python -m pip check
python -m unittest discover -s tests -v
npm ci
npm test
node --check tts_lab/static/app.js
node --check tts_lab/static/desktop.js
python scripts/prepare_frontend.py
TTS_LAB_PYTHON="$(command -v python)" cargo test --manifest-path src-tauri/Cargo.toml --locked --no-default-features
```

记录实际通过、失败和未执行的检查；失败时先定位原因，不将未运行的检查视为通过。
Rust 后端集成测试需要明确设置 `TTS_LAB_PYTHON`；未设置时不能把测试进程成功退出当成真实 Python 启动验证。
在临时数据目录启动本机 Python 服务后，运行 `tests/browser_smoke.py` 和 `tests/browser_tauri_smoke.py`，核对三个页面、参考录音、试听、导出与历史。后者模拟原生桥，不代替真实 WebView2 自定义协议与桌面检查。
现有 Actions 运行 Rust 测试、Tauri 编译、安装器回归检查、打包、完整性校验以及包内 Python / 原生桌面冒烟检查，不代替上述完整应用测试。

如需试打包，在 Windows MSVC Rust 环境准备前端后，编译锁定的桌面程序并生成许可证说明：

```powershell
python scripts/prepare_frontend.py
cargo build --manifest-path src-tauri/Cargo.toml --release --locked --features custom-protocol --target x86_64-pc-windows-msvc
python scripts/third_party_notices.py
python scripts/build_windows.py --desktop-exe src-tauri/target/x86_64-pc-windows-msvc/release/tts-lab.exe --installer
python scripts/build_windows.py --verify "dist/TTS-Lab-<version>-win-x64"
```

保留 TLS、SHA256 和锁定依赖校验，Rust 构建使用 `Cargo.lock` / `--locked`。`THIRD-PARTY-NOTICES.txt` 必须使用本次构建工具链生成并随包分发；不要忽略缺失的依赖许可证说明。
安装版仅附带锁定的小型 Microsoft WebView2 bootstrapper：缺少运行时时联网安装，已有运行时则直接复用。便携版要求系统已有 WebView2；不能宣称 Tauri 包在缺少运行时的电脑上仍可完全离线启动。
保留原安装器标识、当前用户安装路径和 `%LOCALAPPDATA%\TTS Lab` 的凭证、方案、历史及模型目录。升级检查应涵盖旧 Electron 版本到 Tauri 的安装替换，并确认卸载保留个人数据。
Linux 构建不证明 Windows 原生启动或音频播放成功；模拟浏览器桥也不能替代 Windows 桌面冒烟。记录实际输出体积，不把先前估算写成测量结果。
离线验证使用 demo 提示音；发布流程不需要真实服务凭证或付费 API 调用。

## 3. 发布

先确定版本、提交 SHA，并检查本地和远程同名标签。目标提交须已推送到 origin。
仅当同名标签不存在、工作区干净且检查完成时，创建并推送该标签：

```bash
version=$(node -p "require('./package.json').version")
tag="v$version"
git tag "$tag" HEAD
git push origin "refs/tags/$tag"
```

推送会触发构建并自动创建 GitHub Release，附带安装 EXE、便携 ZIP 及各自的 SHA256 文件。
手动触发 `windows-build.yml` 只生成 Actions artifact，不创建 Release。
如工作流配置了迁移验证分支，其分支 push 也只构建验证，不创建 Release。
已有标签或 Release 时先报告现状；不要自动强推标签或重跑覆盖资产，当前工作流会拒绝替换已有 Release。

## 4. 核验与报告

- 用 `gh run list --workflow windows-build.yml --commit <SHA> --event push` 找到本次运行，核对提交后用 `gh run watch <run-id> --exit-status` 等待结果。
- 用 `gh release view <tag> --json tagName,assets,url` 确认 Release 的 `TTS-Lab-<version>-win-x64-setup.exe`、`TTS-Lab-<version>-win-x64.zip` 及两份对应 `.sha256` 均存在。
- 下载本次四个资产到独立临时目录，按 SHA256 sidecar 分别校验安装 EXE 与便携 ZIP；不要只凭工作流启动就报告发布成功。
- 核对原生桌面冒烟报告是否实际通过，并区分包内 Python、WebView2 桌面播放/导出、安装升级卸载与真实云服务验证的覆盖范围。确认发布包没有 Electron 运行时，且有完整 Python 后端、依赖与第三方许可证说明。
- 失败时检查本次日志，修复已确认的问题；区分构建失败、权限问题和发布失败。已有 GitHub 认证应先复用，不在聊天中索取密钥。
- 最后报告版本、提交、检查结果、工作流与 Release 链接，以及 Windows 原生测试等未验证项。未发布时明确说明停在哪一步。
