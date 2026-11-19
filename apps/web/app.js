import {createProduction} from './production.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const ACTIVE_JOBS = new Set(['queued', 'running']);
const TERMINAL_JOBS = new Set(['completed', 'failed', 'cancelled']);
const editTimeFormat = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
});
const TEMPLATES = {
  intro: {title: '我的项目介绍', script: '大家好，今天向大家介绍我正在做的一个项目。\n这个项目从一个实际问题出发，希望让日常工作更简单一些。\n接下来，我会分享它的背景、已经完成的内容，以及下一步的计划。'},
  experience: {title: '一次实践的经验分享', script: '今天想和大家分享一次实践中的经验。\n一开始，我以为问题出在工具上。实际尝试以后，发现把需求和步骤说明白，同样重要。\n接下来，我会讲讲这次尝试的过程，以及值得保留下来的做法。'},
  tutorial: {title: '操作教程讲解', script: '大家好，这次我们一起完成一个简单的操作。\n开始之前，请先准备好需要使用的材料。\n第一步，确认输入内容。第二步，按照顺序完成操作。最后，检查结果是否符合预期。'},
};

const state = {
  status: null, projects: [], project: null, draft: null,
  segmentDrafts: new Map(), tab: 'script', job: null,
  operation: null, conflict: false, timer: null, pollRunning: false,
  requestSequence: 0, connected: false, jobs: [],
};
let production;

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, options = {}) {
  const headers = {Accept: 'application/json'};
  if (options.method && options.method !== 'GET') {
    if (!state.status?.csrf_token) throw new ApiError(503, '尚未连接本地服务，请稍后再试。');
    headers['Content-Type'] = 'application/json';
    headers['X-Studio-Token'] = state.status.csrf_token;
  }
  let response;
  try {
    response = await fetch(path, {
      ...options, headers: {...headers, ...options.headers},
      credentials: 'same-origin', cache: 'no-store',
      body: options.body === undefined ? undefined : options.rawBody ? options.body : JSON.stringify(options.body),
    });
  } catch {
    throw new ApiError(0, '无法连接本地服务。请确认工作台仍在运行，你的输入已保留。');
  }
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, typeof result.error === 'string' ? result.error : '操作未完成，请稍后再试。');
  return result;
}

function safeAudioUrl(url) {
  return typeof url === 'string' && (url.startsWith('/files/') || url === '/api/voice/sample' || /^\/api\/voices\/[a-zA-Z0-9_-]+\/sample$/.test(url)) ? url : null;
}

