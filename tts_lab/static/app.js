'use strict';
const $ = (s, parent=document) => parent.querySelector(s);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const S = {desktop:false,token:'', providers:[], variants:[], runs:[], batch:'', order:[], busy:false, drafts:{}};
const samples = [
  ['中文日常','你好，欢迎来到语音对比工作台。今天我们不看排行榜，而是用同一段文字，听一听不同声音的自然度、停顿和情感。你更喜欢哪一种？'],
  ['数字与多音字','银行行长说，这张银行卡不能转借。重庆的重量级选手重新出发。订单编号是 A-1028，金额为 3,205.60 元，折扣是百分之十五，发车时间是 2026 年 11 月 9 日上午 8 点 05 分。'],
  ['英语课文','Last Saturday, I went to the park with my parents. We played badminton and took many pictures. On Sunday, I stayed at home and did my homework. What did you do last weekend?'],
  ['中英混读','今天我们来学习一个英语短语：take your time，意思是“慢慢来，不着急”。打开 Settings，选择 Wi-Fi，然后点击 Connect。注意，API 和 USB 通常按字母读。'],
  ['叙事表达','“你终于来了。”她轻轻地说，声音里有一点惊喜，也有一点委屈。窗外的雨停了，街灯亮了起来。他停顿片刻，笑着回答：“这一次，我不会再错过了。”']
];
function notice(message, error=false){const box=$('#notice');box.textContent=message;box.hidden=!message;box.classList.toggle('error',error);}
async function api(path, options={}) {const res=await fetch(path,{...options,headers:{'Content-Type':'application/json','X-Local-Token':S.token,...options.headers}});let data;try{data=await res.json();}catch{throw new Error('服务响应异常，请检查本机程序是否仍在运行。');}if(!res.ok){let m=data.detail;if(Array.isArray(m))m=m.map(x=>x.msg).join('；');throw new Error(m||'请求失败');}return data;}
function provider(id){return S.providers.find(p=>p.id===id);}
function defaults(id){return Object.fromEntries(provider(id).fields.map(f=>[f.key,f.default]));}
function makeVariant(id, params){return {uid:crypto.randomUUID(),provider:id,params:params?{...params}:defaults(id)};}
function option(value, label){const e=el('option',label??String(value));e.value=String(value);return e;}
function renderVariants(){const root=$('#variants');root.replaceChildren();S.variants.forEach((v,i)=>{const p=provider(v.provider),card=el('article',undefined,'variant');const top=el('div',undefined,'variant-header');top.append(el('span',`方案 ${i+1}`));const actions=el('div');const clone=el('button','复制','icon-button');clone.type='button';clone.disabled=S.variants.length>=6||S.busy;clone.onclick=()=>{S.variants.push(makeVariant(v.provider,v.params));renderVariants();};const remove=el('button','移除','icon-button');remove.type='button';remove.disabled=S.variants.length===1||S.busy;remove.onclick=()=>{S.variants=S.variants.filter(x=>x.uid!==v.uid);renderVariants();};actions.append(clone,remove);top.append(actions);card.append(top);
  const sel=el('select',undefined,'provider');sel.setAttribute('aria-label',`方案 ${i+1} 服务`);S.providers.forEach(x=>sel.append(option(x.id,`${x.name}${x.ready?'':x.family==='local'?' · 未下载':' · 未配置'}`)));sel.value=v.provider;sel.disabled=S.busy;sel.onchange=()=>{v.provider=sel.value;v.params=defaults(v.provider);renderVariants();};card.append(sel,el('p',p.description,'provider-desc'));const status=p.family==='local'?(p.installed_models.includes(v.params.model)?'● 本地模型已安装 · 文本不发送到云端':'请先在“本地模型配置”中下载所选模型。'):p.ready?(p.family==='demo'?'● 离线提示音，仅验证软件流程':'● 已检测到配置，真实可用性需首次调用验证'):`需要配置：${(p.missing_env||[]).join('、')}`;card.append(el('div',status,p.ready?'ready':'missing'));
  if(p.warning)card.append(el('div',p.warning,'missing'));const grid=el('div',undefined,'field-grid');p.fields.forEach(f=>{const label=el('label',undefined,'field');if(['text','textarea'].includes(f.type))label.classList.add('wide');label.append(el('span',f.label));let input;if(f.type==='select'){input=el('select');(f.options||[]).forEach(o=>input.append(typeof o==='object'?option(o.value,o.label):option(o)));}else if(f.type==='textarea'){input=el('textarea');input.rows=2;}else{input=el('input');input.type=f.type==='checkbox'?'checkbox':f.type==='number'?'number':'text';}if(f.type==='checkbox')input.checked=!!v.params[f.key];else input.value=v.params[f.key]??'';if(f.min!==undefined)input.min=f.min;if(f.max!==undefined)input.max=f.max;if(f.step!==undefined)input.step=f.step;input.disabled=S.busy;input.onchange=()=>{v.params[f.key]=f.type==='checkbox'?input.checked:f.type==='number'?Number(input.value):input.value;};label.append(input);if(f.help)label.append(el('small',f.help));grid.append(label);});card.append(grid);if(p.docs){const link=el('a','查看官方接口说明 ↗','doc-link');link.href=p.docs;link.target='_blank';link.rel='noreferrer';card.append(link);}root.append(card);});$('#add-variant').disabled=S.busy||S.variants.length>=6;}
