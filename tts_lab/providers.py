"""Bounded, non-streaming provider adapters. No retries and no browser credentials."""
from __future__ import annotations
import asyncio
import base64
import io
import json
import math
import os
import re
import struct
import time
import wave
from dataclasses import dataclass
from urllib.parse import urlsplit, quote
from xml.sax.saxutils import escape, quoteattr

import httpx
from .catalog import CATALOG

MAX_AUDIO = 20 * 1024 * 1024
MAX_RESPONSE = MAX_AUDIO * 3  # JSON hex encoding can double audio size.


class ProviderError(Exception):
    pass


@dataclass
class AudioResult:
    audio: bytes
    extension: str
    mime_type: str
    metadata: dict


def validate_params(provider_id: str, params: dict, text: str | None = None) -> dict:
    spec = next((p for p in CATALOG if p['id'] == provider_id), None)
    if spec is None:
        raise ProviderError('未知语音供应商。')
    if not isinstance(params, dict):
        raise ProviderError('参数必须是对象。')
    allowed = {f['key'] for f in spec['fields']}
    if set(params) - allowed:
        raise ProviderError('包含该供应商不支持的参数。')
    clean = {}
    for f in spec['fields']:
        v = params.get(f['key'], f['default'])
        if f['type'] == 'number':
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ProviderError(f"{f['label']} 必须是有限数值。")
            if not f['min'] <= v <= f['max']:
                raise ProviderError(f"{f['label']} 超出允许范围。")
            if f['step'] == 1 and int(v) != v:
                raise ProviderError(f"{f['label']} 必须为整数。")
        elif f['type'] == 'checkbox':
            if not isinstance(v, bool):
                raise ProviderError(f"{f['label']} 必须为布尔值。")
        else:
            if not isinstance(v, str) or len(v) > 2000:
                raise ProviderError(f"{f['label']} 必须为不超过 2000 字的字符串。")
            v = v.strip()
            if f['type'] == 'select' and v not in f['options']:
                raise ProviderError(f"{f['label']} 不在可选值中。")
            if not v and f['default'] != '':
                raise ProviderError(f"{f['label']} 不能为空。")
            if any(ord(c) < 32 and c not in '\n\r\t' for c in v):
                raise ProviderError('参数包含不支持的控制字符。')
        clean[f['key']] = v
    if text is not None:
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ProviderError('文本须为 1–2000 字。')
        if any(ord(c) < 32 and c not in '\n\r\t' for c in text):
            raise ProviderError('文本包含不支持的控制字符。')
        if provider_id == 'qwen' and len(text) > 600:
            raise ProviderError('Qwen HTTP 接口最多允许 600 字。')
        if provider_id == 'google' and len(text.encode('utf-8')) > 5000:
            raise ProviderError('Google Cloud TTS 文本最多允许 5000 UTF-8 字节。')
        if provider_id.startswith('local_') and len(text) > 300:
            raise ProviderError('本地模型单次最多允许 300 字，请缩短文本后生成。')
    if provider_id == 'openai' and clean['instructions'] and not clean['model'].startswith('gpt-4o-mini-tts'):
        raise ProviderError('当前适配器仅对 gpt-4o-mini-tts 模型发送风格指令。')
    if provider_id == 'local_qwen3' and clean['model'] == 'qwen3-0.6b' and clean['instructions']:
        raise ProviderError('Qwen3-TTS 0.6B 不支持风格指令，请留空或选择 1.7B 模型。')
    return clean


def _env(key):
    value = os.environ.get(key, '').strip()
    if not value:
        raise ProviderError(f'服务器尚未设置 {key}。')
    return value


def _wav(pcm: bytes, rate=24000):
    if not pcm or len(pcm) % 2 or len(pcm) > MAX_AUDIO - 44:
        raise ProviderError('PCM 音频为空、过大或帧长度不正确。')
    out = io.BytesIO()
    with wave.open(out, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm)
    return out.getvalue()


def _audio(data: bytes, metadata=None):
    if not isinstance(data, bytes) or not data or len(data) > MAX_AUDIO:
        raise ProviderError('供应商返回的音频为空或超过 20 MB。')
    if data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        try:
            with wave.open(io.BytesIO(data), 'rb') as reader:
                if reader.getnframes() <= 0:
                    raise ValueError()
        except (wave.Error, EOFError, ValueError):
            raise ProviderError('WAV 音频头无效。') from None
        ext, mime = 'wav', 'audio/wav'
    elif data.startswith(b'ID3') or (len(data) > 2 and data[0] == 255 and data[1] & 0xE0 == 0xE0):
        ext, mime = 'mp3', 'audio/mpeg'
    elif data.startswith(b'OggS'):
        ext, mime = 'ogg', 'audio/ogg'
    elif data.startswith(b'fLaC'):
        ext, mime = 'flac', 'audio/flac'
    else:
        raise ProviderError('供应商未返回可识别的 WAV / MP3 / OGG / FLAC 音频。')
    return AudioResult(data, ext, mime, metadata or {})


