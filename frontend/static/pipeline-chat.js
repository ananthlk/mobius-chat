// ── Floating chat drawer ──────────────────────────────────────
let _chatThreadId = null;
let _chatSending  = false;
let _chatUnread   = 0;

// Configurable context function — pages can override via initMobiusChatWidget()
let _chatContextFn = null;
// Page label shown in the pane header (e.g. "Roster", "Pipeline")
let _chatPageLabel = 'this org';

/**
 * _ensureChatWidget(opts)
 *
 * Self-injects the #chatDrawer HTML and the pipeline.css stylesheet into any
 * page that loads pipeline-chat.js.  Call this once at page-init time.
 *
 * opts.contextFn  — () => string  — returns a context hint prepended to every
 *                   message sent to the LLM.  Defaults to reading lastRun.
 * opts.pageName   — string label shown in the pane header (default "this org")
 * opts.placeholder — textarea placeholder text
 *
 * If #chatDrawer already exists in the DOM (i.e. pipeline.html which has it
 * inline) this is a no-op so no double-injection happens.
 */
function initMobiusChatWidget(opts = {}) {
  // Respect caller options
  if (typeof opts.contextFn === 'function') _chatContextFn = opts.contextFn;
  if (opts.pageName) _chatPageLabel = opts.pageName;
  const placeholder = opts.placeholder || `Ask about ${_chatPageLabel}…`;

  // ── 1. Ensure pipeline.css is loaded (idempotent) ───────────────────
  if (!document.querySelector('link[href*="pipeline.css"]')) {
    const link = document.createElement('link');
    link.rel = 'stylesheet'; link.href = '/static/pipeline.css';
    document.head.appendChild(link);
  }

  // ── 2. Inject HTML if drawer is missing ──────────────────────────────
  if (document.getElementById('chatDrawer')) return; // already present

  const html = `
<div id="chatDrawer">
  <div id="chatPane">
    <div class="chat-panel-head">
      <span class="chat-panel-title">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
          <path d="M12 12c-2-2.5-4-4-6-4a4 4 0 0 0 0 8c2 0 4-1.5 6-4z"/>
          <path d="M12 12c2 2.5 4 4 6 4a4 4 0 0 0 0-8c-2 0-4 1.5-6 4z"/>
        </svg>
        Mobius Chat
      </span>
      <div style="display:flex;align-items:center;gap:.6rem">
        <span style="font-size:.7rem;color:var(--indigo);font-weight:500" id="chatOrgLabel">Ask about ${esc(_chatPageLabel)}</span>
        <button class="chat-close-btn" onclick="chatCollapse()" title="Collapse">&times;</button>
      </div>
    </div>
    <div class="chat-progress-bar"><div class="chat-progress-fill" id="pipelineChatProgressFill"></div></div>
    <div class="chat-msgs" id="chatMsgs">
      <div class="chat-empty" id="chatEmpty">
        Ask me anything about <strong id="chatOrgName">${esc(_chatPageLabel)}</strong> — providers, credentialing, billing, coverage, and more.<br><br>
        I have access to RAG, web search, and all pipeline run data.
      </div>
    </div>
    <div class="chat-input-wrap">
      <textarea class="chat-input" id="chatInput" rows="1"
        placeholder="${esc(placeholder)}"
        onkeydown="chatInputKeydown(event)"></textarea>
      <button class="chat-send-btn" id="chatSendBtn" onclick="sendChatMessage()">Send</button>
    </div>
  </div>
  <button id="chatTrigger" onclick="chatToggle()">
    <span class="trigger-dot"></span>
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="opacity:.85">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
    </svg>
    Ask Mobius
    <span id="chatUnreadBadge" style="display:none;background:rgba(255,255,255,.25);border-radius:999px;padding:1px 6px;font-size:.68rem;font-weight:700;margin-left:.1rem"></span>
  </button>
</div>`;

  const wrapper = document.createElement('div');
  wrapper.innerHTML = html.trim();
  document.body.appendChild(wrapper.firstElementChild);
}

/** Update the org label in the chat header dynamically */
function setChatOrgLabel(label) {
  _chatPageLabel = label || _chatPageLabel;
  const el = document.getElementById('chatOrgLabel');
  if (el) el.textContent = `Ask about ${label}`;
  const nameEl = document.getElementById('chatOrgName');
  if (nameEl) nameEl.textContent = label;
}

function chatToggle() {
  const drawer = document.getElementById('chatDrawer');
  const isOpen = drawer.classList.contains('open');
  if (isOpen) {
    chatCollapse();
  } else {
    chatExpand();
  }
}