function duration(value) {
  if (!Number.isFinite(Number(value)) || value == null) return '—';
  const rounded = Math.max(0, Math.round(Number(value)));
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, '0')}`;
}

function dateLabel(value) {
  if (!value) return '刚刚创建';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '已保存';
  return editTimeFormat.format(date);
}

function toast(message) {
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { $('#toast').hidden = true; }, 3400);
}

function showError(error) {
  $('#alert-text').textContent = error.message || String(error);
  $('#alert').hidden = false;
  state.conflict = error.status === 409;
  $('#reload-project').hidden = !state.conflict;
}

function clearError() {
  $('#alert').hidden = true;
  state.conflict = false;
  $('#reload-project').hidden = true;
}

function busy() { return state.jobs.some(job => ACTIVE_JOBS.has(job.status)) || ACTIVE_JOBS.has(state.job?.status); }
function projectBusy() { return state.jobs.some(job => ACTIVE_JOBS.has(job.status) && job.project_id === state.project?.id) || (ACTIVE_JOBS.has(state.job?.status) && state.job.project_id === state.project?.id); }
function scriptDirty() { return !!state.project && (state.draft.title !== state.project.title || state.draft.script !== state.project.script); }
function settingsDirty() {
  return !!state.project && (Number(state.draft.speed) !== Number(state.project.settings.speed) || Number(state.draft.pause_seconds) !== Number(state.project.settings.pause_seconds));
}
function anyDirty() { return scriptDirty() || settingsDirty() || state.segmentDrafts.size > 0; }

function projectEngineReady() {
  const engine = state.status?.engine;
  const project = state.project;
  if (!project || engine?.model_available === undefined) return !!engine?.available;
  const voice = project.voice_snapshot;
  if (voice?.reference_audio && voice.reference_text?.trim()) return engine.model_available;
  const firstBinding = project.voice_id === 'default' && !voice?.reference_audio
    && project.segments.every(segment => !segment.audio_url && !segment.engine);
  return firstBinding && !!engine.available;
}

function makeDraft(project) {
  return {title: project.title, script: project.script, speed: project.settings.speed, pause_seconds: project.settings.pause_seconds};
}

function acceptProject(project, {preserveDraft = false, render = true} = {}) {
  const previous = state.project;
  const same = previous?.id === project.id;
  const oldDraft = state.draft;
  state.project = project;
  if (!same || !preserveDraft) {
    state.draft = makeDraft(project);
    state.segmentDrafts.clear();
  } else {
    state.draft = {
      title: oldDraft.title === previous.title ? project.title : oldDraft.title,
      script: oldDraft.script === previous.script ? project.script : oldDraft.script,
      speed: Number(oldDraft.speed) === Number(previous.settings.speed) ? project.settings.speed : oldDraft.speed,
      pause_seconds: Number(oldDraft.pause_seconds) === Number(previous.settings.pause_seconds) ? project.settings.pause_seconds : oldDraft.pause_seconds,
    };
  }
  const summary = state.projects.find(item => item.id === project.id);
  if (summary) Object.assign(summary, project);
  else state.projects.unshift(project);
  state.projects.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
  if (render) renderProject();
  renderSidebar();
}

function renderSidebar() {
  const list = $('#project-list');
  list.replaceChildren();
  $('#project-count').textContent = state.projects.length;
  if (!state.projects.length) {
    const empty = document.createElement('p');
    empty.className = 'muted sidebar-empty';
    empty.textContent = state.connected ? '还没有配音项目。' : '正在连接本地服务…';
    list.append(empty);
    return;
  }
  for (const project of state.projects) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `project-item${project.id === state.project?.id ? ' active' : ''}`;
    button.dataset.projectId = project.id;
    button.setAttribute('aria-current', project.id === state.project?.id ? 'page' : 'false');
    const title = document.createElement('strong');
    title.textContent = project.title || '未命名项目';
    const time = document.createElement('small');
    time.textContent = `编辑于 ${dateLabel(project.updated_at)}`;
    button.append(title, time);
    button.addEventListener('click', () => loadProject(project.id));
    list.append(button);
  }
}

function renderStatus() {
  const status = state.status;
  const available = projectEngineReady();
  $('#engine-state').classList.toggle('unavailable', !available);
  $('#engine-state span:last-child').textContent = status ? (available ? '本地引擎已就绪' : '本地引擎待配置') : '连接未完成';
  $('#engine-state').title = available ? '可以使用当前声线生成配音' : status?.engine?.message || '正在连接本地服务';
  $('#app-version').textContent = status?.app_version || '';
  production?.render();
  updateControls();
}

function renderProject() {
  const project = state.project;
  $('#empty-workspace').hidden = !!project;
  $('#project-workspace').hidden = !project;
  if (!project) {
    $('#page-title').textContent = '配音制作';
    $('#heading-note').textContent = '从一篇文案开始，用你的声音讲述。';
    updateControls();
    return;
  }
  $('#breadcrumb-title').textContent = project.title;
  $('#page-title').textContent = project.title;
  $('#heading-note').textContent = `本地项目 · ${project.segments.length} 个片段 · 修改于 ${dateLabel(project.updated_at)}`;
  $('#project-title').value = state.draft.title;
  $('#script-editor').value = state.draft.script;
  $('#script-count').textContent = `${state.draft.script.length.toLocaleString('zh-CN')} 字`;
  $('#speed').value = state.draft.speed;
  $('#speed-value').textContent = `${Number(state.draft.speed).toFixed(2)}×`;
  $('#pause').value = state.draft.pause_seconds;
  $('#segment-count').textContent = project.segments.length;
  renderSegments();
  renderExport();
  renderTask();
  production?.render();
  updateControls();
}

const STATUS_LABELS = {pending: '待生成', queued: '等待生成', generating: '正在生成', ready: '已生成 · 待试听', error: '生成未完成'};

function renderSegments() {
  if (!state.project) return;
  const project = state.project;
  const list = $('#segments-list');
  list.replaceChildren();
  for (const [index, segment] of project.segments.entries()) {
    const row = document.createElement('article');
    row.className = 'segment-row';
    row.dataset.segmentId = segment.id;
    row.innerHTML = '<span class="segment-number"></span><div class="segment-content"><div class="segment-top"><span class="segment-status"></span><span class="segment-duration"></span></div><textarea class="segment-text" maxlength="120" rows="2" spellcheck="false"></textarea><p class="segment-error" hidden></p><div class="segment-actions"><button type="button" class="text-button segment-save">保存修改</button><button type="button" class="button secondary segment-play">▷ 试听</button><button type="button" class="button secondary segment-generate">重新生成</button></div></div>';
    $('.segment-number', row).textContent = String(index + 1).padStart(2, '0');
    const badge = $('.segment-status', row);
    badge.textContent = STATUS_LABELS[segment.status] || segment.status;
    badge.classList.add(segment.status);
    $('.segment-duration', row).textContent = segment.duration_seconds == null ? '尚无音频' : duration(segment.duration_seconds);
    const editor = $('.segment-text', row);
    editor.id = `segment-${segment.id}`;
    editor.setAttribute('aria-label', `第 ${index + 1} 段文案`);
    editor.value = state.segmentDrafts.get(segment.id) ?? segment.text;
    editor.addEventListener('input', () => {
      if (editor.value === segment.text) state.segmentDrafts.delete(segment.id);
      else state.segmentDrafts.set(segment.id, editor.value);
      updateControls();
    });
    if (segment.error) {
      $('.segment-error', row).hidden = false;
      $('.segment-error', row).textContent = segment.error;
    }
    $('.segment-save', row).addEventListener('click', () => saveSegment(segment.id));
    $('.segment-play', row).addEventListener('click', () => playAudio(segment.audio_url, `片段 ${String(index + 1).padStart(2, '0')}`, segment.text, '片段试听'));
    $('.segment-generate', row).textContent = segment.status === 'ready' ? '重新生成' : '生成片段';
    $('.segment-generate', row).addEventListener('click', () => startGeneration(segment.id));
    list.append(row);
  }
  const ready = project.segments.filter(segment => segment.status === 'ready').length;
  $('#ready-count').textContent = `${ready} / ${project.segments.length} 已生成`;
}

function renderExport() {
  const exported = state.project?.export;
  const container = $('#export-links');
  container.replaceChildren();
  container.hidden = !exported;
  if (!exported) return;
  const definitions = [['wav_url', '配音音频', 'WAV'], ['mp3_url', '压缩音频', 'MP3'], ['srt_url', '段级字幕', 'SRT'], ['bundle_url', '完整工程', 'ZIP']];
  for (const [key, label, format] of definitions) {
    const url = safeAudioUrl(exported[key]);
    if (!url) continue;
    const link = document.createElement('a');
    link.className = 'download-link'; link.href = url; link.download = '';
    link.textContent = label;
    const type = document.createElement('small'); type.textContent = `${format} ↧`;
    link.append(type); container.append(link);
  }
  const wav = safeAudioUrl(exported.wav_url);
  if (wav) {
    const play = document.createElement('button');
    play.type = 'button'; play.className = 'download-link'; play.textContent = '▷ 试听完整配音';
    play.addEventListener('click', () => playAudio(wav, state.project.title, `完整配音 · ${duration(exported.duration_seconds)}`, '完整配音'));
    container.append(play);
  }
}

function renderTask() {
  const job = state.jobs.find(job => job.project_id === state.project?.id && ACTIVE_JOBS.has(job.status)) || state.job;
  $('#task-panel').hidden = !job;
  if (!job) return;
  const isCurrent = job.project_id === state.project?.id;
  const terminal = TERMINAL_JOBS.has(job.status);
  const labels = {queued:'已加入制作队列',running:job.kind === 'video' ? '正在制作讲解视频' : '正在本地生成',completed:job.kind === 'video' ? '讲解视频已完成' : '配音生成完成',failed:'生成未完成',cancelled:'任务已取消'};
  $('#task-label').textContent = `${labels[job.status] || job.status}${isCurrent ? '' : ' · 其他项目'}`;
  $('#task-progress-label').textContent = `${job.completed || 0} / ${job.total || 0}`;
  $('#task-progress').max = Math.max(1, job.total || 1);
  $('#task-progress').value = job.completed || 0;
  $('#task-panel').classList.toggle('terminal', terminal);
  $('#cancel-job').hidden = terminal;
  $('#cancel-job').disabled = !!state.operation;
  $('#task-note').textContent = job.error || job.message || job.stage || (job.status === 'queued' ? '等待本机前面的任务完成。该项目暂不能编辑，可切换到其他项目。' : job.status === 'completed' ? job.kind === 'video' ? '讲解视频已生成，可在成品导出区下载。' : '新生成的配音尚待试听，请确认字词、停顿和声线。' : job.status === 'cancelled' ? '已经完成的片段会保留，可以继续生成剩余片段。' : '本地生成可能需要一些时间。可以切换项目，已经完成的片段会保留。');
}

function updateControls() {
  const project = state.project;
  const locked = !!state.operation || projectBusy();
  const engineReady = projectEngineReady();
  $('#new-project').disabled = !state.connected || !!state.operation;
  $('#empty-create').disabled = !state.connected || !!state.operation;
  $('#project-title').disabled = !project || locked;
  $('#script-editor').disabled = !project || locked;
  $('#speed').disabled = !project || locked;
  $('#pause').disabled = !project || locked;
  $('#save-settings').disabled = !project || locked || !settingsDirty();
  $('#save-project').disabled = !project || locked || !scriptDirty();
  $('#save-project').textContent = state.operation === 'save' ? '正在保存…' : '保存并分段';
  $('#save-settings').textContent = state.operation === 'settings' ? '正在保存…' : '保存设置';
  $('#import-file').disabled = !project || locked;
  $('.file-button').setAttribute('aria-disabled', String(!project || locked));
  production?.controls();
  if (!project) return;
  const pending = project.segments.filter(segment => segment.status !== 'ready').length;
  const ready = project.segments.length - pending;
  const dirty = anyDirty();
  $('#save-state').textContent = scriptDirty() ? '文案有未保存修改。' : `文案已保存 · 版本 ${project.revision}`;
  $('#generate-project').disabled = locked || !engineReady || dirty || !pending;
  const queued = state.jobs.some(job => job.project_id === project.id && job.status === 'queued');
  $('#generate-project').textContent = state.operation === 'generate' ? '正在提交…' : projectBusy() ? queued ? '已加入配音队列' : '正在制作中…' : pending ? busy() ? `加入配音队列（${pending}）` : `生成待完成片段（${pending}）` : '全部片段已生成';
  $('#generation-title').textContent = dirty ? '有未保存的修改' : pending ? `${pending} 个片段待生成` : '全部片段已生成';
  $('#generation-note').textContent = dirty ? '保存修改后，再生成或导出。' : !engineReady ? '文案可以照常编辑。生成配音前，请在本机配置中绑定模型和参考录音。' : ready ? `已保留 ${ready} 个完成片段。` : '使用当前声线，在本机生成配音。';
  $('#export-project').disabled = locked || dirty || !project.segments.length || pending > 0;
  $('#export-project').textContent = state.operation === 'export' ? '正在导出…' : '生成导出文件 ↗';
  $('#export-note').textContent = project.export ? `导出完成 · ${duration(project.export.duration_seconds)} · SRT 为按片段对齐的时间轴。` : dirty ? '保存修改并完成所需配音后，即可导出。' : '全部片段生成后即可导出。新配音请先试听确认。';
  const segmentsById = new Map(project.segments.map(segment => [segment.id, segment]));
  for (const row of $$('.segment-row')) {
    const segment = segmentsById.get(row.dataset.segmentId);
    if (!segment) continue;
    $('.segment-text', row).disabled = locked || scriptDirty();
    $('.segment-save', row).disabled = locked || scriptDirty() || !state.segmentDrafts.has(segment.id);
    $('.segment-play', row).disabled = !safeAudioUrl(segment.audio_url) || segment.status !== 'ready';
    $('.segment-generate', row).disabled = locked || !engineReady || dirty;
  }
  renderTask();
}

function setTab(tab, focus = false) {
  state.tab = tab;
  for (const name of ['script', 'segments']) {
    const selected = name === tab;
    $(`#tab-${name}`).classList.toggle('active', selected);
    $(`#tab-${name}`).setAttribute('aria-selected', String(selected));
    $(`#tab-${name}`).tabIndex = selected ? 0 : -1;
    $(`#panel-${name}`).hidden = !selected;
  }
  if (focus) $(`#tab-${tab}`).focus();
}