async def _request(client, method, url, **kwargs):
    async with client.stream(method, url, **kwargs) as response:
        if response.status_code >= 300:
            raise ProviderError(f'供应商 HTTP {response.status_code}；请检查额度、凭据与模型权限。')
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE:
                raise ProviderError('供应商响应超过安全大小限制。')
            chunks.append(chunk)
        return b''.join(chunks)


async def _json(client, url, headers, payload):
    raw = await _request(client, 'POST', url, headers=headers, json=payload)
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError()
        return parsed
    except (ValueError, UnicodeError):
        raise ProviderError('供应商返回了无效 JSON。') from None


def _b64(data):
    try:
        if not isinstance(data, str) or len(data) > MAX_RESPONSE:
            raise ValueError()
        return base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        raise ProviderError('供应商返回了无效的 base64 音频。') from None


def _id(value):
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,160}', value):
        raise ProviderError('模型、音色或地域标识符格式无效。')
    return value


async def synthesize(provider_id: str, text: str, params: dict) -> AudioResult:
    p = validate_params(provider_id, params, text)
    start = time.perf_counter()
    try:
        if provider_id == 'demo':
            rate = 24000
            n = int(rate * p['duration'])
            pcm = b''.join(struct.pack('<h', int(5000 * math.sin(2 * math.pi * p['frequency'] * i / rate) * min(1, i / 240, (n-i)/240))) for i in range(n))
            result = _audio(_wav(pcm, rate), {'is_demo': True, 'warning': '测试音，不是语音合成'})
        elif provider_id.startswith('local_'):
            from .local_models import synthesize_local, LocalModelError
            try:
                audio = await synthesize_local(provider_id.removeprefix('local_'), text, p)
            except LocalModelError as exc:
                raise ProviderError(str(exc)) from None
            result = _audio(audio, {'is_local': True, 'model': p['model']})
        elif provider_id == 'polly':
            result = await asyncio.to_thread(_polly, text, p)
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=False, trust_env=False) as client:
                result = await _dispatch(client, provider_id, text, p)
        result.metadata.update(provider=provider_id, latency_seconds=round(time.perf_counter() - start, 4), latency_kind='request_completion_not_first_audio')
        return result
    except ProviderError:
        raise
    except httpx.TimeoutException:
        raise ProviderError('供应商请求超时；没有自动重试，以避免重复计费。') from None
    except httpx.HTTPError:
        raise ProviderError('供应商网络请求失败；没有自动重试。') from None
    except Exception:
        # Never relay exception repr: SDK errors can carry signed URLs or credentials.
        raise ProviderError('供应商响应格式不符合当前适配器，或服务器配置无效。') from None


