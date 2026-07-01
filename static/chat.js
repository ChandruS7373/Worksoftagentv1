/* ============================================================
   chat.js  —  full chat + popup escalation flow
============================================================ */

// ── Session data ────────────────────────────────────────────
const userName    = sessionStorage.getItem('userName')    || 'Alex Johnson';
const userEmail   = sessionStorage.getItem('userEmail')   || '';
const companyName = sessionStorage.getItem('companyName') || 'Acme Corp';
const welcomeMsg  = sessionStorage.getItem('welcomeMsg')  || '';

// ── DOM refs ────────────────────────────────────────────────
const chatScroll      = document.getElementById('chatScroll');
const chatInner       = document.getElementById('chatInner');
const dateStamp       = document.getElementById('dateStamp');
const msgInput        = document.getElementById('msgInput');
const sendBtn         = document.getElementById('sendBtn');
const feedbackBar     = document.getElementById('feedbackBar');
const btnResolved     = document.getElementById('btnResolved');
const btnNotResolved  = document.getElementById('btnNotResolved');
const uploadZone      = document.getElementById('uploadZone');
const fileInput       = document.getElementById('fileInput');
const filePreview     = document.getElementById('filePreview');
const analyzeRow      = document.getElementById('analyzeRow');
const analyzeBtn      = document.getElementById('analyzeBtn');
const toggleUploadBtn = document.getElementById('toggleUploadBtn');
const inputBar        = document.querySelector('.inputbar');

// overlays
const resOverlay      = document.getElementById('resOverlay');
const resIcon         = document.getElementById('resIcon');
const resTitle        = document.getElementById('resTitle');
const resBody         = document.getElementById('resBody');
const resBtns         = document.getElementById('resBtns');
const escOverlay      = document.getElementById('escOverlay');
const escBackBtn      = document.getElementById('escBackBtn');
const escSubmitBtn    = document.getElementById('escSubmitBtn');
const escErrMsg       = document.getElementById('escErrMsg');
const ticketOverlay   = document.getElementById('ticketOverlay');
const ticketContent   = document.getElementById('ticketContent');
const ticketNewChat   = document.getElementById('ticketNewChat');
const ticketBackChat  = document.getElementById('ticketBackChat');
const resolvedOverlay = document.getElementById('resolvedOverlay');
const resolvedCloseBtn= document.getElementById('resolvedCloseBtn');

// ── State ────────────────────────────────────────────────────
let isBotTyping   = false;
let uploadVisible = false;
let popupShown    = false;
let pendingFile   = null;
let fileAnalyzed  = false;   // true after Analyze button used

// ── Navbar ───────────────────────────────────────────────────
function initials(n){
  const p=n.trim().split(' ');
  return p.length>=2?(p[0][0]+p[p.length-1][0]).toUpperCase():n.slice(0,2).toUpperCase();
}
document.getElementById('navName').textContent    = userName;
document.getElementById('navCompany').textContent = companyName;
document.getElementById('navAvatar').textContent  = initials(userName);

// ── Date stamp ───────────────────────────────────────────────
const now = new Date();
dateStamp.textContent = 'Today, ' + now.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});

// ── Layout ───────────────────────────────────────────────────
function updateLayout(){
  const ih = inputBar.offsetHeight;
  const fh = feedbackBar.style.display!=='none' ? feedbackBar.offsetHeight : 0;
  const uh = uploadZone.style.display!=='none'  ? uploadZone.offsetHeight  : 0;
  uploadZone.style.bottom  = ih+'px';
  feedbackBar.style.bottom = (ih+uh)+'px';
  chatScroll.style.bottom  = (ih+uh+fh)+'px';
}
window.addEventListener('resize', updateLayout);
updateLayout();

function scrollBottom(){ setTimeout(()=>{ chatScroll.scrollTop=chatScroll.scrollHeight; },60); }

