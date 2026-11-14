"""Local provider settings. Stored secrets are never returned to the webview."""
from __future__ import annotations

import io
import os
from pathlib import Path
import tempfile

from dotenv.parser import parse_stream

from .catalog import CATALOG

CONFIG_PATH = Path(__file__).resolve().parents[1] / '.env'
FIELDS = {item['id']: list(item['env']) for item in CATALOG
          if item['family'] not in {'local', 'demo'}}
FIELDS['polly'] = ['AWS_PROFILE', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
                   'AWS_SESSION_TOKEN', 'AWS_DEFAULT_REGION', 'AWS_REGION']
PROVIDER_ENV = {key for fields in FIELDS.values() for key in fields}
PUBLIC_FIELDS = {
    'AZURE_SPEECH_REGION': ('服务地域', 'eastasia'),
    'GOOGLE_CLOUD_PROJECT': ('计费项目 ID', ''),
    'AWS_PROFILE': ('本机 AWS Profile 名称', ''),
    'AWS_DEFAULT_REGION': ('默认地域', 'us-east-1'),
    'AWS_REGION': ('AWS_REGION（可选）', ''),
}
LABELS = {'GOOGLE_CLOUD_ACCESS_TOKEN': 'Access Token（会过期）',
          'AWS_ACCESS_KEY_ID': 'Access Key ID', 'AWS_SECRET_ACCESS_KEY': 'Secret Access Key',
          'AWS_SESSION_TOKEN': 'Session Token（临时凭证时填写）'}
HELP = {
    'VOLCENGINE_TTS_API_KEY': '填写火山语音控制台的 API Key。',
    'GOOGLE_CLOUD_ACCESS_TOKEN': '访问令牌过期后需在此更新。',
    'AZURE_SPEECH_REGION': '须与 Speech 资源所在地域一致，例如 eastasia。',
    'AWS_PROFILE': '使用本机已有的 AWS 命名配置。切换认证方式会清除另一种方式的凭证。',
    'AWS_DEFAULT_REGION': '语音生成使用「方案对比」中设置的 AWS region。',
    'AWS_REGION': '可选；保留已有 AWS 环境配置，语音生成地域以方案参数为准。',
}


class CredentialError(ValueError):
    """Only fixed messages may leave this module; never include field values."""


def aws_policy(desktop: bool) -> None:
    if desktop:
        os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'
        for key in ('AWS_CONFIG_FILE', 'AWS_SHARED_CREDENTIALS_FILE'):
            if os.getenv('AWS_PROFILE', '').strip():
                os.environ.pop(key, None)
            else:
                os.environ[key] = os.devnull


def describe(provider: str) -> dict:
    if provider not in FIELDS:
        raise CredentialError('找不到可配置的云服务。')
    fields = []
    for key in FIELDS[provider]:
        value = os.getenv(key, '')
        public = PUBLIC_FIELDS.get(key)
        field = dict(key=key, label=public[0] if public else LABELS.get(key, 'API Key'),
                     secret=public is None, configured=bool(value.strip()), help=HELP.get(key, ''))
        if public:
            field['value'] = value or public[1]
        fields.append(field)
    result = dict(provider=provider, fields=fields)
    if provider == 'polly':
        result.update(mode='profile' if os.getenv('AWS_PROFILE', '').strip() else 'keys',
                      temporary=bool(os.getenv('AWS_SESSION_TOKEN', '').strip()))
    return result


def save(provider: str, body: object, *, desktop: bool) -> dict:
    if provider not in FIELDS:
        raise CredentialError('找不到可配置的云服务。')
    allowed = {'values', 'mode', 'temporary'} if provider == 'polly' else {'values'}
    if not isinstance(body, dict) or set(body) - allowed or not isinstance(body.get('values'), dict):
        raise CredentialError('配置内容无效。')
    values = body['values']
    if set(values) - set(FIELDS[provider]):
        raise CredentialError('配置包含此服务不支持的字段。')
    changes = {}
    for key, value in values.items():
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise CredentialError('配置值须为单行文本，最长 4096 个字符。')
        value = value.strip()
        # A blank secret means keep, never read it back to populate an input.
        if value or key in PUBLIC_FIELDS:
            changes[key] = value
    merged = {key: changes.get(key, os.getenv(key, '')).strip() for key in FIELDS[provider]}
    if provider == 'polly':
        mode = body.get('mode')
        if not isinstance(mode, str) or mode not in {'profile', 'keys'}:
            raise CredentialError('请选择 AWS 认证方式。')
        temporary = body.get('temporary', False)
        if not isinstance(temporary, bool):
            raise CredentialError('请选择是否使用 AWS 临时凭证。')
        unused = ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'] if mode == 'profile' else ['AWS_PROFILE']
        for key in unused:
            changes[key] = merged[key] = ''
        required = ['AWS_PROFILE'] if mode == 'profile' else ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY']
        if mode == 'keys':
            if temporary:
                required.append('AWS_SESSION_TOKEN')
            else:
                changes['AWS_SESSION_TOKEN'] = merged['AWS_SESSION_TOKEN'] = ''
    else:
        required = FIELDS[provider]
    if any(not merged[key] for key in required):
        raise CredentialError('请填写此服务所需的凭证和配置项。')
    # Persist first, then publish to the process. A failed write changes neither.
    _write(changes)
    for key, value in changes.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    if provider == 'polly':
        aws_policy(desktop)
    return describe(provider)


def _write(changes: dict[str, str]) -> None:
    temporary = None
    try:
        source = CONFIG_PATH.read_text(encoding='utf-8-sig') if CONFIG_PATH.exists() else ''
        bindings = list(parse_stream(io.StringIO(source)))
        if any(binding.error for binding in bindings):
            raise CredentialError('本机凭证文件格式异常，未修改原文件。')
        pending, lines = dict(changes), []
        for binding in bindings:
            if binding.key in changes:
                # Remove duplicate definitions of changed keys as well.
                if binding.key in pending:
                    lines.append(_line(binding.key, pending.pop(binding.key)))
            else:
                lines.append(binding.original.string)
        text = ''.join(lines)
        if text and not text.endswith('\n'):
            text += '\n'
        text += ''.join(_line(key, value) for key, value in pending.items())
        # Same-directory replace is atomic; mkstemp creates mode 0600 on POSIX.
        fd, temporary = tempfile.mkstemp(prefix='.credentials-', suffix='.tmp', dir=CONFIG_PATH.parent)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, CONFIG_PATH)
    except (OSError, UnicodeError):
        raise CredentialError('无法保存本机凭证，请检查配置目录权限或磁盘空间后重试。') from None
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _line(key: str, value: str) -> str:
    escaped = value.replace('\\', '\\\\').replace("'", "\\'")
    return f"{key}='{escaped}'\n"