function shuffle(items){const a=[...items];for(let i=a.length-1;i>0;i--){const random=crypto.getRandomValues(new Uint32Array(1))[0];const j=random%(i+1);[a[i],a[j]]=[a[j],a[i]];}return a;}
function stateLabel(s){return {queued:'排队中',running:'正在合成',completed:'已完成',failed:'失败',interrupted:'已中断'}[s]||s;}
function renderResults(){if(!S.runs.length)return;const root=$('#results');const blind=$('#blind').checked;for(const old of root.querySelectorAll('.result-card')){const inputs=old.querySelectorAll('.score-grid select');if(inputs.length)S.drafts[old.dataset.run]={...Object.fromEntries(Array.from(inputs).map(e=>[e.name,e.value])),notes:$('.result-notes',old)?.value||''};}for(const old of Array.from(root.children)){if(!S.order.includes(old.dataset.run))old.remove();}$('#source-text').textContent=S.runs[0].text;$('#source-panel').hidden=false;S.order.forEach((id,index)=>{const run=S.runs.find(x=>x.id===id);if(!run)return;const existing=root.querySelector(`[data-run="${id}"]`);if(existing&&existing.dataset.status===run.status&&existing.dataset.blind===String(blind)&&existing.dataset.index===String(index))return;const card=el('article',undefined,'result-card');card.dataset.run=run.id;card.dataset.status=run.status;card.dataset.blind=String(blind);card.dataset.index=String(index);const head=el('h3',blind?`声音 ${String.fromCharCode(65+index)}`:run.provider_name);head.append(el('span',run.family==='demo'?'演示':blind?'身份隐藏':run.family==='local'?'本地离线':run.family==='neural'?'传统神经':'生成式','badge'));card.append(head);
  const status=el('div',stateLabel(run.status),'status');status.dataset.state=run.status;card.append(status);
  if(run.status==='failed'||run.status==='interrupted'){card.append(el('p',run.error,'hint error'));}
  if(run.status==='completed'){const audio=el('audio');audio.controls=true;audio.preload='metadata';audio.src=`/api/runs/${run.id}/audio`;audio.onplay=()=>document.querySelectorAll('audio').forEach(other=>{if(other!==audio)other.pause();});card.append(audio);
    if(run.family==='demo')card.append(el('p','这是本地合成的提示音，不朗读文本，不能用于判断语音效果。','hint'));
    if(!blind){let metric=`总生成耗时 ${run.elapsed_seconds} s · ${(run.bytes/1024).toFixed(1)} KB`;if(run.duration_seconds!==null&&run.duration_seconds!==undefined)metric+=` · 音频 ${run.duration_seconds} s · RTF ${run.real_time_factor}`;card.append(el('div',metric,'metrics'));const details=el('details');details.append(el('summary','查看本次完整参数'),el('pre',JSON.stringify(run.params,null,2)));card.append(details);const a=el('a','下载音频 ↧','download');a.href=`/api/runs/${run.id}/audio`;a.download='';card.append(a);}
    if(run.family!=='demo'){const fields=[['naturalness','自然度'],['accuracy','读音准确'],['expression','情感表达'],['usability','场景适合度']];const scores=el('div',undefined,'score-grid'),inputs={};fields.forEach(([key,name])=>{const label=el('label',name);const select=el('select');select.name=key;select.append(option('','请选择'));for(let n=1;n<=5;n++)select.append(option(n,`${n} 分${n===1?' · 较差':n===5?' · 很好':''}`));select.value=S.drafts[run.id]?.[key]??run.scores?.[key]??'';label.append(select);inputs[key]=select;scores.append(label);});card.append(scores);const notes=el('textarea',undefined,'result-notes');notes.placeholder='记下漏字、错读、停顿或你喜欢的地方…';notes.maxLength=2000;notes.value=S.drafts[run.id]?.notes??run.scores?.notes??'';notes.setAttribute('aria-label','听感笔记');card.append(notes);const actions=el('div',undefined,'score-actions'),saved=el('span',run.scores?'评分已保存':''),save=el('button','保存评分','secondary');save.onclick=async()=>{if(Object.values(inputs).some(x=>!x.value)){saved.textContent='请完成四项评分';return;}save.disabled=true;try{const data=await api(`/api/runs/${run.id}/score`,{method:'POST',body:JSON.stringify({...Object.fromEntries(Object.entries(inputs).map(([k,e])=>[k,Number(e.value)])),notes:notes.value})});run.scores=data.scores;S.drafts[run.id]=data.scores;saved.textContent='评分已保存';}catch(e){saved.textContent=e.message;}finally{save.disabled=false;}};actions.append(save,saved);card.append(actions);}
  }if(existing)existing.replaceWith(card);else root.append(card);});S.order.forEach((id,index)=>{const node=root.querySelector(`[data-run="${id}"]`);if(node&&root.children[index]!==node)root.insertBefore(node,root.children[index]||null);});$('#exports').hidden=false;$('#download-batch').href=`/api/batches/${S.batch}/download`;}
