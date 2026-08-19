const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function toast(message, error = false) {
  const el = $('#toast');
  if (!el) return;
  el.textContent = message;
  el.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(window.__folioToast);
  window.__folioToast = setTimeout(() => el.className = 'toast', 3500);
}

async function api(url, options = {}) {
  const response = await fetch(url, {headers: {'Content-Type': 'application/json', ...(options.headers || {})}, ...options});
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try { const body = await response.json(); message = body.detail || message; } catch (_) {}
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function initUpload() {
  const zone = $('#drop-zone'), input = $('#pdf-file'), note = $('#file-note');
  if (!zone || !input) return;
  ['dragenter', 'dragover'].forEach(name => zone.addEventListener(name, e => { e.preventDefault(); zone.classList.add('dragging'); }));
  ['dragleave', 'drop'].forEach(name => zone.addEventListener(name, e => { e.preventDefault(); zone.classList.remove('dragging'); }));
  zone.addEventListener('drop', e => { if (e.dataTransfer.files.length) { input.files = e.dataTransfer.files; input.dispatchEvent(new Event('change')); } });
  input.addEventListener('change', () => { const file = input.files[0]; if (file) note.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)}MB`; });
}

function initJob() {
  const root = $('.status-layout');
  if (!root) return;
  const id = root.dataset.jobId, token = root.dataset.token;
  const startedAt = new Date(root.dataset.createdAt);
  const terminal = new Set(['review_required', 'completed', 'canceled', 'expired']);
  async function refresh() {
    try {
      const job = await api(`/api/v1/jobs/${id}?token=${encodeURIComponent(token)}`);
      $('#progress-number').textContent = Math.round(job.progress * 100);
      $('#progress-bar').style.width = `${job.progress * 100}%`;
      $('#stage-label').textContent = job.stage;
      $('#status-value').textContent = job.status;
      $('#current-page').textContent = job.current_page;
      $('#page-count').textContent = job.page_count || '—';
      $('#issue-count').textContent = job.unresolved_issues;
      const elapsedSeconds = Math.max(1, (Date.now() - startedAt.getTime()) / 1000);
      const formatTime = seconds => seconds < 60 ? `${Math.round(seconds)} 秒` : `${Math.floor(seconds/60)} 分 ${Math.round(seconds%60)} 秒`;
      $('#elapsed-time').textContent = formatTime(elapsedSeconds);
      $('#eta-time').textContent = job.progress > .03 && job.progress < .98 ? formatTime(elapsedSeconds * (1-job.progress) / job.progress) : (job.progress >= .98 ? '即将完成' : '计算中');
      const alert = $('#job-alert');
      if (job.error || job.warning) { alert.textContent = job.error || job.warning; alert.classList.remove('hidden'); }
      if (job.status === 'review_required' || job.status === 'completed') {
        location.href = `/jobs/${id}/review?token=${encodeURIComponent(token)}`;
        return;
      }
      if (!terminal.has(job.status)) setTimeout(refresh, 1200);
    } catch (error) { $('#job-alert').textContent = error.message; $('#job-alert').classList.remove('hidden'); setTimeout(refresh, 3500); }
  }
  $$('[data-job-action]').forEach(button => button.addEventListener('click', async () => {
    try { await api(`/api/v1/jobs/${id}/${button.dataset.jobAction}?token=${encodeURIComponent(token)}`, {method: 'POST'}); toast('任务状态已更新'); refresh(); }
    catch (error) { toast(error.message, true); }
  }));
  refresh();
}

function initReview() {
  const root = $('#review-app');
  if (!root) return;
  const id = root.dataset.jobId, token = root.dataset.token, pageCount = Number(root.dataset.pages || 0);
  let currentPage = 1, pageData = null, selected = null;
  const jobUrl = path => `/api/v1/jobs/${id}${path}${path.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`;
  function pageButton(n) { const button = document.createElement('button'); button.className = `thumbnail${n === currentPage ? ' active' : ''}`; button.dataset.page = n; button.innerHTML = `<img src="${jobUrl(`/pages/${n}/preview`)}" loading="lazy" alt="第 ${n} 页"><span>— ${String(n).padStart(2, '0')} —</span>`; button.onclick = () => loadPage(n); return button; }
  const list = $('#page-thumbnails'); for (let n = 1; n <= pageCount; n++) list.append(pageButton(n));
  async function loadPage(n) {
    currentPage = Math.max(1, Math.min(pageCount, n)); selected = null;
    pageData = await api(jobUrl(`/pages/${currentPage}`));
    $('#page-image').src = pageData.preview_url;
    $('#current-review-page').textContent = currentPage;
    $$('.thumbnail').forEach(el => el.classList.toggle('active', Number(el.dataset.page) === currentPage));
    $('#empty-editor').classList.remove('hidden'); $('#segment-editor').classList.add('hidden');
    renderBoxes();
  }
  function renderBoxes() {
    const layer = $('#segment-layer'); layer.innerHTML = '';
    pageData.segments.forEach(segment => {
      const [x0,y0,x1,y1] = segment.bbox; const el = document.createElement('button');
      el.className = `segment-box${segment.issues.some(i => !i.resolved && !i.acknowledged) ? ' issue' : ''}`;
      el.style.cssText = `left:${x0/pageData.width*100}%;top:${y0/pageData.height*100}%;width:${(x1-x0)/pageData.width*100}%;height:${(y1-y0)/pageData.height*100}%`;
      el.title = segment.source_text; el.onclick = () => selectSegment(segment, el); layer.append(el);
    });
  }
  async function selectSegment(segment, box) {
    selected = segment; $$('.segment-box').forEach(el => el.classList.remove('active')); box?.classList.add('active');
    $('#empty-editor').classList.add('hidden'); $('#segment-editor').classList.remove('hidden');
    $('#segment-id').value = segment.id; $('#source-text').value = segment.source_text; $('#target-text').value = segment.target_text || '';
    $('#segment-language').textContent = segment.source_language || 'und'; $('#segment-confidence').textContent = segment.confidence == null ? '—' : `${Math.round(segment.confidence*100)}% 置信`;
    const issueBox = $('#segment-issues'); issueBox.innerHTML = ''; segment.issues.filter(i => !i.resolved && !i.acknowledged).forEach(issue => { const chip = document.createElement('span'); chip.className = issue.severity; chip.textContent = issue.message; issueBox.append(chip); });
    const suggestions = $('#memory-suggestions'); suggestions.innerHTML = '';
    try { const rows = await api(jobUrl(`/segments/${segment.id}/suggestions`)); rows.forEach(row => { const el = document.createElement('div'); el.className='memory-suggestion'; el.textContent=`${Math.round(row.score)}% · ${row.target_text}`; el.onclick=()=>$('#target-text').value=row.target_text; suggestions.append(el); }); } catch (_) {}
  }
  $('#segment-editor').addEventListener('submit', async e => {
    e.preventDefault(); if (!selected) return;
    try {
      const issueIds = selected.issues.filter(i => !i.resolved).map(i => i.id);
      const updated = await api(jobUrl(`/segments/${selected.id}`), {method:'PATCH', body:JSON.stringify({target_text:$('#target-text').value, confirmed:true, remember:$('#remember-translation').checked, acknowledge_issue_ids:issueIds})});
      pageData.segments = pageData.segments.map(s => s.id === updated.id ? updated : s); selected = updated; renderBoxes(); toast('译文已保存并确认'); loadIssues();
    } catch (error) { toast(error.message, true); }
  });
  async function loadIssues() {
    try { const issues = await api(jobUrl('/issues')); const unresolved = issues.filter(i => !i.resolved && !i.acknowledged); $('#review-issue-count').textContent = unresolved.length; const box=$('#issue-list'); box.innerHTML=''; unresolved.forEach(issue=>{const el=document.createElement('div');el.className='issue-item';el.innerHTML=`<b>${issue.severity.toUpperCase()}</b>${issue.message}`;el.onclick=()=>{const seg=pageData?.segments.find(s=>s.id===issue.segment_id);if(seg)selectSegment(seg);};box.append(el);}); } catch (_) {}
  }
  $$('[data-render]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try { const artifact = await api(jobUrl('/render'), {method:'POST', body:JSON.stringify({mode:button.dataset.render, final:button.dataset.final==='true'})}); const link=document.createElement('a'); link.href=jobUrl(`/artifacts/${artifact.id}`); link.textContent=`下载 ${artifact.kind.replace('_',' · ')}`; $('#artifact-list').prepend(link); toast('PDF 已生成'); loadIssues(); }
    catch(error){toast(error.message,true);} finally{button.disabled=false;}
  }));
  $('#prev-page').onclick=()=>loadPage(currentPage-1); $('#next-page').onclick=()=>loadPage(currentPage+1);
  loadPage(1); loadIssues();
}

function initSettings() {
  const form = $('#provider-form'); if (!form) return;
  const payload = () => ({llm_base_url:$('#llm-base-url').value,llm_model:$('#llm-model').value,llm_api_key:$('#llm-api-key').value||null,llm_extra_json:$('#llm-extra-json').value||'{}',azure_endpoint:$('#azure-endpoint').value,azure_api_version:$('#azure-api-version').value,azure_api_key:$('#azure-api-key').value||null});
  form.addEventListener('submit', async e=>{e.preventDefault();try{await api('/api/v1/settings/providers',{method:'PUT',body:JSON.stringify(payload())});$('#settings-result').textContent='设置已保存';toast('服务设置已保存');}catch(error){toast(error.message,true);}});
  $$('[data-provider-test]').forEach(button=>button.onclick=async()=>{try{await api('/api/v1/settings/providers',{method:'PUT',body:JSON.stringify(payload())});const result=await api('/api/v1/settings/providers/test',{method:'POST',body:JSON.stringify({provider:button.dataset.providerTest})});toast(`${result.message}${result.latency_ms?` · ${result.latency_ms}ms`:''}`,!result.ok);}catch(error){toast(error.message,true);}});
}

function initLibrary() {
  if (!$('.library-shell')) return;
  $$('[data-library-tab]').forEach(button=>button.onclick=()=>{$$('[data-library-tab]').forEach(b=>b.classList.remove('active'));button.classList.add('active');$('#memory-library').classList.toggle('hidden',button.dataset.libraryTab!=='memory');$('#terms-library').classList.toggle('hidden',button.dataset.libraryTab!=='terms');});
  $('#term-form')?.addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/v1/terms',{method:'POST',body:JSON.stringify({source_language:$('#term-source-language').value,target_language:$('#term-target-language').value,source_term:$('#source-term').value,target_term:$('#target-term').value,case_sensitive:false})});location.reload();}catch(error){toast(error.message,true);}});
  $$('[data-delete-memory]').forEach(b=>b.onclick=async()=>{if(confirm('删除这条翻译记忆？')){await api(`/api/v1/memory/${b.dataset.deleteMemory}`,{method:'DELETE'});location.reload();}});
  $$('[data-delete-term]').forEach(b=>b.onclick=async()=>{if(confirm('删除这个术语？')){await api(`/api/v1/terms/${b.dataset.deleteTerm}`,{method:'DELETE'});location.reload();}});
  $('#memory-import-form input')?.addEventListener('change',async e=>{const data=new FormData();data.append('file',e.target.files[0]);const response=await fetch('/api/v1/memory/import',{method:'POST',body:data});if(response.ok)location.reload();else toast('导入失败',true);});
}

document.addEventListener('DOMContentLoaded',()=>{initUpload();initJob();initReview();initSettings();initLibrary();});
