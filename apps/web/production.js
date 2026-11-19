const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const ACTIVE = new Set(['queued', 'running']);
const JOB_LABELS = {queued:'排队中', running:'运行中', completed:'已完成', failed:'未完成', cancelled:'已取消'};

const heading = (id, eyebrow, title) => `<div class="dialog-heading"><div><p class="eyebrow">${eyebrow}</p><h2 id="${id}-title">${title}</h2></div><button type="button" class="icon-button" data-close="${id}" aria-label="关闭${title}">×</button></div>`;
const errorArea = '<p class="dialog-error" role="alert" hidden></p>';
const markup = `
<dialog id="resources-dialog" aria-labelledby="resources-title">${heading('resources', 'PRODUCTION LIBRARY', '生产资料')}<p class="dialog-intro">管理本机引用的声音、发音读法与工程档案。</p><div class="resource-list"><button type="button" id="resources-voices"><strong>声线档案 <span>↗</span></strong><small>登记本地 WAV 与对应文本，保留验收范围</small></button><button type="button" id="resources-dictionary"><strong>发音词典 <span>↗</span></strong><small>管理专有名词的读法，按项目预览后应用</small></button><button type="button" id="resources-restore"><strong>恢复工程 ZIP <span>↥</span></strong><small>最多 64 MB；恢复为新项目，保留原项目</small></button></div>${errorArea}<input type="file" id="restore-file" accept=".zip,application/zip" hidden></dialog>
<dialog id="voices-dialog" class="wide-dialog" aria-labelledby="voices-title">${heading('voices', 'LOCAL VOICES', '声线档案')}<p class="dialog-intro">引用这台电脑已有的 WAV 文件。新声线与新文案都需要试听确认。</p><form id="voice-form"><label class="field-label" for="voice-record">选择档案</label><select id="voice-record" class="form-input"></select><p id="voice-readonly" class="inspector-note"></p><div class="form-grid"><div><label class="field-label" for="voice-record-name">声线名称</label><input id="voice-record-name" class="form-input" maxlength="100" required></div><div><label class="field-label" for="voice-record-audio">参考录音绝对路径（WAV）</label><input id="voice-record-audio" class="form-input path-input" required placeholder="D:\\录音\\reference.wav"></div></div><label class="field-label" for="voice-record-text">参考录音对应文本</label><textarea id="voice-record-text" class="form-input" rows="3" maxlength="5000" required></textarea><label class="field-label" for="voice-record-sample">试听样音绝对路径（WAV，可选）</label><input id="voice-record-sample" class="form-input path-input" placeholder="D:\\录音\\sample.wav"><p class="inspector-note" id="voice-record-scope"></p>${errorArea}<div class="dialog-footer"><button type="button" class="button ghost" data-close="voices">关闭</button><button type="submit" class="button primary" id="save-voice">保存档案</button></div></form></dialog>
<dialog id="dictionary-dialog" class="wide-dialog" aria-labelledby="dictionary-title">${heading('dictionary', 'PRONUNCIATION', '发音词典')}<p class="dialog-intro">词典保存后，已有项目继续使用原快照。到项目中预览读法，再决定是否应用。</p><div class="dictionary-column-labels" aria-hidden="true"><span>稿件中的词</span><span>实际读法</span><span>启用</span><span></span></div><div id="dictionary-rows"></div><button class="text-button add-entry" type="button" id="add-dictionary-entry">＋ 新增词条</button><p class="inspector-note" id="dictionary-version"></p>${errorArea}<div class="dialog-footer"><button class="button ghost" type="button" data-close="dictionary">关闭</button><button class="button primary" type="button" id="save-dictionary">保存词典</button></div></dialog>
<dialog id="text-tools-dialog" class="wide-dialog" aria-labelledby="text-tools-title">${heading('text-tools', 'TEXT PREPARATION', '文案整理与读法')}<div class="tool-section"><h3>文案整理</h3><p class="dialog-intro">用本地规则去掉 Markdown 标记、整理空行。先查看建议，采用后才修改文案。</p><div class="tool-actions"><button type="button" class="button secondary" id="prepare-cleanup">生成整理建议</button><button type="button" class="text-button" id="undo-preparation" hidden>撤销上次整理</button></div><p class="inspector-note" id="undo-preparation-note" hidden>有后续编辑时无法撤销，请先检查原稿历史。</p></div><div class="tool-section"><h3>AI 文案改写</h3><p class="dialog-intro" id="rewrite-availability">正在检查文案模型配置。</p><label class="field-label" for="rewrite-instruction">改写要求</label><textarea class="form-input" id="rewrite-instruction" rows="2" maxlength="1000" placeholder="例如：整理为自然的中文口播，保留原意与数字。"></textarea><button type="button" class="button secondary" id="prepare-rewrite">AI改写建议</button></div><div class="tool-section"><h3>发音读法</h3><p class="dialog-intro">先比较原文与当前词典的读法。应用时仅让读法改变的片段重新生成。</p><div class="tool-actions"><button type="button" class="button secondary" id="preview-dictionary">预览读法</button><button type="button" class="text-button" id="manage-dictionary">管理词典</button></div></div>${errorArea}</dialog>
<dialog id="proposal-dialog" class="comparison-dialog" aria-labelledby="proposal-title">${heading('proposal', 'REVIEW BEFORE APPLY', '检查文案建议')}<p class="dialog-intro" id="proposal-description"></p><ul class="proposal-warnings" id="proposal-warnings"></ul><ul class="proposal-review-notes inspector-note" id="proposal-review-notes"></ul><p class="inspector-note" id="proposal-protected"></p><div class="comparison-columns"><section><h3>原稿</h3><pre id="proposal-original"></pre></section><section><h3>建议文案</h3><pre id="proposal-proposed"></pre></section></div><label class="check-label proposal-confirmation"><input type="checkbox" id="proposal-reviewed">我已逐句对照原稿，确认语义、限定条件、数字与事实。</label>${errorArea}<div class="dialog-footer"><button type="button" class="button ghost" data-close="proposal">丢弃建议</button><button type="button" class="button primary" id="apply-proposal" disabled>采用这份文案</button></div></dialog>
<dialog id="pronunciation-dialog" class="wide-dialog" aria-labelledby="pronunciation-title">${heading('pronunciation', 'PROJECT PRONUNCIATION', '预览发音读法')}<p class="dialog-intro">原稿保持可读文本，配音使用下方读法。应用当前词典后，项目将保存这一版快照。</p><p id="pronunciation-version" class="inspector-note"></p><div id="pronunciation-segments" class="pronunciation-segments"></div>${errorArea}<div class="dialog-footer"><button type="button" class="button ghost" data-close="pronunciation">关闭</button><button type="button" class="button primary" id="apply-dictionary">应用当前词典</button></div></dialog>
<dialog id="batch-dialog" class="wide-dialog" aria-labelledby="batch-title">${heading('batch', 'BATCH NARRATION', '批量导入文案')}<p class="dialog-intro">选择最多 20 份 UTF-8 TXT / Markdown 文件，每份建立独立项目。配音按队列依次生成。</p><label class="button secondary batch-file-button" for="batch-files">选择文案文件 ↥</label><input type="file" id="batch-files" accept=".txt,.md,text/plain,text/markdown" multiple hidden><div id="batch-preview" class="batch-preview"><p class="muted">尚未选择文件</p></div><label class="check-label"><input id="batch-generate" type="checkbox" checked> 导入后加入配音队列</label><p class="inspector-note" id="batch-engine-note"></p>${errorArea}<div class="dialog-footer"><button type="button" class="button ghost" data-close="batch">取消</button><button type="button" class="button primary" id="confirm-batch" disabled>确认导入</button></div></dialog>
<dialog id="jobs-dialog" class="jobs-dialog" aria-labelledby="jobs-title">${heading('jobs', 'PRODUCTION QUEUE', '制作任务')}<p class="dialog-intro" id="queue-summary">任务按顺序在本机运行。</p><div id="jobs-list" class="jobs-list"></div>${errorArea}</dialog>`;