async function loadProject(id, {force = false} = {}) {
  if (state.operation) return;
  if (!force && id === state.project?.id) return;
  if (!force && anyDirty() && !window.confirm('当前项目有未保存修改。切换项目会放弃这些修改，是否继续？')) return;
  const sequence = ++state.requestSequence;
  try {
    const project = await api(`/api/projects/${encodeURIComponent(id)}`);
    if (sequence !== state.requestSequence) return;
    clearError();
    acceptProject(project);
    localStorage.setItem('voice-studio:last-project', project.id);
    setTab(state.tab);
  } catch (error) { showError(error); }
}

async function perform(name, action) {
  if (state.operation) return;
  clearError(); state.operation = name; updateControls();
  try { return await action(); }
  catch (error) { showError(error); }
  finally { state.operation = null; updateControls(); }
}

async function saveProject() {
  if (!state.project || !scriptDirty() || projectBusy()) return;
  if (!state.draft.title.trim() || !state.draft.script.trim()) { showError(new Error('项目名称和口播文案都需要填写。')); return; }
  if (state.segmentDrafts.size && !window.confirm('保存完整文案将替代未保存的逐段修改，是否继续？')) return;
  await perform('save', async () => {
    const project = await api(`/api/projects/${state.project.id}`, {method:'PUT',body:{title:state.draft.title.trim(),script:state.draft.script,expected_revision:state.project.revision}});
    acceptProject(project, {preserveDraft:true});
    state.draft.title = project.title; state.draft.script = project.script;
    state.segmentDrafts.clear(); renderProject();
    toast(`文案已保存，共 ${project.segments.length} 个片段。`);
  });
}

