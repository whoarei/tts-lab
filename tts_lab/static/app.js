'use strict';
const $ = (selector, parent = document) => parent.querySelector(selector);
const el = (tag, text, cls) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
};
const newSession = () => ({runs: [], batch: '', order: [], drafts: {}});
const S = {desktop: false, token: '', providers: [], variants: [], savedVariants: [], selected: '',
  page: 'test', busy: false, saving: false, dirty: false, ready: false,
  sessions: {test: newSession(), compare: newSession()}};
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
function notice(message, error = false) {
  const box = $('#notice');
  box.textContent = message; box.hidden = !message; box.classList.toggle('error', error);
  if (error) box.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'Content-Type': 'application/json', 'X-Local-Token': S.token, ...options.headers}});
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
  $('#save-variants').disabled = !S.ready || S.saving || S.busy || !S.dirty;
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
function lockControls() {
  const locked = S.busy || S.saving || !S.ready;
  $('#test-generate').disabled = locked || !S.savedVariants.length;
  $('#compare-generate').disabled = locked || !S.variants.length;
  $('#test-variant').disabled = locked;
  $('#add-variant').disabled = locked || S.variants.length >= 6;
  updateSaveState();
  for (const control of document.querySelectorAll('.variant input, .variant select, .variant textarea, .variant button')) control.disabled = locked;
  for (const button of document.querySelectorAll('.clone-variant')) button.disabled = locked || S.variants.length >= 6;
  for (const button of document.querySelectorAll('.remove-variant')) button.disabled = locked || S.variants.length === 1;
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
    list.append(el('dt', field.label), el('dd', typeof value === 'boolean' ? (value ? '开启' : '关闭') : value === '' ? '默认' : String(value)));
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
      if (field.type === 'select') {
        input = el('select');
        for (const value of field.options || []) input.append(typeof value === 'object' ? option(value.value, value.label) : option(value));
      } else if (field.type === 'textarea') { input = el('textarea'); input.rows = 2; }
      else { input = el('input'); input.type = ['number', 'checkbox'].includes(field.type) ? field.type : 'text'; }
      input.dataset.param = field.key;
      if (field.type === 'checkbox') input.checked = !!variant.params[field.key]; else input.value = variant.params[field.key] ?? '';
      for (const key of ['min', 'max', 'step']) if (field[key] !== undefined) input[key] = field[key];
      input.oninput = () => { variant.params[field.key] = field.type === 'checkbox' ? input.checked : field.type === 'number' ? Number(input.value) : input.value; markDirty(); };
      if (field.type === 'select') input.onchange = () => { input.oninput(); if (field.key === 'model') renderVariants(); };
      label.append(input); if (field.help) label.append(el('small', field.help)); fields.append(label);
    }
    card.append(fields);
    if (item.docs) { const link = el('a', '官方接口说明 ↗', 'doc-link'); link.href = item.docs; link.target = '_blank'; link.rel = 'noreferrer'; card.append(link); }
    root.append(card);
  });
  $('#variant-count').textContent = `${S.variants.length} / 6`;
  updateConsent(); lockControls();
}
async function saveVariants() {
  if (S.saving || S.busy) return;
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
      const audio = el('audio'); audio.controls = true; audio.preload = 'metadata'; audio.src = `/api/runs/${run.id}/audio`;
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
          if (Object.values(inputs).some(input => !input.value)) { saved.textContent = '请完成四项评分'; return; }
          save.disabled = true;
          try { const data = await api(`/api/runs/${run.id}/score`, {method: 'POST', body: JSON.stringify({...Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, Number(input.value)])), notes: notes.value})}); run.scores = data.scores; session.drafts[run.id] = data.scores; saved.textContent = '评分已保存'; }
          catch (error) { saved.textContent = error.message; } finally { save.disabled = false; }
        };
        actions.append(save, saved); card.append(actions);
      }
    }
    if (existing) existing.replaceWith(card); else root.append(card);
  });
  session.order.forEach((id, index) => { const node = root.querySelector(`[data-run="${id}"]`); if (node && root.children[index] !== node) root.insertBefore(node, root.children[index] || null); });
  resultUI(page, '.exports').hidden = false; resultUI(page, '.download-batch').href = `/api/batches/${session.batch}/download`;
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
  if (S.busy || S.saving || !S.ready) return;
  notice(''); const text = $(page === 'test' ? '#text' : '#compare-text').value.trim();
  if (!text) { notice('请先输入要朗读的文本。', true); return; }
  const variants = page === 'test' ? S.savedVariants.filter(item => item.uid === S.selected) : S.variants;
  if (!variants.length) { notice('请先配置并保存一个方案。', true); return; }
  if (variants.some(variant => !isReady(variant))) { notice('所选方案尚未就绪，请在「模型配置」中下载对应模型或编辑 API 凭证并重启。', true); return; }
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
    const card = el('div', undefined, 'service-item'); card.append(el('b', item.name), el('span', item.ready ? '● 已检测到配置' : '○ 尚未配置', item.ready ? 'available' : '')); root.append(card);
  }
}
async function setupDesktop() {
  if (!window.ttsDesktop) return;
  $('#window-controls').hidden = false; $('#desktop-settings').hidden = false;
  $('#config-guide').textContent = '点击「编辑 API 凭证」，在本机文件中填写服务密钥。保存后点击「重启并载入」，即可在方案中选择对应服务。';
  const desktopAction = action => async () => { try { await action(); } catch { notice('桌面操作未完成，请稍后重试。', true); } };
  $('#window-minimize').onclick = desktopAction(() => window.ttsDesktop.minimize());
  $('#window-maximize').onclick = desktopAction(() => window.ttsDesktop.toggleMaximize());
  $('#window-close').onclick = desktopAction(() => window.ttsDesktop.close());
  $('#edit-credentials').onclick = desktopAction(() => window.ttsDesktop.editCredentials());
  $('#restart-app').onclick = desktopAction(() => window.ttsDesktop.restart());
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
  try { await setupDesktop(); } catch { notice('无法读取窗口状态，请稍后重试。', true); }
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
    S.token = data.token; S.providers = data.providers; S.desktop = !!data.desktop;
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
    if (S.page === 'models') void refreshModels();
  } catch (error) { notice(`无法加载工作台：${error.message}`, true); lockControls(); }
}
let modelTimer;
async function modelAction(id, action) {
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
      install.disabled = model.ready || busy;
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
init();
