'use strict';
const $ = (selector, parent = document) => parent.querySelector(selector);
const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
const newSession = () => ({runs: [], batch: '', order: [], drafts: {}});
const S = {desktop: false, token: '', providers: [], references: [], variants: [], savedVariants: [], selected: '',
  page: 'test', busy: false, saving: false, dirty: false, ready: false, modelBusy: false, updating: false,
  sessions: {test: newSession(), compare: newSession()}};
const U = {view: null, pending: false};
const C = {provider: '', loading: false, saving: false, revision: 0};
const samples = [
  ['中文日常', '你好，欢迎来到语音对比工作台。今天我们不看排行榜，而是用同一段文字，听一听不同声音的自然度、停顿和情感。你更喜欢哪一种？'],
  ['数字与多音字', '银行行长说，这张银行卡不能转借。重庆的重量级选手重新出发。订单编号是 A-1028，金额为 3,205.60 元，折扣是百分之十五，发车时间是 2026 年 11 月 9 日上午 8 点 05 分。'],
  ['英语课文', 'Last Saturday, I went to the park with my parents. We played badminton and took many pictures. On Sunday, I stayed at home and did my homework. What did you do last weekend?'],
  ['中英混读', '今天我们来学习一个英语短语：take your time，意思是“慢慢来，不着急”。打开 Settings，选择 Wi-Fi，然后点击 Connect。注意，API 和 USB 通常按字母读。'],
  ['叙事表达', '“你终于来了。”她轻轻地说，声音里有一点惊喜，也有一点委屈。窗外的雨停了，街灯亮了起来。他停顿片刻，笑着回答：“这一次，我不会再错过了。”']
];
const copy = value => JSON.parse(JSON.stringify(value));
const provider = id => S.providers.find(item => item.id === id);
const resultUI = (page, selector) => $(selector, $(`#${page}-results-slot`));
const apiUrl = path => window.ttsDesktop?.apiUrl ? window.ttsDesktop.apiUrl(path) : path;
function notice(message, error = false) {
  const box = $('#notice');
  box.textContent = message; box.hidden = !message; box.classList.toggle('error', error);
  if (error) box.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}
