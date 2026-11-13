---
name: release
description: 准备和发布 TTS Lab Windows x64 安装版与便携版；用于版本升级、发布前检查、打标签和核验 GitHub Release。
---

# TTS Lab 版本发布

沿用仓库的 `.github/workflows/windows-build.yml`，先阅读 `WINDOWS-BUILD.md`。
所有命令从仓库根目录执行。使用当前 checkout，保留用户已有修改。
只要求添加 skill 或准备版本时，不执行远程发布；明确要求发布时，完成检查后继续发布和核验，无需重复确认。

## 1. 确定版本与提交

- 检查 `git status --short`、当前分支和 origin，明确要发布的提交。
- 用户未确定版本号时，先检查 `package.json`、已发布标签及上次发布以来的改动，提出一个明确的推荐版本号，并说明推荐原因；不要静默沿用当前版本或自行跳过推荐。
- 推荐依据：兼容性修复通常升补丁版本，兼容的新功能通常升次版本，破坏性变更按项目约定升级并明确兼容性影响。对于 `0.x` 阶段，遵循已有版本策略，不仅因有破坏性变更就自动推荐 `1.0.0`。
- 推荐说明包含“当前/上次发布版本 → 推荐版本”和对应改动，例如：`0.2.0 → 0.2.1`，因为本次仅修复问题，没有新增功能或改变兼容性。检查推荐标签尚未被使用；若当前清单已包含未发布的版本升级，应结合改动评估，不重复递增。
- 用户已指定版本号时沿用其要求；最终将确定的版本同步到 `package.json`，标签必须是 `v<version>`。
- 若要求升级版本，使用 `npm version <version> --no-git-tag-version` 同步两个 npm 清单；检查桌面界面、说明文档中的版本文字，按需更新。
- 不顺带升级 Electron/Python 或修改依赖锁文件。运行时升级须另行验证。
- 发布前所有预期变更应已提交。不要擅自提交用户无关改动，也不要覆盖已有本地或远程标签。

## 2. 发布前检查

激活项目 Python 环境后执行：

```bash
. .venv/bin/activate
python -m pip check
python -m unittest discover -s tests -v
TTS_LAB_PYTHON="$(command -v python)" npm test
node --check tts_lab/static/app.js
```

记录实际通过、失败和未执行的检查；失败时先定位原因，不将未运行的检查视为通过。
现有 Actions 运行安装器回归检查、打包和完整性校验，不代替上述应用测试。

如需本地试打包，可执行：

```bash
python scripts/build_windows.py --installer
python scripts/build_windows.py --verify "dist/TTS-Lab-<version>-win-x64"
```

保留 TLS、SHA256 和锁定依赖校验。Linux 构建不证明 Windows 原生启动或音频播放成功。
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
已有标签或 Release 时先报告现状；不要自动强推标签或重跑覆盖资产。

## 4. 核验与报告

- 用 `gh run list --workflow windows-build.yml --commit <SHA> --event push` 找到本次运行，核对提交后用 `gh run watch <run-id> --exit-status` 等待结果。
- 用 `gh release view <tag> --json tagName,assets,url` 确认 Release 的 `TTS-Lab-<version>-win-x64-setup.exe`、`TTS-Lab-<version>-win-x64.zip` 及两份对应 `.sha256` 均存在。
- 下载本次四个资产到独立临时目录，按 SHA256 sidecar 分别校验安装 EXE 与便携 ZIP；不要只凭工作流启动就报告发布成功。
- 失败时检查本次日志，修复已确认的问题；区分构建失败、权限问题和发布失败。已有 GitHub 认证应先复用，不在聊天中索取密钥。
- 最后报告版本、提交、检查结果、工作流与 Release 链接，以及 Windows 原生测试等未验证项。未发布时明确说明停在哪一步。
