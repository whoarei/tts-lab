# TTS adapter contracts

Documentation checked 2026-11-09. No paid requests made. These are implementation schemas, not a claim that any particular account has model access. Keep model, voice, region and full endpoint configurable. Fail clearly on unavailable models; never silently substitute another provider/model. All JSON requests use Content-Type: application/json.

## Local saved schemes

`GET /api/variants` returns `{ "variants": [], "selected_uid": "" }` before first use, or the last saved configuration. `PUT /api/variants` replaces that configuration atomically in the data directory's `variants.json`. Like other local writes, it requires `X-Local-Token`; desktop mode additionally requires the desktop session token on every route.

The payload contains 1–6 schemes, each with a unique `uid`, a 1–60 character `name`, a catalog `provider`, and its supported `params`. `selected_uid` must identify one of those schemes. Saving validates parameter types and ranges and fills provider defaults, but does not require credentials, an installed model, or any synthesis/network request. Unknown fields, credential-like parameters, invalid selection and duplicate IDs are rejected without changing the saved file. A corrupt file returns an explicit error rather than being silently replaced.

The text-test page sends exactly the selected saved scheme to the existing `POST /api/batches` API. The comparison page can use its current draft schemes; saving makes those drafts available to text testing. Credentials remain in the separate local configuration file and are never part of a saved scheme.

## OpenAI Speech

POST https://api.openai.com/v1/audio/speech
Header: Authorization: Bearer $OPENAI_API_KEY
Body: {"model":"gpt-4o-mini-tts","input":"TEXT","voice":"alloy","response_format":"wav","speed":1}
Response: raw audio bytes. Available formats include mp3, opus, aac, flac, wav, pcm. Input limit 4096 characters. Instructions are an optional model-dependent field; don't send to tts-1/tts-1-hd.
WARNING: Current official reference labels these Speech models deprecated and gives shutdown 2027-01-06. Use as an explicitly legacy benchmark adapter, not a future-proof integration. Reference recommends Realtime API migration, which is a different protocol.
Source: https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create

## Gemini native TTS, Generate Content protocol

POST https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent
Header: x-goog-api-key: $GEMINI_API_KEY
Body: {"contents":[{"parts":[{"text":"TEXT"}]}],"generationConfig":{"responseModalities":["AUDIO"],"speechConfig":{"voiceConfig":{"prebuiltVoiceConfig":{"voiceName":"Kore"}}}}}
Read candidates[0].content.parts; find inlineData with audio mimeType and base64-decode its data. Older TTS outputs raw 24kHz mono 16-bit little-endian PCM; wrap as WAV, but inspect mimeType and RIFF magic rather than assuming every model is PCM. Preserve complete audio parts in order. Missing audio is an error, not an empty successful file.
Documented IDs include gemini-2.5-flash-preview-tts and gemini-3.1-flash-tts-preview. Model access is not verified. Current docs classify Generate Content as legacy; newest interactions-based TTS has a different contract. Do not put newer interactions-only models into this adapter.
Sources: https://ai.google.dev/gemini-api/docs/generate-content/speech-generation?hl=en and https://ai.google.dev/api/generate-content?hl=en
Caveat: Full guide open redirected to an inaccessible AI Studio page; official search excerpts confirmed voice config, PCM conversion and 3.1 generateContent endpoint. 2.5 model listing: https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-preview-tts

## Alibaba Qwen3-TTS HTTP, legacy multimodal endpoint

POST https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
Header: Authorization: Bearer $DASHSCOPE_API_KEY
Body: {"model":"qwen3-tts-flash","input":{"text":"TEXT","voice":"Cherry","language_type":"Chinese"}}
Nonstream response: output.audio.url points to complete audio; GET it without API authorization headers and save WAV bytes. output.audio.data is empty in nonstream mode. URL expires (documented 24 hours), so download promptly. Input maximum 600 characters. language_type defaults Auto. Beijing uses dashscope.aliyuncs.com with corresponding region key; Singapore/Beijing keys differ.
Do not confuse newer Qwen-Audio models /audio/tts/SpeechSynthesizer with this model family and schema. Current docs retain the above curl example despite newer workspace-specific SDK routing.
Source: https://www.alibabacloud.com/help/en/model-studio/qwen-tts-api

