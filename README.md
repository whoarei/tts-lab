# TTS Lab：本地语音对比工作台

输入同一段文本，选择最多 6 个方案，调整各厂商支持的参数，生成音频、盲听评分、导出结果。可重复选同一家来比较不同音色或参数。

**当前包含 Electron Windows x64 便携发行版与源代码，不是已经替你完成的音质排行榜。** 8 家云服务的适配器已经实现并通过模拟测试；未使用真实 API Key 调用，未验证你的账号是否有指定模型/音色权限。离线演示只生成提示音，不朗读文本，不能拿来比较 TTS 质量。

## 1. 快速启动

### Windows 桌面版（推荐）

下载 `TTS-Lab-0.2.1-win-x64.zip`，把**整个文件夹完整解压**，双击 `TTS Lab.exe`。无需安装 Python、Node.js 或 WebView2，不需要管理员权限，也不会打开外部浏览器。

- 目标系统为 Windows 10/11 x64。不是 32 位或原生 ARM64 发行版。
- 这是便携文件夹，不是单文件程序或安装向导。不要只复制 EXE，旁边的资源与 DLL 都需要保留。
- 第一次启动可直接点“生成并对比”体验两组离线提示音，不联网调用云服务、不收费。
- 真实人声需要自己的云服务凭证。从顶部菜单“文件 → 编辑 API 凭证”填写，保存后选择“重启并载入凭证”。
- 配置为 `%LOCALAPPDATA%\TTS Lab\credentials.env`，历史/音频为 `%LOCALAPPDATA%\TTS Lab\data`。菜单可打开目录。配置是本机明文，不是加密保险箱，请勿分享。
- 移动、替换或删除软件文件夹不会自动删除用户配置和历史。升级应先退出旧版，再解压新版，不要在运行中替换文件。
- 本版无自动更新、无应用代码签名。保留正常 Windows 安全策略；遇到组织安全限制请交由管理员核验，不要关闭防护。
- 已构建真实 Windows PE x64 程序并检查依赖/文件完整性；当前交付环境为 Linux，**尚未在 Windows 原生启动、验证桌面视觉和音频播放**。详见验证章节，建议先用离线演示验收。

### 从源代码运行（开发者）

源码浏览器版需要 Python 3.10+ 和现代浏览器，首次安装依赖需要联网。Windows 可双击 `start.bat`，再打开 http://127.0.0.1:8765。此脚本不是桌面发行版入口。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

### macOS / Linux

```bash
cd tts-lab
sh start.sh
```

或手动启动：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

停止：终端按 Ctrl+C。端口冲突时用 `python run.py --port 8766`，打开对应地址。

首次未配置凭证时，页面会选择两组离线提示音。点击“生成并对比”可检查音频文件、历史、盲听界面和导出流程，**不会调用云服务，也不会付费**。提示音不能评分。

## 2. 配置真实语音服务

1. 桌面版使用“文件 → 编辑 API 凭证”。源码浏览器版将项目中的 `.env.example` 复制为 `.env`。
2. 在自己的电脑上填入需要使用的服务凭证。只配你要测的服务即可。
3. 保存后重启程序，选择服务和音色。桌面版仅从 credentials.env 读取配置，不会悄悄使用系统环境中的 API Key。
4. 确认文本、参数、所选云服务以及可能产生的费用，勾选页面确认项后点击生成。

**不要把密钥发到聊天、上传到仓库或和项目一起分享。** 网页不接受 Key，Key 只由后端读取；页面仅显示是否检测到配置，不显示具体值。不要把私密、敏感或没有授权外传的文本送到云端。

| 已实现服务 | 环境变量 | 主要可调项 | 输出 |
|---|---|---|---|
| OpenAI Speech | OPENAI_API_KEY | model、voice、instructions、speed、格式 | MP3 / WAV / OGG / FLAC |
| Gemini Native TTS | GEMINI_API_KEY | model、voice、风格指令 | PCM 包装为 WAV / 服务返回音频 |
| Qwen / DashScope | DASHSCOPE_API_KEY | model、voice、语言、地域 | 下载服务返回的 WAV |
| MiniMax Speech | MINIMAX_API_KEY | model、voice、语速、音量、音高、情绪、采样率、地域 | MP3 |
| ElevenLabs | ELEVENLABS_API_KEY | model、voice、稳定性、相似度、风格、语速、speaker boost | MP3 |
| Azure Speech Neural | AZURE_SPEECH_KEY、AZURE_SPEECH_REGION | voice、语言、语速、音高、表达风格 | WAV |
| Google Cloud TTS | GOOGLE_CLOUD_ACCESS_TOKEN、GOOGLE_CLOUD_PROJECT | voice、语言、语速、音高、增益 | MP3 |
| AWS Polly（桌面版已内置） | 已配置的 AWS 默认凭据链/配置档案 | voice、region、engine、采样率 | MP3 |

