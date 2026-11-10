"""Public, credential-free provider capabilities; examples are not latest-model claims."""
import copy
import importlib.util
import os


def field(key, label, default, kind='text', **kwargs):
    return dict(key=key, label=label, type=kind, default=default, **kwargs)


def num(key, label, default, low, high, step=.1):
    return field(key, label, default, 'number', min=low, max=high, step=step)


def select(key, label, default, options):
    return field(key, label, default, 'select', options=options)


CATALOG = [
    dict(id='openai', name='OpenAI Speech', family='generative', warning='Legacy 接口：官方已标注弃用，计划于 2027-01-06 停用；仅作历史基准比较。',
         description='历史 Speech 接口基准：官方标注 2027-01-06 停用。风格指令仅用于 gpt-4o-mini-tts。', env=['OPENAI_API_KEY'],
         docs='https://platform.openai.com/docs/guides/text-to-speech', fields=[
             field('model', '模型 ID', 'gpt-4o-mini-tts'), field('voice', '音色', 'coral'),
             field('instructions', '语气 / 风格指令', '', 'textarea'), num('speed', '语速', 1, .25, 4),
             select('format', '格式', 'mp3', ['mp3', 'wav', 'opus', 'flac'])]),
    dict(id='gemini', name='Gemini Native TTS', family='generative',
         description='Legacy generateContent 协议；自然语言风格提示。预览模型及音色依账户而异，PCM 转 WAV。', env=['GEMINI_API_KEY'],
         docs='https://ai.google.dev/gemini-api/docs/speech-generation', fields=[
             field('model', '模型 ID', 'gemini-2.5-flash-preview-tts'), field('voice', '预置音色', 'Kore'),
             field('instructions', '朗读风格提示（会拼接到提示中）', '', 'textarea')]),
    dict(id='qwen', name='Qwen / DashScope TTS', family='generative',
         description='DashScope HTTP 非流式接口；地域必须与 API Key 相符。', env=['DASHSCOPE_API_KEY'],
         docs='https://www.alibabacloud.com/help/en/model-studio/qwen-tts-api', fields=[
             field('model', '模型 ID', 'qwen3-tts-flash'), field('voice', '音色', 'Cherry'),
             select('region', '服务地域', 'international', ['international', 'china']),
             select('language_type', '语言', 'Auto', ['Auto', 'Chinese', 'English', 'German', 'Italian', 'Portuguese', 'Spanish', 'Japanese', 'Korean', 'French', 'Russian'])]),
    dict(id='minimax', name='MiniMax Speech', family='generative',
         description='T2A v2；模型与音色可手动填写，输出 hex 音频。', env=['MINIMAX_API_KEY'],
         docs='https://platform.minimax.io/docs/api-reference/speech-t2a-http', fields=[
             field('model', '模型 ID', 'speech-2.8-hd'), field('voice', '音色 ID', 'English_Graceful_Lady'),
             select('region', '服务地域', 'international', ['international', 'china']),
             num('speed', '语速', 1, .5, 2), num('volume', '音量', 1, .01, 10), num('pitch', '音高（半音）', 0, -12, 12, 1),
             select('emotion', '情绪（空为自动）', '', ['', 'happy', 'sad', 'angry', 'fearful', 'disgusted', 'surprised', 'calm']),
             select('sample_rate', '采样率', '32000', ['16000', '24000', '32000', '44100'])]),
    dict(id='elevenlabs', name='ElevenLabs', family='generative',
         description='voice_settings 随模型而异；默认参数适用于 multilingual_v2。切换模型前请查文档，必要时关闭自定义设置。需要账户可访问的 voice ID。', env=['ELEVENLABS_API_KEY'],
         docs='https://elevenlabs.io/docs/api-reference/text-to-speech/convert', fields=[
             field('model', '模型 ID', 'eleven_multilingual_v2'), field('voice', 'Voice ID', '21m00Tcm4TlvDq8ikWAM'),
             field('use_voice_settings', '发送自定义 voice_settings（依模型支持）', True, 'checkbox'), num('stability', '稳定性', .5, 0, 1), num('similarity_boost', '相似度', .75, 0, 1), num('style', '风格强度', 0, 0, 1),
             num('speed', '语速', 1, .7, 1.2), field('speaker_boost', 'Speaker boost', True, 'checkbox')]),
    dict(id='azure', name='Azure Speech Neural', family='neural',
         description='经典 Neural TTS；安全生成 SSML。风格只适用于支持该风格的音色。', env=['AZURE_SPEECH_KEY', 'AZURE_SPEECH_REGION'],
         docs='https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-text-to-speech', fields=[
             field('voice', '音色', 'zh-CN-XiaoxiaoNeural'), field('language', '语言代码', 'zh-CN'),
             num('rate', '语速变化 (%)', 0, -50, 100, 1), num('pitch', '音高变化 (%)', 0, -50, 50, 1),
             field('style', '表达风格（可选）', '')]),
    dict(id='google', name='Google Cloud TTS', family='neural',
         description='传统 Google Cloud Neural2 / WaveNet 接口，与 Gemini Native TTS 分开比较。访问令牌会过期。',
         env=['GOOGLE_CLOUD_ACCESS_TOKEN', 'GOOGLE_CLOUD_PROJECT'],
         docs='https://cloud.google.com/text-to-speech/docs/reference/rest/v1/text/synthesize', fields=[
             field('voice', '音色', 'en-US-Neural2-F'), field('language', '语言代码', 'en-US'),
             num('speaking_rate', '语速', 1, .25, 2), num('pitch', '音高（半音）', 0, -20, 20, 1),
             num('volume_gain_db', '增益 (dB)', 0, -20, 16, 1)]),
    dict(id='polly', name='AWS Polly', family='neural',
         description='可选 boto3；使用服务器默认凭据链。ready 仅表示 SDK 可用，不保证凭据或地域权限。', env=[],
         docs='https://docs.aws.amazon.com/polly/latest/dg/API_SynthesizeSpeech.html', fields=[
             field('voice', 'Voice ID', 'Joanna'), field('region', 'AWS region', 'us-east-1'),
             select('engine', '引擎', 'neural', ['standard', 'neural', 'long-form', 'generative']),
             select('sample_rate', '采样率', '24000', ['8000', '16000', '22050', '24000'])]),
    dict(id='local_kokoro', name='Kokoro · 本地离线', family='local', env=[],
         description='多语言 INT8 模型；CPU 运行，不发送文本到云端。先在本地模型配置中下载安装。',
         docs='https://github.com/k2-fsa/sherpa-onnx', fields=[
             select('model', '本地模型', 'kokoro-v1.1', ['kokoro-v1.1']),
             num('voice', '音色编号（0–102）', 18, 0, 102, 1), num('speed', '语速', 1, .5, 2)]),
    dict(id='local_qwen3', name='Qwen3-TTS · 本地离线', family='local', env=[],
         description='CustomVoice 预置音色；CPU 或 NVIDIA GPU 运行，不使用 DashScope API。',
         docs='https://github.com/QwenLM/Qwen3-TTS', fields=[
             select('model', '本地模型', 'qwen3-0.6b', ['qwen3-0.6b', 'qwen3-1.7b']),
             select('voice', '音色', 'Vivian', ['Vivian', 'Serena', 'Uncle_Fu', 'Dylan', 'Eric', 'Ryan', 'Aiden', 'Ono_Anna', 'Sohee']),
             select('language', '语言', 'Chinese', ['Auto', 'Chinese', 'English', 'Japanese', 'Korean', 'German', 'French', 'Russian', 'Portuguese', 'Spanish', 'Italian']),
             field('instructions', '风格指令（仅 1.7B 模型支持）', '', 'textarea')]),
    dict(id='local_cosyvoice', name='CosyVoice · 本地离线', family='local', env=[],
         description='300M-SFT 预置音色；无需参考录音，CPU 或 NVIDIA GPU 运行。',
         docs='https://github.com/FunAudioLLM/CosyVoice', fields=[
             select('model', '本地模型', 'cosyvoice-sft', ['cosyvoice-sft']),
             select('voice', '音色', '中文女', ['中文女', '中文男', '英文女', '英文男', '日语男', '粤语女', '韩语女']),
             num('speed', '语速', 1, .5, 2)]),
    dict(id='demo', name='离线测试音（非人声）', family='demo', description='仅验证播放、导出与比较流程；不是真实 TTS，不参与最佳语音排行。',
         env=[], fields=[num('frequency', '测试音频率 (Hz)', 440, 180, 1000, 10), num('duration', '时长（秒）', 1.5, .5, 5, .1)])
]