## MiniMax HTTP T2A v2

POST https://api.minimax.io/v1/t2a_v2
Header: Authorization: Bearer $MINIMAX_API_KEY
Body: {"model":"speech-2.8-hd","text":"TEXT","stream":false,"language_boost":"auto","output_format":"hex","voice_setting":{"voice_id":"English_expressive_narrator","speed":1,"vol":1,"pitch":0},"audio_setting":{"sample_rate":32000,"bitrate":128000,"format":"mp3","channel":1}}
Check base_resp.status_code == 0 even when HTTP succeeds. Decode data.audio using bytes.fromhex, NOT base64. Result is mp3 bytes for above request. Models and voices are configurable; shown model/voice are current official example, not a universal Chinese default. Select a documented Chinese voice for Chinese tests.
Source: https://platform.minimax.io/docs/api-reference/speech-t2a-http

## Xiaomi MiMo (checked 2026-11-13)

POST https://api.xiaomimimo.com/v1/chat/completions with api-key: $MIMO_API_KEY.
Send model=mimo-v2.5-tts, stream=false, messages containing optional user style instructions followed by assistant target text, and audio={format:wav, voice:冰糖}. Decode choices[0].message.audio.data as base64; require finish_reason=stop and recognizable audio. WAV/MP3 are exposed, avoiding raw PCM ambiguity. For mimo-v2.5-tts-voicedesign, require user voice description and omit audio.voice. Do not enable optimize_text_preview, which can rewrite comparison text. VoiceClone exists but is not implemented here.
Sources: https://mimo.mi.com/docs/en-US/api/audio/tts-post and https://mimo.mi.com/docs/en-US/quick-start/usage-guide/audio/speech-synthesis-v2.5

## ByteDance Doubao / Seed TTS (checked 2026-11-13)

POST https://openspeech.bytedance.com/api/v3/tts/unidirectional. Headers: X-Api-Key=$VOLCENGINE_TTS_API_KEY, X-Api-Resource-Id=seed-tts-2.0 (or seed-icl-2.0 for an existing cloned voice), X-Api-Request-Id=random UUID. Use the new Doubao Voice console key, not an Ark chat key; legacy App-ID/Access-Key auth is not implemented.
Body req_params contains text, speaker, audio_params={format:mp3,sample_rate:24000,speech_rate:0,loudness_rate:0}. Optional additions is a JSON STRING encoding context_texts:[style instruction], only exposed for TTS 2.0.
Response is newline-delimited JSON independent of transport chunk boundaries. Decode each code=0 data field from base64 and concatenate bytes; sentence-only records carry no audio. Require code=20000000 completion; any other code, malformed data, missing completion, empty audio or oversized result fails without retry. Buffer the complete response before playback; do not label its timing as first-audio latency. This adapter does not register cloned voices.
Sources: https://www.volcengine.com/docs/6561/1598757 (detailed completion marker) and https://docs.volcengine.com/docs/doubao-voice/unidirectional-streaming-text-to-speech-http?lang=zh

## DeepSeek availability (checked 2026-11-13; no adapter)

The public API documentation reviewed did not establish a supported TTS endpoint/model/response contract. No DeepSeek TTS provider is exposed. This is a bounded documentation check, not proof that its apps lack read-aloud or that no private service exists. Speech input, application playback and third-party TTS integrations do not establish an independently callable DeepSeek TTS model.
Source: https://api-docs.deepseek.com/zh-cn/

## StepFun StepAudio (checked 2026-11-13)