服务名旁边的“已检测到配置”只说明环境变量非空。**它不代表密钥、余额、模型权限或网络已经验证。** 默认模型和音色是示例，可手动替换为账号实际支持的值；程序不会失败后偷偷换模型或厂商。

### 各家要注意的地方

- **OpenAI**：此适配器是旧 Speech API 对照组。核对时官方标注相关模型将在 **2027 年 1 月 6 日** 停用，不适合把它当成长期方案。Realtime 不是这个接口的直接替换。
- **Gemini**：实现的是明确标为 Legacy 的 `generateContent` TTS 协议，默认 `gemini-2.5-flash-preview-tts`。新 Interactions-only 模型不能只改模型名就接入。模型预览版本/权限可能变化；风格提示也可能影响实际朗读，需听检是否漏读或加词。
- **Qwen**：当前实现 Qwen3-TTS HTTP 协议，不是最新 Qwen-Audio 专用接口。中国内地选择 `china`，国际地域选择 `international`；两地 Key 不能混用。本适配器限制 600 字，过长会拒绝，不自动分段收费。
- **MiniMax**：国际站与中国站分别选择对应地域，音色要使用账号可用的准确 ID。示例为英文音色，中文评测请先更换成中文音色，避免把语言/音色失配当作模型能力问题。
- **ElevenLabs**：不同模型对 voice_settings 的支持不同。若换模型后参数不兼容，可关闭“发送自定义 voice_settings”，让服务采用模型默认值；不要把旧模型的 stability 等取值直接套到新模型。
- **Azure**：地域必须与 Speech 资源相匹配。style 只对指定音色支持的风格生效；默认留空。输入当普通文本处理，会安全转义为 SSML，不开放任意 XML。
- **Google Cloud**：这是传统 Cloud TTS，与 Gemini 的 Key 不通用。需要启用相应 API、计费与权限。可用你已配置的 `gcloud auth print-access-token` 获取短期令牌，放到本机环境变量；令牌过期后更新并重启。本程序不自动登录、不创建长期授权。
- **Polly**：桌面版已经内置 SDK，在 credentials.env 中明确选择已有 AWS_PROFILE 或填入已有的 AWS 凭证；不会自动采用未配置的默认档案。源码版先运行 `python -m pip install -r requirements-aws.txt`，使用你已有的 AWS profile 或官方凭据链，不在网页输入凭据。ready 只检查 SDK 已安装，不会在启动时访问账号。voice/engine/region 必须是服务支持的组合；切换 `generative` 时结果分类也变为生成式。Polly 的中文音色选择有限，不能假设任意引擎都支持中文。

## 3. 怎样找到适合你的“最好”

### 推荐做三轮，而不是一次看所有厂商

1. **初筛**：每家先选一个同语言、同类风格的音色，所有参数用默认值，拿真实业务文本比较。中文可以从 Qwen、MiniMax、Azure 三个开始；英文可加入 ElevenLabs、Gemini、Google Neural2。这里只是缩小首轮工作量，不是质量排名。
2. **调参**：留下 2–3 个候选，同一个服务复制几份，只改一个因素，例如音色或语速。生成式的自然语言指令与传统 TTS 的数字语速不是等价单位，不要机械对齐。
3. **复测**：用多种未见文本并换天再测。每个方案生成 3 次，确认是否漏字、重读、停顿不稳。先盲听打分，再揭晓名字。

界面预置日常中文、数字/多音字、英语课文、中英混读、叙事表达，也支持粘贴自己的文本。建议每段 50–300 字；完整输入最多 2000 字，Qwen/Google 还受额外服务限制。

四项主观评分为 1–5 分：自然度、读音准确、情感表达、场景适合度。没有预设“总冠军”，你可以在导出的 CSV 中按用途设置权重。例如教学朗读通常更看重准确、稳定；叙事更重自然与表达。先记录漏字/读错再打分，别仅被悦耳的音色吸引。

### 公平比较的边界

- 音色、语言、音量、文本、网速都影响结果，单次“最好听”不是模型普遍更强的证据。
- 盲听会随机排序并隐藏名字/参数，评分后取消勾选揭晓。下载和导出的文件会包含厂商名，只在评分结束后做。
- 不自动响度归一化，不改动原音频。音量差很大时，请在相同音量下复听，避免“更响 = 更好”的偏差。
- 展示的是**完整请求生成耗时**，不是首包延迟/首声延迟；任务排队时间不计入。默认最多并行 3 个，网络和并行竞争影响数值。严格比较速度时应单独、交替、多次测试。
- WAV 可直接读到时长并计算 RTF = 生成耗时 / 音频时长。压缩格式没有依赖解码器，因此不显示准确时长/RTF，绝不凭字数猜测。
- 分类“生成式/传统神经”用于选择接口对照，不断言每家内部架构都等同于通用 LLM。传统一组也不是古早拼接式语音。
- 未做流式首包测量、自动 ASR 错字率、声音克隆或多说话人功能。未实现的厂商没有伪装成已支持。