def public_catalog():
    from .local_models import list_models
    local = list_models()
    result = copy.deepcopy(CATALOG)
    for provider in result:
        missing = [key for key in provider['env'] if not os.environ.get(key, '').strip()]
        if provider['family'] == 'local':
            installed = [m['id'] for m in local if m['engine'] == provider['id'].removeprefix('local_') and m['ready']]
            provider['installed_models'] = installed
            provider['available_models'] = [m['id'] for m in local if m['engine'] == provider['id'].removeprefix('local_')]
            if installed:
                provider['fields'][0]['default'] = installed[0]
            else:
                missing.append('本地模型配置 → 下载并安装')
        if provider['id'] == 'polly':
            if importlib.util.find_spec('boto3') is None:
                missing.append('optional dependency: boto3')
            if os.getenv('TTS_LAB_DESKTOP') == '1':
                provider['description'] = '桌面版已内置 boto3；请在凭证配置中明确选择 AWS_PROFILE 或已有 AWS 凭证。检测到配置不代表账号和地域权限已验证。'
                profile = os.getenv('AWS_PROFILE', '').strip()
                direct = all(os.getenv(key, '').strip() for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'))
                if not profile and not direct:
                    missing.append('AWS_PROFILE 或 AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY')
        provider['missing_env'] = missing
        provider['ready'] = not missing
    return result


def validate_params(provider_id, params, text=None):
    """Lazy import keeps public validation available alongside the catalog."""
    from .providers import validate_params as validate
    return validate(provider_id, params, text)