POST https://api.stepfun.com/v1/audio/speech (China) or https://api.stepfun.ai/v1/audio/speech (international), Authorization: Bearer $STEP_API_KEY. Use model=stepaudio-3-tts or stepaudio-2.5-tts, input, voice=zixinnansheng, language=zh, response_format=wav, speed=1, volume=1, return_url=false. Optional instruction is a separate global style string (3: max 500 characters; 2.5: max 200). Input max 1000 characters. Parenthesized input is treated as directions and not spoken. Decode raw binary audio; never parse this request as chat or follow a returned URL. This uses normal API billing, not Step Plan endpoints.
Source: https://platform.stepfun.ai/docs/en/api-reference/audio/create-audio

## Fish Audio hosted TTS (checked 2026-11-13)

POST https://api.fish.audio/v1/tts, Authorization: Bearer $FISH_AUDIO_API_KEY, model header selected from s1, s2-pro, s2.1-pro, s2.1-pro-free, drama-3. Body: text, reference_id=an existing permitted voice model ID, format=wav or mp3, temperature=0.7, top_p=0.7, normalize=true. Decode binary audio. The API falls back for unknown model header values, so the adapter restricts models rather than accepting arbitrary strings. No voice upload or voice creation is performed. This is the hosted service, not a local Fish Speech installation; model/voice access remains account-dependent.
Source: https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech

## IndexTTS-2 and GLM-TTS local inference (checked 2026-11-13)

IndexTTS source: d9e41aac89fd00b3d71497fddb287b7f24613712. Instantiate indextts.infer_v2.IndexTTS2 with explicit local aux_paths, use_qwen_emo=false, use_cuda_kernel=false, use_deepspeed=false, and cpu or cuda:0. Call infer(spk_audio_prompt, text, output_path, temperature, top_p, do_sample=true). Download IndexTeam/IndexTTS-2 plus facebook/w2v-bert-2.0, amphion/MaskGCT semantic codec only, funasr/campplus file only, NVIDIA BigVGAN config/weights only. Each repository is frozen to its resolved snapshot and files verified before use. Avoid upstream automatic auxiliary downloads during inference.
GLM source: 4b944f4be7b6c55454751715081f4dc83992897a. Preserve source/ckpt layout for zai-org/GLM-TTS weights. Use glmtts_inference.load_models and generate_long with extracted prompt tokens/features, reference transcript and seed, following upstream Gradio logic. This code requires CUDA. Windows normalization uses portable wetext adapters for the upstream TN entry points; Linux uses WeTextProcessing. 24/32 kHz output is encoded as PCM16 WAV. The model's tokenizer Python code is included only for its documented filename and checked against the frozen snapshot.
User reference WAVs are bounded to 2 MiB / 3–10 seconds / PCM16 / 1–2 channels / 8–48 kHz. IDs, not paths, pass through the API; validate hash before use. Model installation uses upstream example audio for a trial generation and marks ready only after valid output. The IndexTTS reference is fetched separately from its official Demo Space at b01840e8e4fd9753743a6d0466cd73ae1d634a68, examples/voice_01.wav, with SHA256 e33e6ee0107a1dd58e1d66dd90c13df3d55a8683047cc3d7ea206dad84ed3fc8; it is not bundled in the source archive. References stay local and are not included as raw audio in batch exports; run metadata records their hash. Full weights and GPU inference validation are recorded separately from mocked contracts.
Sources: https://github.com/index-tts/index-tts and https://github.com/zai-org/GLM-TTS

## ElevenLabs REST

POST https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}?output_format=mp3_44100_128
Header: xi-api-key: $ELEVENLABS_API_KEY
Body: {"text":"TEXT","model_id":"eleven_multilingual_v2"}
Response: binary MP3. voice_id required. Example documented voice: JBFqnCBsd6RMkjVDRZzb, but expose it as configuration. model_id documented default is eleven_multilingual_v2. GET /v1/models provides can_do_text_to_speech. Optional voice_settings is model-sensitive; omit initially. Some higher-quality formats require paid tiers. Do not promise deterministic seed behavior.
Source: https://elevenlabs.io/docs/api-reference/text-to-speech/convert