async function saveSettings(event) {
  event.preventDefault();
  if (!state.project || !settingsDirty() || projectBusy()) return;
  const speed = Number(state.draft.speed), pause = Number(state.draft.pause_seconds);
  if (!Number.isFinite(speed) || speed < .75 || speed > 1.25 || !Number.isFinite(pause) || pause < 0 || pause > 2 || state.draft.pause_seconds === '') { showError(new Error('语速应为 0.75–1.25 倍，句间停顿应为 0–2 秒。')); return; }
  await perform('settings', async () => {
    const project = await api(`/api/projects/${state.project.id}`, {method:'PUT',body:{settings:{speed,pause_seconds:pause},expected_revision:state.project.revision}});
    acceptProject(project, {preserveDraft:true});
    state.draft.speed = project.settings.speed; state.draft.pause_seconds = project.settings.pause_seconds;
    renderProject(); toast('生成设置已保存。');
  });
}

async function saveSegment(id) {
  if (!state.project || !state.segmentDrafts.has(id) || projectBusy()) return;
  const text = state.segmentDrafts.get(id).trim();
  if (!text) { showError(new Error('片段文案不能为空。')); return; }
  await perform('segment', async () => {
    const project = await api(`/api/projects/${state.project.id}/segments/${id}`, {method:'PATCH',body:{text,expected_revision:state.project.revision}});
    state.segmentDrafts.delete(id);
    acceptProject(project, {preserveDraft:true});
    toast('片段已保存，可以重新生成配音。');
  });
}