function chatExpand() {
  const drawer = document.getElementById('chatDrawer');
  drawer.classList.add('open');
  _chatUnread = 0;
  const badge = document.getElementById('chatUnreadBadge');
  if (badge) badge.style.display = 'none';
  // Focus input
  setTimeout(() => {
    const inp = document.getElementById('chatInput');
    if (inp) inp.focus();
    const msgs = document.getElementById('chatMsgs');
    if (msgs) msgs.scrollTop = msgs.scrollHeight;
  }, 80);
}

function chatCollapse() {
  document.getElementById('chatDrawer').classList.remove('open');
}

// Call when a bot message arrives to nudge the pill if drawer is closed
function _chatNotify() {
  const drawer = document.getElementById('chatDrawer');
  if (drawer.classList.contains('open')) return;
  _chatUnread++;
  const trigger = document.getElementById('chatTrigger');
  if (trigger) trigger.classList.add('has-activity');
  const badge = document.getElementById('chatUnreadBadge');
  if (badge) { badge.textContent = _chatUnread; badge.style.display = ''; }
}

function chatInputKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); }
  // Auto-resize textarea
  const t = e.target;
  t.style.height = 'auto';
  t.style.height = Math.min(t.scrollHeight, 120) + 'px';
}

// Unwrap JSON envelope responses (BLENDED / FACTUAL / CANONICAL) to plain text.
// Mirrors the sanitizeDisplayMessage logic from the full chat (app.ts).
function _sanitizeChat(raw) {
  if (!raw) return '';
  let s = raw.trim();
  // Strip markdown code fences if the JSON arrived wrapped
  s = s.replace(/^```json\s*/i,'').replace(/^```\s*/i,'').replace(/\s*```\s*$/,'').trim();
  if (!s.startsWith('{')) return raw;  // not JSON — return as-is
  try {
    const p = JSON.parse(s);
    // Standard envelope fields (priority order)
    const candidates = [p.direct_answer, p.answer, p.message];
    for (const c of candidates) {
      if (typeof c === 'string' && c.trim() && !c.trim().startsWith('{')) return c.trim();
    }
    // sections[] fallback — join any text blocks
    if (Array.isArray(p.sections) && p.sections.length) {
      const parts = p.sections.map(sec => {
        const body = sec.body || sec.text || sec.content || '';
        return typeof body === 'string' ? body.trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join('\n\n');
    }
  } catch { /* not valid JSON — fall through */ }
  return raw;
}

// Minimal markdown → HTML for the mini chat container.
// Handles: **bold**, `code`, newlines → <br>. Safe — no full parser needed.
function _chatMarkdown(text) {
  if (!text) return '';
  const clean = _sanitizeChat(text);  // strip JSON envelope first
  return clean
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code style="font-family:var(--mobius-font-mono,monospace);font-size:.85em;background:rgba(0,0,0,.06);padding:.1em .3em;border-radius:3px">$1</code>')
    .replace(/\n/g, '<br>');
}

function chatAppend(role, text, opts = {}) {
  const msgsEl = document.getElementById('chatMsgs');
  const empty = document.getElementById('chatEmpty');
  if (empty) empty.style.display = 'none';

  const wrap = document.createElement('div');
  wrap.className = `chat-msg ${role}${opts.thinking ? ' thinking' : ''}`;
  if (opts.id) wrap.id = opts.id;

  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble';
  if (opts.thinking) {
    bubble.textContent = text;
  } else {
    bubble.innerHTML = _chatMarkdown(text);
  }
  wrap.appendChild(bubble);
  msgsEl.appendChild(wrap);
  msgsEl.scrollTop = msgsEl.scrollHeight;

  // Nudge the pill if a real bot response arrives while drawer is collapsed
  if (role.startsWith('bot') && !opts.thinking) _chatNotify();
  return wrap;
}

function chatUpdateBubble(id, text, opts = {}) {
  const wrap = document.getElementById(id);
  if (!wrap) return;
  const bubble = wrap.querySelector('.chat-bubble');
  if (bubble) {
    if (opts.thinking) bubble.textContent = text;
    else bubble.innerHTML = _chatMarkdown(text);
  }
  const msgsEl = document.getElementById('chatMsgs');
  if (msgsEl) msgsEl.scrollTop = msgsEl.scrollHeight;
}

function chatRemove(id) {
  const el = document.getElementById(id);
  if (el) el.remove();
}

function _appendFullAnswerLink(botMsgId) {
  // Appends a "Full answer in Mobius Chat →" deep-link after a quick-mode response
  // that was too long for the mini container. Opens index.html with the same thread_id
  // so the conversation continues seamlessly.
  const msgsEl = document.getElementById('chatMsgs');
  if (!msgsEl) return;
  const existing = document.getElementById('chatFullAnswerLink');
  if (existing) existing.remove();
  const el = document.createElement('div');
  el.id = 'chatFullAnswerLink';
  el.style.cssText = 'padding:.3rem .75rem .5rem;text-align:right';
  const tid = encodeURIComponent(_chatThreadId || '');
  el.innerHTML = `<a href="/index.html?thread_id=${tid}" target="_blank"
    style="font-size:.7rem;color:var(--indigo);text-decoration:none;font-weight:500;opacity:.8">
    Full answer in Mobius Chat →</a>`;
  msgsEl.appendChild(el);
  msgsEl.scrollTop = msgsEl.scrollHeight;
}

async function sendChatMessage() {
  if (_chatSending) return;
  const input = document.getElementById('chatInput');
  const msg = (input.value || '').trim();
  if (!msg) return;

  // Ensure drawer is open so the user sees the conversation
  chatExpand();

  input.value = '';
  input.style.height = 'auto';
  _chatSending = true;
  const sendBtn = document.getElementById('chatSendBtn');
  if (sendBtn) sendBtn.disabled = true;

  chatAppend('user', msg);

  // Build context hint — use caller-supplied contextFn if available, else fall back to pipeline state
  let contextHint = '';
  if (typeof _chatContextFn === 'function') {
    try { contextHint = _chatContextFn() || ''; } catch { /* ignore */ }
  } else {
    const org  = (typeof lastRun !== 'undefined' && lastRun?.org_name) || '';
    const step = (typeof lastRun !== 'undefined' && lastRun?.pending_step_id) || '';
    contextHint = org ? `[Pipeline context: org="${org}"${step ? `, current step="${step}"` : ''}] ` : '';
  }
  const fullMsg = contextHint + msg;

  const thinkingId = 'chat-thinking-' + Date.now();
  chatAppend('bot thinking', '…', { id: thinkingId });

  try {
    const r = await fetch(`${API}/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: fullMsg, thread_id: _chatThreadId || undefined, chat_mode: 'quick' }),
    });
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    _chatThreadId = d.thread_id;
    const cid = d.correlation_id;

    // Stream via SSE
    streamChatResponse(cid, thinkingId);
  } catch (e) {
    chatRemove(thinkingId);
    chatAppend('bot', 'Sorry, something went wrong: ' + e.message);
    _chatSending = false;
    if (sendBtn) sendBtn.disabled = false;
  }
}