async function refreshRuns(){const changed=await Promise.all(S.runs.map(r=>api(`/api/runs/${r.id}`)));const differs=changed.some((r,i)=>r.status!==S.runs[i].status);S.runs=changed;if(differs)renderResults();return changed.some(r=>['running','queued'].includes(r.status));}
async function poll(batchId=S.batch){if(batchId!==S.batch)return;try{if(await refreshRuns()){setTimeout(()=>poll(batchId),1000);return;}S.busy=false;$('#generate').disabled=false;$('#generate').textContent='生成并对比 ↗';renderVariants();const success=S.runs.filter(x=>x.status==='completed').length;notice(`本轮完成：${success}/${S.runs.length} 组成功。${S.runs.every(x=>x.family==='demo')?'当前为提示音演示，真实语音需要配置云服务。':'试听后记录你的评分，再揭晓服务名称。'}`,success===0);}catch(e){S.busy=false;$('#generate').disabled=false;$('#generate').textContent='生成并对比 ↗';renderVariants();notice(`${e.message} 已停止轮询，任务可能仍在后台完成。可从历史记录查看，避免直接重复付费调用。`,true);}}
async function generate(){if(S.busy)return;notice('');const text=$('#text').value.trim();if(!text){notice('请先输入要朗读的文本。',true);return;}if(S.variants.some(v=>!provider(v.provider).ready)){notice(S.desktop?'所选方案中有服务尚未配置。请从“文件 → 编辑 API 凭证”填写配置，保存后重启，或选择离线演示。':'所选方案中有服务尚未配置。先填写本机 .env 并重启，或选择离线演示。',true);return;}S.busy=true;$('#generate').disabled=true;$('#generate').textContent='正在生成…';renderVariants();try{const data=await api('/api/batches',{method:'POST',body:JSON.stringify({text,variants:S.variants.map(({provider,params})=>({provider,params})),allow_cloud:$('#cloud-consent').checked})});S.batch=data.batch_id;S.runs=data.runs;S.order=shuffle(data.runs.map(r=>r.id));renderResults();notice('已提交。请勿重复点击；关闭网页不会取消已经发出的云端请求。');$('#results-section').scrollIntoView({behavior:'smooth',block:'start'});setTimeout(()=>poll(data.batch_id),700);}catch(e){S.busy=false;$('#generate').disabled=false;$('#generate').textContent='生成并对比 ↗';renderVariants();notice(e.message,true);}}
async function history(){try{const data=await api('/api/runs');const groups=new Map();data.runs.forEach(r=>{if(!groups.has(r.batch_id))groups.set(r.batch_id,[]);groups.get(r.batch_id).push(r);});const root=$('#history');root.replaceChildren();if(!groups.size)root.append(el('p','还没有记录。','hint'));groups.forEach((runs,id)=>{const row=el('div',undefined,'history-row'),desc=el('div');desc.append(el('b',`${new Date(runs[0].created_at).toLocaleString()} · ${runs.length} 组`),el('p',runs[0].text.slice(0,65)+(runs[0].text.length>65?'…':'')));const btn=el('button','打开','secondary');btn.disabled=S.busy;btn.onclick=()=>{if(S.busy){notice('请等待当前一轮生成完成后再打开历史。',true);return;}S.runs=runs;S.batch=id;S.order=shuffle(runs.map(r=>r.id));renderResults();$('#history-panel').hidden=true;$('#results-section').scrollIntoView({behavior:'smooth'});if(runs.some(r=>['queued','running'].includes(r.status))){S.busy=true;$('#generate').disabled=true;renderVariants();setTimeout(()=>poll(id),800);}};row.append(desc,btn);root.append(row);});$('#history-panel').hidden=false;$('#history-panel').scrollIntoView({behavior:'smooth'});}catch(e){notice(e.message,true);}}
function updateCount(){$('#text-count').textContent=`${Array.from($('#text').value).length} / 2000`;}
async function init(){try{const data=await api('/api/state');S.token=data.token;S.providers=data.providers;S.desktop=!!data.desktop;if(S.desktop)$('#config-guide').textContent='选择顶部菜单“文件 → 编辑 API 凭证”，在记事本中填入你要测的服务密钥，保存后选择“重启并载入凭证”。配置与历史保存在本机用户目录，升级或移动软件目录不会覆盖。凭证文件为明文，请勿分享。';const demo=S.providers.find(p=>p.family==='demo');const ready=S.providers.filter(p=>p.ready&&p.family!=='demo');const initial=ready.length?ready.slice(0,2):[demo,demo];S.variants=initial.map(p=>makeVariant(p.id));if(!ready.length&&S.variants[1]){const f=demo.fields.find(x=>x.key==='frequency');if(f)S.variants[1].params.frequency=660;}
  samples.forEach(([name,text])=>{const b=el('button',name);b.onclick=()=>{$('#text').value=text;updateCount();};$('#samples').append(b);});$('#text').value=samples[0][1];$('#text').oninput=updateCount;updateCount();renderVariants();$('#add-variant').onclick=()=>{const id=S.variants.at(-1)?.provider||demo.id;S.variants.push(makeVariant(id));renderVariants();};setupModelPanel();$('#generate').onclick=generate;$('#blind').onchange=renderResults;$('#refresh-history').onclick=history;$('#close-history').onclick=()=>$('#history-panel').hidden=true;
}catch(e){notice(`无法连接本机服务：${e.message}`,true);$('#generate').disabled=true;}}
let modelTimer;
function setupModelPanel() {
  $('#open-local-models').onclick = () => {
    $('#local-model-panel').hidden = false;
    $('#local-model-panel').scrollIntoView({behavior:'smooth', block:'start'});
    void refreshModels();
  };
  $('#close-local-models').onclick = () => {
    $('#local-model-panel').hidden = true;
    clearTimeout(modelTimer);
    $('#open-local-models').focus();
  };
}
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
    if (changed && !S.busy) renderVariants();
    if (!$('#local-model-panel').hidden) modelTimer = setTimeout(refreshModels, busy ? 1500 : 5000);
  } catch (e) {
    $('#model-notice').textContent = `无法更新模型状态：${e.message}`;
    if (!$('#local-model-panel').hidden) modelTimer = setTimeout(refreshModels, 5000);
  }
}
init();