async function startGeneration(id = null) {
  if (!state.project || anyDirty() || projectBusy()) return;
  await perform('generate', async () => {
    const path = id ? `/api/projects/${state.project.id}/segments/${id}/generate` : `/api/projects/${state.project.id}/generate`;
    const job = await api(path, {method:'POST',body:{}});
    if (job?.id) { state.job = job; production.mergeJobs([job]); }
    acceptProject(await api(`/api/projects/${state.project.id}`), {preserveDraft:true});
    setTab('segments'); renderTask();
    toast(job.status === 'queued' ? '配音任务已加入队列。' : '配音任务已开始。');
    schedulePoll(300);
  });
}

async function cancelJob() {
  const job = state.jobs.find(item => item.project_id === state.project?.id && ACTIVE_JOBS.has(item.status)) || state.job;
  if (!job || !ACTIVE_JOBS.has(job.status)) return;
  await perform('cancel', async () => {
    state.job = await api(`/api/jobs/${job.id}/cancel`, {method:'POST',body:{}});
    production.mergeJobs([state.job]); production.renderJobs();
    if (state.project) acceptProject(await api(`/api/projects/${state.project.id}`), {preserveDraft:true});
    renderTask(); toast('已请求取消，完成的片段会保留。'); schedulePoll(300);
  });
}