function streamChatResponse(correlationId, thinkingId) {
  const sendBtn = document.getElementById('chatSendBtn');
  const streamUrl = `${API}/chat/stream/${encodeURIComponent(correlationId)}`;
  let botMsgId = null;
  let accText = '';

  if (typeof EventSource === 'undefined') {
    pollChatResponse(correlationId, thinkingId);
    return;
  }

  const es = new EventSource(streamUrl);
  es.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      const event = data.event || data.type || '';

      if (event === 'progress' || event === 'thinking') {
        const line = data.data?.line || data.data?.message || '';
        if (line) chatUpdateBubble(thinkingId, line.substring(0, 80) + '…', {thinking:true});

      } else if (event === 'message_delta' || event === 'delta') {
        const delta = data.data?.delta || data.data?.text || '';
        if (delta) {
          if (!botMsgId) {
            chatRemove(thinkingId);
            const wrap = chatAppend('bot', delta);
            botMsgId = 'chat-bot-' + Date.now();
            wrap.id = botMsgId;
            accText = delta;
          } else {
            accText += delta;
            chatUpdateBubble(botMsgId, accText);
          }
        }

      } else if (event === 'completed' || event === 'done') {
        es.close();
        chatRemove(thinkingId);
        const finalMsg = data.data?.message || data.message || accText;
        if (finalMsg && !botMsgId) {
          chatAppend('bot', finalMsg);
        } else if (finalMsg && botMsgId) {
          chatUpdateBubble(botMsgId, finalMsg);
        }
        // Quick mode: if answer was long or backend flagged truncation, show "Full answer" link
        const isLong = (finalMsg || '').length > 500;
        const wasTruncated = data.data?.quick_truncated || data.quick_truncated;
        if ((isLong || wasTruncated) && _chatThreadId) {
          _appendFullAnswerLink(botMsgId);
        }
        _chatSending = false;
        if (sendBtn) sendBtn.disabled = false;

      } else if (event === 'error') {
        es.close();
        chatRemove(thinkingId);
        chatAppend('bot', 'Error: ' + (data.data?.message || 'unknown error'));
        _chatSending = false;
        if (sendBtn) sendBtn.disabled = false;
      }
    } catch { /* ignore parse errors */ }
  };

  es.onerror = () => {
    es.close();
    // Fall back to polling
    if (!botMsgId) pollChatResponse(correlationId, thinkingId);
  };
}