async def _dispatch(c, provider, text, p):
    if provider == 'openai':
        body = dict(model=p['model'], input=text, voice=p['voice'], response_format=p['format'], speed=p['speed'])
        if p['instructions']:
            body['instructions'] = p['instructions']
        return _audio(await _request(c, 'POST', 'https://api.openai.com/v1/audio/speech', headers={'Authorization': 'Bearer ' + _env('OPENAI_API_KEY')}, json=body))
    if provider == 'gemini':
        prompt = (p['instructions'] + '\n\n请仅朗读以下文本，不要朗读本条指令：\n' + text) if p['instructions'] else text
        body = {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'responseModalities': ['AUDIO'], 'speechConfig': {'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': p['voice']}}}}}
        data = await _json(c, 'https://generativelanguage.googleapis.com/v1beta/models/' + _id(p['model']) + ':generateContent', {'x-goog-api-key': _env('GEMINI_API_KEY')}, body)
        parts = data.get('candidates', [{}])[0].get('content', {}).get('parts', [])
        inline = [x['inlineData'] for x in parts if x.get('inlineData', {}).get('mimeType', '').startswith('audio/')]
        if not inline:
            raise ProviderError('Gemini 未返回音频；请检查模型权限或内容限制。')
        formats = {x['mimeType'] for x in inline}
        if len(formats) != 1:
            raise ProviderError('Gemini 返回混合音频格式，无法安全拼接。')
        mime = inline[0]['mimeType'].lower()
        raw = b''.join(_b64(x['data']) for x in inline)
        if len(raw) > MAX_AUDIO:
            raise ProviderError('音频超过 20 MB。')
        if raw.startswith(b'RIFF'):
            if len(inline) != 1:
                raise ProviderError('多个 WAV 音频块需要单独解码。')
            return _audio(raw)
        if 'pcm' in mime or 'l16' in mime:
            rate = re.search(r'rate=(\d+)', mime)
            rate = int(rate.group(1)) if rate else 24000
            if not 8000 <= rate <= 96000:
                raise ProviderError('Gemini 返回不支持的 PCM 采样率。')
            return _audio(_wav(raw, rate))
        return _audio(raw)
    if provider == 'qwen':
        host = 'dashscope-intl.aliyuncs.com' if p['region'] == 'international' else 'dashscope.aliyuncs.com'
        body = {'model': p['model'], 'input': {'text': text, 'voice': p['voice'], 'language_type': p['language_type']}}
        data = await _json(c, 'https://' + host + '/api/v1/services/aigc/multimodal-generation/generation', {'Authorization': 'Bearer ' + _env('DASHSCOPE_API_KEY')}, body)
        url = data.get('output', {}).get('audio', {}).get('url', '')
        u = urlsplit(url)
        if u.scheme != 'https' or not u.hostname or not u.hostname.endswith('.aliyuncs.com') or u.username or u.password or u.port not in (None, 443):
            raise ProviderError('Qwen 返回的音频 URL 不在安全的阿里云 HTTPS 下载域名内。')
        return _audio(await _request(c, 'GET', url))
    if provider == 'minimax':
        voice = {'voice_id': p['voice'], 'speed': p['speed'], 'vol': p['volume'], 'pitch': int(p['pitch'])}
        if p['emotion']:
            voice['emotion'] = p['emotion']
        body = {'model': p['model'], 'text': text, 'stream': False, 'language_boost': 'auto', 'output_format': 'hex', 'voice_setting': voice, 'audio_setting': {'sample_rate': int(p['sample_rate']), 'bitrate': 128000, 'format': 'mp3', 'channel': 1}}
        host = 'api.minimax.io' if p['region'] == 'international' else 'api.minimaxi.com'
        data = await _json(c, 'https://' + host + '/v1/t2a_v2', {'Authorization': 'Bearer ' + _env('MINIMAX_API_KEY')}, body)
        status = data.get('base_resp', {}).get('status_code')
        if status != 0:
            code = str(status) if isinstance(status, int) else 'unknown'
            raise ProviderError('MiniMax 返回失败状态 ' + code + '；请检查模型、音色、余额与权限。')
        return _audio(bytes.fromhex(data['data']['audio']))
    if provider == 'elevenlabs':
        body = {'text': text, 'model_id': p['model'], 'voice_settings': {'stability': p['stability'], 'similarity_boost': p['similarity_boost'], 'style': p['style'], 'use_speaker_boost': p['speaker_boost'], 'speed': p['speed']}}
        if not p['use_voice_settings']:
            body.pop('voice_settings')
        return _audio(await _request(c, 'POST', 'https://api.elevenlabs.io/v1/text-to-speech/' + _id(p['voice']), params={'output_format': 'mp3_44100_128'}, headers={'xi-api-key': _env('ELEVENLABS_API_KEY')}, json=body))
    if provider == 'azure':
        region = _env('AZURE_SPEECH_REGION')
        if not re.fullmatch(r'[a-z0-9-]{2,40}', region):
            raise ProviderError('AZURE_SPEECH_REGION 必须是地域名称，不是 URL。')
        inner = f'<prosody rate="{p["rate"]:+g}%" pitch="{p["pitch"]:+g}%">{escape(text)}</prosody>'
        if p['style']:
            inner = '<mstts:express-as style=' + quoteattr(p['style']) + '>' + inner + '</mstts:express-as>'
        ssml = '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang=' + quoteattr(p['language']) + '><voice name=' + quoteattr(p['voice']) + '>' + inner + '</voice></speak>'
        headers = {'Ocp-Apim-Subscription-Key': _env('AZURE_SPEECH_KEY'), 'Content-Type': 'application/ssml+xml', 'X-Microsoft-OutputFormat': 'riff-24khz-16bit-mono-pcm', 'User-Agent': 'tts-lab'}
        return _audio(await _request(c, 'POST', f'https://{region}.tts.speech.microsoft.com/cognitiveservices/v1', headers=headers, content=ssml.encode('utf-8')))
    if provider == 'google':
        body = {'input': {'text': text}, 'voice': {'languageCode': p['language'], 'name': p['voice']}, 'audioConfig': {'audioEncoding': 'MP3', 'speakingRate': p['speaking_rate'], 'pitch': p['pitch'], 'volumeGainDb': p['volume_gain_db']}}
        data = await _json(c, 'https://texttospeech.googleapis.com/v1/text:synthesize', {'Authorization': 'Bearer ' + _env('GOOGLE_CLOUD_ACCESS_TOKEN'), 'x-goog-user-project': _env('GOOGLE_CLOUD_PROJECT')}, body)
        return _audio(_b64(data['audioContent']))
    raise ProviderError('未知语音供应商。')


def _polly(text, p):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise ProviderError('请先安装可选 boto3 依赖。') from None
    region = _id(p['region'])
    client = boto3.client('polly', region_name=region, config=Config(connect_timeout=15, read_timeout=120, retries={'total_max_attempts': 1}))
    try:
        response = client.synthesize_speech(Text=text, TextType='text', OutputFormat='mp3', VoiceId=p['voice'], Engine=p['engine'], SampleRate=p['sample_rate'])
        stream = response['AudioStream']
        try:
            data = stream.read(MAX_AUDIO + 1)
        finally:
            stream.close()
        return _audio(data)
    finally:
        client.close()
