TTS Lab 0.3.0 · Windows x64 便携版

开始使用
1. 把整个 ZIP 完整解压到一个文件夹。
2. 双击 TTS Lab.exe。无需安装 Python、Node.js 或 WebView2。
3. 先点“生成并对比”，两组离线提示音可检查流程，不会调用云服务。
4. 真实人声：顶部菜单“文件 → 编辑 API 凭证”，填入需要的服务 Key 并保存。
5. 选择“文件 → 重启并载入凭证”，然后选择服务、模型、音色，确认云端调用后生成。

请保留 EXE 旁边的全部 DLL、resources、locales 等文件，不要只拷贝 EXE。
本包目标为 Windows 10/11 x64，不是安装器，也不是 32 位/原生 ARM64 版本。

数据与凭证
- 配置：%LOCALAPPDATA%\TTS Lab\credentials.env
- 历史和音频：%LOCALAPPDATA%\TTS Lab\data
- 菜单“文件 → 打开数据和配置目录”可以直接找到。
- 凭证为本机明文，只读取明确配置的服务。不要上传或分享这个目录。
- 更新：退出旧版，解压新版，打开新的 EXE。数据不在程序目录中，不会随解压覆盖。
- 不自动删除记录、不自动更新。移除程序文件夹后，个人配置和音频仍保留。

关于费用与功能
离线演示只产生提示音，不朗读文字，不能代表 TTS 音质。
真实语音会将文字和参数发送给所选厂商，需要你自己的账号及网络，并可能收费。
没有使用真实账号替你测过音质、权限或账单。关闭程序不能撤销云端已收到的请求。
不自动重试或更换模型。异常后先核对云端状态，避免重复计费。

验证状态
已实际生成 Windows x64 EXE 发行文件，内置 Python 与 26 个锁定依赖。
已通过 37 项 Python 测试、8 项桌面启动/安全逻辑测试（Linux），依赖完整性、哈希、PE x64 和 ZIP CRC 检查。
当前没有 Windows 原生运行环境，尚未实测 Windows 启动、桌面视觉、音频播放或系统安全软件兼容性。
这是需要先用离线演示验收的便携版本；不要把上述逻辑测试理解为 Windows 实测通过。
本应用代码未单独签名，保留正常 Windows 安全策略；组织限制请请管理员核验，不要关闭系统防护。

可选离线验收（高级用户）
在 PowerShell 中运行：
$p = Start-Process -FilePath '.\TTS Lab.exe' -ArgumentList '--smoke-test' -PassThru
$p.WaitForExit(120000)
$p.ExitCode

验收使用全新临时配置，不会读取你的 Key，不会调用真实语音服务。
结果和截图保存在 %TEMP% 下最新的 tts-lab-smoke-* 文件夹，查看 smoke-result.json。
只有该文件 status 为 passed 且退出码为 0 才表示验收成功。
源码附带 Windows CI 工作流，但本次交付没有执行该工作流。

详细说明：README.md
开发者重建：WINDOWS-BUILD.md
已做/未做检查：VERIFICATION.json
逐文件哈希：BUILD-MANIFEST.json

本地离线人声（0.3.0）
从“文件 → 本地模型配置”选择 Kokoro、Qwen3-TTS 或 CosyVoice 模型，点击下载并安装。首次需联网下载独立运行环境和模型，试生成通过后即可离线使用。建议先选 Kokoro + CPU；大型模型需数 GB 空间，GPU 需要兼容驱动。本地生成单次最多 300 字。
