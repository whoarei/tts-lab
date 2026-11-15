'use strict';

const $ = (selector, parent = document) => parent.querySelector(selector);
const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
const samples = [
  ['中文日常', '你好，欢迎来到语音工作台。今天我们用同一段文字，听一听不同声音的自然度、停顿和情感。你更喜欢哪一种？'],
  ['数字与多音字', '银行行长说，这张银行卡不能转借。重庆的重量级选手重新出发。订单编号是 A-1028，金额为 3,205.60 元，折扣是百分之十五，发车时间是 2026 年 11 月 9 日上午 8 点 05 分。'],
  ['英语课文', 'Last Saturday, I went to the park with my parents. We played badminton and took many pictures. On Sunday, I stayed at home and did my homework. What did you do last weekend?'],
  ['中英混读', '今天我们来学习一个英语短语：take your time，意思是“慢慢来，不着急”。打开 Settings，选择 Wi-Fi，然后点击 Connect。注意，API 和 USB 通常按字母读。'],
  ['叙事表达', '“你终于来了。”她轻轻地说，声音里有一点惊喜，也有一点委屈。窗外的雨停了，街灯亮了起来。他停顿片刻，笑着回答：“这一次，我不会再错过了。”']
];
const studio = blind => ({variants: [], text: samples[0][1], blind,
  runs: [], batch: '', order: [], message: '', error: false, autoplay: false});
const S = {desktop: false, token: '', providers: [], presets: [], view: 'test', studioView: 'test', busy: false,
  references: [], version: '', activityBusy: false, modelBusy: false, updating: false,
  activeMode: '', test: studio(false), compare: studio(true), saveTarget: null, playback: null};
Object.defineProperty(S, 'saving', {get: () => [...S.test.variants, ...S.compare.variants].some(v => v.saving)});
const apiUrl = path => window.ttsDesktop?.apiUrl ? window.ttsDesktop.apiUrl(path) : path;
let modelTimer;
const H = {page: 1, pageSize: 10, revision: 0};
const isStudioView = (view = S.view) => view === 'test' || view === 'compare';

function notice(message = '', error = false) {
  const box = $('#notice');
  box.textContent = message;
  box.hidden = !message;
  box.classList.toggle('error', error);
  if (isStudioView()) Object.assign(S[S.view], {message, error});
}
async function api(path, options = {}) {
  if (S.updating && !['GET', 'HEAD', 'OPTIONS'].includes((options.method || 'GET').toUpperCase())) throw new Error('正在安装应用更新，请等待应用重新启动。');
  const response = await fetch(apiUrl(path), {...options, headers: {'Content-Type': 'application/json',
    'X-Local-Token': S.token, ...options.headers}});
  let data;
  try { data = await response.json(); }
  catch { throw new Error('服务响应异常，请检查本机程序是否仍在运行。'); }
  if (!response.ok) {
    let message = data.detail;
    if (Array.isArray(message)) message = message.map(x => x.msg).join('；');
    throw new Error(message || '请求失败');
  }
  return data;
}
const provider = id => S.providers.find(p => p.id === id);
const providerAvailable = item => !!item?.ready && (item.family === 'local'
  ? !!item.installed_models?.length : item.family !== 'demo' && item.credentials_configured === true);