async function pollChatResponse(correlationId, thinkingId) {
  const sendBtn = document.getElementById('chatSendBtn');
  let attempts = 0;
  while (attempts < 120) {
    await new Promise(r => setTimeout(r, 1500));
    attempts++;
    try {
      const r = await fetch(`${API}/chat/response/${encodeURIComponent(correlationId)}`);
      if (!r.ok) continue;
      const d = await r.json();
      if (d.status === 'processing' && d.message) {
        chatUpdateBubble(thinkingId, (d.message || '').substring(0, 100) + '…', {thinking:true});
      } else if (d.status === 'completed' || d.message) {
        chatRemove(thinkingId);
        const pollMsg = d.message || '(no response)';
        const pollWrap = chatAppend('bot', pollMsg);
        if (((pollMsg.length > 500) || d.quick_truncated) && _chatThreadId) {
          _appendFullAnswerLink(pollWrap?.id);
        }
        break;
      }
    } catch { /* ignore */ }
  }
  _chatSending = false;
  if (sendBtn) sendBtn.disabled = false;
}

// Steps that require an explicit human decision before proceeding
const DECISION_STEPS = new Set(['identify_org', 'find_locations', 'find_associated_providers', 'nppes_alignment']);

// Returns true when a step's content is ready for the user to confirm and proceed
function _isStepReady(stepId, data) {
  if (!data || data.phase !== 'awaiting_validation') return false;
  if (data.mode !== 'copilot') return false;
  // For the roster step, the roster must also be fully validated
  if (stepId === 'nppes_alignment') {
    return window._rosterUploadState?.phase === 'done';
  }
  return true;
}

function _stepActionLabel(stepId) {
  if (stepId === 'identify_org')   return 'Confirm selected NPIs →';
  if (stepId === 'find_locations') return 'Confirm locations →';
  if (stepId === 'nppes_alignment') return 'Accept roster &amp; Continue →';
  return 'Continue →';
}

function _stepActionHandler(stepId) {
  if (stepId === 'identify_org')   return 'validateNpiSelection()';
  if (stepId === 'find_locations') return 'validateLocations()';
  return 'validateStep()';
}

function buildStepFoot(stepId, data, draft) {
  const isDone  = data.phase === 'complete';
  const isError = data.phase === 'error';
  if (isDone || isError) return '';   // completed steps show nothing

  const ready     = _isStepReady(stepId, data);
  const isRunning = !ready && data.phase !== 'awaiting_validation';
  const isWaiting = !ready && data.phase === 'awaiting_validation'; // awaiting but roster not done yet

  const btnCls   = ready ? 'ready' : (isRunning ? 'running' : 'waiting');
  const label    = _stepActionLabel(stepId);
  const handler  = _stepActionHandler(stepId);
  const disabled = ready ? '' : 'disabled';

  // Hint text under/beside button
  let hint = '';
  if (isRunning && data.mode !== 'copilot') {
    hint = `<span class="step-foot-hint"><span class="spinner" style="width:10px;height:10px;border-width:1.5px;display:inline-block;vertical-align:middle;margin-right:3px"></span> Autopilot running…</span>`;
  } else if (isRunning) {
    hint = `<span class="step-foot-hint"><span class="spinner" style="width:10px;height:10px;border-width:1.5px;display:inline-block;vertical-align:middle;margin-right:3px"></span> Running…</span>`;
  } else if (isWaiting && stepId === 'nppes_alignment') {
    hint = `<span class="step-foot-hint">Upload and validate roster to continue</span>`;
  } else if (ready) {
    hint = `<span class="step-foot-hint" style="color:var(--green)">✓ Ready to proceed</span>`;
  }

  const skipBtn = `<button class="btn-action ghost" onclick="skipStep()" style="font-size:.78rem;padding:.3rem .7rem">Skip step</button>`;

  // disabled attribute prevents click but also add onclick guard for safety
  return `
    <button class="step-continue-btn ${btnCls}" id="validateBtn" ${disabled}
      onclick="if(!this.disabled){${handler}}" title="${ready ? '' : 'Complete this step before continuing'}">
      ${ready ? '' : '<span class="spinner" style="width:11px;height:11px;border-width:1.5px;display:inline-block;border-top-color:var(--text-3)"></span>'}
      ${label}
    </button>
    ${skipBtn}
    ${hint}`;
}