## Azure Speech REST

POST https://{REGION}.tts.speech.microsoft.com/cognitiveservices/v1
Headers: Ocp-Apim-Subscription-Key: $AZURE_SPEECH_KEY; Content-Type: application/ssml+xml; X-Microsoft-OutputFormat: riff-24khz-16bit-mono-pcm; User-Agent: tts-lab
Body: <speak version='1.0' xml:lang='en-US'><voice name='en-US-ChristopherNeural'>TEXT</voice></speak>
XML-escape text and safely serialize attributes. Voice/language configurable; use regional voice list to choose Chinese voice. Response 200 is WAV bytes for above output format. No model JSON field: voice name selects voice. Region must match resource. Audio over 10 minutes is truncated.
Source: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-text-to-speech

## Traditional Google Cloud TTS

POST https://texttospeech.googleapis.com/v1/text:synthesize
Headers: Authorization: Bearer ACCESS_TOKEN; x-goog-user-project: PROJECT_ID
Body: {"input":{"text":"TEXT"},"voice":{"languageCode":"en-GB","name":"en-GB-Standard-A"},"audioConfig":{"audioEncoding":"MP3"}}
Response audioContent is base64 MP3. Use google-auth ADC to refresh a scoped cloud-platform token locally, or accept a short-lived supplied token. Billing and Text-to-Speech API must be enabled. This is separate from Gemini API keys and endpoint. Voice selection configurable; no generic model field needed for traditional voices.
Source: https://docs.cloud.google.com/text-to-speech/docs/create-audio-text-command-line

## Optional AWS Polly

Use boto3.client('polly', region_name=REGION) with normal AWS credential/profile chain, not bespoke credential parsing. Call synthesize_speech(Text=TEXT, TextType='text', OutputFormat='mp3', VoiceId=VOICE, Engine=ENGINE). Save response['AudioStream'].read(), close stream. Engine choices include standard, neural, long-form, generative; select only supported voice/engine/region combinations. Omit SampleRate initially and let API choose legal default. describe_voices can validate options. No universal Chinese neural voice assumption.
Source: https://docs.aws.amazon.com/boto3/latest/reference/services/polly/client/synthesize_speech.html

## Implementation cautions

- Nonstream full-request latency is NOT time-to-first-audio. Label it end-to-end latency.
- JSON error bodies can arrive with HTTP 200. Validate payload and decoded audio before success.
- Keep request config and usage metadata, but never log auth headers, tokens or signed audio URLs.
- Never send API credentials to the returned audio download host.
- Handle vendor format differences with ffmpeg normalization after preserving originals.
- Run mock tests locally; live verification remains unperformed until user supplies credentials and authorizes usage.

## Gemini current Interactions option (separate protocol)

Official current guide confirms gemini-3.8-flash-tts and gemini-3.8-flash-lite-tts. POST https://generativelanguage.googleapis.com/v1beta/interactions with x-goog-api-key.
Body: {"model":"gemini-3.8-flash-tts","input":[{"type":"user_input","content":[{"type":"text","text":"TEXT","annotations":[{"type":"speech_metadata","style":"calm and natural"}]}]}],"response_format":{"type":"audio","mime_type":"audio/wav"},"generation_config":{"speech_config":[{"voice":"Kore"}]}}
Unary audio defaults to complete WAV, not raw PCM. SDK exposes output_audio.data (base64). REST uses steps of type model_output containing content blocks of type audio; take the last audio block, decode data. Primary guide identifies output_audio as a convenience property, so do not assume it is always a REST top-level field. Exact REST extraction also corroborated by a guide mirror, but a live request has not verified it. Consider protocol selection explicit and this newer route experimental until smoke-tested. No legacy shutdown date was established.
Source: https://ai.google.dev/gemini-api/docs/speech-generation?authuser=7
Response timeline corroboration: https://ai.google.dev/gemini-api/docs/migrate-to-interactions?authuser=19