async function api(path, options = {}) {
  if (S.updating && !['GET', 'HEAD', 'OPTIONS'].includes((options.method || 'GET').toUpperCase())) throw new Error('正在安装应用更新，请等待应用重新启动。');
  const response = await fetch(apiUrl(path), {...options, headers: {'Content-Type': 'application/json', 'X-Local-Token': S.token, ...options.headers}});
  let data;
  try { data = await response.json(); } catch { throw new Error('服务响应异常，请检查本机程序是否仍在运行。'); }
  if (!response.ok) {
    let message = data.detail;
    if (Array.isArray(message)) message = message.map(item => item.msg).join('；');
    throw new Error(message || '请求失败');
  }
  return data;
}
function defaults(id) {
  const item = provider(id);
  const params = Object.fromEntries(item.fields.map(field => [field.key, field.default]));
  if (item.family === 'local' && item.installed_models.length) params.model = item.installed_models[0];
  return params;
}
function makeVariant(id, params) {
  return {uid: crypto.randomUUID(), name: `方案 ${S.variants.length + 1}`, provider: id, params: params ? {...params} : defaults(id)};
}
function option(value, label) {
  const node = el('option', label ?? String(value)); node.value = String(value); return node;
}
function isReady(variant) {
  const item = provider(variant.provider);
  return !!item?.ready && (item.family !== 'local' || item.installed_models.includes(variant.params.model));
}
function availability(variant) {
  const item = provider(variant.provider);
  if (item.family === 'demo') return '离线提示音 · 仅检查播放流程，不朗读文字';
  if (item.family === 'local') return isReady(variant) ? '本地模型已安装 · 文本不发送到云端' : '所选模型未安装，请前往「模型配置」下载';
  return item.ready ? '已检测到凭证 · 可用性需首次调用验证' : `待配置：${(item.missing_env || []).join('、')}`;
}
function setPage(page, focus = true) {
  if (!['test', 'compare', 'models'].includes(page)) return;
  S.page = page;
  document.querySelectorAll('audio').forEach(audio => audio.pause());
  for (const button of document.querySelectorAll('.nav-button')) {
    const active = button.dataset.page === page;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    $(`#page-${button.dataset.page}`).hidden = !active;
  }
  $('#history-panel').hidden = true;
  clearTimeout(modelTimer);
  if (page === 'models' && S.ready) void refreshModels();
  if (focus) $(`#${page}-title`)?.focus({preventScroll: true});
  window.scrollTo({top: 0});
}
function updateText(value) {
  for (const selector of ['#text', '#compare-text']) if ($(selector).value !== value) $(selector).value = value;
  const count = `${Array.from(value).length} / 2000`;
  $('#text-count').textContent = count; $('#compare-text-count').textContent = count;
}
function updateSaveState(message) {
  $('#save-state').textContent = message ?? (S.dirty ? '有未保存的修改' : '已保存到本机');
  $('#save-variants').disabled = !S.ready || S.saving || S.busy || S.updating || !S.dirty;
}
function markDirty() { S.dirty = true; $('#save-state').classList.remove('error'); updateSaveState(); updateConsent(); }
function updateConsent() {
  const selected = S.savedVariants.find(item => item.uid === S.selected);
  const cloud = item => !['demo', 'local'].includes(provider(item.provider)?.family);
  for (const page of ['test', 'compare']) {
    const variants = page === 'test' ? (selected ? [selected] : []) : S.variants;
    const row = $(`#${page}-consent-row`);
    const needed = variants.some(cloud);
    if (row.hidden !== !needed) $(`#${page}-cloud-consent`).checked = false;
    row.hidden = !needed;
  }
}
function freezeUpdateControls() {
  for (const control of document.querySelectorAll('#text, #compare-text, .samples button, #test-cloud-consent, #compare-cloud-consent, #local-device, #local-model-list button, #cloud-services button, #credential-fields input, #credential-fields select, #credential-fields button, .score-grid select, .result-notes, .score-actions button')) {
    if (S.updating) {
      if (!control.hasAttribute('data-update-disabled')) control.dataset.updateDisabled = control.disabled ? '1' : '0';
      control.disabled = true;
    } else if (control.hasAttribute('data-update-disabled')) {
      control.disabled = control.dataset.updateDisabled === '1';
      delete control.dataset.updateDisabled;
    }
  }
}
function lockControls() {
  const locked = S.busy || S.saving || C.saving || S.updating || !S.ready;
  $('#test-generate').disabled = locked || !S.savedVariants.length;
  $('#compare-generate').disabled = locked || !S.variants.length;
  $('#test-variant').disabled = locked;
  $('#add-variant').disabled = locked || S.variants.length >= 6;
  updateSaveState();
  for (const control of document.querySelectorAll('.variant input, .variant select, .variant textarea, .variant button')) control.disabled = locked;
  for (const button of document.querySelectorAll('.clone-variant')) button.disabled = locked || S.variants.length >= 6;
  for (const button of document.querySelectorAll('.remove-variant')) button.disabled = locked || S.variants.length === 1;
  freezeUpdateControls();
  credentialControls();
  renderUpdateControls();
}
function renderSelection() {
  const select = $('#test-variant'); select.replaceChildren();
  for (const variant of S.savedVariants) select.append(option(variant.uid, variant.name));
  select.value = S.selected;
  const variant = S.savedVariants.find(item => item.uid === S.selected);
  const root = $('#selected-variant'); root.replaceChildren();
  if (!variant) return;
  const item = provider(variant.provider);
  root.append(el('h3', item.name, 'selected-provider'), el('p', item.description, 'selected-description'), el('div', availability(variant), isReady(variant) ? 'ready' : 'missing'));
  const list = el('dl', undefined, 'parameter-list');
  for (const field of item.fields) {
    const value = variant.params[field.key];
    const display = field.type === 'reference' ? (S.references.find(ref => ref.id === value)?.name || '未选择或录音不可用')
      : typeof value === 'boolean' ? (value ? '开启' : '关闭') : value === '' ? '默认' : String(value);
    list.append(el('dt', field.label), el('dd', display));
  }
  root.append(list);
  $('#test-run-hint').textContent = item.family === 'demo' ? '离线演示 · 提示音，非人声' : item.family === 'local' ? '本地离线生成 · 最多 300 字' : '使用所选云服务生成';
  updateConsent(); lockControls();
}
function renderVariants() {
  const root = $('#variants'); root.replaceChildren();
  S.variants.forEach((variant, index) => {
    const item = provider(variant.provider), card = el('article', undefined, 'variant');
    card.dataset.variant = variant.uid;
    const top = el('div', undefined, 'variant-header');
    const name = el('input', undefined, 'variant-name'); name.type = 'text'; name.maxLength = 60; name.value = variant.name;
    name.setAttribute('aria-label', `方案 ${index + 1} 名称`);
    name.oninput = () => { variant.name = name.value; markDirty(); };
    const actions = el('div', undefined, 'variant-actions');
    const clone = el('button', '复制', 'icon-button clone-variant'); clone.type = 'button';
    clone.onclick = () => { const next = makeVariant(variant.provider, variant.params); next.name = `${variant.name.slice(0, 55)} 副本`; S.variants.push(next); markDirty(); renderVariants(); };
    const remove = el('button', '移除', 'icon-button remove-variant'); remove.type = 'button';
    remove.onclick = () => { S.variants = S.variants.filter(value => value.uid !== variant.uid); markDirty(); renderVariants(); };
    actions.append(clone, remove); top.append(name, actions); card.append(top);
    const select = el('select', undefined, 'provider'); select.setAttribute('aria-label', `方案 ${index + 1} 服务`);
    for (const value of S.providers) select.append(option(value.id, `${value.name}${value.ready ? '' : value.family === 'local' ? ' · 未下载' : ' · 未配置'}`));
    select.value = variant.provider;
    select.onchange = () => { variant.provider = select.value; variant.params = defaults(select.value); markDirty(); renderVariants(); };
    card.append(select, el('p', item.description, 'provider-desc'), el('div', availability(variant), isReady(variant) ? 'ready' : 'missing'));
    if (item.warning) card.append(el('div', item.warning, 'missing'));
    const fields = el('div', undefined, 'field-grid');
    for (const field of item.fields) {
      const label = el('label', undefined, 'field');
      if (['text', 'textarea'].includes(field.type)) label.classList.add('wide');
      label.append(el('span', field.label));
      let input;
      if (field.type === 'reference') {
        label.classList.add('wide');
        input = el('select'); input.append(option('', '请选择或导入参考录音'));
        for (const ref of S.references) input.append(option(ref.id, `${ref.name} · ${ref.duration_seconds} 秒`));
      } else if (field.type === 'select') {
        input = el('select');
        for (const value of field.options || []) input.append(typeof value === 'object' ? option(value.value, value.label) : option(value));
      } else if (field.type === 'textarea') { input = el('textarea'); input.rows = 2; }
      else { input = el('input'); input.type = ['number', 'checkbox'].includes(field.type) ? field.type : 'text'; }
      input.dataset.param = field.key;
      input.setAttribute('aria-label', field.label);
      if (field.type === 'checkbox') input.checked = !!variant.params[field.key]; else input.value = variant.params[field.key] ?? '';
      for (const key of ['min', 'max', 'step']) if (field[key] !== undefined) input[key] = field[key];
      input.oninput = () => { variant.params[field.key] = field.type === 'checkbox' ? input.checked : field.type === 'number' ? Number(input.value) : input.value; markDirty(); };
      if (['select', 'reference'].includes(field.type)) input.onchange = () => { input.oninput(); if (field.key === 'model' || field.type === 'reference') renderVariants(); };
      label.append(input);
      if (field.type === 'reference') {
        const upload = el('input'); upload.type = 'file'; upload.accept = '.wav,audio/wav';
        upload.setAttribute('aria-label', '导入参考录音');
        upload.onchange = () => { if (upload.files[0]) void uploadReference(upload.files[0], variant); };
        label.append(upload, el('small', '仅导入你有权使用的声音，3–10 秒 PCM WAV；录音保存在本机，不上传云端。'));
        if (variant.params[field.key]) {
          const preview = el('audio'); preview.controls = true; preview.preload = 'none';
          preview.src = apiUrl(`/api/references/${encodeURIComponent(variant.params[field.key])}/audio`);
          label.append(preview);
        }
      }
      if (field.help) label.append(el('small', field.help)); fields.append(label);
    }
    card.append(fields);
    if (item.docs) { const link = el('a', '官方接口说明 ↗', 'doc-link'); link.href = item.docs; link.target = '_blank'; link.rel = 'noreferrer'; card.append(link); }
    root.append(card);
  });
  $('#variant-count').textContent = `${S.variants.length} / 6`;
  updateConsent(); lockControls();
}
async function saveVariants() {
  if (S.saving || S.busy || S.updating) return;
  S.saving = true; lockControls(); updateSaveState('正在保存…');
  try {
    const selected = S.variants.some(item => item.uid === S.selected) ? S.selected : S.variants[0].uid;
    const data = await api('/api/variants', {method: 'PUT', body: JSON.stringify({variants: S.variants, selected_uid: selected})});
    S.variants = copy(data.variants); S.savedVariants = copy(data.variants); S.selected = data.selected_uid; S.dirty = false;
    $('#save-state').classList.remove('error'); renderSelection(); renderVariants(); notice('方案已保存，可在「文本测试」中选择使用。');
  } catch (error) { $('#save-state').classList.add('error'); notice(`方案未保存：${error.message}`, true); }
  finally { S.saving = false; lockControls(); }
}
async function selectVariant() {
  if (S.updating) return;
  const previous = S.selected; S.selected = $('#test-variant').value; S.saving = true; renderSelection();
  try { await api('/api/variants', {method: 'PUT', body: JSON.stringify({variants: S.savedVariants, selected_uid: S.selected})}); }
  catch (error) { S.selected = previous; renderSelection(); notice(`无法保存所选方案：${error.message}`, true); }
  finally { S.saving = false; lockControls(); }
}
function shuffle(items) {
  const values = [...items];
  for (let index = values.length - 1; index > 0; index--) { const target = crypto.getRandomValues(new Uint32Array(1))[0] % (index + 1); [values[index], values[target]] = [values[target], values[index]]; }
  return values;
}
function stateLabel(state) { return {queued: '排队中', running: '正在合成', completed: '已完成', failed: '失败', interrupted: '已中断'}[state] || state; }
function renderResults(page) {
  const session = S.sessions[page]; if (!session.runs.length) return;
  const root = resultUI(page, '.results'), blind = resultUI(page, '.blind').checked;
  for (const old of root.querySelectorAll('.result-card')) {
    const inputs = old.querySelectorAll('.score-grid select');
    if (inputs.length) session.drafts[old.dataset.run] = {...Object.fromEntries(Array.from(inputs).map(input => [input.name, input.value])), notes: $('.result-notes', old)?.value || ''};
  }
  for (const old of Array.from(root.children)) if (!session.order.includes(old.dataset.run)) old.remove();
  resultUI(page, '.source-text').textContent = session.runs[0].text; resultUI(page, '.source-panel').hidden = false;
  root.classList.toggle('single-result', session.runs.length === 1);
  session.order.forEach((id, index) => {
    const run = session.runs.find(item => item.id === id); if (!run) return;
    const existing = root.querySelector(`[data-run="${id}"]`);
    if (existing && existing.dataset.status === run.status && existing.dataset.blind === String(blind) && existing.dataset.index === String(index)) return;
    const card = el('article', undefined, 'result-card'); Object.assign(card.dataset, {run: run.id, status: run.status, blind: String(blind), index: String(index)});
    const head = el('h3', blind ? `声音 ${String.fromCharCode(65 + index)}` : run.provider_name);
    head.append(el('span', run.family === 'demo' ? '演示' : blind ? '身份隐藏' : run.family === 'local' ? '本地离线' : run.family === 'neural' ? '传统神经' : '生成式', 'badge')); card.append(head);
    const status = el('div', stateLabel(run.status), 'status'); status.dataset.state = run.status; card.append(status);
    if (['failed', 'interrupted'].includes(run.status)) card.append(el('p', run.error, 'hint error'));
    if (run.status === 'completed') {
      const audio = el('audio'); audio.controls = true; audio.preload = 'metadata'; audio.src = apiUrl(`/api/runs/${run.id}/audio`);
      audio.onplay = () => document.querySelectorAll('audio').forEach(other => { if (other !== audio) other.pause(); }); card.append(audio);
      if (run.family === 'demo') card.append(el('p', '这是提示音，不朗读文本，不能用于判断语音效果。', 'hint'));
      if (!blind) {
        let metric = `生成耗时 ${run.elapsed_seconds} s · ${(run.bytes / 1024).toFixed(1)} KB`;
        if (run.duration_seconds != null) metric += ` · 音频 ${run.duration_seconds} s · RTF ${run.real_time_factor}`;
        card.append(el('div', metric, 'metrics'));
        const details = el('details'); details.append(el('summary', '查看本次完整参数'), el('pre', JSON.stringify(run.params, null, 2))); card.append(details);
        const link = el('a', '下载音频 ↓', 'download'); link.href = `/api/runs/${run.id}/audio`; link.download = ''; card.append(link);
      }
      if (run.family !== 'demo') {
        const fields = [['naturalness', '自然度'], ['accuracy', '读音准确'], ['expression', '情感表达'], ['usability', '场景适合度']];
        const scores = el('div', undefined, 'score-grid'), inputs = {};
        for (const [key, labelText] of fields) {
          const label = el('label', labelText), input = el('select'); input.name = key; input.append(option('', '请选择'));
          for (let number = 1; number <= 5; number++) input.append(option(number, `${number} 分${number === 1 ? ' · 较差' : number === 5 ? ' · 很好' : ''}`));
          input.value = session.drafts[run.id]?.[key] ?? run.scores?.[key] ?? ''; label.append(input); inputs[key] = input; scores.append(label);
        }
        card.append(scores);
        const notes = el('textarea', undefined, 'result-notes'); notes.placeholder = '记下错读、停顿或喜欢的地方…'; notes.maxLength = 2000;
        notes.value = session.drafts[run.id]?.notes ?? run.scores?.notes ?? ''; notes.setAttribute('aria-label', '听感笔记'); card.append(notes);
        const actions = el('div', undefined, 'score-actions'), saved = el('span', run.scores ? '评分已保存' : ''), save = el('button', '保存评分', 'secondary');
        save.onclick = async () => {
          if (S.updating) return;
          if (Object.values(inputs).some(input => !input.value)) { saved.textContent = '请完成四项评分'; return; }
          save.disabled = true;
          try { const data = await api(`/api/runs/${run.id}/score`, {method: 'POST', body: JSON.stringify({...Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, Number(input.value)])), notes: notes.value})}); run.scores = data.scores; session.drafts[run.id] = data.scores; saved.textContent = '评分已保存'; }
          catch (error) { saved.textContent = error.message; } finally { save.disabled = false; if (save.hasAttribute('data-update-disabled')) save.dataset.updateDisabled = '0'; freezeUpdateControls(); }
        };
        actions.append(save, saved); card.append(actions);
      }
    }
    if (existing) existing.replaceWith(card); else root.append(card);
  });
  session.order.forEach((id, index) => { const node = root.querySelector(`[data-run="${id}"]`); if (node && root.children[index] !== node) root.insertBefore(node, root.children[index] || null); });
  resultUI(page, '.exports').hidden = false; resultUI(page, '.download-batch').href = `/api/batches/${session.batch}/download`;
  freezeUpdateControls();
}
function finishRun(page) {
  S.busy = false; $(`#${page}-generate`).textContent = page === 'test' ? '▷ 生成语音' : '生成并对比 →'; renderVariants();
}
async function poll(page, batchId) {
  const session = S.sessions[page]; if (batchId !== session.batch) return;
  try {
    const runs = await Promise.all(session.runs.map(run => api(`/api/runs/${run.id}`)));
    const changed = runs.some((run, index) => run.status !== session.runs[index].status); session.runs = runs;
    if (changed) renderResults(page);
    if (runs.some(run => ['queued', 'running'].includes(run.status))) { setTimeout(() => poll(page, batchId), 1000); return; }
    finishRun(page);
    const success = runs.filter(run => run.status === 'completed').length;
    notice(`${page === 'test' ? '文本测试' : '方案对比'}完成：${success}/${runs.length} 组成功。${runs.every(run => run.family === 'demo') ? '当前为提示音演示，真实人声需下载模型或配置云服务。' : '可以开始试听并记录评分。'}`, success === 0);
  } catch (error) { finishRun(page); notice(`${error.message} 已停止轮询，任务可能仍在后台完成，请从历史记录查看，避免重复调用。`, true); }
}
async function generate(page) {
  if (S.busy || S.saving || C.saving || S.updating || !S.ready) return;
  notice(''); const text = $(page === 'test' ? '#text' : '#compare-text').value.trim();
  if (!text) { notice('请先输入要朗读的文本。', true); return; }
  const variants = page === 'test' ? S.savedVariants.filter(item => item.uid === S.selected) : S.variants;
  if (!variants.length) { notice('请先配置并保存一个方案。', true); return; }
  if (variants.some(variant => !isReady(variant))) { notice('所选方案尚未就绪，请在「模型配置」中下载对应模型或点击服务卡片配置凭证。', true); return; }
  S.busy = true; lockControls(); $(`#${page}-generate`).textContent = '正在生成…';
  try {
    const data = await api('/api/batches', {method: 'POST', body: JSON.stringify({text, variants: variants.map(({provider, params}) => ({provider, params})), allow_cloud: $(`#${page}-cloud-consent`).checked})});
    const session = S.sessions[page]; Object.assign(session, {batch: data.batch_id, runs: data.runs, order: shuffle(data.runs.map(run => run.id)), drafts: {}});
    renderResults(page); notice('已提交，生成完成后可试听。');
    resultUI(page, '.results-section').scrollIntoView({behavior: 'smooth', block: 'nearest'}); setTimeout(() => poll(page, data.batch_id), 700);
  } catch (error) { finishRun(page); notice(error.message, true); }
}
async function history() {
  try {
    const data = await api('/api/runs'), groups = new Map();
    for (const run of data.runs) { if (!groups.has(run.batch_id)) groups.set(run.batch_id, []); groups.get(run.batch_id).push(run); }
    const root = $('#history'); root.replaceChildren();
    if (!groups.size) root.append(el('p', '还没有记录，先生成一段声音吧。', 'hint'));
    groups.forEach((runs, id) => {
      const row = el('div', undefined, 'history-row'), description = el('div');
      description.append(el('b', `${new Date(runs[0].created_at).toLocaleString()} · ${runs.length} 组`), el('p', runs[0].text.slice(0, 65) + (runs[0].text.length > 65 ? '…' : '')));
      const open = el('button', '打开', 'secondary'); open.disabled = S.busy;
      open.onclick = () => {
        if (S.busy) { notice('请等待当前任务结束后再打开历史。', true); return; }
        const page = runs.length === 1 ? 'test' : 'compare';
        S.sessions[page] = {runs, batch: id, order: shuffle(runs.map(run => run.id)), drafts: {}};
        setPage(page); renderResults(page); resultUI(page, '.results-section').scrollIntoView({behavior: 'smooth'});
        if (runs.some(run => ['queued', 'running'].includes(run.status))) { S.busy = true; lockControls(); setTimeout(() => poll(page, id), 700); }
      };
      row.append(description, open); root.append(row);
    });
    $('#history-panel').hidden = false; $('#history-panel').scrollIntoView({behavior: 'smooth'});
  } catch (error) { notice(error.message, true); }
}
function renderServices() {
  const root = $('#cloud-services'); root.replaceChildren();
  for (const item of S.providers.filter(item => !['local', 'demo'].includes(item.family))) {
    const card = el('button', undefined, 'service-item');
    card.type = 'button'; card.dataset.provider = item.id;
    card.setAttribute('aria-haspopup', 'dialog'); card.setAttribute('aria-controls', 'credential-dialog');
    card.setAttribute('aria-label', `配置 ${item.name}`);
    card.disabled = S.updating || C.saving;
    card.append(el('b', item.name), el('span', item.credentials_configured ? '● 已配置' : '○ 待配置', item.credentials_configured ? 'available' : ''), el('span', '点击配置 →', 'service-configure'));
    card.onclick = () => openCredentials(item.id); root.append(card);
  }
}
function credentialControls() {
  $('#credential-save').disabled = C.loading || C.saving || !C.provider || S.busy || S.modelBusy || S.updating;
  $('#credential-save').textContent = C.saving ? '正在保存…' : '保存配置';
  $('#credential-close').disabled = C.saving;
  $('#credential-cancel').disabled = C.saving;
  for (const control of document.querySelectorAll('#credential-fields input, #credential-fields select, #credential-fields button')) {
    control.disabled = C.saving || S.updating || !!control.closest('[data-aws-mode][hidden]');
  }
}
function credentialError(message = '') {
  $('#credential-error').textContent = message;
  $('#credential-error').hidden = !message;
}
function closeCredentials() {
  if (C.saving) return;
  const id = C.provider;
  clearCredentials();
  $('#credential-dialog').close();
  // A successful save replaces the card nodes; focus the current card.
  queueMicrotask(() => $(`#cloud-services [data-provider="${id}"]`)?.focus());
}
function clearCredentials() {
  for (const input of $('#credential-dialog').querySelectorAll('input')) { input.value = ''; input.type = 'password'; }
  $('#credential-fields').replaceChildren(); credentialError();
  C.revision++; C.provider = ''; C.loading = false;
}
function setupCredentials() {
  const dialog = $('#credential-dialog');
  $('#credential-close').onclick = closeCredentials;
  $('#credential-cancel').onclick = closeCredentials;
  dialog.addEventListener('cancel', event => { event.preventDefault(); closeCredentials(); });
  dialog.addEventListener('close', () => {
    // Clear even revealed, unsaved inputs on every exit (including Escape).
    if (!dialog.open && C.provider) clearCredentials();
  });
  $('#credential-form').onsubmit = saveCredentials;
}
async function openCredentials(id) {
  if (S.updating || C.saving) return;
  C.provider = id; C.loading = true;
  const revision = ++C.revision;
  $('#credential-title').textContent = `${provider(id).name} 配置`;
  $('#credential-fields').replaceChildren(el('p', '正在读取配置状态…', 'hint'));
  credentialError(); credentialControls(); $('#credential-dialog').showModal();
  try {
    const settings = await api(`/api/credentials/${id}`);
    if (revision !== C.revision) return;
    renderCredentialFields(settings);
  } catch {
    if (revision !== C.revision) return;
    C.provider = ''; $('#credential-fields').replaceChildren();
    credentialError('无法读取配置状态，请关闭后重试。');
  } finally {
    if (revision === C.revision) { C.loading = false; credentialControls(); }
  }
}
function renderCredentialFields(settings) {
  const root = $('#credential-fields'); root.replaceChildren();
  if (settings.provider === 'polly') {
    const label = el('label', '认证方式', 'credential-field');
    const mode = el('select'); mode.id = 'credential-mode';
    mode.append(option('keys', 'Access Key / Secret Key'), option('profile', '本机 AWS Profile'));
    mode.value = settings.mode; label.append(mode); root.append(label);
    mode.onchange = () => {
      for (const row of root.querySelectorAll('[data-aws-mode]')) {
        const hidden = row.dataset.awsMode !== mode.value || (row.dataset.awsTemporary && !$('#credential-temporary')?.checked); row.hidden = hidden;
        for (const control of row.querySelectorAll('input, button')) control.disabled = hidden;
      }
    };
  }
  for (const field of settings.fields) {
    const row = el('div', undefined, 'credential-field');
    if (settings.provider === 'polly') {
      if (field.key === 'AWS_PROFILE') row.dataset.awsMode = 'profile';
      else if (['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'].includes(field.key)) row.dataset.awsMode = 'keys';
      if (field.key === 'AWS_SESSION_TOKEN') row.dataset.awsTemporary = '1';
    }
    const id = `credential-${field.key}`;
    const label = el('label', field.label); label.htmlFor = id;
    if (field.secret) label.append(el('span', field.configured ? '已配置' : '未配置', 'credential-state'));
    const wrap = el('div', undefined, 'credential-input');
    const input = el('input'); input.id = id; input.dataset.credential = field.key;
    input.type = field.secret ? 'password' : 'text'; input.maxLength = 4096;
    input.autocomplete = 'off'; input.spellcheck = false; input.setAttribute('autocapitalize', 'off');
    input.value = field.secret ? '' : field.value || '';
    input.placeholder = field.secret ? (field.configured ? '已配置，留空保留；输入新值替换' : '请输入凭证') : field.key;
    input.setAttribute('aria-describedby', 'credential-help'); wrap.append(input);
    if (field.secret) {
      const eye = el('button', undefined, 'credential-eye'); eye.type = 'button';
      eye.setAttribute('aria-label', `显示新输入的 ${field.label}`); eye.setAttribute('aria-pressed', 'false');
      // Constant SVG only; no API data is interpreted as markup.
      eye.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/><path class="eye-slash" d="m3 3 18 18"/></svg>';
      eye.onclick = () => {
        const shown = input.type === 'password'; input.type = shown ? 'text' : 'password';
        eye.setAttribute('aria-pressed', String(shown));
        eye.setAttribute('aria-label', `${shown ? '隐藏' : '显示'}新输入的 ${field.label}`);
      };
      wrap.append(eye);
    }
    row.append(label, wrap);
    if (field.help) row.append(el('p', field.help, 'credential-variable'));
    root.append(row);
  }
  if (settings.provider === 'polly') {
    const label = el('label', undefined, 'check credential-temporary');
    label.dataset.awsMode = 'keys';
    const check = el('input'); check.type = 'checkbox'; check.id = 'credential-temporary'; check.checked = settings.temporary;
    check.onchange = () => $('#credential-mode').onchange();
    label.append(check, document.createTextNode('使用临时凭证（需要 Session Token）；取消后保存会清除旧 Token'));
    root.append(label); $('#credential-mode').onchange();
  }
  const first = root.querySelector('input:not(:disabled), select'); first?.focus();
}
async function saveCredentials(event) {
  event.preventDefault();
  if (C.loading || C.saving || !C.provider || S.busy || S.modelBusy || S.updating) return;
  const values = {};
  for (const input of document.querySelectorAll('#credential-fields input[data-credential]:not(:disabled)')) values[input.dataset.credential] = input.value;
  const body = {values};
  if (C.provider === 'polly') { body.mode = $('#credential-mode').value; body.temporary = $('#credential-temporary').checked; }
  C.saving = true; credentialError(); lockControls();
  for (const input of document.querySelectorAll('#credential-fields input[data-credential]')) {
    if (input.closest('.credential-input').querySelector('.credential-eye')) input.type = 'password';
  }
  for (const eye of document.querySelectorAll('.credential-eye')) {
    eye.setAttribute('aria-pressed', 'false');
    eye.setAttribute('aria-label', eye.getAttribute('aria-label').replace('隐藏', '显示'));
  }
  try {
    const result = await api(`/api/credentials/${C.provider}`, {method: 'PUT', body: JSON.stringify(body)});
    S.providers = result.providers;
    C.saving = false; closeCredentials(); renderServices();
    renderVariants(); renderSelection(); notice('配置已保存并生效。账号权限与模型可用性将在首次生成时验证。');
  } catch (error) {
    credentialError(error.message || '保存失败，请稍后重试。');
  } finally { C.saving = false; lockControls(); }
}
function renderUpdateControls() {
  const view = U.view;
  if (!view) return;
  const active = U.pending || ['checking', 'downloading', 'installing'].includes(view.status);
  const development = view.distribution === 'development';
  $('#check-app-update').disabled = active;
  $('#check-app-update').textContent = view.status === 'checking' ? '正在检查…' : view.status === 'error' && !view.version ? '重试检查' : '检查更新';
  const download = $('#download-app-update'), install = $('#install-app-update');
  download.hidden = development || !view.version || !['available', 'error'].includes(view.status);
  download.disabled = active;
  download.textContent = view.status === 'error' ? '重试下载' : '下载更新';
  install.hidden = development || !['ready', 'installing'].includes(view.status);
  install.disabled = active || S.busy || S.saving || C.saving || S.modelBusy;
  install.textContent = view.status === 'installing' ? '正在安装…' : '安装并重启';
  $('#app-update-install-hint').textContent = development
    ? '开发环境仅检查版本，请从发布页下载安装。'
    : view.status === 'ready' && (S.busy || S.saving || S.modelBusy)
      ? '请等待语音生成、方案保存或模型安装完成，再安装更新。个人数据会保留。'
      : '安装会关闭应用、更新程序并重新启动；个人配置、本地模型和历史音频会保留。';
}
function renderUpdateView(view) {
  if (!view || typeof view.status !== 'string') return;
  U.view = view;
  const installing = view.status === 'installing';
  if (S.updating !== installing) { S.updating = installing; lockControls(); }
  const version = view.version ? `v${view.version}` : '';
  const labels = {
    idle: '尚未检查更新。', checking: '正在检查新版本…', current: '当前已是最新版本。',
    available: `发现新版本 ${version}，可下载后安装。`, downloading: `正在下载 ${version}，请保持应用打开。`,
    ready: `${version} 已下载并校验完成，可以安装。`, installing: '正在退出并安装更新，请稍候…',
    error: view.error || '更新未完成，请检查网络后重试。',
  };
  const status = $('#app-update-status');
  status.textContent = labels[view.status] || '无法读取更新状态，请重新检查。';
  status.classList.toggle('error', view.status === 'error');
  $('#app-update-version').textContent = `当前 ${view.current_version ? `v${view.current_version}` : $('.version').textContent} · ${{installed: '安装版', portable: '便携版', development: '开发环境'}[view.distribution] || '桌面版'}`;
  const downloaded = Math.max(0, Number(view.downloaded_bytes) || 0), total = Math.max(0, Number(view.total_bytes) || 0);
  const bytes = value => value >= 1048576 ? `${(value / 1048576).toFixed(1)} MiB` : value >= 1024 ? `${(value / 1024).toFixed(1)} KiB` : `${value} B`;
  $('#app-update-progress-wrap').hidden = !['downloading', 'ready'].includes(view.status) && !downloaded;
  const progress = $('#app-update-progress');
  if (total > 0) { progress.max = total; progress.value = Math.min(downloaded, total); }
  else progress.removeAttribute('value');
  $('#app-update-bytes').textContent = total > 0 ? `${bytes(downloaded)} / ${bytes(total)}` : `${bytes(downloaded)} 已下载`;
  $('#app-update-notes-wrap').hidden = !view.notes;
  $('#app-update-notes').textContent = typeof view.notes === 'string' ? view.notes : '';
  const shortcut = $('#updates-shortcut');
  shortcut.textContent = view.status === 'ready' ? '更新已就绪 ↗' : view.version && ['available', 'downloading'].includes(view.status) ? `新版本 ${version} ↗` : '应用更新 ↗';
  renderUpdateControls();
}
function updateStorage(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* Storage can be disabled; manual checks remain available. */ }
  return null;
}
async function updateAction(action) {
  if (U.pending || ['checking', 'downloading', 'installing'].includes(U.view?.status)) return;
  if (action === 'installUpdate' && (U.view?.status !== 'ready' || S.busy || S.saving || C.saving || S.modelBusy)) return;
  U.pending = true;
  if (action === 'checkUpdate') updateStorage('ttsLab.updates.lastCheckAt', String(Date.now()));
  renderUpdateView({...U.view, status: {checkUpdate: 'checking', downloadUpdate: 'downloading', installUpdate: 'installing'}[action], error: ''});
  try {
    const view = await window.ttsDesktop[action]();
    if (view?.status) renderUpdateView(view);
    else renderUpdateView(await window.ttsDesktop.getUpdateState());
  } catch (error) {
    const message = typeof error === 'string' ? error : error?.message;
    renderUpdateView({...U.view, status: 'error', error: message || '更新未完成，请检查网络后重试。'});
  } finally { U.pending = false; renderUpdateControls(); }
}
async function setupUpdates() {
  const desktop = window.ttsDesktop;
  if (!desktop?.getUpdateState) return;
  $('#app-update-panel').hidden = false; $('#updates-shortcut').hidden = false;
  $('#updates-shortcut').onclick = () => { setPage('models'); $('#app-update-panel').scrollIntoView({block: 'start', behavior: 'smooth'}); $('#app-update-title').focus({preventScroll: true}); };
  const auto = $('#app-update-auto');
  auto.checked = updateStorage('ttsLab.updates.autoCheck') !== '0';
  auto.onchange = () => updateStorage('ttsLab.updates.autoCheck', auto.checked ? '1' : '0');
  $('#check-app-update').onclick = () => updateAction('checkUpdate');
  $('#download-app-update').onclick = () => updateAction('downloadUpdate');
  $('#install-app-update').onclick = () => updateAction('installUpdate');
  renderUpdateView({status: 'idle', downloaded_bytes: 0, total_bytes: 0});
  let revision = 0;
  desktop.onUpdateState(view => { revision++; renderUpdateView(view); });
  try {
    const initialRevision = revision;
    const view = await desktop.getUpdateState();
    if (revision === initialRevision && !U.pending) renderUpdateView(view);
    const now = Date.now(), last = Number(updateStorage('ttsLab.updates.lastCheckAt')) || 0;
    if (window.__TTS_UPDATES_AUTO__ === true && auto.checked && ['idle', 'current'].includes(U.view.status) &&
        (last > now || now - last >= 86400000)) void updateAction('checkUpdate');
  } catch (error) {
    renderUpdateView({...U.view, status: 'error', error: '无法读取更新状态，请重新检查更新。'});
  }
}
async function setupDesktop() {
  if (!window.ttsDesktop) return;
  $('#window-controls').hidden = false; $('#desktop-settings').hidden = false;
  const desktopAction = action => async () => { try { await action(); } catch { notice('桌面操作未完成，请稍后重试。', true); } };
  $('#window-minimize').onclick = desktopAction(() => window.ttsDesktop.minimize());
  $('#window-maximize').onclick = desktopAction(() => window.ttsDesktop.toggleMaximize());
  $('#window-close').onclick = desktopAction(() => window.ttsDesktop.close());
  $('#open-data-directory').onclick = desktopAction(() => window.ttsDesktop.openDataDirectory());
  const updateWindowState = state => {
    const button = $('#window-maximize'), label = state.maximized ? '还原' : '最大化';
    button.setAttribute('aria-label', label); button.title = label;
    button.classList.toggle('maximized', state.maximized);
  };
  window.ttsDesktop.onWindowState(updateWindowState);
  updateWindowState(await window.ttsDesktop.getWindowState());
}
async function init() {
  setupCredentials();
  window.addEventListener('tts-desktop-error', event => notice(event.detail, true));
  try { await setupDesktop(); } catch { notice('无法读取窗口状态，请稍后重试。', true); }
  void setupUpdates();
  for (const button of document.querySelectorAll('.nav-button')) button.onclick = () => setPage(button.dataset.page);
  $('#manage-variants').onclick = () => setPage('compare');
  for (const page of ['test', 'compare']) {
    $(`#${page}-results-slot`).append($('#results-template').content.cloneNode(true));
    resultUI(page, '.blind').checked = page === 'compare'; resultUI(page, '.blind-hint').hidden = page === 'test';
    resultUI(page, '.blind').onchange = () => { resultUI(page, '.blind-hint').hidden = !resultUI(page, '.blind').checked; renderResults(page); };
    $(`#${page}-generate`).onclick = () => generate(page);
  }
  for (const button of document.querySelectorAll('.history-button')) button.onclick = history;
  $('#close-history').onclick = () => { $('#history-panel').hidden = true; };
  for (const [name, text] of samples) { const button = el('button', name); button.onclick = () => updateText(text); $('#samples').append(button); }
  $('#text').oninput = () => updateText($('#text').value); $('#compare-text').oninput = () => updateText($('#compare-text').value);
  updateText(samples[0][1]);
  try {
    const [data, saved] = await Promise.all([api('/api/state'), api('/api/variants')]);
    S.token = data.token; S.providers = data.providers; S.references = data.references || []; S.desktop = !!data.desktop;
    if (saved.variants.length) { S.variants = copy(saved.variants); S.savedVariants = copy(saved.variants); S.selected = saved.selected_uid; }
    else {
      const demo = S.providers.find(item => item.family === 'demo'), ready = S.providers.filter(item => item.ready && item.family !== 'demo');
      for (const item of ready.length ? ready.slice(0, 2) : [demo, demo]) S.variants.push(makeVariant(item.id));
      if (!ready.length && S.variants[1]) S.variants[1].params.frequency = 660;
      const initial = await api('/api/variants', {method: 'PUT', body: JSON.stringify({variants: S.variants, selected_uid: S.variants[0].uid})});
      S.savedVariants = copy(initial.variants); S.selected = initial.selected_uid;
    }
    S.ready = true; renderVariants(); renderSelection(); renderServices();
    $('#add-variant').onclick = () => { S.variants.push(makeVariant(S.variants.at(-1)?.provider || 'demo')); markDirty(); renderVariants(); };
    $('#save-variants').onclick = saveVariants; $('#test-variant').onchange = selectVariant;
    $('#local-device').onchange = () => void refreshModels();
    if (S.page === 'models') void refreshModels();
  } catch (error) { notice(`无法加载工作台：${error.message}`, true); lockControls(); }
}
let modelTimer;
async function modelAction(id, action) {
  if (S.updating) return;
  $('#model-notice').textContent = '';
  try {
    if (action === 'delete') {
      if (!window.confirm('删除这个模型的本地文件？历史音频会保留，共用运行环境与下载缓存也会保留。')) return;
      await api(`/api/local-models/${id}`, {method:'DELETE'});
    } else if (action === 'install') {
      const device = id.startsWith('kokoro') ? 'cpu' : $('#local-device').value;
      await api(`/api/local-models/${id}/install`, {method:'POST',body:JSON.stringify({device})});
    } else {
      await api(`/api/local-models/${id}/cancel`, {method:'POST',body:'{}'});
    }
    await refreshModels();
  } catch (e) { $('#model-notice').textContent = e.message; }
}
async function refreshModels() {
  clearTimeout(modelTimer);
  try {
    const data = await api('/api/local-models');
    const root = $('#local-model-list');
    root.replaceChildren();
    const busy = data.models.some(m => m.busy);
    S.modelBusy = busy; renderUpdateControls(); credentialControls();
    for (const model of data.models) {
      const card = el('article', undefined, 'model-card');
      card.dataset.model = model.id;
      card.append(el('h3', model.name), el('p', `${model.size}。${model.ram}。`));
      const state = el('p', `${model.message}${model.ready ? `（${model.device === 'cuda' ? 'NVIDIA GPU' : 'CPU'}）` : ''}`, 'model-state');
      state.dataset.state = model.state;
      card.append(state);
      if (model.busy) {
        const progress = el('progress');
        progress.setAttribute('aria-label', model.name + ' 下载与安装进度');
        if (model.state === 'downloading' && model.total_bytes > 0) {
          progress.max = model.total_bytes;
          progress.value = model.downloaded_bytes;
          card.append(el('p', `${model.file || ''} · ${(model.downloaded_bytes / 1048576).toFixed(1)} / ${(model.total_bytes / 1048576).toFixed(1)} MB`));
        }
        card.append(progress);
      }
      const actions = el('div', undefined, 'model-actions');
      const install = el('button', model.ready ? '已安装' : ['failed','interrupted','incomplete'].includes(model.state) ? '重试安装' : '下载并安装', 'secondary');
      install.disabled = model.ready || busy || !model.devices.includes(model.engine === 'kokoro' ? 'cpu' : $('#local-device').value);
      install.onclick = () => modelAction(model.id, 'install');
      actions.append(install);
      if (model.busy) {
        const cancel = el('button', '取消安装', 'secondary');
        cancel.onclick = () => modelAction(model.id, 'cancel');
        actions.append(cancel);
      } else if (model.state !== 'not_installed') {
        const remove = el('button', '删除模型', 'secondary');
        remove.disabled = busy || S.busy;
        remove.onclick = () => modelAction(model.id, 'delete');
        actions.append(remove);
      }
      const docs = el('a', '模型说明 ↗', 'doc-link');
      docs.href = model.docs; docs.target = '_blank'; docs.rel = 'noreferrer';
      actions.append(docs, el('span', model.license, 'hint'));
      card.append(actions); root.append(card);
    }
    freezeUpdateControls();
    const state = await api('/api/state');
    const changed = JSON.stringify(S.providers) !== JSON.stringify(state.providers);
    S.providers = state.providers;
    if (changed) { renderSelection(); renderServices(); if (!S.busy) renderVariants(); }
    if (S.page === 'models') modelTimer = setTimeout(refreshModels, busy ? 1500 : 5000);
  } catch (e) {
    $('#model-notice').textContent = `无法更新模型状态：${e.message}`;
    if (S.page === 'models') modelTimer = setTimeout(refreshModels, 5000);
  }
}
async function uploadReference(file, variant) {
  if (S.updating) return;
  if (file.size > 2 * 1024 * 1024) { notice('参考录音超过 2 MB，请使用 3–10 秒 WAV。', true); return; }
  try {
    const ref = await api('/api/references?name=' + encodeURIComponent(file.name), {
      method: 'POST', headers: {'Content-Type': 'audio/wav'}, body: file
    });
    S.references.push(ref);
    if (provider(variant.provider).fields.some(field => field.key === 'reference_id')) {
      variant.params.reference_id = ref.id; markDirty();
    }
    renderVariants();
    notice('参考录音已保存在本机。填写所需逐字稿后保存方案，即可在「文本测试」使用。');
  } catch (error) { notice(error.message, true); }
}
init();
