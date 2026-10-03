'use strict';
/* research-normalizer dashboard. Vanilla JS, no build step; every number comes from the local API. */

const TEAM = 'Team Amek';
// The pipeline finishes in ~0.3 s. New runs are replayed one stage at a time at this pace so a
// person can follow what happened; the real runtime is always shown next to it.
const STEP_MS = 1200;

const S = {
  runs: [], details: {}, samples: [],
  route: { view: 'dashboard', runId: null, tab: null },
  threshold: parseFloat(localStorage.getItem('rn.threshold') || '0.95'),
  focusIdx: 0, runFilter: 'all', selected: new Set(), openItems: new Set(), drawer: null,
  varDataset: null, varQuery: '', openVar: null, jsonSection: 'summary',
  streams: {}, replay: {}, online: null,
};

const STAGES = [
  { key: 'inspection', label: 'Inspection', backend: ['discovery', 'classification', 'structure_detection'],
    working: 'Opening each file and working out its encoding, delimiter and header row…' },
  { key: 'schema', label: 'Schema discovery', backend: ['readme_parsing'],
    working: 'Reading the README for project details and variable definitions…' },
  { key: 'reconcile', label: 'Reconciliation', backend: ['variable_matching', 'normalization'],
    working: 'Matching every documented variable to a real column header…' },
  { key: 'review', label: 'Human review', backend: [] },
  { key: 'validation', label: 'Validation', backend: ['validation'] },
  { key: 'export', label: 'Export', backend: [] },
];
const TABS = [['overview', 'Overview'], ['review', 'Review'], ['variables', 'Variables'], ['files', 'Files'],
  ['metadata', 'Metadata'], ['issues', 'Issues'], ['json', 'JSON'], ['log', 'Log']];
const KIND = {
  low_confidence: { label: 'Check match', tag: 'tag-amber', actions: ['accept', 'reject'] },
  ambiguous: { label: 'Ambiguous', tag: 'tag-amber', actions: ['accept', 'reject'] },
  undocumented_column: { label: 'Undocumented column', tag: 'tag-blue', actions: ['acknowledge'] },
  phantom_variable: { label: 'Missing from data', tag: 'tag-red', actions: ['acknowledge'] },
};
const AUTO_NOTE = 'resolved by editing the variable'; // must match review.AUTO_NOTE on the server
const COLUMN_TYPES = ['string', 'integer', 'number', 'boolean', 'date', 'datetime', 'time'];
const COLUMN_ROLES = ['identifier', 'categorical', 'measure', 'temporal', 'text'];
const STATUS = { running: 'Processing', review: 'Needs review', ready: 'Ready to export', exported: 'Exported', failed: 'Failed' };
const DELIM = { '\t': ['tab-separated', '\\t'], ',': ['comma-separated', ','], ';': ['semicolon-separated', ';'], '|': ['pipe-separated', '|'] };

// ------------------------------------------------------------------ utils
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const code = (v) => `<code class="code">${esc(v)}</code>`;
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const joinList = (a) => a.length <= 1 ? (a[0] || '') : `${a.slice(0, -1).join(', ')} and ${a.at(-1)}`;
const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const fmtConf = (c) => (c ?? 0).toFixed(2);
const confTone = (c) => c >= 0.95 ? 'green' : c >= 0.85 ? 'amber' : 'red';
const icon = (name, cls = '') => `<i data-lucide="${name}"${cls ? ` class="${cls}"` : ''}></i>`;
const icons = () => window.lucide && lucide.createIcons();
const relTime = (iso) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
};
function encNote(enc) {
  const e = (enc || '').toLowerCase();
  if (e === 'utf-8-sig') return 'UTF-8 with a byte-order mark, stripped';
  if (e === 'cp1252' || e === 'windows-1252') return 'Windows-1252, converted to UTF-8';
  if (e === 'latin-1' || e === 'iso-8859-1') return 'Latin-1, converted to UTF-8';
  if (e.startsWith('utf-8') || e === 'ascii') return 'plain UTF-8';
  return e || 'unknown';
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, kind = 'info') {
  const cls = { info: '', ok: 'ready', err: 'failed' }[kind];
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = `<span class="status dot-only ${cls}"><span class="d"></span></span><span>${esc(msg)}</span>`;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

const shownStatus = (r) => (S.replay[r.id] ? 'running' : r.status);
function status(st, pending = 0) {
  return `<span class="status ${st}"><span class="d"></span>${STATUS[st] || esc(st)}${st === 'review' && pending ? ` · ${pending}` : ''}</span>`;
}

function download(name, obj) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(obj, null, 2) + '\n'], { type: 'application/json' }));
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ------------------------------------------------------------------ data
async function loadRuns() {
  try { S.runs = await api('/api/runs'); setOnline(true); } catch { setOnline(false); }
}

async function loadDetail(id) {
  const d = await api(`/api/runs/${id}`);
  S.details[id] = d;
  const i = S.runs.findIndex((r) => r.id === id);
  if (i >= 0) { const { events, items, decisions, document: _doc, ...brief } = d; S.runs[i] = brief; }
  return d;
}

function setOnline(ok) {
  if (S.online === ok) return;
  S.online = ok;
  const el = $('#backend');
  el.className = `status ${ok ? 'online' : 'offline'}`;
  el.innerHTML = `<span class="d"></span>${ok ? 'online' : 'offline'}`;
}

function stream(id) {
  if (S.streams[id]) return;
  const es = new EventSource(`/api/runs/${id}/events`);
  S.streams[id] = es;
  let seen = 0;
  es.onmessage = async (msg) => {
    const ev = JSON.parse(msg.data);
    if (ev.stage === '_end') {
      es.close(); delete S.streams[id];
      await loadDetail(id); await loadRuns();
      const r = S.details[id];
      if (r.status === 'failed') toast(`${r.repository} failed: ${r.error}`, 'err');
      else if (S.route.runId !== id) toast(`${r.repository} processed in ${r.duration_ms} ms`, 'ok');
      render();
      return;
    }
    const d = S.details[id];
    if (d && seen++ >= d.events.length) d.events.push(ev);
    scheduleRender();
  };
  es.onerror = () => { es.close(); delete S.streams[id]; };
}

let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; render(); });
}

// ------------------------------------------------------------------ replay
/** How many of the first three stages the replay has "finished", or null when not replaying. */
function replayStep(d) {
  const rp = S.replay[d.id];
  if (!rp) return null;
  if (d.status === 'failed') { delete S.replay[d.id]; return null; }
  const seen = new Set(d.events.map((e) => e.stage));
  const realDone = d.status === 'running'
    ? Math.max(0, STAGES.slice(0, 3).filter((st) => st.backend.some((b) => seen.has(b))).length - 1) : 3;
  const k = Math.min(Math.floor((performance.now() - rp.t0) / STEP_MS), realDone);
  if (k >= 3 && d.document) { delete S.replay[d.id]; return null; }
  return k;
}

setInterval(() => {
  for (const id of Object.keys(S.replay)) {
    const d = S.details[id];
    if (!d) continue;
    const before = S.replay[id].last;
    const k = replayStep(d);
    if (k === null || k !== before) {
      if (S.replay[id]) S.replay[id].last = k;
      render();
    }
  }
}, 150);

// ------------------------------------------------------------------ actions
async function startRun(spec) {
  try {
    const run = await api('/api/runs', { method: 'POST', body: JSON.stringify({ ...spec, threshold: S.threshold }) });
    S.runs.unshift(run);
    S.details[run.id] = { ...run, events: [], items: [], decisions: {}, document: null };
    S.replay[run.id] = { t0: performance.now(), last: -1 };
    stream(run.id);
    closeModal();
    location.hash = `#/run/${run.id}/overview`;
  } catch (e) { toast(e.message, 'err'); }
}

async function decide(runId, itemId, decision) {
  try {
    await api(`/api/runs/${runId}/decisions`, { method: 'POST', body: JSON.stringify({ item_id: itemId, decision }) });
    await loadDetail(runId);
    render();
  } catch (e) { toast(e.message, 'err'); }
}

async function exportRun(runId, log = false) {
  try {
    const d = S.details[runId];
    const data = await api(`/api/runs/${runId}/export${log ? '?log=1' : ''}`);
    download(`${d.repository}.${log ? 'decisions' : 'normalized'}.json`, data);
    await loadDetail(runId); await loadRuns(); render();
  } catch (e) { toast(e.message, 'err'); }
}

function confirmDelete(ids) {
  const runs = ids.map((id) => S.runs.find((r) => r.id === id)).filter(Boolean);
  if (!runs.length) return;
  const one = runs.length === 1;
  modal(one ? 'Delete this run?' : `Delete ${runs.length} runs?`, `
    <p>${one ? `${code(runs[0].repository)} <span class="faint mono">${esc(runs[0].id)}</span>` : runs.map((r) => code(r.repository)).join(' ')}</p>
    <p>This removes the run, every review decision made on it, and any files uploaded for it. JSON you already exported is not affected. This can't be undone.</p>`,
  `<button class="btn" data-action="close-modal">Cancel</button>
   <button class="btn btn-danger-solid" data-action="confirm-delete" data-ids="${esc(ids.join(','))}" autofocus>${icon('trash-2')}Delete${one ? '' : ` ${runs.length}`}</button>`, 'sm');
}

async function deleteRuns(ids) {
  const failed = [];
  for (const id of ids) {
    try {
      await api(`/api/runs/${id}`, { method: 'DELETE' });
      delete S.details[id]; delete S.replay[id]; S.selected.delete(id);
    } catch (e) { failed.push(`${id}: ${e.message}`); }
  }
  closeModal();
  await loadRuns();
  const done = ids.length - failed.length;
  if (done) toast(`Deleted ${plural(done, 'run')}.`, 'ok');
  if (failed.length) toast(failed.join('\n'), 'err');
  if (S.route.view === 'run' && ids.includes(S.route.runId) && !S.details[S.route.runId]) location.hash = '#/runs';
  else render();
}