## 4. 文件、数据与费用

每次生成在数据目录的 `<run-id>/` 留下（桌面版数据目录为 `%LOCALAPPDATA%\TTS Lab\data`，源码版默认为项目内 `data`） `run.json` 和音频文件。记录文本、完整参数、时间、状态与评分，不记录 Key 或带签名的下载 URL。历史界面/CSV 展示最近 200 条。本轮 ZIP 包含音频、JSON、CSV，可用于复现与横向分析。

点击生成后，所选云服务会收到这段文本及参数，它们按各自条款处理并可能计费。本程序不提供实时账单估价，也不会使用“免费”字样保证供应商调用成本。没有自动重试、降级、自动换厂商或后台轮询付费模型。

网络超时或你关掉网页/本机进程，服务端仍可能完成请求并产生费用。不要因页面没显示就连续重试。重启时未完成的本地记录会标为中断，不会重新发送。

项目仅绑定本机 127.0.0.1，带同源检查和本机会话防护，适用于个人本机评测，不是互联网多用户服务。**不要暴露端口，不要把 .env、data 或含私人文本的导出文件公开。** 项目不自动删记录；需要清理时由你在文件管理器中选择相应记录。

## 5. 验证与已知限制

```bash
python -m unittest discover -s tests -v
node --check tts_lab/static/app.js  # 可选，需要 Node.js
```

本版通过 37 项 Python 自动化测试与 8 项桌面启动/安全逻辑测试：

- 离线生成有效 WAV，音频下载、ZIP、CSV、历史、评分保存。
- 7 个 HTTP 服务的请求/响应模拟，以及 Polly SDK 模拟；无真实账号调用。
- 云端确认、错误隔离、密钥不进入 API 返回值、跨站请求与路径保护。
- Electron 到 Python 的真实子进程启动、随机端口、每次启动独立会话令牌、持久数据、正常退出；测试在 Linux 运行。
- 桌面前端不启用 Node.js，启用 Chromium 沙箱和上下文隔离；所有请求只允许本机该实例地址，外部文档须明确确认后交给系统浏览器。
- 在隔离环境中按桌面锁文件的全部 26 个依赖版本复测，通过 pip check。宿主为 Linux/Python 3.12，这不等同于 Windows/Python 3.13 原生测试。
- 官方 Electron 与 Python 运行时 SHA256、全部 wheel SHA256、Windows 依赖关系、EXE/native extension 的 x64 PE 标头、最终 ZIP CRC 和逐文件清单检查。

**尚未验证**：Windows 原生启动/退出、桌面视觉、真实音频播放、杀毒软件和系统策略兼容性、真实云端调用、听感质量、账号权限和账单。没有将这些标为已通过。

包内提供 Windows `--smoke-test` 离线验收模式，使用全新临时配置，不读取你的 Key；覆盖桌面界面、提示音播放和下载等。源码中 `.github/workflows/windows-build.yml` 可在 Windows CI 构建并跑验收，**交付时该工作流尚未执行**。构建与测试步骤见 `WINDOWS-BUILD.md`。

源码浏览器测试仍保留 `tests/browser_smoke.py`（需额外安装 Playwright 与浏览器）。`VERIFICATION.json` 记录已做和未做的检查，不把逻辑测试当成 Windows UI 测试。

## 6. 项目结构与扩展

```text
desktop/               Electron 主进程、安全、启停与离线验收
scripts/build_windows.py 锁定依赖的跨平台 Windows 打包脚本
runtime-lock.json       官方运行时与全部 Windows wheel 的 SHA256
run.py                 本机入口
.env.example           环境变量模板，无真实凭证
tts_lab/app.py          任务、记录、评分、导出、访问保护
tts_lab/catalog.py      服务与参数定义
tts_lab/providers.py    各厂商适配器
tts_lab/static/         中文界面，无 CDN / 外部字体
tests/                 unittest 与可选浏览器测试
api_contracts.md        已核对的官方协议与链接
```

新增服务的步骤：

1. 在 `catalog.py` 添加服务 id、环境变量、参数字段和官方文档链接。
2. 在 `providers.py` 添加鉴权、请求及音频解码逻辑，返回 `AudioResult`。
3. 为新的协议添加离线测试。不要把 API Key、任意 URL 或任意 HTTP Header 放进前端可调字段。
4. 自己授权做小文本实测后，再认定账号可用。若涉及声音克隆或新的敏感数据外发，应另做明确的授权流程。

未来适合添加：Seed-TTS、腾讯/讯飞/百度等国内引擎、Piper/Kokoro 等本地模型、真实流式首包测量、响度归一化副本、ASR 错字率。它们**没有包含在此版本的已实现清单中**。

官方接口依据与核对日期见 `api_contracts.md`。依赖更新、预览模型停用或服务协议变化后，须重新核对适配器，不能仅依据旧聊天内容替换模型名。