const selectableProviders = () => S.providers.filter(providerAvailable);
const variantAvailable = variant => {
  const item = provider(variant.provider);
  return providerAvailable(item) && (item.family !== 'local' || item.installed_models.includes(variant.params.model));
};
function defaults(id) {
  const item = provider(id);
  if (!item) return {};
  const params = Object.fromEntries(item.fields.map(f => [f.key, f.default]));
  if (item.family === 'local' && item.installed_models?.length) params.model = item.installed_models[0];
  return params;
}
const initialProvider = () => selectableProviders()[0];
function makeVariant(id = initialProvider()?.id || '', params) {
  return {uid: crypto.randomUUID(), provider: id, params: {...(params || defaults(id))},
    presetId: '', advanced: false, saving: false};
}
function option(value, label = String(value)) {
  const node = el('option', label);
  node.value = String(value);
  return node;
}
function button(text, cls, action) {
  const node = el('button', text, cls);
  node.type = 'button';
  node.onclick = event => { if (!node.disabled) return action(event); };
  return node;
}
function isDirty(v) {
  const saved = S.presets.find(p => p.id === v.presetId);
  return !saved || saved.provider !== v.provider ||
    Object.keys(v.params).some(key => v.params[key] !== saved.params[key]);
}
function updateSaveState(card, v) {
  const dirty = isDirty(v);
  const status = $('.preset-status', card);
  status.textContent = v.saving ? '保存中…' : v.presetId ? dirty ? '有未保存更改' : '已保存' : '';
  status.classList.toggle('dirty', !!v.presetId && dirty);
  $('.save-preset', card).disabled = S.busy || S.updating || C.saving || v.saving || !variantAvailable(v) || (!!v.presetId && !dirty);
  $('.save-as', card).disabled = S.busy || S.updating || C.saving || v.saving || !variantAvailable(v);
}
function renderField(field, v, card) {
  const label = el('label', undefined, 'field');
  if (field.type === 'textarea') label.classList.add('wide');
  label.append(el('span', field.label));
  let input;
  if (field.type === 'reference') {
    label.classList.add('wide'); input = el('select');
    input.append(option('', '选择参考录音'));
    S.references.forEach(ref => input.append(option(ref.id, `${ref.name} · ${ref.duration_seconds} 秒`)));
  } else if (field.type === 'select') {
    input = el('select');
    const item = provider(v.provider);
    const installed = field.key === 'model' && item?.family === 'local' ? item.installed_models || [] : null;
    const values = (field.options || []).filter(o => !installed || installed.includes(typeof o === 'object' ? o.value : o));
    if (installed && v.params[field.key] && !installed.includes(v.params[field.key])) {
      const current = option(v.params[field.key], `${v.params[field.key]} · 未安装`);
      current.disabled = true; input.append(current);
    }
    values.forEach(o => input.append(typeof o === 'object' ? option(o.value, o.label) : option(o)));
  } else if (field.type === 'textarea') {
    input = el('textarea'); input.rows = 2;
  } else {
    input = el('input');
    input.type = ['number', 'checkbox'].includes(field.type) ? field.type : 'text';
  }
  input.dataset.param = field.key;
  if (field.type === 'checkbox') input.checked = !!v.params[field.key];
  else input.value = v.params[field.key] ?? '';
  for (const key of ['min', 'max']) if (field[key] !== undefined) input[key] = field[key];
  // Fractional catalog steps are editing hints. Providers accept continuous
  // values; a min of .25 with step .1 would otherwise reject the default 1.
  if (field.type === 'number') input.step = field.step === 1 ? '1' : 'any';
  if (['text', 'textarea'].includes(field.type)) input.maxLength = 2000;
  if (field.default !== '' && field.type !== 'checkbox') input.required = true;
  input.disabled = S.busy || S.updating || C.saving || v.saving || !providerAvailable(provider(v.provider));
  input.addEventListener('input', () => {
    v.params[field.key] = field.type === 'checkbox' ? input.checked :
      field.type === 'number' && input.value !== '' ? Number(input.value) : input.value;
    updateSaveState(card, v);
    updateRunbar();
  });
  label.append(input);
  if (field.type === 'reference') {
    input.onchange = () => renderVariants();
    const upload = el('input'); upload.type = 'file'; upload.accept = '.wav,audio/wav';
    upload.setAttribute('aria-label', '导入参考录音'); upload.disabled = input.disabled;
    upload.onchange = () => { if (upload.files[0]) void uploadReference(upload.files[0], v); };
    label.append(upload);
    if (v.params[field.key]) {
      const preview = el('audio'); preview.controls = true; preview.preload = 'none';
      preview.src = apiUrl(`/api/references/${encodeURIComponent(v.params[field.key])}/audio`); label.append(preview);
    }
  }
  if (field.help) label.append(el('small', field.help));
  return label;
}
function validEditor(v) {
  const card = Array.from(document.querySelectorAll('.variant')).find(node => node.dataset.uid === v.uid);
  if (!card) return true;
  for (const input of card.querySelectorAll('input, select, textarea')) {
    if (!input.checkValidity()) {
      const details = input.closest('details');
      if (details) details.open = true;
      input.reportValidity();
      return false;
    }
  }
  return true;
}
function renderVariants() {
  if (!isStudioView()) { updateRunbar(); return; }
  const mode = S[S.view];
  const root = $('#variants');
  root.replaceChildren();
  mode.variants.forEach((v, index) => {
    const p = provider(v.provider);
    const card = el('article', undefined, 'variant');
    card.dataset.uid = v.uid;
    if (S.view === 'compare') {
      const head = el('div', undefined, 'variant-header');
      head.append(el('span', `方案 ${index + 1}`));
      const actions = el('div');
      const clone = button('复制', 'icon-button', () => {
        const copy = makeVariant(v.provider, v.params);
        copy.presetId = v.presetId;
        mode.variants.push(copy); renderVariants();
      });
      clone.disabled = S.busy || S.updating || C.saving || v.saving || !variantAvailable(v) || mode.variants.length >= 6;
      const remove = button('移除', 'icon-button', () => {
        mode.variants = mode.variants.filter(x => x.uid !== v.uid); renderVariants();
      });
      remove.disabled = S.busy || S.updating || C.saving || v.saving || mode.variants.length <= 2;
      actions.append(clone, remove); head.append(actions); card.append(head);
    }
    const baseLabel = el('label', undefined, 'field preset-select');
    baseLabel.append(el('span', '基础方案（可选）'));
    const base = el('select');
    base.className = 'preset';
    base.setAttribute('aria-label', `方案 ${index + 1} 基础方案`);
    base.append(option('', '不使用基础方案'));
    S.presets.filter(saved => provider(saved.provider)).forEach(saved => base.append(option(saved.id, saved.name)));
    base.value = v.presetId;
    base.disabled = S.busy || S.updating || C.saving || v.saving;
    base.onchange = () => {
      const saved = S.presets.find(x => x.id === base.value);
      v.presetId = saved?.id || '';
      v.provider = saved?.provider || v.provider;
      v.params = {...(saved?.params || defaults(v.provider))};
      renderVariants();
    };
    baseLabel.append(base); card.append(baseLabel);
    const serviceLabel = el('label', undefined, 'field provider-field');
    serviceLabel.append(el('span', '语音服务'));
    const service = el('select', undefined, 'provider');
    service.setAttribute('aria-label', `方案 ${index + 1} 语音服务`);
    const available = selectableProviders();
    if (!providerAvailable(p)) {
      const current = option(v.provider, p ? `${p.name} · 暂不可用` : available.length ? '选择语音服务' : '暂无可用服务');
      current.disabled = true; service.append(current);
    }
    available.forEach(item => service.append(option(item.id, item.name)));
    service.value = v.provider;
    service.disabled = S.busy || S.updating || C.saving || v.saving || !available.length;
    service.onchange = () => {
      if (!providerAvailable(provider(service.value))) return;
      v.provider = service.value; v.params = defaults(v.provider); renderVariants();
    };
    serviceLabel.append(service); card.append(serviceLabel);
    const coreKeys = new Set(['model', 'voice', 'voice_id', 'speed', 'speaking_rate', 'rate', 'instructions', 'reference_id', 'reference_text']);
    const basic = el('div', undefined, 'field-grid');
    const more = el('div', undefined, 'field-grid');
    (p?.fields || []).forEach(field => (coreKeys.has(field.key) ? basic : more).append(renderField(field, v, card)));
    card.append(basic);
    if (more.childElementCount) {
      const advanced = el('details', undefined, 'advanced');
      advanced.open = v.advanced;
      advanced.ontoggle = () => { v.advanced = advanced.open; };
      advanced.append(el('summary', '更多参数'), more); card.append(advanced);
    }
    const saveActions = el('div', undefined, 'save-actions');
    const save = button('保存', 'text-button save-preset', () => {
      if (validEditor(v)) v.presetId ? void savePreset(v) : openSave(v);
    });
    const saveAs = button('另存为', 'text-button save-as', () => { if (validEditor(v)) openSave(v); });
    saveActions.append(el('span', '', 'preset-status'), save, saveAs); card.append(saveActions);
    if (p) {
      const info = el('details', undefined, 'provider-info');
      info.append(el('summary', p.warning ? '服务说明 · Legacy 接口' : '服务说明'));
      info.append(el('p', p.description));
      if (p.warning) info.append(el('p', p.warning, 'warning'));
      if (!p.ready) info.append(el('p', p.family === 'local' ? '所选服务需要先安装本地模型。' : `凭证变量：${p.missing_env.join('、')}`));
      if (p.docs) {
        const link = el('a', '接口文档 ↗', 'doc-link');
        link.href = p.docs; link.target = '_blank'; link.rel = 'noreferrer'; info.append(link);
      }
      card.append(info);
    }
    root.append(card); updateSaveState(card, v);
  });
  $('#add-variant').disabled = S.busy || S.updating || C.saving || !selectableProviders().length || mode.variants.length >= 6;
  $('#variant-limit').hidden = S.view !== 'compare' || mode.variants.length < 6;
  $('#add-variant').hidden = S.view !== 'compare' || mode.variants.length >= 6;
  document.querySelectorAll('.history-row button').forEach(node => { node.disabled = S.busy; });
  updateRunbar(); freezeUpdateControls();
}
function openSave(v) {
  if (S.updating || S.busy || C.saving || v.saving || !variantAvailable(v)) return;
  S.saveTarget = v;
  const saved = S.presets.find(x => x.id === v.presetId);
  $('#preset-name').value = saved ? `${saved.name} 副本`.slice(0, 80) : '';
  $('#save-error').hidden = true;
  $('#save-dialog').showModal();
  $('#preset-name').focus();
}
async function savePreset(v, newName) {
  if (S.busy || S.updating || C.saving || v.saving || !variantAvailable(v)) return;
  const existing = S.presets.find(x => x.id === v.presetId);
  const isNew = newName !== undefined;
  const name = isNew ? newName.trim() : existing?.name;
  if (!name) {
    $('#save-error').textContent = '请输入方案名称。'; $('#save-error').hidden = false; return;
  }
  v.saving = true;
  $('#confirm-save').disabled = true;
  $('#cancel-save').disabled = true;
  renderVariants();
  try {
    const saved = await api(isNew ? '/api/presets' : `/api/presets/${v.presetId}`, {
      method: isNew ? 'POST' : 'PUT', body: JSON.stringify({name, provider: v.provider, params: v.params})});
    S.presets = [saved, ...S.presets.filter(x => x.id !== saved.id)];
    v.presetId = saved.id; v.params = {...saved.params};
    if (isNew) $('#save-dialog').close();
  } catch (error) {
    if (isNew) { $('#save-error').textContent = error.message; $('#save-error').hidden = false; }
    else notice(error.message, true);
  } finally {
    v.saving = false;
    $('#confirm-save').disabled = false;
    $('#cancel-save').disabled = false;
    renderVariants();
  }
}
function updateCount() {
  $('#text-count').textContent = `${Array.from($('#text').value).length} / 2000`;
  if (isStudioView()) S[S.view].text = $('#text').value;
  for (const node of $('#samples').children) node.classList.toggle('active', node.dataset.text === $('#text').value);
  updateRunbar();
}
function requestKey(text, variants) {
  const normalized = variants.map(v => JSON.stringify([v.provider,
    Object.fromEntries(Object.entries(v.params).sort(([a], [b]) => a.localeCompare(b))
      .map(([key, value]) => [key, typeof value === 'string' ? value.trim() : value]))]));
  return JSON.stringify([text.trim(), normalized.sort()]);
}
function matchesRuns(runs, text, variants) {
  return runs.length > 0 && runs.every(run => run.status === 'completed') &&
    requestKey(runs[0].text, runs) === requestKey(text, variants);
}
async function cachedRuns(text, variants) {
  const {runs} = await api('/api/runs');
  const batches = new Map();
  for (const run of runs) {
    if (!batches.has(run.batch_id)) batches.set(run.batch_id, []);
    batches.get(run.batch_id).push(run);
  }
  return Array.from(batches.values()).find(batch => matchesRuns(batch, text, variants));
}
function updateRunbar() {
  const mode = S[S.studioView];
  const canListen = mode.variants.length && (mode.variants.every(variantAvailable) || matchesRuns(mode.runs, mode.text, mode.variants));
  $('#generate').disabled = S.busy || S.updating || C.saving || S.saving || !canListen;
  $('#generate').textContent = S.busy ? '准备试听…' : S.studioView === 'test' ? '试听' : '试听对比';
  credentialControls(); renderUpdateControls();
}
function showView(view) {
  if (!['test', 'compare', 'models', 'settings', 'about'].includes(view)) return;
  if (isStudioView()) {
    if (S.view !== view) S[S.view].autoplay = false;
    S[S.view].text = $('#text').value;
  }
  S.view = view;
  if (isStudioView(view)) S.studioView = view;
  S.playback = null;
  clearTimeout(modelTimer);
  clearTimeout(activityTimer);
  document.querySelectorAll('[data-view]').forEach(node => {
    node.classList.toggle('active', node.dataset.view === view);
    if (node.dataset.view === view) node.setAttribute('aria-current', 'page');
    else node.removeAttribute('aria-current');
  });
  $('#page-title').textContent = {test: '文本测试', compare: '方案对比', models: '模型配置', settings: '设置', about: '关于'}[view];
  $('#studio-view').hidden = !isStudioView(view);
  $('#local-model-panel').hidden = view !== 'models';
  $('#about-panel').hidden = view !== 'about';
  $('#settings-panel').hidden = view !== 'settings';
  document.querySelectorAll('audio').forEach(audio => audio.pause());
  if (view === 'settings') {
    $('#notice').hidden = true;
    void loadPreferences(); return;
  }
  if (view === 'about') {
    $('#notice').hidden = true;
    renderAboutVersion(); void refreshAboutActivity(); return;
  }
  if (view === 'models') {
    $('#notice').hidden = true;
    renderCloudModels(); void refreshModels(); return;
  }
  $('#studio-layout').classList.toggle('compare-layout', view === 'compare');
  $('#variants-title').textContent = view === 'test' ? '语音参数' : '对比方案';
  $('#text').value = S[view].text;
  $('#text').rows = view === 'compare' ? 4 : 10;
  $('#blind').checked = S[view].blind;
  $('#blind-control').hidden = view !== 'compare';
  $('#blind-hint').hidden = view !== 'compare' || !S[view].blind;
  $('#results').replaceChildren();
  notice(S[view].message, S[view].error);
  updateCount(); renderVariants(); renderResults(); void history();
}
function shuffle(items) {
  const list = [...items];
  for (let i = list.length - 1; i > 0; i--) {
    const j = crypto.getRandomValues(new Uint32Array(1))[0] % (i + 1);
    [list[i], list[j]] = [list[j], list[i]];
  }
  return list;
}
function stateLabel(state) {
  return {queued: '排队中', running: '正在合成', failed: '生成失败', interrupted: '已中断'}[state] || state;
}
function renderResults() {
  if (!isStudioView()) return;
  const mode = S[S.view], root = $('#results'), blind = mode.blind;
  root.classList.toggle('comparison', S.view === 'compare');
  $('#results-section').hidden = !mode.runs.length;
  $('#exports').hidden = !mode.runs.some(run => run.status === 'completed');
  if (!mode.runs.length) { root.replaceChildren(); return; }
  for (const old of Array.from(root.children)) if (!mode.order.includes(old.dataset.run)) old.remove();
  $('#download-batch').href = `/api/batches/${mode.batch}/download`;
  const sequence = [...mode.runs].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const remaining = mode.variants.map((_, index) => index), labels = new Map();
  for (const [index, run] of sequence.entries()) {
    const match = remaining.find(slot => requestKey(run.text, [mode.variants[slot]]) === requestKey(run.text, [run]));
    labels.set(run.id, (match ?? index) + 1);
    if (match !== undefined) remaining.splice(remaining.indexOf(match), 1);
  }
  mode.order.forEach((id, index) => {
    const run = mode.runs.find(x => x.id === id);
    if (!run) return;
    const existing = root.querySelector(`[data-run="${id}"]`);
    if (existing && existing.dataset.status === run.status && existing.dataset.blind === String(blind) && existing.dataset.scheme === String(labels.get(id))) return;
    const card = el('article', undefined, 'result-card');
    Object.assign(card.dataset, {run: id, status: run.status, blind: String(blind), scheme: String(labels.get(id))});
    if (S.view === 'compare') {
      card.append(el('h3', blind ? `声音 ${String.fromCharCode(65 + index)}` : `方案 ${labels.get(id)}`));
    }
    if (run.status !== 'completed') {
      const status = el('div', stateLabel(run.status), 'status');
      status.dataset.state = run.status; status.setAttribute('role', 'status'); card.append(status);
    }
    if (['failed', 'interrupted'].includes(run.status)) card.append(el('p', run.error, 'hint error'));
    if (run.status === 'completed') {
      const audio = el('audio'); audio.controls = true; audio.preload = 'metadata'; audio.src = apiUrl(`/api/runs/${id}/audio`);
      audio.setAttribute('aria-label', S.view === 'test' ? '试听音频' : blind ? `声音 ${String.fromCharCode(65 + index)}` : `方案 ${labels.get(id)}`);
      audio.setAttribute('controlslist', 'nodownload');
      audio.onplay = () => {
        if (S.playback?.ids[S.playback.index] !== id) S.playback = null;
        document.querySelectorAll('audio').forEach(other => { if (other !== audio) other.pause(); });
      };
      audio.onended = () => {
        const playback = S.playback;
        if (playback?.ids[playback.index] !== id) return;
        playback.index++;
        if (playback.index < playback.ids.length) void playNext();
        else S.playback = null;
      };
      card.append(audio);
      if (run.family === 'demo') card.append(el('p', '历史提示音，不朗读文本。', 'hint'));
      if (!blind) {
        let metrics = `生成 ${run.elapsed_seconds} s · ${(run.bytes / 1024).toFixed(1)} KB`;
        if (run.duration_seconds != null) metrics += ` · 音频 ${run.duration_seconds} s · RTF ${run.real_time_factor}`;
        card.append(el('div', metrics, 'metrics'));
      }
    }
    if (existing) existing.replaceWith(card); else root.append(card);
  });
  mode.order.forEach((id, index) => {
    const node = root.querySelector(`[data-run="${id}"]`);
    if (node && root.children[index] !== node) root.insertBefore(node, root.children[index] || null);
  });
}
async function playNext() {
  const playback = S.playback;
  if (!playback || playback.view !== S.view) return;
  const audio = $(`[data-run="${playback.ids[playback.index]}"] audio`);
  if (!audio) { S.playback = null; return; }
  try { audio.currentTime = 0; await audio.play(); }
  catch {
    if (S.playback !== playback) return;
    S.playback = null;
    notice('音频已就绪，请点击播放。');
  }
}
async function playResults(view) {
  const mode = S[view];
  if (!mode.autoplay || S.view !== view) return;
  mode.autoplay = false;
  const ids = mode.order.filter(id => mode.runs.some(run => run.id === id && run.status === 'completed'));
  if (!ids.length) return;
  S.playback = {view, ids, index: 0};
  await playNext();
}
function modeNotice(view, message, error = false) {
  Object.assign(S[view], {message, error});
  if (S.view === view) notice(message, error);
}
async function poll(view, batch) {
  const mode = S[view];
  if (batch !== mode.batch) return;
  try {
    mode.runs = await Promise.all(mode.runs.map(run => api(`/api/runs/${run.id}`)));
    if (S.view === view) renderResults();
    if (mode.runs.some(run => ['queued', 'running'].includes(run.status))) { setTimeout(() => poll(view, batch), 1000); return; }
    const success = mode.runs.filter(run => run.status === 'completed').length;
    modeNotice(view, success === mode.runs.length ? '' : `本轮 ${success}/${mode.runs.length} 组成功，请查看失败原因。`, !success);
    if (success) await playResults(view);
    else mode.autoplay = false;
  } catch (error) {
    modeNotice(view, `${error.message} 已停止查询，可从历史记录查看任务状态。请确认后再重新生成。`, true);
  }
  S.busy = false; S.activeMode = ''; renderVariants();
  void history(1);
}
async function listen() {
  if (S.busy || S.updating || C.saving || S.saving || !isStudioView()) return;
  notice();
  const view = S.view, mode = S[view], text = $('#text').value.trim();
  if (!text) { notice('请先输入要朗读的文本。', true); return; }
  const replay = matchesRuns(mode.runs, text, mode.variants);
  if (!replay && (!mode.variants.every(variantAvailable) || !mode.variants.every(validEditor))) return;
  const variants = mode.variants.map(v => ({provider: v.provider, params: {...v.params}}));
  mode.autoplay = true;
  S.playback = null;
  document.querySelectorAll('audio').forEach(audio => audio.pause());
  S.busy = true; S.activeMode = view; renderVariants();
  let submitted = false;
  try {
    const cached = matchesRuns(mode.runs, text, variants) ? mode.runs : await cachedRuns(text, variants);
    if (cached) {
      const sameBatch = mode.batch === cached[0].batch_id;
      mode.batch = cached[0].batch_id; mode.runs = cached;
      if (!sameBatch) mode.order = shuffle(cached.map(run => run.id));
      if (S.view === view) renderResults();
      await playResults(view);
      return;
    }
    for (const variant of variants) {
      const p = provider(variant.provider);
      if (!variantAvailable(variant)) {
        throw new Error(p.family === 'local' ? '所选本地模型尚未安装，请在“模型配置”中下载。' :
          `${p.name} 尚未配置，请在“模型配置”中填写凭证。`);
      }
    }
    const cloud = variants.some(v => provider(v.provider).family !== 'local');
    const data = await api('/api/batches', {method: 'POST', body: JSON.stringify({text,
      variants, allow_cloud: cloud})});
    mode.batch = data.batch_id; mode.runs = data.runs; mode.order = shuffle(data.runs.map(run => run.id));
    if (S.view === view) renderResults();
    submitted = true;
    setTimeout(() => poll(view, data.batch_id), 700);
  } catch (error) {
    mode.autoplay = false; modeNotice(view, error.message, true);
  } finally {
    if (!submitted) {
      S.busy = false; S.activeMode = ''; renderVariants();
    }
  }
}
async function history(page = H.page) {
  const revision = ++H.revision;
  $('#history-prev').disabled = true; $('#history-next').disabled = true;
  try {
    const data = await api(`/api/history?page=${page}&page_size=${H.pageSize}`);
    if (revision !== H.revision) return;
    H.page = data.page;
    const root = $('#history'); root.replaceChildren();
    if (!data.batches.length) root.append(el('p', '还没有记录。', 'hint'));
    data.batches.forEach(({runs, batch_id: id}) => {
      const row = el('div', undefined, 'history-row'), desc = el('div');
      row.dataset.batch = id;
      desc.append(el('b', `${new Date(runs[0].created_at).toLocaleString()} · ${runs.length} 组`),
        el('p', runs[0].text.slice(0, 65) + (runs[0].text.length > 65 ? '…' : '')));
      const open = button('打开', 'secondary', () => {
        if (S.busy || S.updating) return;
        const view = runs.length === 1 ? 'test' : 'compare', mode = S[view];
        if (S.view === view) $('#text').value = runs[0].text;
        mode.text = runs[0].text;
        if (runs.every(run => provider(run.provider))) {
          mode.variants = [...runs].sort((a, b) => a.created_at.localeCompare(b.created_at))
            .map(run => makeVariant(run.provider, run.params));
        }
        mode.message = ''; mode.error = false; mode.autoplay = false;
        mode.runs = runs; mode.batch = id; mode.order = shuffle(runs.map(run => run.id));
        showView(view);
        $('#results-section').scrollIntoView({behavior: 'smooth'});
        if (runs.some(run => ['queued', 'running'].includes(run.status))) {
          S.busy = true; S.activeMode = view; renderVariants(); setTimeout(() => poll(view, id), 800);
        }
      });
      open.disabled = S.busy || S.updating; row.append(desc, open); root.append(row);
    });
    $('#history-pagination').hidden = data.total_pages <= 1;
    $('#history-page').textContent = `${data.page} / ${data.total_pages}`;
    $('#history-prev').disabled = data.page <= 1;
    $('#history-next').disabled = data.page >= data.total_pages;
  } catch (error) {
    if (revision !== H.revision) return;
    $('#history').replaceChildren(el('p', error.message, 'hint error'));
    $('#history-pagination').hidden = true;
  }
}
async function refreshRetainedHistory() {
  void history(1);
  for (const mode of [S.test, S.compare]) {
    if (!mode.runs.length || mode.runs.some(run => ['queued', 'running'].includes(run.status))) continue;
    try { await api(`/api/runs/${mode.runs[0].id}`); }
    catch (error) {
      // A shorter retention period can remove the currently loaded result.
      // Preserve the editable draft, while discarding only an expired player.
      if (!error.message.includes('找不到记录')) continue;
      mode.runs = []; mode.batch = ''; mode.order = [];
    }
  }
  if (isStudioView()) renderResults();
}
function renderCloudModels() { renderServices(); }
async function modelAction(id, action) {
  if (S.updating) return;
  $('#model-notice').hidden = true;
  try {
    if (action === 'delete') {
      if (!window.confirm('删除这个模型的本地文件？历史音频与共用运行环境会保留。')) return;
      await api(`/api/local-models/${id}`, {method: 'DELETE'});
    } else if (action === 'install') {
      const device = id.startsWith('kokoro') ? 'cpu' : $('#local-device').value;
      await api(`/api/local-models/${id}/install`, {method: 'POST', body: JSON.stringify({device})});
    } else await api(`/api/local-models/${id}/cancel`, {method: 'POST', body: '{}'});
    if (S.view === 'models') await refreshModels();
  } catch (error) { $('#model-notice').textContent = error.message; $('#model-notice').hidden = false; }
}
async function refreshModels() {
  clearTimeout(modelTimer);
  try {
    const data = await api('/api/local-models');
    if (S.view !== 'models') return;
    const root = $('#local-model-list'); root.replaceChildren();
    const busy = data.models.some(model => model.busy);
    S.modelBusy = busy; credentialControls(); renderUpdateControls();
    for (const model of data.models) {
      const card = el('article', undefined, 'model-card'); card.dataset.model = model.id;
      card.append(el('h3', model.name), el('p', `${model.size} · ${model.ram}`));
      if (model.state !== 'not_installed') {
        const state = el('p', model.ready ? `已安装 · ${model.device === 'cuda' ? 'NVIDIA GPU' : 'CPU'}` : model.message, 'model-state');
        state.dataset.state = model.state; card.append(state);
      }
      if (model.busy) {
        const progress = el('progress'); progress.setAttribute('aria-label', model.name + ' 安装进度');
        if (model.state === 'downloading' && model.total_bytes > 0) {
          progress.max = model.total_bytes; progress.value = model.downloaded_bytes;
          card.append(el('p', `${model.file || ''} · ${(model.downloaded_bytes / 1048576).toFixed(1)} / ${(model.total_bytes / 1048576).toFixed(1)} MB`));
        }
        card.append(progress);
      }
      const actions = el('div', undefined, 'model-actions');
      if (!model.ready) {
        const install = button(['failed', 'interrupted', 'incomplete'].includes(model.state) ? '重试安装' : '下载模型',
          'secondary', () => modelAction(model.id, 'install'));
        install.disabled = busy || S.updating || !model.devices.includes(model.engine === 'kokoro' ? 'cpu' : $('#local-device').value); actions.append(install);
      }
      if (model.busy) actions.append(button('取消安装', 'text-button', () => modelAction(model.id, 'cancel')));
      else if (model.state !== 'not_installed') {
        const remove = button('删除', 'text-button', () => modelAction(model.id, 'delete'));
        remove.disabled = busy || S.busy || S.updating; actions.append(remove);
      }
      const docs = el('a', '说明 ↗', 'doc-link');
      docs.href = model.docs; docs.target = '_blank'; docs.rel = 'noreferrer';
      actions.append(docs, el('span', model.license, 'hint')); card.append(actions); root.append(card);
    }
    const state = await api('/api/state');
    S.providers = state.providers.filter(p => p.family !== 'demo'); renderCloudModels();
    freezeUpdateControls();
    if (S.view === 'models') modelTimer = setTimeout(refreshModels, busy ? 1500 : 5000);
  } catch (error) {
    if (S.view !== 'models') return;
    $('#model-notice').textContent = `无法更新模型状态：${error.message}`; $('#model-notice').hidden = false;
    modelTimer = setTimeout(refreshModels, 5000);
  }
}
async function init() {
  await setupSettings();
  try {
    const data = await api('/api/state');
    S.token = data.token; S.providers = data.providers.filter(p => p.family !== 'demo'); S.desktop = !!data.desktop; S.references = data.references || [];
    S.version = data.version; renderAboutVersion();
    if (S.desktop) $('#config-guide').textContent = '在“文件 → 编辑 API 凭证”中填写服务密钥，保存后选择“重启并载入凭证”。配置与历史保存在本机用户目录，凭证文件为明文，请勿分享。';
    S.presets = (await api('/api/presets')).presets;
    S.test.variants = [makeVariant()];
    const ready = selectableProviders();
    S.compare.variants = [makeVariant(), makeVariant(ready[1]?.id || initialProvider()?.id || '')];
    samples.forEach(([name, text]) => {
      const sample = button(name, '', () => { $('#text').value = text; updateCount(); });
      sample.dataset.text = text; $('#samples').append(sample);
    });
    document.querySelectorAll('[data-view]').forEach(node => { node.onclick = () => showView(node.dataset.view); });
    $('#text').oninput = updateCount;
    $('#clear-text').onclick = () => { $('#text').value = ''; updateCount(); $('#text').focus(); };
    $('#add-variant').onclick = () => {
      if (S.busy || S.updating || C.saving || S.view !== 'compare' || !selectableProviders().length || S.compare.variants.length >= 6) return;
      const previous = S.compare.variants.at(-1);
      S.compare.variants.push(makeVariant(variantAvailable(previous) ? previous.provider : initialProvider().id)); renderVariants();
    };
    $('#generate').onclick = () => void listen();
    $('#blind').onchange = () => {
      S[S.view].blind = $('#blind').checked;
      $('#blind-hint').hidden = !$('#blind').checked; renderResults();
    };
    $('#history-prev').onclick = () => void history(H.page - 1);
    $('#history-next').onclick = () => void history(H.page + 1);
    setupPreferences();
    $('#cancel-save').onclick = () => $('#save-dialog').close();
    $('#save-dialog').oncancel = event => { if (S.saveTarget?.saving) event.preventDefault(); };
    $('#save-form').onsubmit = event => { event.preventDefault(); void savePreset(S.saveTarget, $('#preset-name').value); };
    $('#local-device').onchange = () => void refreshModels();
    $('#text').value = S.test.text;
    showView('test');
    document.body.dataset.ready = 'true';
  } catch (error) {
    document.body.dataset.ready = 'error';
    notice(`无法连接本机服务：${error.message}`, true); $('#generate').disabled = true;
  }
}
async function uploadReference(file, variant) {
  if (S.updating || S.busy || variant.saving) return;
  if (file.size > 2 * 1024 * 1024) { notice('参考录音超过 2 MB，请使用 3–10 秒 WAV。', true); return; }
  try {
    const ref = await api('/api/references?name=' + encodeURIComponent(file.name), {
      method: 'POST', headers: {'Content-Type': 'audio/wav'}, body: file});
    S.references.push(ref); variant.params.reference_id = ref.id; renderVariants();
  } catch (error) { notice(error.message, true); }
}
init();