async function exportProject() {
  if (!state.project || anyDirty() || projectBusy()) return;
  await perform('export', async () => {
    const exported = await api(`/api/projects/${state.project.id}/export`, {method:'POST',body:{}});
    acceptProject(await api(`/api/projects/${state.project.id}`), {preserveDraft:true});
    if (!state.project.export) { state.project.export = exported; renderExport(); }
    toast('导出完成，可以下载音频、字幕和完整工程。');
  });
}

function playAudio(url, title, subtitle, tag) {
  const safe = safeAudioUrl(url);
  if (!safe) return;
  const player = $('#main-player');
  if (player.getAttribute('src') !== safe) player.src = safe;
  $('#player-title').textContent = title;
  $('#player-subtitle').textContent = subtitle;
  $('#player-tag').textContent = tag;
  $('#reference-player').pause();
  player.play().catch(() => toast('音频已载入，请点击播放按钮试听。'));
}

function openNewProject() {
  if (state.operation || !state.connected) return;
  $('#new-project-error').hidden = true;
  $('#new-project-dialog').showModal();
  $('#new-title').focus();
}

async function createProject(event) {
  event.preventDefault();
  if (state.operation) return;
  const title = $('#new-title').value.trim(), script = $('#new-script').value.trim();
  if (!title || !script) { $('#new-project-error').textContent = '请填写项目名称和口播文案。'; $('#new-project-error').hidden = false; return; }
  if (anyDirty() && !window.confirm('当前项目有未保存修改。创建并打开新项目会放弃这些修改，是否继续？')) return;
  state.operation = 'create'; $('#create-project').disabled = true; $('#create-project').textContent = '正在创建…';
  updateControls();
  try {
    const project = await api('/api/projects', {method:'POST',body:{title,script}});
    ++state.requestSequence;
    clearError(); acceptProject(project); setTab('script');
    localStorage.setItem('voice-studio:last-project', project.id);
    $('#new-project-dialog').close(); $('#new-project-form').reset();
    toast('项目已创建。确认文案后即可生成。');
  } catch (error) {
    $('#new-project-error').textContent = error.message;
    $('#new-project-error').hidden = false;
  } finally {
    state.operation = null; $('#create-project').disabled = false; $('#create-project').textContent = '创建项目'; updateControls();
  }
}

function schedulePoll(delay) {
  clearTimeout(state.timer);
  state.timer = setTimeout(poll, delay ?? (busy() ? 1500 : 5000));
}