export function createProduction(context) {
  const {state, api, acceptProject, anyDirty, projectBusy, updateControls, renderSidebar, showError, toast, schedulePoll, loadProject, safeAudioUrl, duration} = context;
  const local = {voices:[], dictionary:null, batch:[], proposal:null, pronunciation:null, renderingVoice:null};
  $('#production-dialogs').innerHTML = markup;

  const clearDialogError = dialog => { const area = $('.dialog-error', dialog); if (area) area.hidden = true; };
  function dialogError(dialog, error) {
    const area = $('.dialog-error', dialog);
    if (area) { area.textContent = error.message || String(error); area.hidden = false; }
    else showError(error);
  }
  function open(name) {
    const dialog = $(`#${name}-dialog`);
    clearDialogError(dialog);
    if (!dialog.open) dialog.showModal();
    return dialog;
  }
  function close(name) { if (!state.operation) $(`#${name}-dialog`).close(); }
  async function run(name, dialog, action) {
    if (state.operation) return;
    state.operation = name;
    if (dialog) { clearDialogError(dialog); dialog.setAttribute('aria-busy','true'); }
    updateControls();
    try { return await action(); }
    catch (error) { dialog ? dialogError(dialog, error) : showError(error); }
    finally { state.operation = null; dialog?.removeAttribute('aria-busy'); updateControls(); }
  }
  function projectReady(dialog) {
    if (!state.project || projectBusy()) { dialogError(dialog,new Error('当前项目正在排队或运行，完成或取消任务后可继续。')); return false; }
    if (anyDirty()) { dialogError(dialog,new Error('请先保存当前项目的文案、片段与生成设置。')); return false; }
    return true;
  }
  const projectPath = suffix => `/api/projects/${encodeURIComponent(state.project.id)}${suffix}`;

  async function loadVoices() {
    const result = await api('/api/voices');
    local.voices = result.voices || [];
    renderVoice();
  }
  function renderVoice() {
    const project = state.project;
    const voice = project?.voice_snapshot || local.voices.find(item => item.id === (project?.voice_id || 'default')) || state.status?.voice;
    const selected = project?.voice_id || voice?.id || 'default';
    const select = $('#project-voice');
    const options = local.voices.length ? local.voices : [{id:'default',name:state.status?.voice?.name || '我的声线'}];
    select.replaceChildren(...options.map(item => new Option(item.name, item.id)));
    if (!options.some(item => item.id === selected)) select.append(new Option(voice?.name || '项目保存的声线',selected));
    select.value = selected;
    $('#voice-name').textContent = voice?.name || '我的声线';
    const record = local.voices.find(item => item.id === selected);
    const isDefault = selected === 'default';
    let sample = null;
    if (isDefault) {
      sample = safeAudioUrl(state.status?.voice?.sample_url);
    } else if (record?.sample_audio || record?.reference_audio) {
      sample = safeAudioUrl(`/api/voices/${encodeURIComponent(selected)}/sample`);
    }
    const scope = isDefault ? state.status?.voice?.acceptance_scope : voice?.acceptance_scope;
    $('#voice-scope').textContent = scope || '';
    $('#voice-acceptance').textContent = sample ? '可试听样音' : '本机暂无样音';
    const snapshot = project?.voice_snapshot;
    const newer = !!snapshot && !!record && ['revision', 'reference_audio', 'reference_text', 'reference_sha256']
      .some(key => (record[key] ?? '') !== (snapshot[key] ?? ''));
    $('#refresh-project-voice').hidden=!newer;
    const label = $('label[for="reference-player"]');
    label.textContent = newer ? '档案最新样音 · 项目仍用原快照' : '参考样音';
    label.hidden = !sample;
    const player = $('#reference-player');
    if (sample && player.getAttribute('src') !== sample) player.src = sample;
    if (!sample && player.hasAttribute('src')) { player.pause(); player.removeAttribute('src'); player.load(); }
    player.hidden = !sample;
  }
  function renderVoiceForm() {
    const id = $('#voice-record').value;
    const voice = local.voices.find(item => item.id === id);
    local.renderingVoice = voice || null;
    const readonly = voice?.id === 'default' || !!voice?.readonly || !!voice?.read_only;
    $('#voice-readonly').textContent = readonly ? '默认声线由本机配置管理。新增档案可引用其他本地录音。' : voice ? '更新档案后，可在项目中选择使用新版本。' : '新建本地声线档案，文件保留原位置。';
    for (const [field,key] of [['name','name'],['audio','reference_audio'],['text','reference_text'],['sample','sample_audio']]) {
      const input = $(`#voice-record-${field}`);
      input.value = voice?.[key] || '';
      input.readOnly = readonly;
    }
    $('#voice-record-scope').textContent = voice?.acceptance_scope || '登记路径仅验证资源可用，不能代替声音试听验收。';
    $('#save-voice').disabled = readonly || !!state.operation;
    clearDialogError($('#voices-dialog'));
  }
  async function openVoices() {
    const dialog = open('voices');
    await run('load-voices', dialog, async () => {
      await loadVoices();
      $('#voice-record').replaceChildren(...local.voices.map(voice=>new Option(voice.name,voice.id)), new Option('＋ 新增声线档案','new'));
      $('#voice-record').value = state.project?.voice_id || 'default';
      if (!$('#voice-record').value) $('#voice-record').value = 'new';
      renderVoiceForm();
    });
  }
  async function saveVoice(event) {
    event.preventDefault();
    const dialog = $('#voices-dialog');
    const voice = local.renderingVoice;
    if (voice?.id === 'default' || voice?.readonly || voice?.read_only) return;
    await run('save-voice',dialog,async () => {
      const body = {name:$('#voice-record-name').value.trim(), reference_audio:$('#voice-record-audio').value.trim(), reference_text:$('#voice-record-text').value.trim(), sample_audio:$('#voice-record-sample').value.trim() || null};
      if (voice) Object.assign(body,{id:voice.id,expected_revision:voice.revision});
      const saved = await api('/api/voices',{method:'POST',body});
      await loadVoices();
      $('#voice-record').replaceChildren(...local.voices.map(item=>new Option(item.name,item.id)),new Option('＋ 新增声线档案','new'));
      $('#voice-record').value = saved.id || saved.voice?.id;
      renderVoiceForm(); toast('声线档案已保存，请用新文案试听确认。');
    });
  }
  async function changeVoice(event, refresh = false) {
    const selected = event.target.value;
    if (!state.project || (!refresh && selected === state.project.voice_id)) return;
    if (anyDirty()) { renderVoice(); showError(new Error('请先保存当前项目的修改，再切换声线。')); return; }
    if (state.project.segments.some(segment => segment.audio_url) &&
        !window.confirm('切换声线将让当前片段音频失效，需要重新生成。是否继续？')) { renderVoice(); return; }
    await run('change-voice',null,async()=>{
      const project = await api(projectPath(''),{method:'PUT',body:{voice_id:selected,expected_revision:state.project.revision}});
      acceptProject(project); toast('项目声线已切换，请重新生成配音。');
    });
    renderVoice();
  }

  function dictionaryRow(entry = {}) {
    const row = document.createElement('div'); row.className = 'dictionary-row';
    row.dataset.entryId = entry.id || '';
    row.innerHTML = '<input class="form-input term-input" maxlength="60" aria-label="稿件中的词" placeholder="例如：WMS"><input class="form-input spoken-input" maxlength="100" aria-label="实际读法" placeholder="例如：仓储管理系统"><label class="dictionary-enabled"><input type="checkbox" aria-label="启用词条"><span>启用</span></label><button class="icon-button remove-entry" type="button" aria-label="删除词条">×</button>';
    $('.term-input',row).value = entry.term || ''; $('.spoken-input',row).value = entry.spoken || '';
    $('input[type=checkbox]',row).checked = entry.enabled !== false;
    $('.remove-entry',row).addEventListener('click',()=>row.remove());
    return row;
  }
  async function openDictionary() {
    const dialog = open('dictionary');
    await run('load-dictionary',dialog,async()=>{
      local.dictionary = await api('/api/dictionary');
      $('#dictionary-rows').replaceChildren(...(local.dictionary.entries || []).map(dictionaryRow));
      if (!local.dictionary.entries?.length) $('#dictionary-rows').append(dictionaryRow());
      $('#dictionary-version').textContent = `当前词典版本 ${local.dictionary.revision} · 按词条替换读法，不改写原稿。`;
    });
  }
  async function saveDictionary() {
    const dialog = $('#dictionary-dialog');
    if (!local.dictionary) return;
    const entries = $$('.dictionary-row').map(row=>({id:row.dataset.entryId || undefined,term:$('.term-input',row).value.trim(),spoken:$('.spoken-input',row).value.trim(),enabled:$('input[type=checkbox]',row).checked}));
    if (entries.some(entry=>!entry.term || !entry.spoken)) { dialogError(dialog,new Error('每个词条都需要填写原词与实际读法，空行可以删除。')); return; }
    await run('save-dictionary',dialog,async()=>{
      local.dictionary = await api('/api/dictionary',{method:'PUT',body:{expected_revision:local.dictionary.revision,entries}});
      $('#dictionary-rows').replaceChildren(...(local.dictionary.entries || []).map(dictionaryRow));
      $('#dictionary-version').textContent = `已保存词典版本 ${local.dictionary.revision} · 项目需预览后主动应用。`;
      toast('发音词典已保存，已有配音保持原读法。');
    });
  }

  function openTextTools() { open('text-tools'); renderFeatures(); }
  async function prepare(mode) {
    const dialog = $('#text-tools-dialog');
    if (!projectReady(dialog)) return;
    await run('prepare-text',dialog,async()=>{
      const projectId = state.project.id;
      local.proposal = await api(projectPath('/prepare'),{method:'POST',body:{mode,expected_revision:state.project.revision,instruction:$('#rewrite-instruction').value.trim()}});
      local.proposal.project_id = projectId;
      $('#proposal-original').textContent = local.proposal.original_text;
      $('#proposal-proposed').textContent = local.proposal.proposed_text;
      $('#proposal-description').textContent = mode === 'cleanup' ? '本地规则整理建议。请核对原意、数字与停顿，再决定是否采用。' : '模型可能改变语义或遗漏限定条件。请逐句对照原稿，核对事实、数字与专有名词后采用。';
      $('#proposal-warnings').replaceChildren(...(local.proposal.warnings || []).map(warning=>{ const item=document.createElement('li'); item.textContent=typeof warning==='string' ? warning : warning.message || JSON.stringify(warning); return item; }));
      $('#proposal-review-notes').replaceChildren(...(Array.isArray(local.proposal.review_notes) ? local.proposal.review_notes : []).map(note=>{ const item=document.createElement('li'); item.textContent=typeof note==='string' ? note : note?.message || JSON.stringify(note); return item; }));
      $('#proposal-reviewed').checked=false;
      $('#proposal-protected').textContent = local.proposal.protected_tokens?.length ? `受保护内容：${local.proposal.protected_tokens.join('、')}` : '采用后保留原稿历史。';
      dialog.close(); open('proposal');
    });
  }
  async function applyProposal() {
    const dialog = $('#proposal-dialog');
    if (!projectReady(dialog) || !local.proposal) return;
    if (!$('#proposal-reviewed').checked) { dialogError(dialog,new Error('请先逐句对照原稿，并勾选核对确认。')); return; }
    if (state.project.id !== local.proposal.project_id || state.project.revision !== local.proposal.source_revision) { dialogError(dialog,new Error('原项目已更新，请丢弃建议后重新生成。')); return; }
    await run('apply-proposal',dialog,async()=>{
      const project = await api(projectPath(`/proposals/${encodeURIComponent(local.proposal.id)}/apply`),{method:'POST',body:{expected_revision:state.project.revision,confirmed:true}});
      acceptProject(project); local.proposal=null; dialog.close(); toast('文案建议已采用，原稿历史已保留。');
    });
  }
  async function undoPreparation() {
    const dialog=$('#text-tools-dialog');
    if (!projectReady(dialog) || !state.project.last_preparation) return;
    await run('undo-preparation',dialog,async()=>{
      const project=await api(projectPath(`/proposals/${encodeURIComponent(state.project.last_preparation.id)}/undo`),{method:'POST',body:{expected_revision:state.project.revision,confirmed:true}});
      acceptProject(project); dialog.close(); toast('已恢复上次整理前的原稿。');
    });
  }
  async function previewDictionary() {
    const dialog = $('#text-tools-dialog');
    if (!projectReady(dialog)) return;
    await run('preview-dictionary',dialog,async()=>{
      local.pronunciation = await api(projectPath('/dictionary/preview'),{method:'POST',body:{}});
      local.pronunciation.project_id = state.project.id;
      local.pronunciation.source_revision = state.project.revision;
      $('#pronunciation-version').textContent = `预览当前词典版本 ${local.pronunciation.revision} · ${local.pronunciation.segments?.length || 0} 个片段`;
      $('#pronunciation-segments').replaceChildren(...(local.pronunciation.segments || []).map((segment,index)=>{
        const row=document.createElement('article'); row.className='pronunciation-row';
        const number=document.createElement('span'); number.className='segment-number'; number.textContent=String(index+1).padStart(2,'0');
        const content=document.createElement('div');
        const original=document.createElement('p'); original.className='muted pronunciation-original'; original.textContent=segment.text;
        const spoken=document.createElement('p'); spoken.textContent=segment.spoken_text || segment.text;
        const note=document.createElement('small'); note.className='muted'; note.textContent=segment.replacements?.length ? `${segment.replacements.length} 处读法替换` : '读法未变化';
        content.append(original,spoken,note); row.append(number,content); return row;
      }));
      dialog.close(); open('pronunciation');
    });
  }
  async function applyDictionary() {
    const dialog=$('#pronunciation-dialog');
    if (!projectReady(dialog) || !local.pronunciation) return;
    if (state.project.id !== local.pronunciation.project_id || state.project.revision !== local.pronunciation.source_revision) { dialogError(dialog,new Error('项目已更新，请关闭后重新预览读法。')); return; }
    await run('apply-dictionary',dialog,async()=>{
      const current=await api('/api/dictionary');
      if (current.revision !== local.pronunciation.revision) throw new Error('词典在预览后已有更新，请关闭后重新预览。');
      const project=await api(projectPath('/dictionary/apply'),{method:'POST',body:{expected_revision:state.project.revision,enabled:true}});
      acceptProject(project); dialog.close(); toast('当前词典已应用，读法改变的片段需要重新生成。');
    });
  }

  async function selectBatch(event) {
    const dialog=$('#batch-dialog'); clearDialogError(dialog); local.batch=[];
    const files=[...(event.target.files || [])];
    try {
      if (!files.length) return;
      if (files.length>20) throw new Error('一次最多导入 20 份文案，请重新选择。');
      for (const file of files) {
        if (!/\.(txt|md)$/i.test(file.name)) throw new Error(`${file.name} 不是 TXT 或 Markdown 文件。`);
        if (file.size>180000) throw new Error(`${file.name} 超过 180 KB，请先拆分。`);
        const script=(await file.text()).replace(/^\uFEFF/,'');
        if (!script.trim() || script.length>30000) throw new Error(`${file.name} 需要有 1–30,000 字文案。`);
        if (script.includes('\ufffd')) throw new Error(`${file.name} 不是有效 UTF-8 文案，请以 UTF-8 编码保存后导入。`);
        local.batch.push({title:file.name.replace(/\.(txt|md)$/i,'').slice(0,100),script});
      }
      if (local.batch.reduce((total,project)=>total+project.script.length,0)>180000) throw new Error('同一批文案合计最多 180,000 字，请分批导入。');
      $('#batch-preview').replaceChildren(...local.batch.map((project,index)=>{
        const row=document.createElement('div'); row.className='batch-preview-row';
        const number=document.createElement('span'); number.className='muted'; number.textContent=String(index+1).padStart(2,'0');
        const title=document.createElement('strong'); title.textContent=project.title;
        const count=document.createElement('small'); count.className='muted'; count.textContent=`${project.script.length.toLocaleString('zh-CN')} 字`;
        row.append(number,title,count); return row;
      }));
    } catch(error) { local.batch=[]; $('#batch-preview').replaceChildren(); dialogError(dialog,error); }
    finally { event.target.value=''; updateControls(); }
  }
  async function confirmBatch() {
    const dialog=$('#batch-dialog');
    if (!local.batch.length) return;
    await run('batch-import',dialog,async()=>{
      const result=await api('/api/batch',{method:'POST',body:{projects:local.batch,generate:$('#batch-generate').checked}});
      const projects=await api('/api/projects'); state.projects=projects.projects || [];
      if (result.jobs) mergeJobs(result.jobs);
      renderSidebar(); dialog.close(); local.batch=[]; $('#batch-preview').replaceChildren();
      toast(`已导入 ${result.projects?.length || 0} 个项目${result.jobs?.length ? '，配音已加入队列。' : '。'}`);
      schedulePoll(200);
    });
  }
  async function restoreArchive(event) {
    const file=event.target.files?.[0]; event.target.value='';
    if (!file) return;
    const dialog=$('#resources-dialog');
    if (!/\.zip$/i.test(file.name) || file.size>64*1024*1024) { dialogError(dialog,new Error('请选择不超过 64 MB 的声稿工程 ZIP。')); return; }
    await run('restore-project',dialog,async()=>{
      const project=await api('/api/projects/import',{method:'POST',headers:{'Content-Type':'application/zip'},body:file,rawBody:true});
      const result=await api('/api/projects'); state.projects=result.projects || []; renderSidebar();
      dialog.close(); toast('工程已恢复为新项目，原项目已保留。');
      if (!anyDirty()) { acceptProject(project.project || project); localStorage.setItem('voice-studio:last-project',state.project.id); }
    });
  }

  function mergeJobs(jobs) {
    const current=new Map((state.jobs || []).map(job=>[job.id,job]));
    for (const job of jobs) current.set(job.id,job);
    state.jobs=[...current.values()];
  }
  function renderJobs() {
    const jobs=state.jobs || [];
    const active=jobs.filter(job=>ACTIVE.has(job.status));
    $('#job-count').textContent=active.length;
    $('#open-jobs').classList.toggle('has-jobs',!!active.length);
    $('#queue-summary').textContent=active.length ? `${active.filter(job=>job.status==='running').length} 个任务运行中，${active.filter(job=>job.status==='queued').length} 个排队中。可以切换项目；排队与运行中的项目暂不能编辑。` : '当前没有排队或运行的任务。新生成的配音请试听确认。';
    const list=$('#jobs-list');
    list.replaceChildren();
    if (!jobs.length) { const empty=document.createElement('p'); empty.className='muted'; empty.textContent='还没有制作任务。'; list.append(empty); return; }
    const sorted=[...jobs].sort((a,b)=>Number(ACTIVE.has(b.status))-Number(ACTIVE.has(a.status)) || String(b.created_at || b.id).localeCompare(String(a.created_at || a.id)));
    for (const job of sorted.slice(0,60)) {
      const row=document.createElement('article'); row.className='job-row';
      const body=document.createElement('div'); body.className='job-row-body';
      const title=document.createElement('button'); title.type='button'; title.className='text-button job-project';
      title.textContent=state.projects.find(project=>project.id===job.project_id)?.title || '配音项目';
      title.addEventListener('click',async()=>{ $('#jobs-dialog').close(); await loadProject(job.project_id); });
      const detail=document.createElement('p'); detail.className='muted'; detail.textContent=`${job.kind==='video' ? '讲解视频' : '配音'} · ${JOB_LABELS[job.status] || job.status} · ${job.completed || 0} / ${job.total || 0}`;
      body.append(title,detail);
      if (job.error) { const error=document.createElement('p'); error.className='job-error'; error.textContent=job.error; body.append(error); }
      row.append(body);
      if (ACTIVE.has(job.status)) {
        const cancel=document.createElement('button'); cancel.type='button'; cancel.className='text-button'; cancel.textContent='取消'; cancel.disabled=!!state.operation;
        cancel.addEventListener('click',()=>cancelJob(job.id)); row.append(cancel);
      }
      list.append(row);
    }
  }
  async function cancelJob(id) {
    await run('cancel-task',$('#jobs-dialog'),async()=>{
      const job=await api(`/api/jobs/${encodeURIComponent(id)}/cancel`,{method:'POST',body:{}});
      mergeJobs([job]); if (state.job?.id===id) state.job=job;
      if (state.project?.id===job.project_id) acceptProject(await api(projectPath('')),{preserveDraft:true});
      renderJobs(); toast('已请求取消任务，已完成的成果会保留。'); schedulePoll(200);
    });
  }

  function renderFeatures() {
    const feature=state.status?.features?.rewrite;
    $('#rewrite-availability').textContent=feature?.message || '尚未配置文案模型。可先使用本地规则整理，不会自动连接云服务。';
    $('#prepare-rewrite').disabled=!feature?.available || !!state.operation;
    $('#rewrite-instruction').disabled=!feature?.available || !!state.operation;
    $('#prepare-rewrite').title=feature?.available ? '' : $('#rewrite-availability').textContent;
    const preparation=state.project?.last_preparation;
    const canUndo=!!preparation && state.project.revision===preparation.applied_revision;
    $('#undo-preparation').hidden=!preparation;
    $('#undo-preparation').disabled=!canUndo || !!state.operation;
    $('#undo-preparation-note').textContent='采用建议后已有其他修改，无法直接撤销。';
    $('#undo-preparation-note').hidden=!preparation || canUndo;
    $('#batch-engine-note').textContent=state.status?.engine?.available ? '生成在本机进行；长稿可能需要较长时间。' : '本地引擎尚未就绪，可先导入项目。';
    if (state.status && !state.status.engine?.available) $('#batch-generate').checked=false;
    $('#batch-generate').disabled=!state.status?.engine?.available || !!state.operation;
  }
  function renderVideo() {
    const project=state.project;
    const feature=state.status?.features?.video;
    const dirty=anyDirty();
    const allReady=!!project?.segments.length && project.segments.every(segment=>segment.status==='ready');
    const ready=feature?.available && allReady && project.export && !dirty && !projectBusy();
    $('#export-video').disabled=!ready || !!state.operation;
    $('#video-aspect').disabled=!ready || !!state.operation;
    $('#video-layout').disabled=!ready || !!state.operation;
    const virtualHost=$('#video-layout').value==='virtual_host';
    $('#video-host-preview').hidden=!virtualHost;
    $('#video-note').textContent=!feature?.available ? feature?.message || '视频环境尚未就绪，暂不可生成。' : dirty ? '先保存当前修改，再完成配音导出。' : !allReady ? '先生成全部片段，并试听确认。' : !project?.export ? '先生成当前版本的配音导出文件。' : projectBusy() ? '当前项目正在排队或运行。' : virtualHost ? '使用当前配音与段级字幕制作虚拟讲解视频；请先试听。' : '使用当前配音与段级字幕制作纯字幕讲解；请先试听。';
    const links=$('#video-links'); links.replaceChildren();
    const video=project?.video;
    const url=safeAudioUrl(video?.video_url);
    if (url) {
      for (const [key,label] of [['video_url','下载讲解视频 MP4 ↧'],['bundle_url','下载完整视频工程 ZIP ↧'],['composition_url','下载时间轴 HTML ↧']]) {
        const safe=safeAudioUrl(video[key]); if (!safe) continue;
        const link=document.createElement('a'); link.className='download-link'; link.href=safe; link.download=''; link.textContent=label; links.append(link);
      }
      const note=document.createElement('span'); note.className='muted video-result-note'; note.textContent=`${video.layout==='virtual_host' ? '虚拟讲解员' : '纯字幕讲解'} · ${video.aspect_ratio || '—'} · ${duration(video.duration_seconds)}`; links.append(note);
    }
    links.hidden=!url;
  }
  async function exportVideo() {
    if (!state.project || anyDirty() || projectBusy()) return;
    await run('video',null,async()=>{
      const layout=$('#video-layout').value;
      const body={expected_revision:state.project.revision,aspect_ratio:$('#video-aspect').value,layout};
      if (layout==='virtual_host') body.avatar_id='studio-bot';
      const job=await api(projectPath('/video'),{method:'POST',body});
      mergeJobs([job]); state.job=job; renderJobs(); toast('讲解视频任务已加入队列。'); schedulePoll(200);
    });
  }
  function render() {
    renderFeatures(); renderVoice(); renderVideo(); renderJobs();
    const snapshot=state.project?.dictionary_snapshot;
    $('#dictionary-snapshot-note').textContent=snapshot?.revision ? `项目词典快照 · 版本 ${snapshot.revision}` : '使用项目保存的读法';
  }
  function controls() {
    const locked=!!state.operation;
    $('#open-batch').disabled=!state.connected || locked;
    $('#open-resources').disabled=!state.connected || locked;
    $('#open-jobs').disabled=!state.connected;
    $('#manage-voices').disabled=!state.connected || locked;
    $('#open-text-tools').disabled=!state.project || locked || projectBusy() || anyDirty();
    $('#project-voice').disabled=!state.project || locked || projectBusy() || anyDirty();
    $('#refresh-project-voice').disabled=!state.project || locked || projectBusy() || anyDirty();
    $('#confirm-batch').disabled=locked || !local.batch.length;
    $('#save-voice').disabled=locked || local.renderingVoice?.id==='default' || !!local.renderingVoice?.readonly || !!local.renderingVoice?.read_only;
    $('#save-dictionary').disabled=locked || !local.dictionary;
    for (const button of $$('#production-dialogs .dialog-footer button, #prepare-cleanup, #preview-dictionary, #apply-proposal, #apply-dictionary')) {
      if (!['save-voice','save-dictionary','confirm-batch'].includes(button.id)) button.disabled=locked;
    }
    $('#apply-proposal').disabled=locked || local.proposal?.can_apply===false || !$('#proposal-reviewed').checked;
    $('#proposal-reviewed').disabled=locked;
    renderFeatures(); renderVideo();
    for (const button of $$('.job-row > button')) button.disabled=locked;
  }

  for (const button of $$('[data-close]')) button.addEventListener('click',()=>close(button.dataset.close));
  for (const dialog of $$('#production-dialogs dialog')) dialog.addEventListener('cancel',event=>{if(state.operation)event.preventDefault();});
  $('#open-resources').addEventListener('click',()=>open('resources'));
  $('#open-batch').addEventListener('click',()=>{open('batch');renderFeatures();});
  $('#open-jobs').addEventListener('click',()=>{open('jobs');renderJobs();});
  $('#resources-voices').addEventListener('click',()=>{$('#resources-dialog').close();openVoices();});
  $('#resources-dictionary').addEventListener('click',()=>{$('#resources-dialog').close();openDictionary();});
  $('#resources-restore').addEventListener('click',()=>$('#restore-file').click());
  $('#restore-file').addEventListener('change',restoreArchive);
  $('#manage-voices').addEventListener('click',openVoices);
  $('#voice-record').addEventListener('change',renderVoiceForm);
  $('#voice-form').addEventListener('submit',saveVoice);
  $('#project-voice').addEventListener('change',changeVoice);
  $('#refresh-project-voice').addEventListener('click',()=>changeVoice({target:$('#project-voice')},true));
  $('#add-dictionary-entry').addEventListener('click',()=>{ const row=dictionaryRow(); $('#dictionary-rows').append(row); $('.term-input',row).focus(); });
  $('#save-dictionary').addEventListener('click',saveDictionary);
  $('#open-text-tools').addEventListener('click',openTextTools);
  $('#prepare-cleanup').addEventListener('click',()=>prepare('cleanup'));
  $('#prepare-rewrite').addEventListener('click',()=>prepare('rewrite'));
  $('#apply-proposal').addEventListener('click',applyProposal);
  $('#proposal-reviewed').addEventListener('change',controls);
  $('#undo-preparation').addEventListener('click',undoPreparation);
  $('#preview-dictionary').addEventListener('click',previewDictionary);
  $('#manage-dictionary').addEventListener('click',()=>{$('#text-tools-dialog').close();openDictionary();});
  $('#apply-dictionary').addEventListener('click',applyDictionary);
  $('#batch-files').addEventListener('change',selectBatch);
  $('#confirm-batch').addEventListener('click',confirmBatch);
  $('#export-video').addEventListener('click',exportVideo);
  $('#video-layout').addEventListener('change',renderVideo);
  return {render, controls, renderJobs, mergeJobs, loadVoices};
}
