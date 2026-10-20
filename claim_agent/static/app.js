const $ = id => document.getElementById(id);
let packet = null, selected = null, socket = null;
let comparisonBusy = false, comparisonSweep = null;
const display = (v, suffix='') => v === null || v === undefined ? 'Unknown' : `${Number(v).toLocaleString(undefined,{maximumFractionDigits:2})}${suffix}`;
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) {
    const detail=data.detail;
    const error=new Error(typeof detail==='string'?detail:detail?.message || `Request failed (${response.status})`);
    error.code=detail?.code;error.status=response.status;
    throw error;
  }
  return data;
}
function message(text) { $('message').textContent = text; }
function text(tag, value, cls) { const n=document.createElement(tag); n.textContent=value; if(cls)n.className=cls; return n; }
async function paired() { $('pair-panel').hidden=true; $('workspace').hidden=false; await refresh(); }
async function refresh() {
  try {
    const sweeps=await api('/api/sweeps'), old=$('sweep-select').value;
    $('sweep-select').replaceChildren(new Option('Choose a sweep',''));
    for (const s of sweeps) $('sweep-select').add(new Option(`${s.synthetic?'TEST · ':''}${new Date(s.captured_at).toLocaleString()} · ${s.book_count} books · ${s.completion}`,s.id));
    if(old && sweeps.some(s=>s.id===old)) $('sweep-select').value=old;
    else if(sweeps.length) { $('sweep-select').value=sweeps[0].id; await choose(sweeps[0].id); }
  } catch(e) { message(e.message); }
}
async function choose(id) {
  if(!id)return;
  try {
    render(await api(`/api/sweeps/${id}`));
    loadCameraComparison(id);
    if(socket)socket.close();
    socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/api/sweeps/${id}/live`);
    socket.onmessage=({data})=>{
      const event=JSON.parse(data);
      if(event.packet) render(event.packet);
      if(event.type==='comparison_ready' && event.report) renderCameraComparison(event.report);
      if(event.type==='second_locale' && event.result) {$('second-result').hidden=false;$('second-result').textContent=JSON.stringify(event.result,null,2);}
      if(['guidance','voice_error','recognition_status'].includes(event.type)) $('guidance').textContent=event.text;
      if(event.type==='transcript') {
        $('transcript').append(text('p',`${event.role==='user'?'You':'Agent'}: ${event.text}`));
        while($('transcript').children.length>100)$('transcript').firstChild.remove();
        $('transcript').scrollTop=$('transcript').scrollHeight;
      }
    };
    socket.onclose=()=>{ $('guidance').textContent='Live connection closed. Refresh to reconnect; saved evidence remains available.'; };
    message('');
  } catch(e) {message(e.message);}
}
function cell(row,main,secondary) { const c=text('td',main); if(secondary)c.append(text('span',secondary,'sub')); row.append(c); }
function render(p) {
  packet=p; updateComparisonButton(); $('empty').hidden=true; $('inventory').hidden=false; $('synthetic').hidden=!p.sweep.synthetic;
  $('status').textContent=p.completion; $('sweep-info').textContent=`${p.sweep.country} · ${p.sweep.currency} · revision ${p.revision}`;
  const cost=p.metrics?.cost_usd;
  $('api-cost').textContent=`API usage: ${cost==null?'unavailable':'$'+Number(cost).toFixed(4)+' USD'} · ${p.metrics?.cost_status||'unavailable'} · ${p.metrics?.duplicate_images_suppressed||0} repeat photos skipped. Hosting and device costs excluded.`;
  $('report-link').href=`/api/sweeps/${p.sweep.id}/report.html`; $('packet-link').href=`/api/sweeps/${p.sweep.id}/packet.zip`;
  $('book-count').textContent=p.totals.book_count; $('identified-count').textContent=`${p.totals.books_identified} identified · ${p.totals.books_unidentified} unreadable`;
  $('shelf-run').textContent=display(p.totals.shelf_run_m,' m'); $('book-value').textContent=`${p.sweep.currency} ${display(p.totals.books_replacement_cost)}`;
  $('price-coverage').textContent=`${p.totals.replacement_prices_missing} replacement prices missing`;
  $('floor-area').textContent=display(p.room.floor_area_m2,' m²'); $('wall-area').textContent=`Gross walls: ${display(p.room.wall_area_m2,' m²')}`;
  $('books').replaceChildren();
  for(const b of p.books) {
    const row=document.createElement('tr'); row.onclick=()=>detail(b);
    cell(row,b.title || 'Book',`${b.author} · ${b.shelf || 'Shelf unknown'} · ${b.id}`);
    cell(row,`${display(b.spine_height_cm)} × ${display(b.spine_thickness_cm)} cm`);
    cell(row,`${display(b.replacement_cost.amount)} / ${display(b.used_value.amount)}`,p.sweep.currency);
    cell(row,b.excluded?'Excluded by owner':b.status.replaceAll('_',' ')); $('books').append(row);
  }
  $('items').replaceChildren();
  for(const i of p.items) {
    const row=document.createElement('tr'); row.onclick=()=>detail(i);
    cell(row,i.category,i.brand_model || i.description); cell(row,`${display(i.dimensions_cm.w)} × ${display(i.dimensions_cm.h)} × ${display(i.dimensions_cm.d)} cm`);
    cell(row,`${display(i.replacement_cost.low)} – ${display(i.replacement_cost.high)}`,p.sweep.currency);
    cell(row,i.excluded?'Excluded by owner':i.status.replaceAll('_',' ')); $('items').append(row);
  }
  $('review-count').textContent=`(${p.review_queue.length})`; $('review-list').replaceChildren();
  for(const r of p.review_queue){const li=text('li',`${r.ref_id}: ${r.reason}`);li.onclick=()=>{const x=[...p.books,...p.items].find(x=>x.id===r.ref_id);if(x)detail(x);};$('review-list').append(li);}
}
function detail(record) {
  selected=record; $('detail-title').textContent=record.title || record.category || 'Book';
  $('detail-frame').src=`/api/sweeps/${packet.sweep.id}/${record.frame_ref}`;
  $('detail-facts').textContent=JSON.stringify(record,null,2);
  $('confirm-print').hidden=!record.category || !/art|portrait|painting/i.test(record.category) || record.is_print;
  $('detail').showModal();
}
async function correct(action,value) {
  try {render(await api(`/api/sweeps/${packet.sweep.id}/corrections`,{action,ref_id:selected.id,value}));$('detail').close();message('Correction saved. Dependent valuations have been invalidated.');}catch(e){message(e.message);}
}
$('refresh').onclick=refresh; $('sweep-select').onchange=e=>choose(e.target.value);
$('close-detail').onclick=()=>$('detail').close();
$('exclude-item').onclick=()=>correct('exclude','Owner says this item is not theirs'); $('confirm-print').onclick=()=>correct('confirm_print','Owner confirms it is a print');
(async()=>{
  try {const h=await api('/api/health');$('health').textContent=h.model_ready?'Model access configured':'API key needed for live AI';}catch(e){$('health').textContent='Server unavailable';}
  const fragment=new URLSearchParams(location.hash.slice(1)); const token=fragment.get('token'), sweep=fragment.get('sweep');
  if(location.hash)history.replaceState(null,'',location.pathname);
  try {if(token)await api('/api/pair',{token});await api('/api/sweeps');await paired();if(sweep){$('sweep-select').value=sweep;await choose(sweep);}}catch(e){if(token)message(e.message);}
})();


function updateComparisonButton() {
  const eligible=packet && ['ready','partial'].includes(packet.completion) && (packet.sweep.capture_mode || 'lidar')==='lidar';
  $('camera-comparison-run').disabled=comparisonBusy || !eligible;
  $('camera-comparison-panel').hidden=packet?.sweep.capture_mode==='camera';
}
async function loadCameraComparison(id) {
  if(comparisonSweep!==id) {
    comparisonSweep=id;
    $('camera-comparison-result').hidden=true;
    $('camera-comparison-status').textContent='Available after a LiDAR sweep finishes. Existing saved comparisons can be reopened without another analysis.';
  }
  try { const result=await api(`/api/sweeps/${id}/camera-comparison`); if(packet?.sweep.id===id)renderCameraComparison(result); }
  catch(e) { /* A comparison is optional and may not have been requested. */ }
}
function renderCameraComparison(report) {
  const root=$('camera-comparison-result'); root.replaceChildren(); root.hidden=false;
  $('camera-comparison-status').textContent=report.status==='running'?'Independent camera analysis is running. Refresh the sweep to check its saved status.':report.status==='failed'?report.error:'Saved same-frame depth ablation. Reopening it does not run the models again.';
  if(report.analysis_status==='failed')root.append(text('p','Camera analysis failed: no frames were processed successfully. Inventory counts remain unknown.','notice'));
  if(report.synthetic)root.append(text('p','SYNTHETIC TEST — these are test inputs, not real-room accuracy.','notice'));
  if(report.status==='complete' && report.lidar && report.camera) {
    const table=document.createElement('table'), head=document.createElement('tr');
    for(const label of ['Measure','LiDAR response','Camera response'])head.append(text('th',label));
    table.append(head);
    const rows=[['Book candidates',report.lidar.book_count,report.camera.book_count],['Object candidates',report.lidar.object_count,report.camera.object_count],['Identified book candidates',report.lidar.identity_coverage.identified_books,report.camera.identity_coverage.identified_books],['Book candidates with spine dimensions',report.lidar.measurement_coverage.books_with_both_spine_dimensions,report.camera.measurement_coverage.books_with_both_spine_dimensions],['Frames processed',report.lidar.frames_processed,report.camera.frames_processed],['Review needs',report.lidar.review_count,report.camera.review_count],['Floor area (m²)',report.lidar.room.floor_area_m2.value,report.camera.room.floor_area_m2.value],['Gross wall area (m²)',report.lidar.room.wall_area_m2.value,report.camera.room.wall_area_m2.value],['Usage cost (USD)',report.lidar.cost_usd,report.camera.cost_usd]];
    for(const [label,lidar,camera] of rows){const row=document.createElement('tr');cell(row,label);cell(row,display(lidar));cell(row,display(camera));table.append(row);}
    root.append(table);
    root.append(text('p',`LiDAR capture-end to packet: ${display(report.timing.lidar_time_to_packet_s,' s')}. Saved-frame camera replay: ${display(report.timing.camera_replay_wall_s,' s')}. Timing bases differ; these are not a speed benchmark.`));
    for(const [name,summary] of [['LiDAR',report.lidar],['Camera',report.camera]]) {
      const details=document.createElement('details');details.append(text('summary',`${name} evidence-based response`));
      details.append(text('pre',JSON.stringify(summary.response,null,2)));root.append(details);
    }
  }
  for(const note of report.limitations || [])root.append(text('p',note,'muted'));
  const details=document.createElement('details');details.append(text('summary','Full comparison, provenance and review needs'));details.append(text('pre',JSON.stringify(report,null,2)));root.append(details);
}
$('camera-comparison-run').onclick=async()=>{
  const id=packet?.sweep.id;if(!id)return;
  comparisonBusy=true;updateComparisonButton();
  $('camera-comparison-status').textContent='Running an independent camera analysis using the saved frames…';
  try { const result=await api(`/api/sweeps/${id}/camera-comparison`,{});if(packet?.sweep.id===id)renderCameraComparison(result); }
  catch(e){
    if(packet?.sweep.id===id) {
      if(e.code==='already_running') {
        try { const saved=await api(`/api/sweeps/${id}/camera-comparison`);if(packet?.sweep.id===id)renderCameraComparison(saved); }
        catch(readError){if(packet?.sweep.id===id)$('camera-comparison-status').textContent=`${readError.message}. Try comparison again to read its saved result.`;}
      } else $('camera-comparison-status').textContent=e.code==='capacity'?`${e.message}. Try comparison again when active work finishes.`:e.message;
    }
  }
  finally{comparisonBusy=false;updateComparisonButton();}
};