let _autoTimer = null;
let _autoCountdownTimer = null;
let _autoAdvanceCancelled = false;
let _validationInFlight = false;   // true while any validate/skip POST is in-flight

function scheduleAutoAdvance(stepId, seconds) {
  // Never schedule while a validate POST is in-flight — prevents duplicate submits
  if (_validationInFlight) return;
  clearAutoAdvanceTimers();
  _autoAdvanceCancelled = false;
  _validationInFlight   = false;  // reset stale flag on fresh schedule
  let remaining = seconds;

  _autoCountdownTimer = setInterval(() => {
    remaining--;
    const el = document.getElementById('autoCountdown');
    if (el) el.textContent = remaining;
    if (remaining <= 0) clearInterval(_autoCountdownTimer);
  }, 1000);

  _autoTimer = setTimeout(async () => {
    if (_autoAdvanceCancelled) return;
    if (_validationInFlight) return;   // another validate beat us to it
    // Safety check: only fire if the server is still waiting for this exact step
    if (!lastRun || lastRun.pending_step_id !== stepId) return;
    await validateStep();
  }, seconds * 1000);
}

function cancelAutoAdvance() {
  _autoAdvanceCancelled = true;
  clearAutoAdvanceTimers();
  // Refresh the foot — button should already be in ready state
  if (lastRun) {
    const sid  = lastRun.pending_step_id;
    const foot = document.getElementById('scFoot');
    if (foot && sid) foot.innerHTML = buildStepFoot(sid, lastRun, lastRun.draft_output || {});
  }
}

function clearAutoAdvanceTimers() {
  if (_autoTimer)          { clearTimeout(_autoTimer);           _autoTimer = null; }
  if (_autoCountdownTimer) { clearInterval(_autoCountdownTimer); _autoCountdownTimer = null; }
}

function renderInsight(stepId, draft, data, steps) {
  const el = document.getElementById('insightBody');
  const lines = [];

  const doneCount = steps.filter(s => s.status === 'done').length;
  const total = PLAN.length;
  const pct = Math.round(doneCount / total * 100);

  lines.push(`<p class="insight-text"><strong>${doneCount}/${total}</strong> steps complete (${pct}%)</p>`);
  lines.push(`<div style="height:6px;border-radius:3px;background:var(--border);overflow:hidden;margin:.1rem 0 .5rem">
    <div style="height:100%;width:${pct}%;background:var(--green);border-radius:3px;transition:width .4s"></div>
  </div>`);

  // Step-specific insight
  function irow(cls, text) { return `<div class="insight-row"><span class="dot-lg di-${cls}"></span><span>${text}</span></div>`; }
  switch (stepId) {
    case 'identify_org': {
      const npis = (draft.org_npis || []).length;
      if (npis > 0) lines.push(irow('green', `${npis} org NPI${npis !== 1 ? 's' : ''} confirmed`));
      else if (draft.status !== 'pending') lines.push(irow('amber', 'No org NPI found — check name'));
      break;
    }
    case 'find_locations': {
      const n = (draft.locations || []).length;
      if (n > 0) lines.push(irow('green', `${n} practice location${n !== 1 ? 's' : ''} mapped`));
      break;
    }
    case 'find_associated_providers': {
      const bc  = draft.bucket_counts  || {};
      const tot = draft.provider_count || 0;
      if (tot) {
        if (bc.aligned)       lines.push(irow('green', `${bc.aligned} providers confirmed on roster`));
        if (bc.external_only) lines.push(irow('amber', `${bc.external_only} billing but not on roster — ghost billing risk`));
        if (bc.anomaly)       lines.push(irow('red',   `${bc.anomaly} anomalies flagged for review`));
      } else {
        lines.push(irow('grey', 'Compliance audit pending prior steps'));
      }
      break;
    }
    case 'nppes_alignment': {
      const rs = window._rosterUploadState;
      const rosterClean = rs?.phase === 'done' ? (rs.report?.clean || []) : [];
      const validated   = rosterClean.filter(p => p._decision === 'validated').length;
      const pending     = rosterClean.filter(p => !p._decision).length;
      if (validated) lines.push(irow('green', `${validated} NPIs validated`));
      if (pending)   lines.push(irow('amber', `${pending} NPIs pending review`));
      if (!validated && !pending) lines.push(irow('grey', 'No roster loaded yet'));
      break;
    }
    case 'pml_alignment': {
      const v = draft.pml_validated_count || 0, f = draft.pml_flagged_count || 0;
      if (v) lines.push(irow('green', `${v} providers enrolled in PML`));
      if (f) lines.push(irow('amber', `${f} flagged for review`));
      if (!v && !f) lines.push(irow('grey', 'PML data not yet available'));
      break;
    }
    case 'taxonomy_optimization': {
      lines.push(irow('grey', 'Taxonomy analysis pending provider data'));
      break;
    }
  }

  const err = steps.filter(s => s.status === 'failed').length;
  const skip = steps.filter(s => s.status === 'skipped').length;
  if (err > 0) lines.push(`<div class="insight-row"><span class="dot-lg di-red"></span><span>${err} step${err !== 1 ? 's' : ''} failed</span></div>`);
  if (skip > 0) lines.push(`<div class="insight-row"><span class="dot-lg di-grey"></span><span>${skip} step${skip !== 1 ? 's' : ''} skipped</span></div>`);

  el.innerHTML = lines.join('');
}

