"""Inference entry point run by a managed Python environment, never by the UI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def generate_index(request):
    import torch
    from indextts.infer_v2 import IndexTTS2
    model = Path(request['model_dir'])
    params = request['params']
    aux = model / 'aux'
    paths = dict(w2v_bert=str(aux / 'w2v'),
                 semantic_codec=str(aux / 'codec' / 'semantic_codec' / 'model.safetensors'),
                 campplus=str(aux / 'campplus' / 'campplus_cn_common.bin'),
                 bigvgan=str(aux / 'bigvgan'))
    torch.manual_seed(int(params['seed']))
    tts = IndexTTS2(cfg_path=str(model / 'config.yaml'), model_dir=str(model),
                    device='cuda:0' if request['device'] == 'cuda' else 'cpu',
                    use_fp16=request['device'] == 'cuda', use_cuda_kernel=False,
                    use_deepspeed=False, use_qwen_emo=False, aux_paths=paths)
    tts.infer(spk_audio_prompt=request['reference_audio'], text=request['text'],
              output_path=request['output'], verbose=False,
              temperature=params['temperature'], top_p=params['top_p'], do_sample=True)


def glm_windows_normalizers():
    """Use the portable WeText runtime for the same normalization entry points."""
    if sys.platform != 'win32':
        return
    import types
    from wetext import Normalizer
    for language, module_name in [('zh', 'tn.chinese.normalizer'), ('en', 'tn.english.normalizer')]:
        class PortableNormalizer:
            def __init__(self, _language=language, **kwargs):
                self.normalizer = Normalizer(lang=_language, operator='tn',
                                             **({'remove_erhua': kwargs['remove_erhua']} if 'remove_erhua' in kwargs else {}))

            def normalize(self, text):
                return self.normalizer.normalize(text)
        module = types.ModuleType(module_name)
        module.Normalizer = PortableNormalizer
        sys.modules[module_name] = module


def generate_glm(request):
    import torch
    glm_windows_normalizers()
    import glmtts_inference as glm
    params = request['params']
    rate = int(params['sample_rate'])
    frontend, text_frontend, _, llm, flow = glm.load_models(use_phoneme=False, sample_rate=rate)
    prompt_text = text_frontend.text_normalize(params['reference_text']) + ' '
    text = text_frontend.text_normalize(request['text'])
    reference = request['reference_audio']
    prompt_text_token = frontend._extract_text_token(prompt_text)
    prompt_speech_token = frontend._extract_speech_token([reference])
    speech_feat = frontend._extract_speech_feat(reference, sample_rate=rate)
    embedding = frontend._extract_spk_embedding(reference)
    speech_tokens = [prompt_speech_token.squeeze().tolist()]
    cache = {'cache_text': [prompt_text], 'cache_text_token': [prompt_text_token],
             'cache_speech_token': speech_tokens, 'use_cache': True}
    audio, _, _, _ = glm.generate_long(
        frontend=frontend, text_frontend=text_frontend, llm=llm, flow=flow,
        text_info=['tts-lab', text], cache=cache, embedding=embedding,
        flow_prompt_token=torch.tensor(speech_tokens, dtype=torch.int32).to(glm.DEVICE),
        speech_feat=speech_feat, seed=int(params['seed']), device=glm.DEVICE, use_phoneme=False)
    return audio.detach().cpu().numpy().reshape(-1), rate


def generate(request):
    import numpy as np
    import soundfile as sf

    engine = request['engine']
    model = Path(request['model_dir'])
    params = request['params']
    text = request['text']
    if engine in {'glmtts', 'indextts'}:
        import torch
        torch.set_num_threads(2)
        if request['device'] == 'cuda' and not torch.cuda.is_available():
            raise RuntimeError('CUDA is unavailable')
        source = Path(request['source_dir'])
        sys.path.insert(0, str(source))
        if engine == 'indextts':
            generate_index(request)
            return
        if request['device'] != 'cuda':
            raise RuntimeError('GLM-TTS requires CUDA')
        os.chdir(source)
        samples, rate = generate_glm(request)
    elif engine == 'kokoro':
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