// ------------------------------------------------------------------ routing
function parseRoute() {
  const [view = 'dashboard', runId = null, tab = null] = location.hash.replace(/^#\/?/, '').split('/');
  S.route = { view: view || 'dashboard', runId, tab };
}

async function onRoute() {
  const prevRun = S.route.runId;
  parseRoute();
  const { view, runId } = S.route;
  if (runId !== prevRun) S.focusIdx = 0;
  if (view === 'run' && runId) {
    try {
      const d = await loadDetail(runId);
      if (!S.route.tab) S.route.tab = 'overview';
      if (d.status === 'running') stream(runId);
      const ids = (d.document?.datasets || []).map((x) => x.id);
      if (!ids.includes(S.varDataset)) { S.varDataset = ids[0] ?? null; S.openVar = null; }
    } catch (e) { toast(e.message, 'err'); location.hash = '#/runs'; return; }
  }
  if (view === 'inbox') await Promise.all(S.runs.filter((r) => r.pending).map((r) => loadDetail(r.id)));
  render();
  $('#view').scrollTop = 0;
}

// ------------------------------------------------------------------ chrome
function renderChrome() {
  const { view, runId } = S.route;
  document.querySelectorAll('[data-nav]').forEach((a) => {
    const on = a.dataset.nav === view;
    a.classList.toggle('active', on);
    on ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current');
  });
  const pending = S.runs.reduce((n, r) => n + (S.replay[r.id] ? 0 : r.pending || 0), 0);
  const inbox = $('#count-inbox');
  inbox.textContent = pending || ''; inbox.classList.toggle('hot', pending > 0);
  $('#count-runs').textContent = S.runs.length || '';
  $('#sb-threshold').textContent = `< ${fmtConf(S.threshold)}`;

  const latest = new Map();
  for (const r of S.runs) if (!latest.has(r.repository)) latest.set(r.repository, r);
  $('#sb-repos').innerHTML = latest.size ? [...latest.values()].map((r) => `
    <a class="repo-item ${r.id === runId ? 'active' : ''}" href="#/run/${r.id}" title="${esc(r.repository)}">
      <span class="truncate">${esc(r.repository)}</span><span class="status dot-only ${shownStatus(r)}" title="${STATUS[shownStatus(r)]}"><span class="d"></span></span>
    </a>`).join('') : '<p class="sb-empty">Nothing run yet.</p>';

  const d = runId && S.details[runId];
  const names = { dashboard: 'Dashboard', runs: 'Runs', inbox: 'Review' };
  $('#crumbs').innerHTML = view === 'run'
    ? `<a href="#/runs">Runs</a><span class="sep">/</span><span class="here">${esc(d?.repository || runId)}</span>`
    : `<span class="here" style="font-family:var(--sans)">${names[view] || 'Dashboard'}</span>`;
  const ver = Object.values(S.details).find((x) => x.document)?.document.processing.tool_version;
  $('#sb-version').textContent = ver ? `v${ver}` : '';
}

function render() {
  renderChrome();
  const v = S.route.view;
  $('#view').innerHTML = v === 'runs' ? viewRuns() : v === 'inbox' ? viewInbox() : v === 'run' ? viewRun() : viewDashboard();
  icons();
}

// ------------------------------------------------------------------ dashboard / runs / inbox
function emptyState() {
  return `<div class="empty fade">
    <h2>Run your first repository</h2>
    <p>Point the pipeline at a folder, a .zip or a single file. It reads the data and the README, links every column to its description, and asks you about anything it isn't sure of.</p>
    <div class="btn-row">
      ${S.samples.map((s) => `<button class="btn" data-sample="${esc(s.name)}">${icon('play')}<span class="mono">${esc(s.name)}</span></button>`).join('')}
      <button class="btn btn-primary" data-action="new-run">${icon('folder-open')}Choose a repository</button>
    </div>
  </div>`;
}

function runRow(r, { select = false } = {}) {
  const s = r.summary;
  const busy = r.status === 'running';
  return `<tr class="click" data-href="#/run/${r.id}" tabindex="0">
    ${select ? `<td class="cell-check"><input type="checkbox" class="checkbox" data-select="${r.id}" ${S.selected.has(r.id) ? 'checked' : ''} ${busy ? 'disabled' : ''} aria-label="Select ${esc(r.repository)} ${r.id}"></td>` : ''}
    <td><div class="run-cell"><span class="mono">${esc(r.repository)} <span class="faint">${esc(r.id)}</span></span><span class="faint truncate">${esc(r.title || r.error || '')}</span></div></td>
    <td>${status(shownStatus(r), r.pending)}</td>
    <td class="r mono num muted">${s ? `${s.matched}/${s.variables}` : '—'}</td>
    <td class="r mono num faint">${r.duration_ms != null ? `${r.duration_ms} ms` : '—'}</td>
    <td class="r faint nowrap">${relTime(r.created_at)}</td>
    <td class="r cell-actions"><button class="btn btn-ghost btn-icon btn-sm row-action" data-delete="${r.id}" ${busy ? 'disabled' : ''} aria-label="Delete run ${r.id}" title="Delete run">${icon('trash-2')}</button></td>
  </tr>`;
}

function runsTable(runs, { select = false } = {}) {
  if (!runs.length) return '<div class="panel-empty">No runs here.</div>';
  const selectable = runs.filter((r) => r.status !== 'running');
  const all = selectable.length > 0 && selectable.every((r) => S.selected.has(r.id));
  return `<table class="table"><thead><tr>
    ${select ? `<th class="cell-check"><input type="checkbox" class="checkbox" data-select-all ${all ? 'checked' : ''} aria-label="Select all runs"></th>` : ''}
    <th>Repository</th><th>Status</th><th class="r">Matched</th><th class="r">Runtime</th><th class="r">Started</th><th class="cell-actions"><span class="sr-only">Actions</span></th>
  </tr></thead><tbody>${runs.map((r) => runRow(r, { select })).join('')}</tbody></table>`;
}

function viewDashboard() {
  const pending = S.runs.reduce((n, r) => n + (S.replay[r.id] ? 0 : r.pending || 0), 0);
  const active = S.runs.filter((r) => r.status === 'running' || r.pending || S.replay[r.id]);
  const rest = S.runs.filter((r) => !active.includes(r));
  const [m, t] = S.runs.reduce((a, r) => (r.summary ? [a[0] + r.summary.matched, a[1] + r.summary.variables] : a), [0, 0]);
  const durs = S.runs.map((r) => r.duration_ms).filter((x) => x != null).sort((a, b) => a - b);
  const stat = (label, value, note, hot = false) => `<div class="stat"><div class="stat-label">${label}</div><div class="stat-value ${hot ? 'hot' : ''}">${value}</div><div class="stat-note">${note}</div></div>`;
  const sub = S.runs.length
    ? `${plural(S.runs.length, 'run')} this session · ${pending ? `<span class="c-accent">${plural(pending, 'decision')} waiting on you</span>` : 'nothing waiting on you'}`
    : 'No runs yet this session';
  return `<div class="page fade">
    <div class="page-head"><div><h1>${TEAM}</h1><p class="sub">${sub}</p></div></div>
    ${!S.runs.length ? emptyState() : `
    <div class="stats">
      ${stat('Runs', S.runs.length, `${rest.length} finished`)}
      ${stat('Waiting on you', pending, `${plural(S.runs.filter((r) => r.pending).length, 'run')} at the review gate`, pending > 0)}
      ${stat('Columns matched', t ? `${((m / t) * 100).toFixed(1)}%` : '—', `${m} of ${t}`)}
      ${stat('Median runtime', durs.length ? `${durs[Math.floor(durs.length / 2)]} ms` : '—', 'real pipeline time')}
    </div>
    <section class="section">
      <div class="section-head"><h2>Needs attention</h2><span class="faint">${active.length}</span></div>
      <div class="panel">${active.length ? runsTable(active) : '<div class="panel-empty">Nothing is waiting on you.</div>'}</div>
    </section>
    <section class="section">
      <div class="section-head"><h2>Recent</h2><a class="link" href="#/runs">All runs</a></div>
      <div class="panel">${rest.length ? runsTable(rest.slice(0, 8)) : '<div class="panel-empty">Finished runs show up here.</div>'}</div>
    </section>`}
  </div>`;
}

function viewRuns() {
  const filters = [['all', 'All'], ['review', 'Needs review'], ['ready', 'Ready'], ['exported', 'Exported'], ['failed', 'Failed']];
  const runs = S.runFilter === 'all' ? S.runs : S.runs.filter((r) => r.status === S.runFilter);
  const sel = [...S.selected].filter((id) => S.runs.some((r) => r.id === id));
  return `<div class="page fade">
    <div class="page-head"><div><h1>Runs</h1><p class="sub">Held in server memory for this session. Deleting a run also removes anything uploaded for it.</p></div></div>
    ${!S.runs.length ? emptyState() : `
    <div class="toolbar">
      <div class="segmented" role="group" aria-label="Filter by status">
        ${filters.map(([k, l]) => `<button data-filter="${k}" aria-pressed="${S.runFilter === k}">${l}<span class="faint mono"> ${k === 'all' ? S.runs.length : S.runs.filter((r) => r.status === k).length}</span></button>`).join('')}
      </div>
      <div class="toolbar-right">${sel.length ? `
        <span class="muted">${sel.length} selected</span>
        <button class="btn btn-sm btn-ghost" data-action="clear-selection">Clear</button>
        <button class="btn btn-sm btn-danger" data-action="delete-selected">${icon('trash-2')}Delete</button>` : '<span class="faint">Select runs to delete several at once</span>'}
      </div>
    </div>
    <div class="panel">${runsTable(runs, { select: true })}</div>`}
  </div>`;
}

function viewInbox() {
  const groups = S.runs.filter((r) => r.pending && !S.replay[r.id] && S.details[r.id]);
  const total = groups.reduce((n, r) => n + r.pending, 0);
  return `<div class="page fade">
    <div class="page-head"><div><h1>Review</h1>
      <p class="sub">Everything the pipeline wasn't sure about, across runs. Matches below ${fmtConf(S.threshold)}, ambiguous links, undocumented columns, and README variables missing from the data.</p></div>
      ${total ? status('review', total) : ''}</div>
    ${groups.length ? groups.map((r) => `
      <section class="section">
        <div class="section-head"><a class="mono" href="#/run/${r.id}/review">${esc(r.repository)} <span class="faint">${esc(r.id)}</span></a><span class="faint">${plural(r.pending, 'item')}</span></div>
        ${reviewList(S.details[r.id], { onlyPending: true })}
      </section>`).join('')
    : `<div class="empty"><h2>All caught up</h2><p>No decisions are waiting.</p></div>`}
  </div>`;
}

// ------------------------------------------------------------------ run page
function stageStates(d) {
  const seen = new Set(d.events.map((e) => e.stage));
  const last = d.events.filter((e) => e.stage !== '_end').at(-1)?.stage;
  const durs = d.document?.processing?.stage_durations_ms || {};
  let states = STAGES.map((st) => {
    const ms = st.backend.reduce((n, b) => n + (durs[b] || 0), 0);
    let state;
    if (d.status === 'failed') state = st.backend.some((b) => seen.has(b)) ? 'done' : 'todo';
    else if (d.status === 'running') state = st.backend.includes(last) ? 'active' : st.backend.some((b) => seen.has(b)) ? 'done' : 'todo';
    else if (st.key === 'review') state = d.pending ? 'active' : 'done';
    else if (st.key === 'validation') state = d.pending ? 'todo' : 'done';
    else if (st.key === 'export') state = d.status === 'exported' ? 'done' : d.pending ? 'todo' : 'active';
    else state = 'done';
    return { ...st, state, ms };
  });
  const k = replayStep(d);
  if (k !== null) states = states.map((s, i) => ({ ...s, state: i < k ? 'done' : i === k ? 'active' : 'todo' }));
  return states;
}

function stepper(d) {
  return `<ol class="stepper" aria-label="Pipeline stages">${stageStates(d).map((st) => `
    <li class="step ${st.state}" ${st.state === 'active' ? 'aria-current="step"' : ''}>
      <span class="dot"></span><div class="step-name">${st.label}</div>
      <div class="step-ms">${st.state === 'done' && st.ms ? `${st.ms} ms` : st.state === 'active' ? (st.key === 'review' ? 'waiting on you' : st.key === 'export' ? 'ready' : 'working') : '&nbsp;'}</div>
    </li>`).join('')}</ol>`;
}

function viewRun() {
  const d = S.details[S.route.runId];
  if (!d) return '<div class="page faint">Loading…</div>';
  const doc = d.document;
  const tab = S.route.tab || 'overview';
  const replaying = !!S.replay[d.id];
  const shown = replaying ? 'running' : d.status;
  const canExport = doc && !d.pending && !replaying;
  const issues = doc?.issues || [];
  const serious = issues.filter((i) => i.severity !== 'info').length;
  const count = {
    review: d.items.length && !replaying ? `<span class="n ${d.pending ? 'hot' : ''}">${d.pending || '✓'}</span>` : '',
    variables: doc ? `<span class="n">${doc.summary.variables}</span>` : '',
    issues: issues.length ? `<span class="n ${serious ? 'hot' : ''}">${issues.length}</span>` : '',
  };
  const meta = doc ? [
    ['datasets', doc.summary.datasets], ['columns matched', `${doc.summary.matched}/${doc.summary.variables}`],
    ['warnings', doc.summary.warnings], ['errors', doc.summary.errors], ['runtime', `${doc.processing.duration_ms ?? '—'} ms`],
    ['review below', fmtConf(d.threshold)], ['run', d.id],
  ].map(([k, v]) => `<span>${k} <b>${esc(v)}</b></span>`).join('') : `<span>${d.status === 'failed' ? esc(d.error) : 'Processing…'}</span>`;
  const body = doc || tab === 'overview' || tab === 'log' ? renderTab(d, tab)
    : `<p class="faint">${d.status === 'failed' ? esc(d.error) : 'Still processing…'}</p>`;
  return `<div class="fade">
    <div class="run-head">
      <div class="run-head-row">
        <div class="min0">
          <div class="run-title"><h1>${esc(d.repository)}</h1>${status(shown, d.pending)}</div>
          ${doc?.project?.title ? `<p class="run-sub">${esc(doc.project.title)}</p>` : ''}
        </div>
        <div class="actions">
          <button class="btn" data-action="rerun" title="Run again with the current threshold (${fmtConf(S.threshold)})">${icon('rotate-cw')}Re-run</button>
          <button class="btn btn-icon btn-danger" data-action="delete-run" ${d.status === 'running' ? 'disabled' : ''} aria-label="Delete run" title="Delete run">${icon('trash-2')}</button>
          <button class="btn btn-primary" data-action="export" ${canExport ? '' : 'disabled'} title="${canExport ? 'Download the reviewed, re-validated JSON' : 'Finish the review first'}">${icon('download')}Export JSON</button>
        </div>
      </div>
      <div class="run-meta">${meta}</div>
      ${stepper(d)}
    </div>
    <div class="tabs-wrap"><nav class="tabs" role="tablist" aria-label="Run sections">
      ${TABS.map(([k, l], i) => `<a class="tab" role="tab" aria-selected="${tab === k}" href="#/run/${d.id}/${k}" title="Shortcut ${i + 1}">${l}${count[k] || ''}</a>`).join('')}
    </nav></div>
    <div class="page" role="tabpanel">${body}</div>
  </div>`;
}

function renderTab(d, tab) {
  const fn = { review: tabReview, variables: tabVariables, files: tabFiles, metadata: tabMetadata, issues: tabIssues, json: tabJson, log: tabLog }[tab];
  return (fn || tabOverview)(d);
}

// ---- the narrative: one plain sentence per stage, with the technical facts underneath
function narrate(d) {
  const doc = d.document, P = doc.project;
  const tabular = doc.files.filter((f) => f.role === 'tabular');
  const docs = doc.files.filter((f) => f.role === 'documentation');
  const other = doc.files.filter((f) => f.role === 'other');
  const failed = doc.files.filter((f) => f.status === 'failed');

  const inspection = {
    plain: `Found ${plural(doc.files.length, 'file')}: ${plural(tabular.length, 'data table')} and ${plural(docs.length, 'documentation file')}${other.length ? `. Skipped ${other.length} the pipeline doesn't read` : ''}.`,
    tech: `${fmtBytes(doc.files.reduce((n, f) => n + f.size_bytes, 0))} on disk · sha256 recorded for every file`,
    details: [
      ...doc.datasets.map((ds) => {
        const st = ds.structure, dl = DELIM[st.delimiter];
        const kind = ds.worksheet_name ? `a spreadsheet sheet (“${esc(ds.worksheet_name)}”)` : dl ? `a ${dl[0]} table` : 'a table';
        let plain = `${code(ds.file)} is ${kind} with ${ds.row_count} rows and ${ds.column_count} columns. ${st.header_row ? `The header is on row ${st.header_row}.` : 'No header row was found, so columns were given generated names.'}`;
        if (ds.documented_as && ds.documented_as !== ds.file) plain += ` The README calls it ${code(ds.documented_as)}; the file on disk wins.`;
        const tech = [`encoding ${esc(st.encoding)} (${encNote(st.encoding)})`, dl && `delimiter ${dl[1]}`, st.quote_char && `quote ${esc(st.quote_char)}`, `header confidence ${fmtConf(st.header_confidence)}`].filter(Boolean).join(' · ');
        return { plain, tech, tone: ds.documented_as && ds.documented_as !== ds.file ? 'amber' : null };
      }),
      ...failed.map((f) => ({ plain: `${code(f.path)} could not be read. Details are on the Issues tab.`, tech: `${esc(f.format)} · ${fmtBytes(f.size_bytes)}`, tone: 'red' })),
    ],
  };

  const found = [];
  if (P.title) found.push('the title');
  if (P.creators.length) found.push(plural(P.creators.length, 'author'));
  if (P.contacts.length) found.push(plural(P.contacts.length, 'contact'));
  if (P.collection_period) found.push(`the collection period (${esc(P.collection_period.text)})`);
  if (P.geographic_location) found.push('the location');
  if (P.license) found.push('the license');
  if (P.funding.length) found.push('funding');
  if (P.related_datasets.length) found.push(plural(P.related_datasets.length, 'related-dataset link'));
  const notStated = ['description', 'identifier', 'citation', 'keywords', 'funding'].filter((k) => !P[k] || (Array.isArray(P[k]) && !P[k].length));
  const describedBy = (file) => doc.datasets.reduce((n, ds) => n
    + ds.variables.filter((v) => Object.values(v.sources || {}).some((s) => s.file === file)).length, 0);
  const schema = {
    plain: docs.length ? `Read ${plural(docs.length, 'README')} and picked up ${joinList(found) || 'no project details'}.`
      : 'No README was found, so the columns have no descriptions.',
    tech: `${P.additional_fields.length} extra key/value pairs kept as-is${notStated.length ? ` · not stated: ${notStated.join(', ')}` : ''}`,
    details: doc.documents.map((dc) => ({
      plain: `${code(dc.file)} has ${plural(dc.sections.length, 'section')} and ${plural(describedBy(dc.file), 'variable description')}${dc.describes.length ? `, covering ${dc.describes.map(code).join(', ')}` : ''}.`,
      tech: `format ${esc(dc.format)} · encoding ${esc(dc.encoding)} (${encNote(dc.encoding)})`,
    })),
  };

  const all = doc.datasets.flatMap((ds) => ds.variables.map((v) => ({ ds, v })));
  const matched = all.filter(({ v }) => v.match.status === 'matched').length;
  const by = {};
  all.forEach(({ v }) => { const k = v.match.status === 'matched' ? v.match.method : v.match.status; by[k] = (by[k] || 0) + 1; });
  const phantoms = doc.datasets.flatMap((ds) => ds.unmatched_documented_variables.map((u) => ({ ds, u })));
  const why = (m) => ({
    case_insensitive: 'same name, different capitalisation',
    normalized: 'same name once spacing and punctuation are ignored',
    token_abbreviation: m.evidence[0] ? esc(m.evidence[0]).replace('abbreviates', 'reads as shorthand for') : 'looks like an abbreviation',
    fuzzy: 'spelled very similarly',
  }[m.method] || esc(m.method));
  const reconcile = {
    plain: `Linked ${matched} of ${all.length} columns to their README descriptions${matched === all.length ? '' : `; ${all.length - matched} have no description`}.${phantoms.length ? ` ${plural(phantoms.length, 'documented variable')} ${phantoms.length === 1 ? 'has' : 'have'} no matching column.` : ''}`,
    tech: Object.entries(by).map(([k, n]) => `${k} ${n}`).join(' · '),
    details: [
      ...all.filter(({ v }) => v.match.status !== 'matched' || v.match.method !== 'exact').map(({ ds, v }) => {
        const m = v.match;
        if (m.status === 'unmatched') return { plain: `${code(v.name)} is in ${code(ds.file)} but the README never describes it.`, tech: `column ${v.position + 1} · ${esc(v.type)}`, tone: 'blue' };
        return {
          plain: `${code(v.name)} ↔ ${code(m.documented_name)}: ${why(m)}.`,
          tech: `${esc(m.method)} · confidence ${fmtConf(m.confidence)}${m.alternatives[0] ? ` · runner-up ${esc(m.alternatives[0].documented_name)} ${fmtConf(m.alternatives[0].confidence)}` : ''}`,
          tone: m.confidence < d.threshold ? 'amber' : null,
        };
      }),
      ...phantoms.map(({ ds, u }) => ({
        plain: `The README describes ${code(u.name)}${u.description ? ` (“${esc(u.description)}”)` : ''}, but ${code(ds.file)} has no such column.`,
        tech: `DOCUMENTED_VARIABLE_NOT_IN_DATA${u.lines ? ` · README line ${u.lines[0]}` : ''}`, tone: 'red',
      })),
      ...doc.relationships.filter((r) => r.type === 'shared_variables').map((r) => ({
        plain: `${r.datasets.map(code).join(' and ')} share ${plural(r.variables.length, 'column')} (${r.variables.slice(0, 5).map(code).join(', ')}${r.variables.length > 5 ? '…' : ''}), so they can likely be joined.`,
        tech: `shared_variables · confidence ${fmtConf(r.confidence)}`,
      })),
    ],
  };

  const n = d.items.length, p = d.pending;
  const review = {
    plain: !n ? `Nothing needed a human decision at the ${fmtConf(d.threshold)} threshold.`
      : p ? `${plural(p, 'item')} ${p === 1 ? 'needs' : 'need'} your decision before export.` : `All ${plural(n, 'decision')} made.`,
    tech: `flagged when confidence < ${fmtConf(d.threshold)}, ambiguous, undocumented, or documented but missing · ${n} flagged`,
    details: d.items.map((it) => {
      const dec = d.decisions[it.id];
      return { plain: questionText(it), tech: dec ? `you chose: ${esc(dec.decision)}` : 'waiting for you', tone: dec ? null : 'amber' };
    }),
    action: p ? `<div class="btn-row"><a class="btn btn-primary btn-sm" href="#/run/${d.id}/review">Start review${icon('arrow-right')}</a></div>` : '',
  };

  const sev = ['error', 'warning', 'info'].map((s) => [s, doc.issues.filter((i) => i.severity === s).length]).filter(([, c]) => c);
  const validation = {
    plain: p ? 'The pipeline output already passes the JSON Schema. It gets checked again once your decisions are applied.'
      : `Passes JSON Schema v${esc(doc.schema_version)}, with ${plural(doc.summary.warnings, 'warning')} and ${plural(doc.summary.errors, 'error')}.`,
    tech: `pydantic validation · ${doc.issues.length} issues on record${sev.length ? ` (${sev.map(([s, c]) => `${c} ${s}`).join(', ')})` : ''}`,
  };

  const records = doc.datasets.reduce((s, ds) => s + ds.records_included, 0);
  const exp = {
    plain: d.status === 'exported' ? 'Exported. You can download it again any time.' : p ? 'Unlocks once the review is finished.' : 'Ready to download.',
    tech: `${esc(doc.repository.name)}.normalized.json · ${plural(doc.datasets.length, 'dataset')} · ${records} records · plus a decision log`,
    action: p ? '' : `<div class="btn-row"><button class="btn btn-primary btn-sm" data-action="export">${icon('download')}Export JSON</button><button class="btn btn-sm" data-action="export-log">${icon('scroll-text')}Decision log</button></div>`,
  };
  return [inspection, schema, reconcile, review, validation, exp];
}

function tabOverview(d) {
  const states = stageStates(d);
  const replaying = !!S.replay[d.id];
  const story = d.document ? narrate(d) : null;
  const MAX = 6;
  const items = STAGES.map((st, i) => {
    const { state, ms } = states[i];
    const n = story?.[i];
    let body = '';
    if (state === 'active' && (replaying || d.status === 'running')) {
      body = `<p class="tl-plain working">${icon('loader-circle', 'spin')}${esc(st.working || 'Working…')}</p>`;
    } else if (state === 'todo' && (replaying || d.status === 'running')) {
      body = '';
    } else if (n) {
      const det = n.details || [];
      body = `<p class="tl-plain">${n.plain}</p>${n.tech ? `<p class="tl-tech">${n.tech}</p>` : ''}
        ${det.length ? `<ul class="tl-details">${det.slice(0, MAX).map((x) => `<li class="${x.tone ? `tone-${x.tone}` : ''}"><div>${x.plain}</div>${x.tech ? `<div class="tl-tech">${x.tech}</div>` : ''}</li>`).join('')}
          ${det.length > MAX ? `<li class="more">and ${det.length - MAX} more · see the ${i === 2 ? 'Variables' : 'Files'} tab</li>` : ''}</ul>` : ''}
        ${n.action || ''}`;
    } else if (d.status === 'failed') {
      body = '<p class="tl-plain faint">Did not run.</p>';
    }
    return `<li class="tl-item ${state}">
      <span class="tl-marker" aria-hidden="true">${state === 'done' ? icon('check') : i + 1}</span>
      <div class="min0"><div class="tl-title">${st.label}${state === 'done' && ms ? `<span class="ms">${ms} ms</span>` : ''}<span class="sr-only">(${state})</span></div>${body}</div>
    </li>`;
  }).join('');
  return `<div class="overview">
    <ol class="timeline">${items}</ol>
    <aside class="ov-side">${nextStep(d, replaying)}${glance(d, replaying)}</aside>
  </div>`;
}

function nextStep(d, replaying) {
  if (d.status === 'failed') return `<div class="panel next"><h2 class="c-red">Run failed</h2><p class="mono">${esc(d.error)}</p><div class="btn-row"><button class="btn btn-sm" data-action="rerun">${icon('rotate-cw')}Try again</button></div></div>`;
  if (replaying || d.status === 'running') return `<div class="panel next"><h2>Walking through the run</h2>
    <p>The pipeline itself finishes in well under a second. This page replays each stage slowly so you can follow along.</p>
    <div class="btn-row"><button class="btn btn-sm" data-action="skip-replay">Skip to the result</button></div></div>`;
  const replay = `<button class="btn btn-sm btn-ghost" data-action="replay">${icon('rotate-ccw')}Replay</button>`;
  if (d.pending) return `<div class="panel next hot"><h2>${plural(d.pending, 'decision')} waiting on you</h2>
    <p>The pipeline wasn't sure about ${d.pending === 1 ? 'this one' : 'these'}. What you decide goes into the exported file and the decision log.</p>
    <div class="btn-row"><a class="btn btn-primary btn-sm" href="#/run/${d.id}/review">Start review${icon('arrow-right')}</a>${replay}</div></div>`;
  return `<div class="panel next"><h2>${d.status === 'exported' ? 'Exported' : 'Ready to export'}</h2>
    <p>${d.items.length ? 'Every flagged item has a decision.' : 'Nothing needed a human decision.'} The export is re-validated against the schema.</p>
    <div class="btn-row"><button class="btn btn-primary btn-sm" data-action="export">${icon('download')}Export JSON</button><button class="btn btn-sm" data-action="export-log">${icon('scroll-text')}Log</button>${replay}</div></div>`;
}

function glance(d) {
  const doc = d.document;
  const src = d.spec?.sample || d.spec?.path || (d.spec?.upload ? 'uploaded files' : d.source);
  const rows = [
    ['source', `${d.source}: ${src}`],
    doc && ['files', doc.repository.file_count],
    doc && ['datasets', doc.summary.datasets],
    doc && ['columns', `${doc.summary.matched} matched / ${doc.summary.variables}`],
    doc && ['issues', `${doc.summary.warnings} warn · ${doc.summary.errors} err`],
    doc && ['runtime', `${doc.processing.duration_ms} ms`],
    ['review below', fmtConf(d.threshold)],
    ['your edits', Object.keys(d.edits || {}).length ? plural(Object.keys(d.edits).length, 'variable') : 'none'],
    doc && ['tool', `v${doc.processing.tool_version}`],
  ].filter(Boolean);
  return `<div class="panel"><div class="panel-head"><h2>At a glance</h2></div>
    <dl class="facts">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl></div>`;
}

// ---- review
function questionText(it) {
  switch (it.kind) {
    case 'low_confidence':
    case 'ambiguous': return `Is the column ${code(it.column)} the README's ${code(it.documented_name)}?`;
    case 'undocumented_column': return `The column ${code(it.column)} isn't described anywhere in the README.`;
    default: return `The README describes ${code(it.documented_name)}, but the data has no column for it.`;
  }
}
function reasonText(it) {
  if (it.kind === 'low_confidence' || it.kind === 'ambiguous') {
    const ev = it.evidence[0];
    return ev ? `Linked because ${esc(ev).replace('abbreviates', 'looks like shorthand for')}.` : 'The names are similar but not identical.';
  }
  if (it.kind === 'phantom_variable') return 'Usually a typo in the README, a column dropped before upload, or a variable that lives in another file.';
  return 'It stays in the output, just without a description.';
}

// What each kind of review item is, in plain terms, and which backend issue code it corresponds to.
const ISSUE_INFO = {
  low_confidence: {
    title: 'Uncertain match', area: 'Variable matching', code: 'MATCH_LOW_CONFIDENCE',
    what: 'The pipeline linked a data column to a README variable, but the two names are not identical, so the link rests on a guess.',
    why: (it, d) => it.confidence >= 0.85
      ? `The backend accepted it (it accepts anything at 0.85 or above), but ${fmtConf(it.confidence)} is below your review threshold of ${fmtConf(d.threshold)}.`
      : `${fmtConf(it.confidence)} is also below the backend's own acceptance level of 0.85.`,
    actions: [['Accept', 'keep the link. The column gets the README description and unit.'],
      ['Reject', 'drop the link. The column is exported without a description and the README name is listed as missing from the data.'],
      ['Edit', 'link it to a different README variable, or correct the description, unit or type by hand.']],
  },
  ambiguous: {
    title: 'Ambiguous match', area: 'Variable matching', code: 'MATCH_AMBIGUOUS',
    what: 'Two or more README variables fit this column almost equally well, so the pipeline could not pick one with confidence.',
    why: () => 'The best and second-best candidates scored within 0.05 of each other.',
    actions: [['Accept', 'keep the top candidate.'], ['Reject', 'leave the column undocumented.'], ['Edit', 'pick the right README variable yourself.']],
  },
  undocumented_column: {
    title: 'Undocumented column', area: 'Documentation gap', code: 'VARIABLE_UNDOCUMENTED',
    what: 'This column exists in the data file, but nothing in the README describes it.',
    why: () => 'No README variable scored high enough to be linked to it.',
    actions: [['Got it', 'export it as-is, without a description.'], ['Edit', 'write a description yourself, or link it to a README variable.']],
  },
  phantom_variable: {
    title: 'Missing from data', area: 'Documentation vs. data', code: 'DOCUMENTED_VARIABLE_NOT_IN_DATA',
    what: 'The README documents a variable, but no column in the data file matches it.',
    why: () => 'Common causes: a typo in the README, a column renamed or dropped before upload, or a variable that lives in a different file.',
    actions: [['Got it', 'record it as documented but absent.'], ['Edit a column', 'if one of the columns below really is this variable, open it and link it.']],
  },
};
const METHOD_INFO = {
  exact: 'Step 1 of 5: the names are identical.',
  case_insensitive: 'Step 2 of 5: same letters, different capitalisation.',
  normalized: 'Step 3 of 5: identical once spaces, punctuation and case are removed.',
  token_abbreviation: 'Step 4 of 5: one name reads as an abbreviation of the other.',
  fuzzy: 'Step 5 of 5: a RapidFuzz similarity score; the names are only spelled alike.',
  human_edited: 'Set by hand.',
};

function reviewDetail(d, it) {
  const info = ISSUE_INFO[it.kind];
  const doc = d.document;
  const ds = doc.datasets.find((x) => x.id === it.dataset);
  const v = it.column && ds?.variables.find((x) => x.name === it.column);
  const related = doc.issues.filter((i) => (it.column && i.location?.column === it.column && (!i.location.file || i.location.file === it.file))
    || (it.documented_name && i.code === info.code && i.message.includes(`'${it.documented_name}'`)));
  const sevTag = { fatal: 'tag-red', error: 'tag-red', warning: 'tag-amber', info: 'tag-blue' };
  const block = (title, html) => `<div class="rd-block"><div class="mini-label">${title}</div>${html}</div>`;

  const parts = [block('What kind of issue', `
    <div class="ri-head"><span class="tag ${KIND[it.kind].tag}">${info.title}</span><span class="muted">${info.area}</span><span class="code faint">${info.code}</span></div>
    <p style="margin-top:8px">${info.what}</p><p class="muted" style="margin-top:4px">${info.why(it, d)}</p>`)];

  parts.push(block('Recorded by the pipeline', related.length ? related.map((i) => `<div class="rd-issue">
      <span class="tag ${sevTag[i.severity]}">${esc(i.severity)}</span><div class="min0"><div>${esc(i.message)}</div>
      ${i.technical_detail ? `<div class="tl-tech">${esc(i.technical_detail)}</div>` : ''}${i.suggestion ? `<div class="issue-fix">${esc(i.suggestion)}</div>` : ''}</div></div>`).join('')
    : '<p class="muted">No backend issue for this one. It was flagged only because of your review threshold.</p>'));

  if (it.column && it.documented_name) {
    const cands = [{ documented_name: it.documented_name, confidence: it.confidence, chosen: true }, ...it.alternatives];
    parts.push(block('How the match was made', `<p>${esc(METHOD_INFO[it.method] || it.method || '')}</p>
      ${it.evidence.length ? `<p class="tl-tech">${it.evidence.map(esc).join(' · ')}</p>` : ''}
      <table class="table dense rd-cands"><tbody>${cands.map((c) => `<tr><td class="mono ${c.chosen ? '' : 'muted'}">${esc(c.documented_name)}${c.chosen ? ' <span class="tag">chosen</span>' : ''}</td>
        <td class="r nowrap"><span class="meter"><span class="bg-${confTone(c.confidence)}" style="width:${c.confidence * 100}%"></span></span><span class="mono c-${confTone(c.confidence)}">${fmtConf(c.confidence)}</span></td></tr>`).join('')}
      <tr><td class="faint">your review threshold</td><td class="r mono faint">${fmtConf(d.threshold)}</td></tr></tbody></table>`));
  }

  if (v) {
    const st = v.statistics || {};
    const sample = (ds.records || []).slice(0, 8).map((r) => r[v.name] ?? r[v.original_name]).filter((x) => x != null);
    parts.push(block('What the column contains', `<dl class="kv">
      <dt>type</dt><dd>${esc(v.type)} · ${esc(v.role)}</dd>
      <dt>values</dt><dd>${st.count ?? '—'} rows · ${st.missing ?? 0} missing · ${st.distinct ?? '—'} distinct</dd>
      ${st.min != null ? `<dt>range</dt><dd>${esc(st.min)} to ${esc(st.max)}</dd>` : ''}
      ${sample.length ? `<dt>first rows</dt><dd>${sample.map(esc).join(', ')}</dd>` : ''}
      ${st.top_values?.length ? `<dt>common</dt><dd>${st.top_values.slice(0, 6).map(esc).join(', ')}</dd>` : ''}</dl>`));
  }

  const l = it.source?.lines;
  if (it.description || l) {
    parts.push(block('What the README says', `${it.description ? `<p>“${esc(it.description)}”${it.unit ? ` <span class="faint">in ${esc(it.unit)}</span>` : ''}</p>` : ''}
      ${l ? `<p class="tl-tech">${esc(it.source.file || ds?.file || 'README')} · line ${l[0]}${l[1] !== l[0] ? `–${l[1]}` : ''}</p>` : ''}`));
  }

  if (it.kind === 'phantom_variable' && ds) {
    const loose = ds.variables.filter((x) => x.match.status !== 'matched');
    parts.push(block(`Columns in ${esc(ds.file)} with no README link`, loose.length
      ? `<div class="btn-row" style="margin-top:0">${loose.map((x) => `<button class="btn btn-sm" data-run="${d.id}" data-edit-var="${esc(`${ds.id}::${x.name}`)}"><span class="mono">${esc(x.name)}</span></button>`).join('')}</div>`
      : '<p class="muted">Every column is already linked, so this is most likely a README error. If a column was linked wrongly, open it on the Variables tab.</p>'));
  }

  parts.push(block('Your options', `<ul class="rd-actions">${info.actions.map(([a, t]) => `<li><b>${a}</b>: ${t}</li>`).join('')}</ul>
    ${v ? `<div class="btn-row"><button class="btn btn-sm" data-run="${d.id}" data-edit-var="${esc(`${ds.id}::${v.name}`)}">${icon('pencil')}Edit ${esc(v.name)}</button></div>` : ''}`));
  return `<div class="rd">${parts.join('')}</div>`;
}

function reviewList(d, { onlyPending = false } = {}) {
  const rows = d.items.map((it, idx) => ({ it, idx })).filter(({ it }) => !onlyPending || !d.decisions[it.id]);
  if (!rows.length) return '';
  const label = { accept: 'Accepted', reject: 'Rejected', acknowledge: 'Acknowledged' };
  const btn = (a, it) => ({
    accept: `<button class="btn btn-sm btn-accept" data-decide="accept" data-run="${d.id}" data-item="${esc(it.id)}">${icon('check')}Accept <span class="kbd">A</span></button>`,
    reject: `<button class="btn btn-sm" data-decide="reject" data-run="${d.id}" data-item="${esc(it.id)}">${icon('x')}Reject <span class="kbd">R</span></button>`,
    acknowledge: `<button class="btn btn-sm" data-decide="acknowledge" data-run="${d.id}" data-item="${esc(it.id)}">${icon('check')}Got it <span class="kbd">A</span></button>`,
  }[a]);
  return `<div class="panel">${rows.map(({ it, idx }) => {
    const dec = d.decisions[it.id], k = KIND[it.kind];
    const l = it.source?.lines;
    const tech = [
      it.method && `method ${esc(it.method)}`,
      it.column && it.documented_name && `confidence ${fmtConf(it.confidence)}`,
      it.alternatives.length && `alternatives ${it.alternatives.map((a) => `${esc(a.documented_name)} ${fmtConf(a.confidence)}`).join(', ')}`,
      l && `${esc(it.source.file || 'README')} line ${l[0]}${l[1] !== l[0] ? `–${l[1]}` : ''}`,
    ].filter(Boolean).join(' · ');
    const actions = dec
      ? `<span class="decided ${dec.decision}">${icon(dec.decision === 'reject' ? 'x' : 'check')}${label[dec.decision]}</span>
         <button class="btn btn-sm btn-ghost" data-decide="undo" data-run="${d.id}" data-item="${esc(it.id)}">${icon('undo-2')}Undo</button>`
      : k.actions.map((a) => btn(a, it)).join('');
    const open = S.openItems.has(it.id);
    const noteTag = dec?.note === AUTO_NOTE ? ' <span class="tag">via edit</span>' : '';
    return `<div class="review-item ${!onlyPending && idx === S.focusIdx ? 'focus' : ''} ${dec ? 'is-decided' : ''}" data-review-idx="${idx}">
      <div class="min0">
        <div class="ri-head">
          <button class="ri-toggle" data-expand="${esc(it.id)}" aria-expanded="${open}" aria-label="${open ? 'Hide' : 'Show'} details">${icon(open ? 'chevron-down' : 'chevron-right')}</button>
          <span class="tag ${k.tag}">${ISSUE_INFO[it.kind].title}</span><span class="faint">${ISSUE_INFO[it.kind].area}</span><span class="faint mono">${esc(it.file)}</span>${noteTag}
        </div>
        <p class="ri-q">${questionText(it)}</p>
        ${it.description ? `<p class="ri-readme"><span class="faint">README says</span> “${esc(it.description)}”${it.unit ? ` <span class="faint">in ${esc(it.unit)}</span>` : ''}</p>` : ''}
        <p class="ri-why">${reasonText(it)}</p>
        ${tech ? `<p class="ri-tech">${tech}</p>` : ''}
        ${open ? '' : `<button class="link ri-more" data-expand="${esc(it.id)}">Why is this flagged?</button>`}
      </div>
      <div class="ri-actions">${actions}</div>
      ${open ? `<div class="ri-detail">${reviewDetail(d, it)}</div>` : ''}
    </div>`;
  }).join('')}</div>`;
}

function tabReview(d) {
  if (S.replay[d.id]) return '<p class="faint">The review opens once the walkthrough reaches it. You can skip ahead on the Overview tab.</p>';
  if (!d.items.length) return `<div class="empty"><h2>Nothing to review</h2><p>Every column matched at ${fmtConf(d.threshold)} or above and every documented variable was found. Export is unlocked.</p></div>`;
  return `<div class="toolbar">
      <p class="hint"><span class="kbd">J</span><span class="kbd">K</span> move · <span class="kbd">O</span> details · <span class="kbd">A</span> accept · <span class="kbd">R</span> reject · <span class="kbd">U</span> undo · editing a variable also resolves its item</p>
      ${d.pending ? status('review', d.pending) : '<span class="status ready"><span class="d"></span>All resolved</span>'}
    </div>
    ${reviewList(d)}
    <p class="hint" style="margin-top:12px">Rejecting a match turns that column into an undocumented one and records the README name as missing from the data. The export is re-checked against the schema.</p>`;
}

// ---- variables
function tabVariables(d) {
  const datasets = d.document.datasets;
  if (!datasets.length) return '<div class="empty"><h2>No datasets</h2><p>No table could be read from this repository.</p></div>';
  const ds = datasets.find((x) => x.id === S.varDataset) || datasets[0];
  const q = S.varQuery.toLowerCase();
  const vars = ds.variables.filter((v) => !q || [v.name, v.match.documented_name, v.description, v.unit].some((x) => (x || '').toLowerCase().includes(q)));
  return `<div class="toolbar">
      ${datasets.length > 1 ? `<div class="segmented" role="group" aria-label="Dataset">${datasets.map((x) => `<button data-dataset="${esc(x.id)}" aria-pressed="${x.id === ds.id}" class="mono">${esc(x.file)}</button>`).join('')}</div>` : `<span class="mono muted">${esc(ds.file)}</span>`}
      <div class="toolbar-right">
        <input id="var-filter" class="input" style="width:220px" type="search" value="${esc(S.varQuery)}" placeholder="Filter columns…" aria-label="Filter columns">
        <button class="btn btn-sm" data-run="${d.id}" data-edit-var="${esc(`${ds.id}::${ds.variables[0]?.name}`)}" title="Step through every column next to the README and raw data">${icon('scan-search')}Check extraction</button>
      </div>
    </div>
    <div class="panel"><table class="table dense">
      <thead><tr><th style="width:40px">#</th><th>Column</th><th>README name</th><th>Type</th><th>Unit</th><th>How it matched</th><th>Confidence</th><th class="r">Missing</th><th>Description</th><th style="width:44px"><span class="sr-only">Edit</span></th></tr></thead>
      <tbody>${vars.map((v) => {
        const key = `${ds.id}::${v.name}`, open = S.openVar === key, m = v.match, st = v.statistics || {};
        const renamed = m.documented_name && m.documented_name !== v.name;
        return `<tr class="click ${open ? 'open' : ''}" data-var="${esc(key)}" tabindex="0" aria-expanded="${open}">
          <td class="mono faint">${v.position + 1}</td>
          <td class="mono nowrap">${esc(v.name)}${d.edits?.[key] ? ' <span class="tag tag-blue" title="Edited by you">edited</span>' : ''}</td>
          <td class="mono ${renamed ? 'c-accent' : 'faint'}">${esc(m.documented_name || '—')}</td>
          <td class="mono muted">${esc(v.type)}</td>
          <td class="mono muted">${esc(v.unit || '')}</td>
          <td class="mono ${m.method === 'exact' ? 'faint' : m.status === 'matched' ? 'muted' : 'c-amber'}">${esc(m.status === 'matched' ? m.method : m.status)}</td>
          <td class="nowrap"><span class="meter"><span class="bg-${confTone(m.confidence)}" style="width:${m.confidence * 100}%"></span></span><span class="mono c-${confTone(m.confidence)}">${fmtConf(m.confidence)}</span></td>
          <td class="r mono ${st.missing ? 'c-amber' : 'faint'}">${st.missing ?? '—'}</td>
          <td class="muted truncate" style="max-width:280px" title="${esc(v.description || '')}">${esc(v.description || '')}</td>
          <td class="r"><button class="btn btn-ghost btn-icon btn-sm" data-run="${d.id}" data-edit-var="${esc(key)}" aria-label="Check and edit ${esc(v.name)}" title="Check against the source and edit">${icon('pencil')}</button></td>
        </tr>${open ? `<tr class="var-detail"><td colspan="10">${varDetail(d, ds, v)}</td></tr>` : ''}`;
      }).join('') || '<tr><td colspan="10" class="faint">No columns match the filter.</td></tr>'}</tbody></table></div>
    ${ds.unmatched_documented_variables.length ? `<div class="panel"><div class="panel-head"><h2>In the README, not in ${esc(ds.file)}</h2></div>
      ${ds.unmatched_documented_variables.map((u) => `<div class="issue">${icon('circle-alert', 'c-red')}<div>${code(u.name)} <span class="muted">${esc(u.description || '')}</span> <span class="faint mono">${u.lines ? `line ${u.lines[0]}` : ''}</span></div></div>`).join('')}</div>` : ''}`;
}

function varEditor(d, ds, v) {
  const key = `${ds.id}::${v.name}`;
  const edits = d.edits?.[key] || {};
  const names = [...new Set([...ds.variables.map((x) => x.match.documented_name), ...ds.unmatched_documented_variables.map((u) => u.name)].filter(Boolean))].sort();
  const was = (f) => {
    if (!edits[f]) return '';
    const o = edits[f].original;
    return `<p class="field-hint"><span class="tag tag-blue">edited</span> pipeline had: <span class="mono">${o == null || o === '' || (Array.isArray(o) && !o.length) ? 'nothing' : esc(Array.isArray(o) ? o.join(', ') : o)}</span></p>`;
  };
  const text = (f, label, value, hint = '') => `<div><label class="field-label" for="ve-${f}">${label}</label>
    <input id="ve-${f}" name="${f}" class="input${f === 'documented_name' ? ' mono' : ''}" value="${esc(value ?? '')}" ${f === 'documented_name' ? 'list="ve-names"' : ''}>${hint}${was(f)}</div>`;
  const area = (f, label, value) => `<div class="span2"><label class="field-label" for="ve-${f}">${label}</label>
    <textarea id="ve-${f}" name="${f}" class="input textarea" rows="2">${esc(value ?? '')}</textarea>${was(f)}</div>`;
  const select = (f, label, value, opts, hint = '') => `<div><label class="field-label" for="ve-${f}">${label}</label>
    <select id="ve-${f}" name="${f}" class="select" style="width:100%">${opts.map((o) => `<option ${o === value ? 'selected' : ''}>${o}</option>`).join('')}</select>${hint}${was(f)}</div>`;
  const m = v.match;
  return `<form class="var-edit" data-ve-ds="${esc(ds.id)}" data-ve-col="${esc(v.name)}" autocomplete="off">
    <div class="extracted">
      <div class="mini-label">What the pipeline extracted</div>
      <dl class="kv">
        <dt>linked to</dt><dd>${m.documented_name ? `${esc(m.documented_name)} <span class="faint">· ${esc(m.method)} ${fmtConf(m.confidence)}</span>` : '<span class="faint">no README variable</span>'}</dd>
        <dt>read as</dt><dd>${esc(v.type)} · ${esc(v.role)}${v.statistics ? ` <span class="faint">· ${v.statistics.count} values, ${v.statistics.missing} missing</span>` : ''}</dd>
      </dl>
      <p class="field-hint">Compare with the original README and data file on the right. Anything wrong can be corrected below; the pipeline's values are kept so you can revert.</p>
    </div>
    <datalist id="ve-names">${names.map((n) => `<option value="${esc(n)}"></option>`).join('')}</datalist>
    <div class="var-edit-grid">
      ${text('documented_name', 'README variable it links to', v.match.documented_name, '<p class="field-hint">Pick from the README names, or clear it to unlink the column.</p>')}
      ${text('label', 'Label', v.label)}
      ${area('description', 'Description', v.description)}
      ${text('unit', 'Unit', v.unit)}
      ${text('missing_values', 'Missing-value markers', v.missing_values.join(', '), '<p class="field-hint">Comma-separated, e.g. NA, ., -99</p>')}
      ${select('type', 'Type', v.type, COLUMN_TYPES, '<p class="field-hint">Changes the declared type only; record values are not converted.</p>')}
      ${select('role', 'Role', v.role, COLUMN_ROLES)}
      ${area('notes', 'Notes', v.notes)}
    </div>
    <div class="drawer-actions">
      <button type="submit" class="btn btn-primary btn-sm">${icon('check')}Save</button>
      <button type="submit" class="btn btn-sm" data-next="1" title="Save, then open the next column (Ctrl+Enter)">Save &amp; next${icon('arrow-right')}</button>
      <button type="button" class="btn btn-sm btn-ghost" data-action="drawer-close">Cancel</button>
      ${Object.keys(edits).length ? `<button type="button" class="btn btn-sm btn-danger" style="margin-left:auto" data-action="revert-var" data-ve-ds="${esc(ds.id)}" data-ve-col="${esc(v.name)}">${icon('undo-2')}Revert to pipeline values</button>` : ''}
    </div>
  </form>`;
}

// ------------------------------------------------------------------ review drawer: form + original sources
function drawerCtx() {
  if (!S.drawer) return null;
  const d = S.details[S.drawer.runId];
  const [dsId, col] = S.drawer.key.split('::');
  const ds = d?.document?.datasets.find((x) => x.id === dsId);
  const idx = ds ? ds.variables.findIndex((x) => x.name === col) : -1;
  return idx < 0 ? null : { d, ds, v: ds.variables[idx], idx };
}
const formState = () => { const f = $('#drawer-root .var-edit'); return f ? JSON.stringify([...new FormData(f)]) : ''; };
const drawerDirty = () => !!S.drawer && formState() !== S.drawer.snapshot;
const confirmDiscard = () => !drawerDirty() || confirm('Discard your unsaved changes to this column?');

function openDrawer(key, runId = S.route.runId) {
  S.drawer = { runId, key, full: false, snapshot: '' };
  renderDrawer();
}
function closeDrawer(force = false) {
  if (!force && !confirmDiscard()) return;
  S.drawer = null;
  $('#drawer-root').innerHTML = '';
}
function stepDrawer(dir) {
  const ctx = drawerCtx();
  if (!ctx || !confirmDiscard()) return;
  const next = ctx.ds.variables[ctx.idx + dir];
  if (next) { S.drawer = { ...S.drawer, key: `${ctx.ds.id}::${next.name}`, full: false }; renderDrawer(); }
}

function renderDrawer() {
  const ctx = drawerCtx();
  if (!ctx) { S.drawer = null; $('#drawer-root').innerHTML = ''; return; }
  const { d, ds, v, idx } = ctx;
  const n = ds.variables.length;
  $('#drawer-root').innerHTML = `<div class="drawer-overlay" data-action="drawer-close"></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title">
      <header class="drawer-head">
        <div class="min0">
          <div class="faint mono" style="font-size:11.5px">${esc(ds.file)} · column ${idx + 1} of ${n}</div>
          <h2 id="drawer-title" class="mono" style="font-size:16px">${esc(v.name)}${d.edits?.[S.drawer.key] ? ' <span class="tag tag-blue">edited</span>' : ''}</h2>
        </div>
        <div class="actions">
          <button class="btn btn-icon btn-sm" data-action="drawer-prev" ${idx === 0 ? 'disabled' : ''} aria-label="Previous column" title="Previous column ( [ )">${icon('chevron-left')}</button>
          <button class="btn btn-icon btn-sm" data-action="drawer-next" ${idx === n - 1 ? 'disabled' : ''} aria-label="Next column" title="Next column ( ] )">${icon('chevron-right')}</button>
          <button class="btn btn-icon btn-sm btn-ghost" data-action="drawer-close" aria-label="Close" title="Close (Esc)">${icon('x')}</button>
        </div>
      </header>
      <div class="drawer-body">
        <section class="drawer-form">${varEditor(d, ds, v)}</section>
        <section class="drawer-source" aria-label="Original sources">${sourceShell(d, ds, v)}</section>
      </div>
    </aside>`;
  icons();
  S.drawer.snapshot = formState();
  $('#drawer-root .var-edit input')?.focus({ preventScroll: true });
  loadSources(ctx);
}

function readmeSource(d, ds, v) {
  const s = Object.values(v.sources || {}).find((x) => x.file !== 'reviewer' && x.lines);
  const file = s?.file || d.document.documents.find((dc) => dc.describes.includes(ds.file))?.file || d.document.documents[0]?.file;
  return { file, lines: s?.lines || null };
}

function sourceShell(d, ds, v) {
  const rs = readmeSource(d, ds, v);
  const st = ds.structure;
  return `<div class="src-block">
      <div class="src-head">
        <div><h3>README${rs.file ? ` <span class="faint mono">${esc(rs.file)}</span>` : ''}</h3>
          <p class="field-hint">${rs.lines ? `The description was extracted from line ${rs.lines[0]}${rs.lines[1] !== rs.lines[0] ? `–${rs.lines[1]}` : ''} (highlighted).` : 'No README line was recorded for this column.'}</p></div>
        ${rs.file ? `<button class="btn btn-sm btn-ghost" data-action="src-full">${S.drawer.full ? 'Show nearby lines' : 'Show whole README'}</button>` : ''}
      </div>
      ${rs.file ? `<div class="src-use-bar"><span class="faint">Select text, then use it as</span>
        <button class="btn btn-sm" data-use-sel="description">Description</button><button class="btn btn-sm" data-use-sel="label">Label</button><button class="btn btn-sm" data-use-sel="unit">Unit</button></div>` : ''}
      <div id="src-readme" class="src-text">${rs.file ? '<p class="faint">Loading…</p>' : '<p class="faint">This repository has no README.</p>'}</div>
    </div>
    <div class="src-block">
      <div class="src-head"><div><h3>Data file <span class="faint mono">${esc(ds.file)}</span></h3>
        <p class="field-hint">Raw rows as stored on disk${st.header_row ? `. The pipeline took row ${st.header_row} as the header` : ''}; this column is highlighted.</p></div></div>
      <div id="src-data" class="src-data"><p class="faint">Loading…</p></div>
    </div>`;
}

async function fetchSource(runId, file, start, end) {
  const q = new URLSearchParams({ file, ...(start ? { start } : {}), ...(end ? { end } : {}) });
  return api(`/api/runs/${runId}/source?${q}`);
}

async function loadSources({ d, ds, v }) {
  const key = S.drawer.key;
  const rs = readmeSource(d, ds, v);
  const stale = () => !S.drawer || S.drawer.key !== key;
  if (rs.file) {
    const [lo, hi] = rs.lines || [1, 1];
    fetchSource(d.id, rs.file, S.drawer.full ? 1 : Math.max(1, lo - 8), S.drawer.full ? null : hi + 8)
      .then((x) => { if (!stale()) { $('#src-readme').innerHTML = readmeLines(x, rs.lines, v); $('#src-readme .hl')?.scrollIntoView({ block: 'center' }); } })
      .catch((e) => { if (!stale()) $('#src-readme').innerHTML = `<p class="c-amber">${esc(e.message)}</p>`; });
  }
  fetchSource(d.id, ds.file)
    .then((x) => { if (stale()) return; $('#src-data').innerHTML = dataRows(x, ds, v); const c = $('#src-data td.hl'); if (c) c.closest('.src-table-wrap').scrollLeft = c.offsetLeft - 80; })
    .catch((e) => { if (!stale()) $('#src-data').innerHTML = `<p class="c-amber">${esc(e.message)}</p>`; });
}

function readmeLines(x, lines, v) {
  const [lo, hi] = lines || [0, -1];
  return `${x.changed ? '<p class="c-amber" style="margin-bottom:8px">This file changed on disk after the run, so line numbers may not match.</p>' : ''}
    <ol class="src-lines">${x.lines.map((l) => `<li class="${l.n >= lo && l.n <= hi ? 'hl' : ''}">
      <span class="ln">${l.n}</span><span class="lt">${esc(l.text) || '&nbsp;'}</span>
      ${l.text.trim() ? `<button type="button" class="src-use" data-use-line="${l.n}" title="Use this line as the description">use</button>` : ''}</li>`).join('')}</ol>
    ${x.end < x.total ? `<p class="faint" style="margin-top:6px">Lines ${x.start}–${x.end} of ${x.total}</p>` : ''}`;
}

function dataRows(x, ds, v) {
  if (x.kind !== 'table') {
    const vals = (ds.records || []).slice(0, 15).map((r) => r[v.name]);
    return `<p class="muted">${esc(x.reason || 'No raw preview.')}</p>
      <table class="table dense src-table" style="margin-top:8px"><tbody>${vals.map((val, i) => `<tr><td class="ln">${i + 1}</td><td class="hl">${esc(val ?? '')}</td></tr>`).join('')}</tbody></table>`;
  }
  const header = x.rows.find((r) => r.n === x.header_row)?.cells || [];
  let col = header.indexOf(v.original_name);
  if (col < 0) col = v.position;
  return `${x.changed ? '<p class="c-amber" style="margin-bottom:8px">This file changed on disk after the run.</p>' : ''}
    <div class="src-table-wrap"><table class="src-table"><tbody>${x.rows.map((r) => `<tr class="${r.n === x.header_row ? 'hdr' : r.n < (x.header_row || 0) ? 'pre' : ''}">
      <td class="ln">${r.n}</td>${r.cells.map((c, i) => `<td class="${i === col ? 'hl' : ''}">${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
    <p class="faint" style="margin-top:6px">First ${x.rows.length} rows of ${x.total_rows + (x.header_row || 0)} · delimiter ${esc(DELIM[x.delimiter]?.[1] ?? x.delimiter)} · ${esc(x.encoding)}</p>`;
}

/** Strip a leading variable name and/or field label ("Description:", "Units:") from a README line. */
function cleanReadmeText(text, v) {
  const names = [v?.name, v?.match.documented_name].filter(Boolean).map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const labels = ['description', 'definition', 'desc', 'notes?', 'label', 'units?', 'name', 'variable'];
  let out = text.trim().replace(/^[\s"“”'•*\-–]+|["“”']+$/g, '');
  for (const pattern of [names.length ? `(?:${names.join('|')})` : null, `(?:${labels.join('|')})`].filter(Boolean)) {
    out = out.replace(new RegExp(`^\\s*${pattern}\\s*[:=\\-–,\\t]\\s*`, 'i'), '');
  }
  return out.trim();
}

function useText(field, text, v) {
  const el = $(`#ve-${field}`);
  if (!el || !text) return toast('Select some text in the README first.', 'err');
  // A variable-list line usually starts with the variable name; drop it so only the meaning remains.
  el.value = cleanReadmeText(text, v);
  el.focus();
  el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 700);
}

async function saveVarEdit(form, next = false) {
  const fields = {};
  new FormData(form).forEach((value, name) => { fields[name] = value; });
  fields.missing_values = String(fields.missing_values || '').split(',').map((x) => x.trim()).filter(Boolean);
  const runId = S.drawer?.runId || S.route.runId;
  const before = S.details[runId]?.pending ?? 0;
  try {
    const brief = await api(`/api/runs/${runId}/edits`, { method: 'POST', body: JSON.stringify({ dataset: form.dataset.veDs, column: form.dataset.veCol, fields }) });
    await loadDetail(runId);
    toast(brief.pending < before ? 'Saved. The related review item is resolved too.' : 'Saved. Applied when you export.', 'ok');
    render();
    if (next && drawerCtx()?.idx < drawerCtx()?.ds.variables.length - 1) { S.drawer.snapshot = formState(); stepDrawer(1); } else closeDrawer(true);
  } catch (e) { toast(e.message, 'err'); }
}

async function revertVarEdit(ds, col) {
  const runId = S.drawer?.runId || S.route.runId;
  try {
    await api(`/api/runs/${runId}/edits`, { method: 'POST', body: JSON.stringify({ dataset: ds, column: col, revert: true }) });
    await loadDetail(runId);
    toast('Reverted to the pipeline values.', 'ok');
    render();
    if (S.drawer) renderDrawer();
  } catch (e) { toast(e.message, 'err'); }
}

function varDetail(d, ds, v) {
  const key = `${ds.id}::${v.name}`;
  const edits = d.edits?.[key];
  const editBar = `<div class="toolbar" style="margin-bottom:14px">
    <span class="muted">${edits ? `You changed ${plural(Object.keys(edits).length, 'field')}: <span class="mono">${Object.keys(edits).map(esc).join(', ')}</span>` : 'Something wrong? You can correct the link, description, unit, type and more.'}</span>
    <div class="toolbar-right">${edits ? `<button class="btn btn-sm btn-ghost" data-action="revert-var" data-ve-ds="${esc(ds.id)}" data-ve-col="${esc(v.name)}">${icon('undo-2')}Revert</button>` : ''}
      <button class="btn btn-sm" data-run="${d.id}" data-edit-var="${esc(key)}">${icon('pencil')}Edit</button></div></div>`;
  return editBar + varDetailBody(v);
}

function varDetailBody(v) {
  const st = v.statistics || {};
  const kv = (rows) => `<dl class="kv">${rows.filter(([, x]) => x != null && x !== '').map(([k, x]) => `<dt>${k}</dt><dd>${esc(x)}</dd>`).join('')}</dl>`;
  const num = (x) => (x == null ? null : (+x).toPrecision(6).replace(/\.?0+$/, ''));
  const sources = Object.entries(v.sources || {}).map(([f, s]) => [f, `${s.file}${s.section ? ` § ${s.section}` : ''}${s.lines ? ` · line ${s.lines[0]}${s.lines[1] !== s.lines[0] ? `–${s.lines[1]}` : ''}` : ''}${s.method ? ` · ${s.method}` : ''}`]);
  return `<div class="var-grid">
    <div><div class="mini-label">Values</div>${kv([['count', st.count], ['missing', st.missing], ['distinct', st.distinct], ['min', st.min], ['max', st.max], ['mean', num(st.mean)], ['std', num(st.std)], ['common', st.top_values?.slice(0, 6).join(', ')]])}</div>
    <div><div class="mini-label">Match</div>${kv([['status', v.match.status], ['method', v.match.method], ['confidence', fmtConf(v.match.confidence)], ['evidence', v.match.evidence.join(' · ')], ['warning', v.match.warning], ['runner-up', v.match.alternatives.map((a) => `${a.documented_name} ${fmtConf(a.confidence)}`).join(', ')], ['original', v.original_name !== v.name ? v.original_name : null], ['role', v.role]])}</div>
    <div><div class="mini-label">Where it came from</div>${sources.length ? kv(sources) : '<p class="faint">No README source recorded.</p>'}
      ${v.value_labels.length ? `<div class="mini-label" style="margin-top:12px">Codes</div>${kv(v.value_labels.map((l) => [l.code, l.label]))}` : ''}
      ${v.notes ? `<p class="muted" style="margin-top:10px">${esc(v.notes)}</p>` : ''}</div>
  </div>`;
}

// ---- files
function tabFiles(d) {
  const doc = d.document;
  const byFile = Object.fromEntries(doc.datasets.map((x) => [x.file, x]));
  const docs = Object.fromEntries(doc.documents.map((x) => [x.file, x]));
  const tagFor = (s) => `<span class="tag ${s === 'processed' ? 'tag-green' : s === 'failed' ? 'tag-red' : ''}">${esc(s)}</span>`;
  return `<p class="muted" style="margin-bottom:12px">What the inspector found on disk. Real format, encoding, delimiter and header position, not what the file name or README claims.</p>
    <div class="panel"><table class="table">
      <thead><tr><th>File</th><th>Kind</th><th class="r">Size</th><th>Encoding</th><th>Structure</th><th class="r">Status</th></tr></thead>
      <tbody>${doc.files.map((f) => {
        const ds = byFile[f.path], dc = docs[f.path];
        const enc = ds?.structure.encoding || dc?.encoding;
        const structure = ds
          ? `${ds.row_count} × ${ds.column_count} · delimiter ${esc(DELIM[ds.structure.delimiter]?.[1] ?? '—')} · header row ${ds.structure.header_row ?? 'none'}`
          : dc ? `${plural(dc.sections.length, 'section')}` : '—';
        const note = ds?.documented_as && ds.documented_as !== f.path ? `<div class="c-amber" style="font-size:12px">README calls it ${esc(ds.documented_as)}</div>` : '';
        return `<tr><td><div class="mono">${esc(f.path)}</div><div class="faint mono" style="font-size:11px" title="sha256 ${esc(f.sha256)}">${esc(f.sha256.slice(0, 12))}</div>${note}</td>
          <td class="muted">${esc(f.role)} <span class="faint mono">.${esc(f.format)}</span></td>
          <td class="r mono num muted">${fmtBytes(f.size_bytes)}</td>
          <td>${enc ? `<div class="mono">${esc(enc)}</div><div class="faint" style="font-size:12px">${encNote(enc)}</div>` : '<span class="faint">—</span>'}</td>
          <td class="mono muted">${structure}</td><td class="r">${tagFor(f.status)}</td></tr>`;
      }).join('')}</tbody></table></div>
    ${doc.documents.map((dc) => `<div class="panel"><div class="panel-head"><h2 class="mono">${esc(dc.file)}</h2><span class="faint">${plural(dc.sections.length, 'section')}</span></div>
      <table class="table dense"><tbody>${dc.sections.map((s) => `<tr><td>${esc(s.title)}</td><td class="r mono faint">lines ${s.lines[0]}–${s.lines[1]}</td></tr>`).join('')}</tbody></table></div>`).join('')}
    ${doc.relationships.length ? `<div class="panel"><div class="panel-head"><h2>Relationships</h2></div><table class="table dense"><tbody>${doc.relationships.map((r) => `<tr>
      <td><span class="tag tag-blue">${r.type === 'shared_variables' ? 'shared columns' : 'documented link'}</span></td>
      <td class="mono">${r.type === 'shared_variables' ? `${r.datasets.map(esc).join(' ↔ ')} <span class="faint">on</span> ${r.variables.map(esc).join(', ')}` : `${esc(r.from)} → ${esc(r.target)}`}</td>
      <td class="r mono faint">${fmtConf(r.confidence)}</td></tr>`).join('')}</tbody></table></div>` : ''}`;
}

// ---- metadata: what the README claims vs what the data shows
function tabMetadata(d) {
  const doc = d.document, P = doc.project;
  const verdict = { agree: ['tag-green', 'agrees'], differ: ['tag-amber', 'differs'], only_data: ['', 'not stated'] };
  const rows = doc.datasets.flatMap((ds) => {
    const dv = ds.declared.variable_count, dr = ds.declared.row_count;
    const documented = ds.variables.filter((v) => v.match.documented_name).length + ds.unmatched_documented_variables.length;
    return [
      [ds.file, 'File name', ds.documented_as || ds.file, ds.file, ds.documented_as && ds.documented_as !== ds.file ? 'differ' : 'agree'],
      [ds.file, 'Number of variables', dv ?? '—', ds.column_count, dv == null ? 'only_data' : dv === ds.column_count ? 'agree' : 'differ'],
      [ds.file, 'Number of rows', dr ?? '—', ds.row_count, dr == null ? 'only_data' : dr === ds.row_count ? 'agree' : 'differ'],
      [ds.file, 'Variables described', documented, `${ds.variables.filter((v) => v.match.status === 'matched').length} found in data`, ds.unmatched_documented_variables.length ? 'differ' : 'agree'],
    ];
  });
  const src = (f) => { const s = P.sources?.[f]; return s ? `${esc(s.file)}${s.lines ? ` · line ${s.lines[0]}` : ''}` : ''; };
  const field = (label, value, f) => `<tr><td class="muted" style="width:180px">${label}</td><td>${value ? esc(value) : '<span class="faint">not in README</span>'}</td><td class="r mono faint" style="font-size:11px">${f ? src(f) : ''}</td></tr>`;
  const people = [...P.creators, ...P.contacts];
  return `<div class="section-head"><h2>README claims vs. the data</h2><span class="faint">checked against what the inspector measured</span></div>
    <div class="panel"><table class="table">
      <thead><tr><th>Dataset</th><th>Claim</th><th>README says</th><th>Data shows</th><th class="r">Result</th></tr></thead>
      <tbody>${rows.map(([f, k, a, b, s]) => `<tr><td class="mono faint">${esc(f)}</td><td>${k}</td><td class="mono muted">${esc(a)}</td><td class="mono">${esc(b)}</td><td class="r"><span class="tag ${verdict[s][0]}">${verdict[s][1]}</span></td></tr>`).join('')}</tbody></table></div>
    <div class="section"><div class="section-head"><h2>Project</h2><span class="faint">with the line each value came from</span></div>
    <div class="panel"><table class="table"><tbody>
      ${field('Title', P.title, 'title')}${field('Description', P.description, 'description')}
      ${field('Collection period', P.collection_period?.text, 'collection_period')}${field('Location', P.geographic_location, 'geographic_location')}
      ${field('License', P.license, 'license')}${field('Identifier', P.identifier, 'identifier')}${field('Citation', P.citation, 'citation')}
      ${field('Funding', P.funding.join('; '), 'funding')}${field('Keywords', P.keywords.join(', '), 'keywords')}
      ${field('Related datasets', P.related_datasets.join(', '), 'related_datasets')}${field('Related publications', P.related_publications.join(', '), 'related_publications')}
    </tbody></table></div></div>
    ${people.length ? `<div class="section"><div class="section-head"><h2>People</h2><span class="faint">${plural(people.length, 'person')}</span></div>
    <div class="panel"><table class="table"><thead><tr><th>Name</th><th>Role</th><th>Affiliation</th><th>ORCID</th><th>Email</th></tr></thead><tbody>
      ${people.map((x) => `<tr><td>${esc(x.name)}</td><td class="muted">${esc(x.role || '')}</td><td class="muted">${esc(x.affiliation || '')}</td>
        <td class="mono">${x.orcid ? `<a class="link" target="_blank" rel="noopener noreferrer" href="https://orcid.org/${encodeURIComponent(x.orcid)}">${esc(x.orcid)}</a>` : '<span class="faint">none</span>'}</td>
        <td class="mono muted">${esc(x.email || '')}</td></tr>`).join('')}</tbody></table></div></div>` : ''}
    ${P.additional_fields.length ? `<div class="section"><div class="section-head"><h2>Other README fields</h2><span class="faint">kept as-is, not mapped</span></div>
    <div class="panel"><table class="table dense"><tbody>${P.additional_fields.map((f) => `<tr><td class="muted" style="width:260px">${esc(f.label)}</td><td>${esc(f.value)}</td></tr>`).join('')}</tbody></table></div></div>` : ''}`;
}

// ---- issues
function tabIssues(d) {
  const order = ['fatal', 'error', 'warning', 'info'];
  const issues = [...d.document.issues].sort((a, b) => order.indexOf(a.severity) - order.indexOf(b.severity));
  if (!issues.length) return '<div class="empty"><h2>No issues</h2><p>Nothing went wrong and nothing notable was decided.</p></div>';
  const look = { fatal: ['octagon-alert', 'c-red'], error: ['circle-alert', 'c-red'], warning: ['triangle-alert', 'c-amber'], info: ['info', 'faint'] };
  const counts = order.map((s) => [s, issues.filter((i) => i.severity === s).length]).filter(([, n]) => n);
  return `<div class="toolbar"><span class="muted">${counts.map(([s, n]) => plural(n, s)).join(' · ')}</span></div>
    <div class="panel">${issues.map((i) => {
      const loc = i.location || {};
      const where = [loc.file, loc.sheet && `sheet ${loc.sheet}`, loc.lines && `lines ${loc.lines[0]}–${loc.lines[1]}`, loc.column && `column ${loc.column}`].filter(Boolean).map(esc).join(' · ');
      return `<div class="issue">${icon(...look[i.severity])}<div class="min0">
        <div class="issue-head"><span>${esc(i.message)}</span><span class="code faint">${esc(i.code)}</span></div>
        ${i.technical_detail || where ? `<p class="tl-tech">${[where, i.technical_detail && esc(i.technical_detail)].filter(Boolean).join(' · ')}</p>` : ''}
        ${i.suggestion ? `<p class="issue-fix">${esc(i.suggestion)}</p>` : ''}</div></div>`;
    }).join('')}</div>`;
}

// ---- JSON
function highlight(json) {
  return esc(json).replace(/(&quot;(?:[^&\\]|\\.|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false)\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g, (m, str, colon, bool) => {
    if (str) return colon ? `<span class="j-key">${str}</span>${colon}` : `<span class="j-str">${str}</span>`;
    if (bool) return `<span class="j-bool">${m}</span>`;
    return m === 'null' ? `<span class="j-null">${m}</span>` : `<span class="j-num">${m}</span>`;
  });
}
function jsonSlice(doc, section) {
  if (section === 'all') return { ...doc, datasets: doc.datasets.map((x) => ({ ...x, records: x.records.slice(0, 3) })) };
  if (section.startsWith('ds:')) { const x = doc.datasets.find((y) => y.id === section.slice(3)); return { ...x, records: x.records.slice(0, 5) }; }
  return doc[section];
}
function tabJson(d) {
  const doc = d.document;
  const sections = [['summary', 'summary'], ['project', 'project'], ...doc.datasets.map((x) => [`ds:${x.id}`, `datasets › ${x.file}`]),
    ['relationships', 'relationships'], ['files', 'files'], ['issues', 'issues'], ['processing', 'processing'], ['all', 'whole document']];
  if (!sections.some(([k]) => k === S.jsonSection)) S.jsonSection = 'summary';
  return `<div class="toolbar">
      <p class="muted">Pipeline output before review, schema v${esc(doc.schema_version)}. Records are trimmed here; the export has all of them.</p>
      <div class="toolbar-right">
        <select id="json-section" class="select" aria-label="JSON section">${sections.map(([k, l]) => `<option value="${esc(k)}" ${k === S.jsonSection ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>
        <button class="btn btn-sm" data-action="copy-json">${icon('copy')}Copy</button>
      </div>
    </div>
    <div class="panel"><pre class="json"><code>${highlight(JSON.stringify(jsonSlice(doc, S.jsonSection), null, 2))}</code></pre></div>`;
}

// ---- log: every backend event, plain message first, technical fields underneath
function tabLog(d) {
  const evs = d.events.filter((e) => e.stage !== '_end');
  const t0 = evs[0] ? new Date(evs[0].timestamp).getTime() : 0;
  const live = d.status === 'running';
  return `<div class="toolbar"><span class="muted">${plural(evs.length, 'event')} from the backend, in real time order${live ? ' · streaming' : ''}</span>
      <span class="faint mono">total ${d.document?.processing.duration_ms ?? '…'} ms</span></div>
    <div class="panel"><div class="log" aria-live="polite">
      ${evs.map((e) => {
        const tech = [`${e.stage}.${e.status}`, `${e.progress}%`, e.file && `file=${e.file}`,
          ...Object.entries(e.data || {}).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`), e.technical_message].filter(Boolean).map(esc).join(' · ');
        return `<div class="log-row ${e.stage === 'complete' ? 'done' : ''}"><span class="log-time">+${new Date(e.timestamp).getTime() - t0} ms</span>
          <div class="min0"><div class="log-msg">${esc(e.message)}</div><div class="log-tech">${tech}</div></div></div>`;
      }).join('') || '<div class="panel-empty">Waiting for events…</div>'}
      ${!live && d.document ? `<div class="log-row ${d.pending ? 'gate' : 'done'}"><span class="log-time">gate</span><div><div class="log-msg">${d.pending ? `Paused for review: ${plural(d.pending, 'item')} flagged at threshold ${fmtConf(d.threshold)}.` : 'Review gate clear.'}</div><div class="log-tech">dashboard review layer · not a backend event</div></div></div>` : ''}
    </div></div>`;
}

// ------------------------------------------------------------------ modals
function modal(title, body, footer = '', size = '') {
  $('#modal-root').innerHTML = `<div class="overlay" data-overlay>
    <div class="modal ${size}" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head"><h2 id="modal-title">${title}</h2><button class="btn btn-ghost btn-icon btn-sm" data-action="close-modal" aria-label="Close">${icon('x')}</button></div>
      <div class="modal-body">${body}</div>
      ${footer ? `<div class="modal-foot">${footer}</div>` : ''}
    </div></div>`;
  icons();
  ($('#modal-root [autofocus]') || $('#modal-root .modal-body button, #modal-root .modal-body input'))?.focus();
}
function closeModal() { $('#modal-root').innerHTML = ''; }

function openNewRun() {
  modal('New run', `
    ${S.samples.length ? `<div><span class="field-label">Bundled samples</span><div class="samples">
      ${S.samples.map((s) => `<button class="sample" data-sample="${esc(s.name)}"><span class="mono">${esc(s.name)}</span><span class="faint">${plural(s.files.length, 'file')}</span></button>`).join('')}</div></div>` : ''}
    <div><label class="field-label" for="path-input">Local path</label>
      <div class="row"><input id="path-input" class="input mono" type="text" placeholder="C:\\data\\my-repository or a .zip" autofocus>
      <button class="btn btn-primary" data-action="run-path">Run</button></div>
      <p class="field-hint">A folder, a .zip, or a single file. Read by the local server; nothing leaves this machine.</p></div>
    <div id="dropzone" class="dropzone">
      ${icon('upload')}
      <div>Drop a research folder or .zip here</div>
      <p class="field-hint">.csv .tsv .tab .txt .xlsx .xls .ods plus a README (.txt .md .pdf .docx)</p>
      <div class="btn-row">
        <label class="btn btn-sm">Choose folder<input id="pick-folder" type="file" webkitdirectory multiple class="sr-only"></label>
        <label class="btn btn-sm">Choose files<input id="pick-files" type="file" multiple class="sr-only"></label>
      </div>
      <p id="upload-status" class="field-hint mono" aria-live="polite"></p>
    </div>
    <p class="field-hint">Matches below <span class="mono">${fmtConf(S.threshold)}</span> will be sent to review. Change this in settings.</p>`);
}

function openSettings() {
  modal('Settings', `
    <div><div class="row" style="justify-content:space-between"><label class="field-label" for="th-input">Review threshold</label><span id="th-val" class="mono c-accent">${fmtConf(S.threshold)}</span></div>
      <input id="th-input" class="range" type="range" min="0.70" max="1" step="0.01" value="${S.threshold}">
      <p class="field-hint">Any match below this confidence waits for a person. The backend itself accepts at 0.85 and warns below 0.70; this gate sits on top. Applies to new runs.</p></div>
    <div class="panel"><dl class="facts" style="padding:10px 14px">
      <dt>encoding ladder</dt><dd>BOM → UTF-8 → chardet → cp1252 → latin-1</dd>
      <dt>matcher</dt><dd>exact → case → normalized → abbreviation → fuzzy</dd>
      <dt>LLM hook</dt><dd>off</dd></dl></div>`,
  `<button class="btn" data-action="close-modal">Cancel</button><button class="btn btn-primary" data-action="save-settings">Save</button>`, 'sm');
}

function openShortcuts() {
  const rows = [['N', 'New run'], ['/', 'Search'], ['1 – 8', 'Run tabs'], ['J  K', 'Next / previous review item'], ['O', 'Show or hide item details'], ['[  ]', 'Previous / next column in the editor'], ['Ctrl + Enter', 'Save and go to the next column'], ['A', 'Accept or acknowledge'], ['R', 'Reject'], ['U', 'Undo'], ['E', 'Export'], ['G then D / R / I', 'Dashboard / Runs / Review'], ['Esc', 'Close']];
  modal('Keyboard shortcuts', rows.map(([k, l]) => `<div class="shortcut-row"><span>${l}</span><span class="kbd">${k}</span></div>`).join(''), '', 'sm');
}

// ------------------------------------------------------------------ uploads
async function collectEntry(entry, prefix = '') {
  if (entry.isFile) return [await new Promise((res, rej) => entry.file((f) => res({ file: f, path: prefix + f.name }), rej))];
  const reader = entry.createReader();
  const all = [];
  for (;;) {
    const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
    if (!batch.length) break;
    all.push(...batch);
  }
  return (await Promise.all(all.map((e) => collectEntry(e, `${prefix}${entry.name}/`)))).flat();
}

async function uploadAndRun(files) {
  if (!files.length) return;
  const statusEl = $('#upload-status');
  try {
    const { id } = await api('/api/uploads', { method: 'POST' });
    let i = 0;
    for (const { file, path } of files) {
      if (statusEl) statusEl.textContent = `uploading ${++i}/${files.length} · ${path}`;
      const res = await fetch(`/api/uploads/${id}?path=${encodeURIComponent(path)}`, { method: 'PUT', body: file });
      if (!res.ok) throw new Error((await res.json()).error || `upload failed: ${path}`);
    }
    const root = files[0].path.split('/')[0];
    await startRun({ upload: id, label: files.every((f) => f.path.startsWith(`${root}/`)) ? root : 'upload' });
  } catch (e) { toast(e.message, 'err'); if (statusEl) statusEl.textContent = ''; }
}

// ------------------------------------------------------------------ search
function searchIndex(q) {
  q = q.toLowerCase();
  const out = [];
  for (const r of S.runs) {
    if ([r.repository, r.title, r.id].some((x) => (x || '').toLowerCase().includes(q))) out.push({ href: `#/run/${r.id}`, icon: 'database', main: r.repository, sub: `${r.id} · ${STATUS[r.status] || r.status}` });
  }
  for (const d of Object.values(S.details)) {
    for (const ds of d.document?.datasets || []) for (const v of ds.variables) {
      if ([v.name, v.match.documented_name, v.description].some((x) => (x || '').toLowerCase().includes(q))) {
        out.push({ href: `#/run/${d.id}/variables`, icon: 'variable', main: v.name, sub: `${ds.file}${v.description ? ` · ${v.description}` : ''}`, dataset: ds.id, varKey: `${ds.id}::${v.name}` });
      }
    }
    for (const dc of d.document?.documents || []) for (const s of dc.sections) {
      if ((s.title || '').toLowerCase().includes(q)) out.push({ href: `#/run/${d.id}/files`, icon: 'book-open', main: s.title, sub: `${dc.file} · lines ${s.lines[0]}–${s.lines[1]}` });
    }
  }
  return out.slice(0, 30);
}

function renderSearch() {
  const q = $('#search').value.trim();
  const box = $('#search-results');
  if (!q) { box.hidden = true; return; }
  const res = searchIndex(q);
  box.innerHTML = res.length ? res.map((r) => `<a class="hit" href="${r.href}" role="option" data-search-hit ${r.varKey ? `data-var-key="${esc(r.varKey)}" data-ds="${esc(r.dataset)}"` : ''}>
      ${icon(r.icon)}<span class="min0"><span class="hit-main truncate" style="display:block">${esc(r.main)}</span><span class="hit-sub truncate" style="display:block">${esc(r.sub)}</span></span></a>`).join('')
    : '<div class="panel-empty">No matches. Columns are searchable once a run has loaded.</div>';
  box.hidden = false;
  icons();
}

// ------------------------------------------------------------------ events
const currentRun = () => (S.route.view === 'run' ? S.details[S.route.runId] : null);
document.addEventListener('submit', (e) => {
  if (e.target.matches('.var-edit')) { e.preventDefault(); saveVarEdit(e.target, !!e.submitter?.dataset.next); }
});

const CLICKABLE = '[data-use-line],[data-use-sel],[data-expand],[data-edit-var],[data-action],[data-sample],[data-decide],[data-href],[data-dataset],[data-var],[data-filter],[data-search-hit],[data-delete],[data-select],[data-select-all],[data-overlay]';

document.addEventListener('click', async (e) => {
  if (!e.target.closest('.search')) $('#search-results').hidden = true;
  const t = e.target.closest(CLICKABLE);
  if (!t) return;
  if (t.matches('[data-overlay]')) { if (e.target === t) closeModal(); return; }
  if (t.dataset.searchHit !== undefined) {
    if (t.dataset.varKey) { S.varDataset = t.dataset.ds; S.openVar = t.dataset.varKey; }
    $('#search').value = ''; $('#search-results').hidden = true;
    if (location.hash === t.getAttribute('href')) { e.preventDefault(); render(); }
    return;
  }
  if (t.dataset.select) {
    e.stopPropagation();
    t.checked ? S.selected.add(t.dataset.select) : S.selected.delete(t.dataset.select);
    return render();
  }
  if (t.dataset.selectAll !== undefined) {
    const ids = (S.runFilter === 'all' ? S.runs : S.runs.filter((r) => r.status === S.runFilter)).filter((r) => r.status !== 'running').map((r) => r.id);
    t.checked ? ids.forEach((id) => S.selected.add(id)) : ids.forEach((id) => S.selected.delete(id));
    return render();
  }
  if (t.dataset.expand) {
    S.openItems.has(t.dataset.expand) ? S.openItems.delete(t.dataset.expand) : S.openItems.add(t.dataset.expand);
    return render();
  }
  if (t.dataset.editVar) return openDrawer(t.dataset.editVar, t.dataset.run);
  if (t.dataset.useLine) {
    const li = t.closest('li');
    return useText('description', li.querySelector('.lt').textContent, drawerCtx()?.v);
  }
  if (t.dataset.useSel) {
    const sel = window.getSelection();
    const inside = sel && sel.rangeCount && $('#src-readme')?.contains(sel.getRangeAt(0).commonAncestorContainer);
    return useText(t.dataset.useSel, inside ? sel.toString().replace(/\s*\n\s*/g, ' ') : '', drawerCtx()?.v);
  }
  if (t.dataset.delete) return confirmDelete([t.dataset.delete]);
  if (t.dataset.sample) return startRun({ sample: t.dataset.sample });
  if (t.dataset.decide) return decide(t.dataset.run, t.dataset.item, t.dataset.decide);
  if (t.dataset.href) { location.hash = t.dataset.href; return; }
  if (t.dataset.var) { S.openVar = S.openVar === t.dataset.var ? null : t.dataset.var; return render(); }
  if (t.dataset.dataset) { S.varDataset = t.dataset.dataset; S.openVar = null; return render(); }
  if (t.dataset.filter) { S.runFilter = t.dataset.filter; return render(); }
  const run = currentRun();
  switch (t.dataset.action) {
    case 'new-run': return openNewRun();
    case 'settings': return openSettings();
    case 'shortcuts': return openShortcuts();
    case 'close-modal': return closeModal();
    case 'run-path': { const p = $('#path-input').value.trim(); return p ? startRun({ path: p }) : toast('Enter a path first.', 'err'); }
    case 'save-settings':
      S.threshold = parseFloat($('#th-input').value);
      localStorage.setItem('rn.threshold', String(S.threshold));
      closeModal(); toast(`New runs will send matches below ${fmtConf(S.threshold)} to review.`, 'ok');
      return render();
    case 'rerun': return run && startRun(run.spec);
    case 'delete-run': return run && confirmDelete([run.id]);
    case 'delete-selected': return confirmDelete([...S.selected]);
    case 'clear-selection': S.selected.clear(); return render();
    case 'drawer-close': return closeDrawer();
    case 'drawer-prev': return stepDrawer(-1);
    case 'drawer-next': return stepDrawer(1);
    case 'src-full': if (S.drawer) { S.drawer.full = !S.drawer.full; const snap = S.drawer.snapshot; const vals = [...new FormData($('#drawer-root .var-edit'))]; renderDrawer(); S.drawer.snapshot = snap; vals.forEach(([k, val]) => { const el = $(`#ve-${k}`); if (el) el.value = val; }); } return;
    case 'revert-var': return revertVarEdit(t.dataset.veDs, t.dataset.veCol);
    case 'confirm-delete': return deleteRuns(t.dataset.ids.split(','));
    case 'export': return run && exportRun(run.id);
    case 'export-log': return run && exportRun(run.id, true);
    case 'replay': if (run) { S.replay[run.id] = { t0: performance.now(), last: -1 }; render(); } return;
    case 'skip-replay': if (run) { delete S.replay[run.id]; render(); } return;
    case 'copy-json':
      try { await navigator.clipboard.writeText(JSON.stringify(jsonSlice(run.document, S.jsonSection), null, 2)); toast('Copied.', 'ok'); }
      catch { toast('Clipboard is not available here.', 'err'); }
  }
});

document.addEventListener('input', (e) => {
  if (e.target.id === 'search') renderSearch();
  if (e.target.id === 'th-input') $('#th-val').textContent = fmtConf(parseFloat(e.target.value));
  if (e.target.id === 'var-filter') {
    S.varQuery = e.target.value; render();
    const el = $('#var-filter'); el.focus(); el.setSelectionRange(el.value.length, el.value.length);
  }
});
document.addEventListener('change', (e) => {
  if (e.target.id === 'json-section') { S.jsonSection = e.target.value; render(); }
  if (e.target.id === 'pick-folder' || e.target.id === 'pick-files') {
    uploadAndRun([...e.target.files].map((f) => ({ file: f, path: f.webkitRelativePath || f.name })));
  }
});

document.addEventListener('dragover', (e) => { const z = e.target.closest?.('#dropzone'); if (z) { e.preventDefault(); z.classList.add('drag'); } });
document.addEventListener('dragleave', (e) => { e.target.closest?.('#dropzone')?.classList.remove('drag'); });
document.addEventListener('drop', async (e) => {
  const z = e.target.closest?.('#dropzone');
  if (!z) return;
  e.preventDefault(); z.classList.remove('drag');
  const entries = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry?.()).filter(Boolean);
  const files = entries.length ? (await Promise.all(entries.map((en) => collectEntry(en)))).flat()
    : [...e.dataTransfer.files].map((f) => ({ file: f, path: f.name }));
  uploadAndRun(files);
});

// ------------------------------------------------------------------ keyboard
let gPrefix = false;
document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (S.drawer) {
    if (e.key === 'Escape') { e.preventDefault(); return closeDrawer(); }
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); return $('#drawer-root [data-next]')?.click(); }
    if (!typing && (e.key === '[' || e.key === ']')) { e.preventDefault(); return stepDrawer(e.key === ']' ? 1 : -1); }
    return; // the drawer is modal; page shortcuts are paused
  }
  if (e.key === 'Escape') {
    if ($('#modal-root').innerHTML) closeModal();
    $('#search-results').hidden = true;
    if (typing) e.target.blur();
    return;
  }
  if (e.target.id === 'path-input' && e.key === 'Enter') return $('[data-action="run-path"]').click();
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); return $('#search').focus(); }
  if (e.key === 'Enter' && e.target.matches('tr[data-href], tr[data-var]')) return e.target.click();
  if (typing || e.metaKey || e.ctrlKey || e.altKey || $('#modal-root').innerHTML) return;
  const k = e.key.toLowerCase();
  if (gPrefix) {
    gPrefix = false;
    const dest = { d: 'dashboard', r: 'runs', i: 'inbox' }[k];
    if (dest) location.hash = `#/${dest}`;
    return;
  }
  if (k === 'g') { gPrefix = true; setTimeout(() => { gPrefix = false; }, 800); return; }
  if (k === 'n') { e.preventDefault(); return openNewRun(); }
  if (k === '/') { e.preventDefault(); return $('#search').focus(); }
  if (e.key === '?') return openShortcuts();
  const run = currentRun();
  if (!run) return;
  if (/^[1-8]$/.test(k)) { location.hash = `#/run/${run.id}/${TABS[+k - 1][0]}`; return; }
  if (k === 'e' && run.document && !run.pending && !S.replay[run.id]) return exportRun(run.id);
  if ((S.route.tab || 'overview') !== 'review' || !run.items.length || S.replay[run.id]) return;
  const n = run.items.length;
  const move = (dir) => { e.preventDefault(); S.focusIdx = Math.max(0, Math.min(n - 1, S.focusIdx + dir)); render(); $(`[data-review-idx="${S.focusIdx}"]`)?.scrollIntoView({ block: 'nearest' }); };
  if (k === 'j' || k === 'arrowdown') return move(1);
  if (k === 'k' || k === 'arrowup') return move(-1);
  const item = run.items[S.focusIdx];
  if (!item) return;
  if (k === 'o') { S.openItems.has(item.id) ? S.openItems.delete(item.id) : S.openItems.add(item.id); return render(); }
  const actions = KIND[item.kind].actions;
  const advance = () => { const next = run.items.findIndex((it, i) => i > S.focusIdx && !run.decisions[it.id]); if (next >= 0) S.focusIdx = next; };
  if (k === 'a' && !run.decisions[item.id]) { decide(run.id, item.id, actions.includes('accept') ? 'accept' : 'acknowledge'); advance(); }
  else if (k === 'r' && !run.decisions[item.id] && actions.includes('reject')) { decide(run.id, item.id, 'reject'); advance(); }
  else if (k === 'u' && run.decisions[item.id]) decide(run.id, item.id, 'undo');
});

// ------------------------------------------------------------------ init
window.addEventListener('hashchange', onRoute);
(async function init() {
  try { S.samples = await api('/api/samples'); } catch { S.samples = []; }
  await loadRuns();
  await Promise.all(S.runs.filter((r) => r.status !== 'running').map((r) => loadDetail(r.id).catch(() => null)));
  S.runs.filter((r) => r.status === 'running').forEach((r) => stream(r.id));
  await onRoute();
  setInterval(async () => {
    const n = S.runs.length;
    await loadRuns();
    if (S.runs.length !== n) render(); else renderChrome();
  }, 5000);
})();