async function poll() {
  if (state.pollRunning || state.operation) { schedulePoll(); return; }
  state.pollRunning = true;
  try {
    const wasConnected = state.connected;
    const status = await api('/api/status');
    state.status = status; state.connected = true;
    if (!wasConnected && !state.project) {
      const result = await api('/api/projects');
      state.projects = result.projects || [];
      renderSidebar();
      const last = localStorage.getItem('voice-studio:last-project');
      const first = state.projects.find(project => project.id === last) || state.projects[0];
      if (first) await loadProject(first.id);
    }
    const previousJobState = state.job?.status;
    const jobsResult = await api('/api/jobs');
    state.jobs = jobsResult.jobs || [];
    const latest = state.jobs.find(job => job.id === state.job?.id);
    if (latest) state.job = latest;
    if (status.active_job && (!state.job || !ACTIVE_JOBS.has(state.job.status))) state.job = state.jobs.find(job=>job.id === status.active_job.id) || status.active_job;
    if (state.project && !state.operation) {
      const id = state.project.id;
      const incoming = await api(`/api/projects/${id}`);
      if (state.project?.id === id && !state.operation && incoming.revision !== state.project.revision) {
        if (anyDirty()) {
          // Keep the editing baseline unchanged; a later save will receive a 409.
          showError(new ApiError(409, '项目内容已在其他窗口更新。你的输入已保留，请复制需要的修改后重新载入项目。'));
        } else {
          acceptProject(incoming, {preserveDraft:true});
        }
      } else if (state.project?.id === id && !state.operation && !anyDirty() && (projectBusy() || previousJobState !== state.job?.status || JSON.stringify(incoming.export) !== JSON.stringify(state.project.export) || JSON.stringify(incoming.video) !== JSON.stringify(state.project.video) || incoming.segments.some((segment,index)=>segment.status !== state.project.segments[index]?.status))) {
        acceptProject(incoming, {preserveDraft:true});
      }
    }
    if (previousJobState && ACTIVE_JOBS.has(previousJobState) && TERMINAL_JOBS.has(state.job?.status)) toast(state.job.status === 'completed' ? state.job.kind === 'video' ? '讲解视频已生成，可以下载。' : '配音生成完成，请试听确认。' : state.job.status === 'cancelled' ? '任务已取消。' : '生成未完成，请查看任务提示。');
    renderStatus(); renderTask();
  } catch (error) {
    state.connected = false;
    $('#engine-state').classList.add('unavailable');
    $('#engine-state span:last-child').textContent = '本地服务连接中断';
    $('#engine-state').title = error.message;
  } finally { state.pollRunning = false; schedulePoll(); }
}