function renderHistory() {
  // Removed — completed steps are navigable via the stepper above.
}


// ── Copilot actions ───────────────────────────────────────────
function _showStepTransition(label) {
  // Freeze the poll so it can't re-enable the button mid-flight
  clearInterval(pollTimer);
  pollTimer = null;
  // Dim the step body
  const body = document.getElementById('scBody');
  if (body) body.style.opacity = '0.4';
  // Replace footer with a progress bar
  const foot = document.getElementById('scFoot');
  if (foot) foot.innerHTML = `
    <span class="spinner" style="width:14px;height:14px;border-width:2px;flex-shrink:0"></span>
    <span style="font-size:.78rem;font-weight:600;color:var(--text-2)">${esc(label)}</span>
    <div style="flex:1;min-width:60px;height:3px;border-radius:2px;background:var(--border);overflow:hidden;max-width:160px">
      <div id="stepTransitionBar" style="height:100%;width:0%;background:var(--indigo);border-radius:2px;transition:width 25s linear"></div>
    </div>`;
  // Animate progress bar (visual only — fills over ~25s, then stalls at 90%)
  requestAnimationFrame(() => {
    const bar = document.getElementById('stepTransitionBar');
    if (bar) { requestAnimationFrame(() => { bar.style.width = '90%'; }); }
  });
}

function _clearStepTransition() {
  const body = document.getElementById('scBody');
  if (body) body.style.opacity = '';
}

async function validateStep() {
  if (!runId || !lastRun) return;
  if (_validationInFlight) return;   // already in-flight — ignore duplicate calls
  const sid = lastRun.pending_step_id;
  if (!sid) return;

  _validationInFlight = true;
  clearAutoAdvanceTimers();

  const stepLabels = {
    identify_org:              'Step 1 — Identifying organization…',
    find_locations:            'Step 2 — Finding practice locations…',
    nppes_alignment:           'Step 3 — Running NPPES alignment…',
    pml_alignment:             'Step 4 — Checking Medicaid enrollment…',
    find_associated_providers: 'Step 5 — Finding associated providers…',
    taxonomy_optimization:     'Step 6 — Optimizing taxonomy codes…',
  };
  const nextIdx = (lastRun.orchestrator_state?.steps || []).findIndex(s => s.id === sid);
  const nextStep = (lastRun.orchestrator_state?.steps || [])[nextIdx + 1];
  const label = nextStep ? (stepLabels[nextStep.id] || `Running ${nextStep.id}…`) : 'Saving and advancing…';
  feEmit(`✓ Step confirmed — ${label}`);
  _showStepTransition(label);

  const validatedOutput = buildValidatedOutput(sid, lastRun.draft_output || {});

  // Persist validated providers to truth table before advancing
  if (sid === 'nppes_alignment') {
    await saveRosterTruth().catch(() => {});
  }

  try {
    const r = await fetch(`${API}/chat/credentialing-runs/${runId}/validate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step_id: sid, validated_output: validatedOutput }),
    });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    _clearStepTransition();
    feEmit(`✓ Validation saved — pipeline advancing`, 'ok');
    render(data);
    schedulePoll(data);
  } catch (e) {
    _clearStepTransition();
    feEmit(`Validation failed — ${e.message}`, 'error');
    // Restore footer button on error
    document.getElementById('scFoot').innerHTML = buildStepFoot(sid, lastRun, lastRun.draft_output || {});
    alert('Validation failed: ' + e.message);
    schedulePoll(lastRun);
  } finally {
    _validationInFlight = false;
  }
}

async function skipStep() {
  if (!runId || !lastRun) return;
  if (_validationInFlight) return;
  const sid = lastRun.pending_step_id;
  _validationInFlight = true;
  clearAutoAdvanceTimers();
  feEmit(`Skipping step: ${sid}`);
  _showStepTransition('Skipping step…');
  try {
    const r = await fetch(`${API}/chat/credentialing-runs/${runId}/validate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step_id: sid, validated_output: { skip: true } }),
    });
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    _clearStepTransition();
    feEmit(`✓ Step skipped — advancing`, 'ok');
    render(data);
    schedulePoll(data);
  } catch (e) {
    _clearStepTransition();
    feEmit(`Skip failed — ${e.message}`, 'error');
    document.getElementById('scFoot').innerHTML = buildStepFoot(sid, lastRun, lastRun.draft_output || {});
    alert('Skip failed: ' + e.message);
    schedulePoll(lastRun);
  } finally { _validationInFlight = false; }
}