// ── Markdown renderer ────────────────────────────────────────
function md(text){
  // escape HTML first
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

  // inline formatting applied to a single line of text
  function inline(s){
    return esc(s)
      .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
      .replace(/\*(.+?)\*/g,'<em>$1</em>')
      .replace(/`([^`]+)`/g,'<code style="background:#f0f4ff;color:#1d4ed8;padding:1px 6px;border-radius:4px;font-size:12px;font-family:monospace;">$1</code>');
  }

  const lines = text.split('\n');
  const out = [];
  let inOl=false, inUl=false, tableRows=[], inTable=false;

  function flushLists(){
    if(inOl){ out.push('</ol>'); inOl=false; }
    if(inUl){ out.push('</ul>'); inUl=false; }
  }
  function flushTable(){
    if(!inTable || !tableRows.length) return;
    inTable=false;
    let html='<table style="border-collapse:collapse;width:100%;margin:8px 0;font-size:13px;">';
    tableRows.forEach((row,i)=>{
      const cells=row.split('|').map(c=>c.trim()).filter((_,ci,a)=>ci>0&&ci<a.length-1);
      if(!cells.length) return;
      html+='<tr>';
      cells.forEach(c=>{
        const tag = i===0 ? 'th' : 'td';
        const style = i===0
          ? 'background:#eff6ff;color:#1e40af;font-weight:700;padding:6px 10px;border:1px solid #bfdbfe;text-align:left;'
          : 'padding:5px 10px;border:1px solid #e5e7eb;color:#111827;';
        html+=`<${tag} style="${style}">${inline(c)}</${tag}>`;
      });
      html+='</tr>';
    });
    html+='</table>';
    out.push(html);
    tableRows=[];
  }

  for(let i=0;i<lines.length;i++){
    const ln=lines[i];
    const raw=ln.trim();

    // blank line
    if(!raw){ flushLists(); flushTable(); continue; }

    // table row
    if(raw.startsWith('|') && raw.endsWith('|')){
      flushLists();
      // skip separator rows like |---|---|
      if(/^\|[-| :]+\|$/.test(raw)){ inTable=true; continue; }
      inTable=true;
      tableRows.push(raw);
      continue;
    } else {
      flushTable();
    }

    // headings
    const h3=raw.match(/^###\s+(.*)/);
    const h2=raw.match(/^##\s+(.*)/);
    const h1=raw.match(/^#\s+(.*)/);
    if(h1){ flushLists(); out.push(`<p style="font-size:15px;font-weight:700;color:#1e40af;margin:10px 0 4px;">${inline(h1[1])}</p>`); continue; }
    if(h2){ flushLists(); out.push(`<p style="font-size:14px;font-weight:700;color:#1e40af;margin:8px 0 3px;">${inline(h2[1])}</p>`); continue; }
    if(h3){ flushLists(); out.push(`<p style="font-size:13px;font-weight:700;color:#374151;margin:6px 0 2px;">${inline(h3[1])}</p>`); continue; }

    // horizontal rule
    if(/^---+$/.test(raw)){ flushLists(); out.push('<hr style="border:none;border-top:1px solid #e5e7eb;margin:8px 0;">'); continue; }

    // numbered list
    const ol=raw.match(/^(\d+)[\.\)]\s+(.*)/);
    if(ol){
      if(inUl){ out.push('</ul>'); inUl=false; }
      if(!inOl){ out.push('<ol style="margin:6px 0 6px 20px;padding:0;">'); inOl=true; }
      out.push(`<li style="margin:4px 0;line-height:1.6;">${inline(ol[2])}</li>`);
      continue;
    }

    // bullet list (-, •, *)
    const ul=raw.match(/^[-•*]\s+(.*)/);
    if(ul){
      if(inOl){ out.push('</ol>'); inOl=false; }
      if(!inUl){ out.push('<ul style="margin:6px 0 6px 20px;padding:0;">'); inUl=true; }
      out.push(`<li style="margin:4px 0;line-height:1.6;">${inline(ul[1])}</li>`);
      continue;
    }

    // indented sub-bullet (2+ spaces or tab + - / •)
    const sub=raw.match(/^[-•]\s+(.*)/);
    if(sub && (ln.startsWith('  ') || ln.startsWith('\t'))){
      if(!inUl && !inOl){ out.push('<ul style="margin:4px 0 4px 28px;padding:0;">'); inUl=true; }
      out.push(`<li style="margin:2px 0;line-height:1.5;color:#4b5563;">${inline(sub[1])}</li>`);
      continue;
    }

    // plain paragraph
    flushLists();
    out.push(`<p style="margin:0 0 6px;line-height:1.65;">${inline(raw)}</p>`);
  }

  flushLists();
  flushTable();
  return out.join('');
}

// ── Append bubble ────────────────────────────────────────────
function appendMsg(text, role){
  const row = document.createElement('div');
  row.className = `msg-row ${role}`;

  if(role === 'assistant'){
    // meta row: avatar + label
    const meta = document.createElement('div');
    meta.className = 'msg-meta';
    const av = document.createElement('div');
    av.className = 'msg-av bot';
    av.textContent = '🤖';
    const lbl = document.createElement('span');
    lbl.className = 'msg-label';
    lbl.textContent = 'AI Assistant';
    meta.appendChild(av);
    meta.appendChild(lbl);
    row.appendChild(meta);

    const bub = document.createElement('div');
    bub.className = 'bot-text';
    bub.innerHTML = md(text);
    row.appendChild(bub);
  } else {
    const bub = document.createElement('div');
    bub.className = 'user-bubble';
    bub.innerHTML = `<p style="margin:0;">${text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</p>`;
    row.appendChild(bub);
  }

  chatInner.appendChild(row);
  scrollBottom();
  return row.querySelector('.bot-text, .user-bubble');
}

// ── Typing dots ──────────────────────────────────────────────
function showTyping(){
  const row = document.createElement('div');
  row.className = 'msg-row assistant'; row.id = 'typingRow';
  const meta = document.createElement('div'); meta.className = 'msg-meta';
  const av = document.createElement('div'); av.className = 'msg-av bot'; av.textContent = '🤖';
  const lbl = document.createElement('span'); lbl.className = 'msg-label'; lbl.textContent = 'AI Assistant';
  meta.appendChild(av); meta.appendChild(lbl); row.appendChild(meta);
  const bub = document.createElement('div'); bub.className = 'bot-text';
  bub.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
  row.appendChild(bub);
  chatInner.appendChild(row); scrollBottom();
}
function hideTyping(){ const t=document.getElementById('typingRow'); if(t)t.remove(); }

// ── Feedback bar ─────────────────────────────────────────────
function showFeedback(){ feedbackBar.style.display='block'; updateLayout(); scrollBottom(); }
function hideFeedback(){ feedbackBar.style.display='none'; updateLayout(); }

// ── Upload panel ──────────────────────────────────────────────
function showUpload(){
  uploadZone.style.display='block';
  toggleUploadBtn.style.display='inline-block';
  toggleUploadBtn.textContent='Hide upload panel';
  uploadVisible=true; updateLayout(); scrollBottom();
}
function hideUpload(){
  uploadZone.style.display='none';
  toggleUploadBtn.style.display='none';
  filePreview.innerHTML='';
  analyzeRow.style.display='none';
  pendingFile=null; uploadVisible=false; updateLayout();
}
toggleUploadBtn.addEventListener('click',()=> uploadVisible?hideUpload():showUpload());

// ── Input lock ────────────────────────────────────────────────
function setDisabled(v){ msgInput.disabled=v; sendBtn.disabled=v; isBotTyping=v; }

// ════════════════════════════════════════════════════════════
//  SEND (streaming SSE)
// ════════════════════════════════════════════════════════════
async function sendMessage(text, file){
  if((!text&&!file)||isBotTyping) return;
  hideFeedback();
  if(text) appendMsg(text,'user');
  setDisabled(true);

  const fetchOpts = file
    ? (()=>{ const fd=new FormData(); fd.append('message',text||''); fd.append('file',file); return {method:'POST',body:fd}; })()
    : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message:text})};

  showTyping();

  try {
    const resp = await fetch('/api/chat', fetchOpts);
    if(!resp.ok) throw new Error(`Server error ${resp.status}`);

    const data = await resp.json();
    hideTyping();

    if(data.reply){
      appendMsg(data.reply, 'assistant');
    }

    if(data.show_popup && !popupShown){
      popupShown=true;
      showResolutionPopup(data.popup_reason||'resolved');
    } else if(fileAnalyzed){
      showPostAnalyzePopup();
    } else {
      showFeedback();
    }
  } catch(err){
    if(err.name!=='AbortError'){ hideTyping(); appendMsg('⚠️ Connection error. Please try again.','assistant'); }
  } finally {
    setDisabled(false); msgInput.focus();
  }
}

// Input handlers
sendBtn.addEventListener('click',()=>{
  const t=msgInput.value.trim(); if(t){ msgInput.value=''; sendMessage(t); }
});
msgInput.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){
    e.preventDefault(); const t=msgInput.value.trim(); if(t){ msgInput.value=''; sendMessage(t); }
  }
});

// ── Feedback buttons ──────────────────────────────────────────
btnResolved.addEventListener('click', async ()=>{
  hideFeedback();
  await fetch('/api/resolve',{method:'POST'});
  resolvedOverlay.style.display='flex';
});

btnNotResolved.addEventListener('click',()=>{
  hideFeedback();
  showUpload();
  appendMsg(
    "No problem! Please upload a screenshot or error log below and I'll take a closer look. " +
    "This helps me diagnose the exact issue much faster.", 'assistant'
  );
});

resolvedCloseBtn.addEventListener('click',()=>{
  resolvedOverlay.style.display='none';
  appendMsg('🎉 Your issue has been marked as resolved. Feel free to reach out anytime!','assistant');
});

// ── File input ─────────────────────────────────────────────────
fileInput.addEventListener('change',()=>{
  const file=fileInput.files[0]; if(!file) return;
  pendingFile=file; filePreview.innerHTML='';
  if(file.type.startsWith('image/')){
    const img=document.createElement('img');
    img.src=URL.createObjectURL(file);
    img.onload=()=>URL.revokeObjectURL(img.src);
    filePreview.appendChild(img);
  } else {
    const d=document.createElement('div'); d.className='file-info';
    d.textContent=`✅ File received: ${file.name}`;
    filePreview.appendChild(d);
  }
  analyzeRow.style.display='block'; updateLayout(); scrollBottom();
});

// ── Analyze ───────────────────────────────────────────────────
analyzeBtn.addEventListener('click', async ()=>{
  if(!pendingFile) return;
  analyzeBtn.disabled=true; analyzeBtn.textContent='⏳ Analysing…';
  const file=pendingFile;
  hideUpload();
  fileAnalyzed=true;                         // ← flag so we show popup after
  const userText=msgInput.value.trim()||'Please analyze this file and help me fix the issue.';
  msgInput.value='';
  await sendMessage(userText, file);
  fileAnalyzed=false;                        // ← reset
  analyzeBtn.disabled=false; analyzeBtn.textContent='🔍 Analyze File';
});

// ════════════════════════════════════════════════════════════
//  POPUP after file analysis — "Was this resolved?"
// ════════════════════════════════════════════════════════════
function showPostAnalyzePopup(){
  resIcon.textContent  = '🔍';
  resTitle.textContent = 'Did the analysis resolve your issue?';
  resBody.textContent  = 'If the solution above worked, great! If not, we can raise a support ticket for you.';
  resBtns.innerHTML='';

  const b1=document.createElement('button'); b1.className='btn-purple';
  b1.textContent='✅  Yes, Issue Resolved';
  b1.onclick=async()=>{
    resOverlay.style.display='none';
    await fetch('/api/resolve',{method:'POST'});
    resolvedOverlay.style.display='flex';
  };

  const b2=document.createElement('button'); b2.className='btn-red';
  b2.style.cssText='padding:11px 22px;background:#e53935;color:#fff;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;flex:1;';
  b2.textContent='❌  No, Raise a Ticket';
  b2.onclick=()=>{ resOverlay.style.display='none'; openEscalation(); };

  const b3=document.createElement('button'); b3.className='btn-gray';
  b3.textContent='💬  Keep chatting';
  b3.onclick=()=>{ resOverlay.style.display='none'; popupShown=false; showFeedback(); };

  resBtns.appendChild(b1); resBtns.appendChild(b2); resBtns.appendChild(b3);
  resOverlay.style.display='flex';
}

// ════════════════════════════════════════════════════════════
//  RESOLUTION POPUP  (AI-triggered after multi-turn chat)
// ════════════════════════════════════════════════════════════
function showResolutionPopup(reason){
  const stuck = reason==='stuck';
  resIcon.textContent  = stuck ? '⚠️' : '🔍';
  resTitle.textContent = stuck ? 'Looks like this needs WorkSoft Support Engineers' : 'Did this resolve your issue?';
  resBody.textContent  = stuck
    ? "We've tried several steps but the issue persists. Would you like to escalate to the Support Engineers, or keep chatting?"
    : 'Confirm below — or keep chatting if you still need help.';

  resBtns.innerHTML='';

  const b1=document.createElement('button'); b1.className='btn-purple';
  b1.textContent='✅  Yes, Resolved';
  b1.onclick=async()=>{
    resOverlay.style.display='none';
    await fetch('/api/resolve',{method:'POST'});
    resolvedOverlay.style.display='flex';
  };

  const b2=document.createElement('button');
  b2.style.cssText=`padding:11px 22px;background:${stuck?'#e53935':'#f3f4f6'};color:${stuck?'#fff':'#374151'};border:${stuck?'none':'1px solid #e5e7eb'};border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;flex:1;`;
  b2.textContent='🔺  No, Forward to L2';
  b2.onclick=()=>{ resOverlay.style.display='none'; openEscalation(); };

  const b3=document.createElement('button'); b3.className='btn-gray';
  b3.textContent='💬  Not yet — keep chatting';
  b3.onclick=()=>{ resOverlay.style.display='none'; popupShown=false; showFeedback(); };

  resBtns.appendChild(b1); resBtns.appendChild(b2); resBtns.appendChild(b3);
  resOverlay.style.display='flex';
}

// ════════════════════════════════════════════════════════════
//  ESCALATION POPUP  (matches screenshot — full form)
// ════════════════════════════════════════════════════════════
function openEscalation(){
  document.getElementById('escName').value     = (userName && userName!=='Alex Johnson') ? userName : '';
  document.getElementById('escEmail').value    = userEmail || '';
  document.getElementById('escExtra').value    = '';
  document.getElementById('escPriority').value = 'High';
  escErrMsg.textContent = '';
  escOverlay.style.display = 'flex';
}

escBackBtn.addEventListener('click',()=>{
  escOverlay.style.display='none';
  showFeedback();
});

escSubmitBtn.addEventListener('click', async ()=>{
  const name     = document.getElementById('escName').value.trim();
  const email    = document.getElementById('escEmail').value.trim();
  const extra    = document.getElementById('escExtra').value.trim();
  const priority = document.getElementById('escPriority').value;

  escSubmitBtn.disabled=true;
  escSubmitBtn.textContent='⏳ Raising ticket…';
  escErrMsg.textContent='';

  try{
    const res  = await fetch('/api/escalate',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,email,extra,priority}),
    });
    const data = await res.json();
    escOverlay.style.display='none';
    showTicketResult(data);
  } catch(err){
    escErrMsg.textContent='❌ ' + err.message;
  } finally{
    escSubmitBtn.disabled=false;
    escSubmitBtn.textContent='🚀 Raise Ticket & Notify IT Admin';
  }
});

// ════════════════════════════════════════════════════════════
//  TICKET RESULT POPUP
// ════════════════════════════════════════════════════════════
function showTicketResult(data){
  const t       = data.ticket||{};
  const caseNum = t.case_number||t.id||'N/A';
  const caseUrl = t.url||'#';
  const priority= t.priority||'High';
  const created = t.created_at||new Date().toLocaleString();
  const emailOk = data.email_ok;
  const itAdmin = data.it_admin||'';

  const pBg  = {Critical:'rgba(220,38,38,.12)',High:'rgba(220,38,38,.1)',Medium:'rgba(217,119,6,.1)',Low:'rgba(22,163,74,.1)'};
  const pClr = {Critical:'#f87171',High:'#ef4444',Medium:'#d97706',Low:'#16a34a'};

  const btnBase = `display:flex;align-items:center;justify-content:center;gap:7px;
                   flex:1;min-width:130px;padding:10px 16px;border-radius:10px;
                   text-decoration:none;font-size:13px;font-weight:700;`;

  const sfLink = caseUrl!=='#sf-not-configured'
    ? `<a href="${caseUrl}" target="_blank"
          style="${btnBase}background:#534AB7;color:#fff;box-shadow:0 2px 8px rgba(83,74,183,.3);">
         🔗 View in Salesforce
       </a>`
    : '';

  const slackUrl = data.slack_url||'';
  const slackLink = slackUrl
    ? `<a href="${slackUrl}" target="_blank"
          style="${btnBase}background:#4a154b;color:#fff;box-shadow:0 2px 8px rgba(74,21,75,.3);">
         <svg width="15" height="15" viewBox="0 0 54 54" fill="none" style="flex-shrink:0"><path d="M19.7 33.7a4 4 0 1 1-4-4h4v4z" fill="#E01E5A"/><path d="M21.7 33.7a4 4 0 0 1 8 0v10a4 4 0 0 1-8 0v-10z" fill="#E01E5A"/><path d="M25.7 19.7a4 4 0 1 1 4-4v4h-4z" fill="#36C5F0"/><path d="M25.7 21.7a4 4 0 0 1 0 8H15.7a4 4 0 0 1 0-8h10z" fill="#36C5F0"/><path d="M39.7 25.7a4 4 0 1 1 4 4h-4v-4z" fill="#2EB67D"/><path d="M37.7 25.7a4 4 0 0 1-8 0v-10a4 4 0 0 1 8 0v10z" fill="#2EB67D"/><path d="M33.7 39.7a4 4 0 1 1-4 4v-4h4z" fill="#ECB22E"/><path d="M33.7 37.7a4 4 0 0 1 0-8h10a4 4 0 0 1 0 8h-10z" fill="#ECB22E"/></svg>
         View in Slack
       </a>`
    : '';

  ticketContent.innerHTML=`
    <div style="margin-bottom:18px;">
      <div style="font-size:10px;font-weight:700;color:#534AB7;letter-spacing:.6px;text-transform:uppercase;margin-bottom:4px;">Salesforce Case</div>
      <div style="font-size:28px;font-weight:900;color:#111827;">#${caseNum}</div>
    </div>

    <div class="ticket-row">
      <span class="ticket-lbl">Priority</span>
      <span style="background:${pBg[priority]||pBg.High};color:${pClr[priority]||pClr.High};
            font-size:11px;font-weight:700;padding:3px 12px;border-radius:99px;">${priority}</span>
    </div>
    <div class="ticket-row">
      <span class="ticket-lbl">Status</span>
      <span style="background:#ede9fe;color:#534AB7;font-size:10px;font-weight:700;
            padding:3px 10px;border-radius:99px;text-transform:uppercase;letter-spacing:.4px;">New</span>
    </div>
    <div class="ticket-row">
      <span class="ticket-lbl">Created</span>
      <span class="ticket-val">${created}</span>
    </div>
    ${itAdmin?`
    <div class="ticket-row">
      <span class="ticket-lbl">IT Admin</span>
      <span class="ticket-val">${itAdmin}</span>
    </div>`:''}
    ${t.sf_error?`
    <div style="margin-top:10px;padding:9px 12px;background:#fef2f2;border:1px solid #fecaca;
         border-radius:8px;font-size:12px;color:#b91c1c;">⚠️ ${t.sf_error}</div>`:''}
    <div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:16px;">
      ${sfLink}
      ${slackLink}
    </div>
    <div style="margin-top:12px;padding:10px 14px;border-radius:9px;font-size:12.5px;font-weight:500;
         background:${emailOk?'#f0fdf4':'#fffbeb'};
         color:${emailOk?'#15803d':'#92400e'};
         border:1px solid ${emailOk?'#bbf7d0':'#fde68a'};">
      ${emailOk
        ? `✅ Email sent to IT Admin${itAdmin?' ('+itAdmin+')':''}`
        : '⚠️ Email failed — ticket still created in Salesforce.'}
    </div>
    <div style="margin-top:12px;font-size:12px;color:#6b7280;line-height:1.7;text-align:center;padding:0 8px;">
      Your ticket is with the IT Admin team.<br>Check your email for the Salesforce case confirmation.
    </div>`;

  ticketOverlay.style.display='flex';
}

ticketNewChat.addEventListener('click',()=>{
  ticketOverlay.style.display='none';
  sessionStorage.clear();
  window.location.href='/';
});
ticketBackChat.addEventListener('click',()=>{ ticketOverlay.style.display='none'; });

// close overlays on backdrop click (except escalation form)
[resOverlay, resolvedOverlay].forEach(ov=>{
  ov.addEventListener('click',e=>{ if(e.target===ov) ov.style.display='none'; });
});

// ════════════════════════════════════════════════════════════
//  INIT
// ════════════════════════════════════════════════════════════
(function(){
  updateLayout();
  appendMsg(
    welcomeMsg ||
    `Hello ${userName.split(' ')[0]}! I'm your AI Support Assistant. ` +
    `How can I help you today? Describe any issue with CTM, Certify, Portal, or Capture.`,
    'assistant'
  );
})();






