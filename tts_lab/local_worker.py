"""Inference entry point run by a managed Python environment, never by the UI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def generate(request):
    import numpy as np
    import soundfile as sf

    engine = request['engine']
    model = Path(request['model_dir'])
    params = request['params']
    text = request['text']
    if engine == 'kokoro':
        import sherpa_onnx
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(model / 'model.int8.onnx'), voices=str(model / 'voices.bin'),
                    tokens=str(model / 'tokens.txt'), data_dir=str(model / 'espeak-ng-data'),
                    lexicon=','.join(str(model / n) for n in ('lexicon-us-en.txt', 'lexicon-zh.txt'))),
                num_threads=2, provider='cpu'),
            rule_fsts=','.join(str(model / n) for n in ('phone-zh.fst', 'date-zh.fst')))
        tts = sherpa_onnx.OfflineTts(config)
        audio = tts.generate(text, sid=int(params['voice']), speed=params['speed'])
        samples, rate = np.asarray(audio.samples), audio.sample_rate
    elif engine == 'qwen3':
        import torch
        from qwen_tts import Qwen3TTSModel
        device = request['device']
        if device == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('CUDA is unavailable')
        torch.set_num_threads(2)
        tts = Qwen3TTSModel.from_pretrained(
            str(model), device_map='cuda:0' if device == 'cuda' else 'cpu',
            dtype=(torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16) if device == 'cuda' else torch.float32,
            attn_implementation='sdpa', local_files_only=True)
        wavs, rate = tts.generate_custom_voice(
            text=text, language=params['language'], speaker=params['voice'],
            instruct=params.get('instructions', ''), max_new_tokens=8192)
        samples = np.concatenate(wavs)
    elif engine == 'cosyvoice':
        source = Path(request['source_dir'])
        sys.path[:0] = [str(source), str(source / 'third_party' / 'Matcha-TTS')]
        import torch
        from cosyvoice.cli.cosyvoice import AutoModel
        torch.set_num_threads(2)
        if request['device'] == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('CUDA is unavailable')
        tts = AutoModel(model_dir=str(model), load_jit=False, load_trt=False, fp16=False)
        chunks = [chunk['tts_speech'].detach().cpu().numpy().reshape(-1)
                  for chunk in tts.inference_sft(text, params['voice'], stream=False,
                                                 speed=params['speed'], text_frontend=False)]
        if not chunks:
            raise RuntimeError('Empty audio')
        samples, rate = np.concatenate(chunks), tts.sample_rate
    else:
        raise ValueError('Unknown local engine')
    if not samples.size or not np.isfinite(samples).all() or samples.size * 2 > 20_000_000 - 44:
        raise ValueError('Invalid or oversized local audio')
    sf.write(request['output'], samples, rate, subtype='PCM_16', format='WAV')


if __name__ == '__main__':
    try:
        generate(json.load(sys.stdin))
    except Exception:
        # Provider text, paths, and inherited credentials must not enter API errors.
        import traceback
        traceback.print_exc()
        print('Local inference failed; check the selected model and runtime.', file=sys.stderr)
        raise SystemExit(1)
