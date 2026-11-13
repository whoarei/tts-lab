"""Private, immutable reference WAVs addressed by opaque IDs, never user paths."""
import hashlib
import io
import json
import re
import uuid
import wave

MAX_REFERENCE = 2 * 1024 * 1024


class ReferenceError(ValueError):
    pass


def root():
    from .local_models import model_root
    return model_root() / 'references'


def path_for(reference_id):
    if not isinstance(reference_id, str) or not re.fullmatch(r'[0-9a-f]{32}', reference_id):
        raise ReferenceError('参考录音 ID 无效。')
    path = root() / (reference_id + '.wav')
    if path.is_symlink() or not path.is_file():
        raise ReferenceError('参考录音不存在，请重新导入。')
    return path


def info(reference_id):
    path = path_for(reference_id)
    try:
        meta = json.loads(path.with_suffix('.json').read_text('utf-8'))
        if meta['id'] != reference_id or path.stat().st_size > MAX_REFERENCE:
            raise ValueError()
        if hashlib.sha256(path.read_bytes()).hexdigest() != meta['sha256']:
            raise ValueError()
        return {k: meta[k] for k in ('id', 'name', 'sha256', 'duration_seconds', 'sample_rate', 'channels')}
    except (OSError, ValueError, KeyError, TypeError):
        raise ReferenceError('参考录音已损坏，请重新导入。') from None


def list_references():
    result = []
    for path in sorted(root().glob('*.wav')):
        try:
            result.append(info(path.stem))
        except ReferenceError:
            continue
    return result


def save(raw, name):
    if not raw or len(raw) > MAX_REFERENCE:
        raise ReferenceError('参考录音须小于 2 MB。')
    try:
        with wave.open(io.BytesIO(raw), 'rb') as reader:
            rate, channels, width = reader.getframerate(), reader.getnchannels(), reader.getsampwidth()
            frames = reader.getnframes()
            if width != 2 or channels not in (1, 2) or not 8000 <= rate <= 48000 or not 3 <= frames / rate <= 10:
                raise ValueError()
            if len(reader.readframes(frames)) != frames * channels * width:
                raise ValueError()
    except (wave.Error, EOFError, ValueError, ZeroDivisionError):
        raise ReferenceError('请导入 3–10 秒、8–48 kHz、单声道或双声道的 16-bit PCM WAV。') from None
    reference_id = uuid.uuid4().hex
    name = str(name).replace('\\', '/').rsplit('/', 1)[-1]
    name = ''.join(c for c in name if ord(c) >= 32)[:100] or '参考录音.wav'
    meta = dict(id=reference_id, name=name, sha256=hashlib.sha256(raw).hexdigest(),
                duration_seconds=round(frames / rate, 3), sample_rate=rate, channels=channels)
    root().mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root() / (reference_id + '.wav')
    path.write_bytes(raw)
    path.with_suffix('.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    return meta