function buildValidatedOutput(stepId, draft) {
  // Pass the draft back as-is; for providers step include autopilot cutoff flag
  const out = Object.assign({}, draft);
  if (stepId === 'find_associated_providers') {
    out.use_autopilot_active_cutoff = true;
    out.allow_empty_active_roster = true;
  }
  return out;
}

function getReconciliationUploadId(data) {
  const state = data.orchestrator_state;
  if (!state) return null;
  return state.step3_roster_upload_id || null;
}

// ── Inline reconciliation panel ───────────────────────────────
let _reconProviders = [];
let _reconUploadId = null;

async function loadReconciliation(uploadId) {
  if (!skillBase) {
    // Try to get it now
    try {
      const r = await fetch(`${API}/chat/skills/urls`);
      if (r.ok) { const d = await r.json(); skillBase = (d.roster_base || '').replace(/\/+$/, ''); }
    } catch { /* ignore */ }
  }
  if (!skillBase) {
    setReconciliationError('Skill server URL not configured (CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL).');
    return;
  }
  _reconUploadId = uploadId;
  const statusEl = document.getElementById('reconcStatus');
  const bodyEl   = document.getElementById('reconcBody');
  if (!statusEl || !bodyEl) return;
  statusEl.textContent = 'Loading report…';

  // Kick off reconciliation if not already running, then fetch report
  try {
    // Start reconciliation silently (idempotent — 409 is fine)
    await fetch(`${skillBase}/roster/reconcile/${uploadId}`, { method: 'POST' }).catch(() => {});
  } catch { /* ignore */ }

  // Poll for report (may need a moment if reconciliation just started)
  let attempts = 0;
  const tryLoad = async () => {
    attempts++;
    try {
      const r = await fetch(`${skillBase}/roster/reconcile/${uploadId}/report`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const report = await r.json();
      _reconProviders = report.providers || [];
      renderReconciliationPanel(report, statusEl, bodyEl);
    } catch (e) {
      if (attempts < 6) {
        setTimeout(tryLoad, 3000);
        statusEl.textContent = `Reconciliation running… (${attempts})`;
      } else {
        setReconciliationError(`Could not load report: ${e.message}`);
      }
    }
  };
  tryLoad();
}

function setReconciliationError(msg) {
  const statusEl = document.getElementById('reconcStatus');
  if (statusEl) { statusEl.textContent = msg; statusEl.style.color = 'var(--red)'; }
}

function renderReconciliationPanel(report, statusEl, bodyEl) {
  const total    = report.total_count || 0;
  const valid    = report.validated_count || 0;
  const review   = report.needs_review_count || 0;
  const invalid  = report.invalid_count || 0;
  const parseErr = report.parse_error_count || 0;

  statusEl.textContent = `${total} providers · last updated ${report.generated_at ? new Date(report.generated_at).toLocaleTimeString() : 'just now'}`;
  statusEl.style.color = 'var(--text-3)';

  const providers = _reconProviders.slice(0, 12); // show top 12 rows inline
  const more = Math.max(0, _reconProviders.length - 12);

  bodyEl.innerHTML = `
    <div class="recon-stats">
      <span class="recon-stat rs-green">✓ ${valid} validated</span>
      ${review  ? `<span class="recon-stat rs-amber">⚠ ${review} review</span>` : ''}
      ${invalid ? `<span class="recon-stat rs-red">✗ ${invalid} invalid</span>` : ''}
      ${parseErr? `<span class="recon-stat rs-grey">⚠ ${parseErr} parse errors</span>` : ''}
    </div>
    <table class="recon-table">
      <thead><tr>
        <th>#</th><th>Provider</th><th>Uploaded NPI</th><th>Registry NPI</th><th>Status</th><th></th>
      </tr></thead>
      <tbody>
        ${providers.map(p => {
          const needsResolve = p.status === 'needs_review' &&
            ((p.latest_validation?.validation_details?.candidates || []).length > 0);
          return `<tr>
            <td>${p.row_number || ''}</td>
            <td class="rt-name" title="${esc(p.provider_name)}">${esc(titleCase(p.provider_name))}</td>
            <td class="rt-npi">${p.npi_uploaded || '—'}</td>
            <td class="rt-npi">${p.npi_validated || p.latest_validation?.npi_validated || '—'}</td>
            <td><span class="rt-status s-${p.status}">${(p.status||'').replace(/_/g,' ')}</span></td>
            <td>${needsResolve ? `<button class="recon-resolve-btn" onclick="openPipelineMatchDialog(${p.id})">Resolve →</button>` : ''}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>
    ${more > 0 ? `<p class="recon-more">+ ${more} more providers — <button class="link-btn" onclick="openFullReport()">view all in report</button></p>` : ''}
    <div style="display:flex;gap:.5rem;margin-top:.25rem">
      <button class="btn-action ghost" style="font-size:.75rem;padding:.3rem .7rem" onclick="openFullReport()">Open full report ↗</button>
      <button class="btn-action ghost" style="font-size:.75rem;padding:.3rem .7rem" onclick="loadReconciliation('${_reconUploadId}')">↺ Refresh</button>
    </div>
  `;
}

function openFullReport() {
  if (!_reconUploadId || !skillBase) return;
  window.open(`${skillBase}/roster-ui/report.html?upload_id=${_reconUploadId}`, '_blank', 'noopener');
}

// ── Inline match-selection dialog (mirrors report.html version) ──
let _matchDialogPid = null;
let _matchDialogProviders = null;

function openPipelineMatchDialog(providerId) {
  _matchDialogPid = providerId;
  _matchDialogProviders = _reconProviders;
  const p = _reconProviders.find(x => x.id === providerId);
  if (!p) return;
  const candidates = (p.latest_validation?.validation_details?.candidates || []);
  if (!candidates.length) return;

  const scored = candidates.map(c => ({
    ...c,
    _sim: simScore(p.provider_name || '', c.name || ''),
  })).sort((a, b) => b._sim - a._sim);
  const best = scored[0]._sim;

  document.getElementById('pdTitle').textContent = `Select correct provider for: ${p.provider_name}`;
  document.getElementById('pdBody').innerHTML = scored.map((c, i) => {
    const pct = Math.round(c._sim * 100);
    const cls = c._sim >= .8 ? 'high' : c._sim >= .55 ? 'mid' : 'low';
    const isBest = i === 0 && c._sim === best && c._sim >= .5;
    const loc = [c.city, c.state].filter(Boolean).join(', ');
    return `<div class="pd-card${isBest ? ' pd-best' : ''}" onclick="pipelineAcceptNpi(${p.id},'${esc(c.npi||'')}')">
      <div class="pd-meta">
        <div class="pd-name">${esc(c.name||'Unknown')}${isBest?' <span class="pd-best-badge">Best match</span>':''}</div>
        <div class="pd-tags">
          <span class="pd-npi">${esc(c.npi||'—')}</span>
          ${c.taxonomy?`<span>${esc(c.taxonomy)}${c.taxonomy_code?` <span class="pd-code">${esc(c.taxonomy_code)}</span>`:''}</span>`:''}
          ${loc?`<span>📍 ${esc(loc)}</span>`:''}
          ${c.status?`<span style="color:${c.status==='A'?'var(--green)':'var(--red)'}">● ${c.status==='A'?'Active':c.status}</span>`:''}
        </div>
        ${c.address?`<div class="pd-address">${esc(c.address)}</div>`:''}
      </div>
      <div class="pd-conf ${cls}">${pct}%<div style="font-size:.62rem;opacity:.7;font-weight:500">match</div></div>
    </div>`;
  }).join('');

  const bd = document.getElementById('pipelineMatchDialog');
  bd.style.display = 'flex';
}

function closePipelineMatchDialog() {
  document.getElementById('pipelineMatchDialog').style.display = 'none';
  _matchDialogPid = null;
}

async function pipelineAcceptNpi(providerId, npi) {
  closePipelineMatchDialog();
  if (!skillBase) return;
  try {
    await fetch(`${skillBase}/roster/provider/${providerId}`, {
      method: 'PATCH', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ npi_corrected: npi, correction_source: 'registry_selected' }),
    });
    if (_reconUploadId) loadReconciliation(_reconUploadId);
  } catch (e) { console.error(e); }
}

