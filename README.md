# TTS Lab

简体中文 | [English](README.en.md) | [下载 Windows 版](https://github.com/whoarei/tts-lab/releases/latest)

本地运行的语音合成与对比工具，支持云端服务和本地模型。提供 Windows 桌面版，也可从源码在浏览器中运行。

## 功能

- **直接试听**：调整模型、音色和参数，自动合成播放，复用已有音频。
- **方案管理**：保存、修改或另存参数方案，最多同时对比 6 组，支持盲听。
- **云端服务**：适配 OpenAI、DeepSeek、Gemini、Qwen 等服务，可在界面配置凭证。
- **本地模型**：管理 Kokoro、Qwen3-TTS、CosyVoice、IndexTTS-2、GLM-TTS，支持参考录音。
- **运行指标**：查看生成耗时、音频时长、文件大小与实时率。
- **历史记录**：分页回看并恢复文本和参数，默认保留 30 天，可设为永久。
- **音频下载**：打包下载本轮音频与参数。
- **在线更新**：在“关于”页查看版本、检查和安装更新。

## 截图

### 文本测试

输入文本、调整参数，直接试听。

![文本测试](docs/screenshots/workspace.png)

### 模型配置

管理本地模型与云端服务凭证。

![模型配置](docs/screenshots/local-models.png)

### 方案对比

对比不同方案，下载音频与参数。截图使用模拟音频。

![方案对比](docs/screenshots/results.png)

其他截图：[历史保留设置](docs/screenshots/settings.png) · [版本与更新](docs/screenshots/about.png)