function bindEvents() {
  $('#new-project').addEventListener('click', openNewProject);
  $('#empty-create').addEventListener('click', openNewProject);
  $('#close-dialog').addEventListener('click', () => { if (state.operation !== 'create') $('#new-project-dialog').close(); });
  $('#cancel-dialog').addEventListener('click', () => { if (state.operation !== 'create') $('#new-project-dialog').close(); });
  $('#new-project-dialog').addEventListener('cancel', event => { if (state.operation === 'create') event.preventDefault(); });
  $('#new-project-form').addEventListener('submit', createProject);
  for (const button of $$('.template-option')) button.addEventListener('click', () => {
    const template = TEMPLATES[button.dataset.template];
    if ($('#new-script').value.trim() && !window.confirm('应用起稿模板会替换当前对话框中的文案，是否继续？')) return;
    $('#new-title').value = template.title; $('#new-script').value = template.script; $('#new-script').focus();
  });
  $('#project-title').addEventListener('input', event => { if (state.draft) { state.draft.title = event.target.value; updateControls(); } });
  $('#script-editor').addEventListener('input', event => { if (state.draft) { state.draft.script = event.target.value; $('#script-count').textContent = `${event.target.value.length.toLocaleString('zh-CN')} 字`; updateControls(); } });
  $('#speed').addEventListener('input', event => { if (state.draft) { state.draft.speed = Number(event.target.value); $('#speed-value').textContent = `${Number(event.target.value).toFixed(2)}×`; updateControls(); } });
  $('#pause').addEventListener('input', event => { if (state.draft) { state.draft.pause_seconds = event.target.value; updateControls(); } });
  $('#save-project').addEventListener('click', saveProject);
  $('#settings-form').addEventListener('submit', saveSettings);
  $('#generate-project').addEventListener('click', () => startGeneration());
  $('#cancel-job').addEventListener('click', cancelJob);
  $('#export-project').addEventListener('click', exportProject);
  $('#dismiss-alert').addEventListener('click', clearError);
  $('#reload-project').addEventListener('click', () => {
    if (!state.project) return;
    if (anyDirty() && !window.confirm('重新载入将放弃当前未保存修改。请先复制需要保留的内容，确认继续？')) return;
    loadProject(state.project.id, {force:true});
  });
  for (const name of ['script', 'segments']) {
    $(`#tab-${name}`).addEventListener('click', () => setTab(name));
    $(`#tab-${name}`).addEventListener('keydown', event => {
      if (['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
        event.preventDefault();
        setTab(event.key === 'Home' ? 'script' : event.key === 'End' ? 'segments' : name === 'script' ? 'segments' : 'script', true);
      }
    });
  }
  $('#import-file').addEventListener('change', async event => {
    const file = event.target.files?.[0];
    if (!file || !state.project) return;
    try {
      if (file.size > 180000) throw new Error('文案文件过大，请选择不超过 180 KB 的 UTF-8 TXT 或 Markdown 文件。');
      if (!/\.(txt|md)$/i.test(file.name)) throw new Error('请选择 TXT 或 Markdown 文案文件。');
      const text = (await file.text()).replace(/^\uFEFF/, '');
      if (!text.trim()) throw new Error('文件中没有可用的文案。');
      if (text.length > 30000) throw new Error('文案超过 30,000 字，请拆成多个项目。');
      if (state.draft.script.trim() && !window.confirm('导入文案会替换编辑区内容，保存后才会影响项目。是否继续？')) return;
      state.draft.script = text; $('#script-editor').value = text;
      $('#script-count').textContent = `${text.length.toLocaleString('zh-CN')} 字`;
      setTab('script'); updateControls(); toast('文案已读入编辑区，请检查后保存。');
    } catch (error) { showError(error); }
    finally { event.target.value = ''; }
  });
  $('#reference-player').addEventListener('play', () => $('#main-player').pause());
  $('#main-player').addEventListener('error', () => { if ($('#main-player').getAttribute('src')) showError(new Error('音频暂时无法播放，请确认文件仍在，或重新生成该片段。')); });
  window.addEventListener('beforeunload', event => { if (anyDirty()) { event.preventDefault(); event.returnValue = ''; } });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) schedulePoll(0); });
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && state.project && !$('dialog[open]')) { event.preventDefault(); if (state.tab === 'script') saveProject(); else if (document.activeElement?.classList.contains('segment-text')) saveSegment(document.activeElement.closest('.segment-row').dataset.segmentId); }
  });
}

async function initialize() {
  bindEvents(); renderSidebar(); renderProject();
  try {
    state.status = await api('/api/status'); state.connected = true;
    state.job = state.status.active_job || null;
    const [result, jobs] = await Promise.all([api('/api/projects'), api('/api/jobs'), production.loadVoices()]);
    state.projects = result.projects || [];
    state.jobs = jobs.jobs || [];
    renderStatus(); renderSidebar();
    const last = localStorage.getItem('voice-studio:last-project');
    const first = state.projects.find(project => project.id === last) || state.projects[0];
    if (first) await loadProject(first.id);
  } catch (error) { showError(error); renderStatus(); }
  finally { schedulePoll(); }
}

production = createProduction({state,api,acceptProject,anyDirty,projectBusy,updateControls,renderSidebar,showError,toast,schedulePoll,loadProject,safeAudioUrl,duration});
initialize();
