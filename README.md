# TTS Lab

在电脑上输入文字，选择声音，试听并比较。支持本地离线模型和多家云端语音服务，音频与测试记录保存在本机。

**0.8.1 支持点击云服务卡片配置凭证，保存后立即生效**。API Key 默认隐藏，小眼睛仅查看本次输入；已保存的 Key 不回显，留空保留，输入新值替换。保留 0.8.0 加入的 DeepSeek 官方 TTS。保留 0.7.0 加入的桌面在线更新，可检查、下载并安装新版本。桌面自 0.6.0 起使用 Tauri 2 和系统 WebView2，保留现有 Python 后端、语音服务、本地模型及个人数据目录。

[已发布的 0.6.0](https://github.com/whoarei/tts-lab/releases/tag/v0.6.0) 便携 ZIP 为 **53.25 MiB**、安装版为 **42.46 MiB**，较 Electron 0.5.0 分别减少约 **74%** 和 **71%**。发布后已独立下载并核验 SHA256、ZIP CRC 和全部载荷哈希；此前迁移候选构建的独立证据保留在 `VERIFICATION.json`。缺少 WebView2 时的首次安装、交互式安装向导和实际扬声器输出仍需实机验证。

## 安装与启动

支持 **Windows 10 / 11 x64**，Python 和应用依赖随包提供，无需另装 Python 或 Node.js。Tauri 版需要 **Microsoft Edge WebView2 Runtime**：安装版会检测并在缺少时联网安装；便携版须先确保电脑已安装该运行时。使用已有运行时无需重复下载，离线电脑请提前准备运行时。

- **安装版**：运行 [TTS-Lab-0.8.1-win-x64-setup.exe](https://github.com/whoarei/tts-lab/releases/download/v0.8.1/TTS-Lab-0.8.1-win-x64-setup.exe)，按提示安装，再从开始菜单打开 TTS Lab。默认安装到当前用户目录，无需管理员权限。
- **便携版**：完整解压 [TTS-Lab-0.8.1-win-x64.zip](https://github.com/whoarei/tts-lab/releases/download/v0.8.1/TTS-Lab-0.8.1-win-x64.zip)，双击文件夹里的 `TTS Lab.exe`。请保留旁边的全部文件。

升级前先退出应用。安装版可直接安装新版；便携版解压到新文件夹后打开。个人配置、模型和历史会保留，卸载安装版也不会删除这些数据。

## 在线更新

**0.7.0、0.8.0 可在应用内检查并升级到本版本。已发布的 0.6.0 没有在线更新功能，需要手动安装 0.8.1。** 从包含更新功能的桌面版起，可在应用内获取后续版本。

在桌面版“模型配置”中的“应用更新”查看版本，也可点击左下角的“应用更新”入口。默认在启动时检查新版本，每天最多一次；可取消“启动时检查更新”，之后仍可手动点击“检查更新”。检查只读取发布信息，不会自动下载或安装。

发现新版本后，点击“下载更新”查看进度；下载完整并通过校验后，再点击“安装并重启”。请先等待语音生成、方案保存和模型安装结束；任务未结束时，已校验的更新包会保留，结束后可再次安装。安装版会运行安装器升级，便携版会替换原文件夹中属于应用的文件；个人凭证、方案、历史、模型和不属于应用的文件会保留。网络失败时可重试检查或下载，也可从发布页手动安装。

更新来自本项目固定的 GitHub Release 地址，使用 HTTPS、下载长度和 SHA256 校验。当前发布渠道没有独立的更新签名，SHA256 也不代替发布者签名；更新依赖该 GitHub 仓库与发布账号的可信性。本机浏览器源码模式不提供原地更新。

## 第一次使用

左侧有三个页面：

| 页面 | 用途 |
| --- | --- |
| 文本测试 | 选择一个已保存的方案，输入文字，生成并试听 |
| 方案对比 | 配置、命名和保存方案，用同一段文字比较多个声音 |
| 模型配置 | 下载本地模型，配置云服务凭证，查看可用状态 |

第一次可以用默认的离线测试音检查播放和导出。**测试音只是提示音，不会朗读文字。** 想听真实人声，请先按下方说明配置一种本地模型或云服务。

### 使用本地人声

1. 打开“模型配置”，推荐先选 **Kokoro + CPU**，点击“下载并安装”。首次需要联网，请等待安装完成。
2. 到“方案对比”选择相应的本地引擎、已安装模型和音色，给方案命名并保存。
3. 到“文本测试”选中这个方案，输入文字后生成。

本地生成时文字留在电脑上，无需 API Key。每次最多 300 字。还可下载 Qwen3-TTS 或 CosyVoice；它们需要更多磁盘和内存，CPU 生成可能较慢。使用 NVIDIA GPU 需要兼容显卡与驱动。

当前源码还支持 **IndexTTS-2**（CPU / NVIDIA GPU）和 **GLM-TTS**（需要 NVIDIA GPU）。在“模型配置”中下载安装，然后在“方案对比”中导入你有权使用的参考录音：3–10 秒、8–48 kHz、单声道或双声道、16-bit PCM WAV，不超过 2 MB。GLM 还需填写准确逐字稿。保存方案后可在“文本测试”中重复使用，录音和逐字稿保留在本机。

IndexTTS 连同辅助模型约需 8 GB；GLM 权重约 9 GB，另需独立 Python 与推理环境。安装会校验下载文件，并使用官方样例试生成，通过才标为可用。IndexTTS 已在 Linux CPU 完成真实生成和应用 API 验证；GLM 完整权重及 GPU、Windows 原生推理尚未实测。

### 使用云端人声

1. 打开“模型配置”，点击要使用的云服务卡片，在弹窗中填写 API Key 等配置。
2. 点击“保存配置”，无需重启即可生效。Key 默认隐藏，输入时可点击小眼睛确认；保存后只显示“已配置”，再次打开时留空保留原 Key，输入新值替换。取消或按 Esc 不保存。
3. 在“方案对比”选择服务，填写账号支持的模型、音色等参数，保存方案。
4. 在“文本测试”选择方案，输入文字，确认云端调用后生成。

支持 OpenAI、DeepSeek、Gemini、Qwen / DashScope、MiniMax、小米 MiMo、字节豆包 / Seed TTS、ElevenLabs、StepAudio、Fish Audio、Azure、Google Cloud TTS 和 AWS Polly。只需配置想使用的服务，Azure 会显示地域、Google 会显示项目 ID，AWS 可选择本机 Profile 或 Access Key。配置弹窗不会发起语音请求；生成或模型安装期间须等待任务结束再保存。服务显示“已配置”后，仍需首次调用确认 Key、余额和模型权限。

DeepSeek 使用独立的官方语音接口：在 DeepSeek 卡片中保存 API Key 后选择 Stella（默认女声）或 Tide（男声），可开关 Markdown 过滤。无需填写聊天模型名，不提供语速或风格指令。应用固定接收 24 kHz、单声道、16-bit PCM 并保存为 WAV，不上传参考录音。官方说明支持普通话、粤语、英语、日语、韩语、马来语、阿拉伯语、印尼语、泰语和越南语，自动识别语言；本应用仍限制每次最多 2000 字。

云服务会收到你的文字和参数，**可能收费**。不要提交不适合外传的文本，也不要分享凭证文件。生成失败或超时后，先检查服务状态，再决定是否重试。

### 新增语音服务与模型覆盖

下表汇总新增语音服务与本地模型；2026-11-13 核对官方文档。DeepSeek 于 0.8.0 新增，尚未使用真实账号调用验证。

| 服务 / 模型 | 方式与配置 | 官方依据 |
| --- | --- | --- |
| 小米 MiMo | `MIMO_API_KEY`；MiMo-V2.5-TTS 预置音色和 VoiceDesign；VoiceDesign 需音色描述 | [官方接口](https://mimo.mi.com/docs/en-US/api/audio/tts-post) |
| 字节豆包 / Seed TTS | `VOLCENGINE_TTS_API_KEY`；使用豆包语音新版控制台 Key；TTS 2.0 和已有 ICL 2.0 音色 | [官方接口](https://www.volcengine.com/docs/6561/1598757) |
| StepAudio | `STEP_API_KEY`；3 / 2.5；地域应与 Key 一致；括号中的内容作为指令，不朗读 | [官方接口](https://platform.stepfun.ai/docs/en/api-reference/audio/create-audio) |
| Fish Audio / Fish Speech 托管 API | `FISH_AUDIO_API_KEY` 和已有声音模型 ID；本次接入官方云 API，未接入本地 Fish Speech 权重 | [官方接口](https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech) |
| GLM-TTS | 本地 NVIDIA GPU；参考录音和逐字稿 | [官方仓库](https://github.com/zai-org/GLM-TTS) |
| IndexTTS-2 | 本地 CPU / GPU；参考录音；不混用 2.5 权重 | [官方仓库](https://github.com/index-tts/index-tts) |
| DeepSeek | `DEEPSEEK_API_KEY`；独立 TTS API；Stella / Tide，Markdown 过滤；本应用固定 PCM 转 WAV，无需模型名，不支持调速、风格指令或参考音频 | [官方 TTS 文档](https://api-docs.deepseek.com/guides/tts/) |

已有凭证文件不会被升级覆盖；使用 DeepSeek 时，点击对应服务卡片填写 API Key 并保存即可。官方接口上限为 10,000 计费字符与 64 KiB 请求体（中文字符按 2、其他字符按 1 计费），本应用沿用更小的 2000 字限制。HTTP 错误或超时不会自动重试，避免意外重复调用。

本地模型的参考录音保存在 `models/references/`；参数记录录音 ID 与 SHA256，导出 ZIP 不包含原始参考音频。更详细的接口限制与验证边界见 `api_contracts.md` 和 `VERIFICATION.json`。

## 比较多个方案

在“方案对比”中最多配置 6 个方案，也可以复制同一个服务，只调整音色或语速。保存后，用同一段文字生成：

- 开启盲听会随机排列声音，隐藏服务名称与参数；取消勾选即可揭晓。
- 为真实语音记录自然度、读音准确、情感表达、场景适合度和听感笔记。
- 可下载单段音频、本轮音频与配置 ZIP，或导出历史 CSV。

建议每段 50–300 字，先试日常文本，再试数字、多音字和中英混读。生成耗时会受网络与设备影响，评分代表你自己的听感。

## 数据在哪里

在“模型配置”中点击“打开配置目录”即可找到：

| 内容 | Windows 位置 |
| --- | --- |
| 云服务凭证 | `%LOCALAPPDATA%\TTS Lab\credentials.env` |
| 已保存方案 | `%LOCALAPPDATA%\TTS Lab\data\variants.json` |
| 历史、音频与评分 | `%LOCALAPPDATA%\TTS Lab\data` |
| 本地模型与运行环境 | `%LOCALAPPDATA%\TTS Lab\models` |

凭证以本机明文文件保存，界面不会读取已保存的 Key；小眼睛仅切换本次输入的可见性。需要备份时可复制上述目录，请自行妥善保管。删除模型会保留历史音频。

## 常见问题

- **只听到“嘟”声？** 当前选的是离线测试音。下载本地模型或配置云服务，再保存并选择对应方案。
- **服务或模型不可用？** 查看“模型配置”的状态。本地模型须完成安装；云服务须点击服务卡片保存凭证，模型和音色也须在账号权限范围内。
- **没有生成完成就退出了？** 关闭应用会停止本机等待，云端已收到的请求仍可能完成并计费；重新打开后先查看历史。
- **Windows 提示安全限制？** 应用目前未做代码签名，请遵循系统或组织的安全要求。Windows 原生安装与运行情况见验证说明。
- **Tauri 便携版打不开？** 先确认已安装 Microsoft Edge WebView2 Runtime，并完整解压全部文件。Tauri 版复用系统浏览器运行时，未在便携包中附带完整浏览器。

## 从源码运行

需要 Python 3.10+。在项目目录执行：

```bash
python -m venv .venv
# Windows：.venv\Scripts\activate
# macOS / Linux：source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，停止时在终端按 `Ctrl+C`。源码模式同样可点击服务卡片配置，保存到项目根目录 `.env`（显式 `--config` 时保存到该文件），立即生效；也可手动编辑 `.env` 后重启。数据与模型默认保存在项目内的 `data/` 和 `models/`。

运行 Tauri 桌面窗口还需要 Node.js 22+、Rust 工具链和系统 WebView 开发依赖；Windows 需要 MSVC C++ 构建工具和 WebView2。完成上述 Python 环境安装后执行：

```bash
npm ci
npm start
```

桌面模式使用用户目录中的 `credentials.env`，并保留现有方案、模型与历史所在位置。桌面壳通过原生层访问本机 Python 服务，鉴权令牌不交给页面；界面、模型推理和云服务适配继续使用现有实现。Tauri 迁移不改变本地模型按需下载方式，也不会直接提高模型合成速度。

构建安装包见 [WINDOWS-BUILD.md](WINDOWS-BUILD.md)，服务协议与限制见 [api_contracts.md](api_contracts.md)，已做和待做的检查见 [VERIFICATION.json](VERIFICATION.json)。云端账号权限、费用、音质和 Windows 原生运行需在自己的环境中验证。
