/* BlendFleet web UI.
 *
 * The rule this file exists to keep: NOTHING here invents state. The
 * design this is ported from (ref/render-farm (7).html) is a simulation --
 * it ships mkInstance(), tickRender(), a fake instances[] array and
 * generated frame images. Every one of those is gone. Each figure on
 * screen arrives through `backend`, the QWebChannel object defined in
 * blendfleet/ui/bridge.py, or it is not shown at all.
 *
 * The honesty rules the Qt UI established come with it:
 *   - Quota is what the API said at the last poll, never a promise.
 *   - Hardware is LAST-KNOWN and always carries its age; Kaggle
 *     reallocates between runs.
 *   - There are no idle instances to poll. `worker: null` means idle, and
 *     idle is a real state, not a missing reading.
 *   - Status is never colour alone: every badge carries a word.
 */
'use strict';

let backend = null;
let unread = 0;
/* `sound` defaults TRUE here as well as in Settings: the guard in
   tone() reads prefs.sound, and an undefined key is falsy -- which
   silently disabled every sound in the app until this was set. */
let prefs = { theme: 'light', accent: 'orange', translucent: false,
              sound: true };
const history = { inst: [], run: [], gpus: [], frames: [] };

/* ---------------- sounds -------------------------------------------------
   Synthesised, exactly as the reference does it -- no audio files to ship,
   and the tones stay identical across themes and platforms. Audio cannot
   start until the user has interacted with the page, so the context is
   created lazily on the first real input. */
let audio = null;
function ensureAudio() {
  if (!audio) {
    try { audio = new (window.AudioContext || window.webkitAudioContext)(); }
    catch (e) { audio = null; }
  }
}
document.addEventListener('pointerdown', ensureAudio, { once: true });
document.addEventListener('keydown', ensureAudio, { once: true });

function tone(freq, dur, vol = 0.022, when = 0, attack = 0.05) {
  if (!audio || !prefs.sound) return;
  const t0 = audio.currentTime + when;
  const osc = audio.createOscillator();
  const gain = audio.createGain();
  osc.type = 'sine';
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0.0001, t0);
  gain.gain.exponentialRampToValueAtTime(vol, t0 + attack);
  gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
  osc.connect(gain).connect(audio.destination);
  osc.start(t0);
  osc.stop(t0 + dur + 0.02);
}
const sfx = {
  done:     () => { tone(660, 0.18); tone(880, 0.22, 0.018, 0.09); },
  error:    () => { tone(300, 0.26, 0.03); tone(220, 0.3, 0.025, 0.12); },
  reconnect:() => { tone(520, 0.16); tone(780, 0.2, 0.018, 0.08); },
  notify:   () => { tone(720, 0.14, 0.016); },
};

/* ---------------- chrome ------------------------------------------------ */
function applyPrefs(p) {
  prefs = Object.assign(prefs, p);
  document.documentElement.dataset.theme = prefs.theme;
  document.documentElement.dataset.accent = prefs.accent;
  document.body.classList.toggle('glass', !!prefs.translucent);
}

document.querySelectorAll('.nav-btn').forEach(button => {
  button.addEventListener('click', () => {
    const page = button.dataset.page;
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('on', b === button));
    document.querySelectorAll('.page').forEach(p => p.classList.toggle('on', p.id === 'page-' + page));
    document.getElementById('page-title').textContent = button.querySelector('.nav-txt').textContent;
  });
});

document.getElementById('btn-collapse').addEventListener('click', () => {
  document.getElementById('sidebar').classList.toggle('collapsed');
});

const themeBtn = document.getElementById('btn-theme');
themeBtn.addEventListener('click', () => {
  const next = prefs.theme === 'dark' ? 'light' : 'dark';
  themeBtn.classList.add('spin');
  applyPrefs({ theme: next });
  /* Persisted through Settings, not localStorage: the Qt host reads the
     same file to decide the window's own chrome (Mica, dark title bar). */
  if (backend) backend.setPreference('theme', JSON.stringify(next));
});
themeBtn.addEventListener('animationend', () => themeBtn.classList.remove('spin'));

const bell = document.getElementById('btn-bell');
const notifPanel = document.getElementById('notif-panel');
const wifiBtn = document.getElementById('btn-wifi');
const healthPanel = document.getElementById('health-panel');

bell.addEventListener('click', e => {
  e.stopPropagation();
  notifPanel.classList.toggle('show');
  healthPanel.classList.remove('show');
  unread = 0;
  updateBubble();
});
wifiBtn.addEventListener('click', e => {
  e.stopPropagation();
  healthPanel.classList.toggle('show');
  notifPanel.classList.remove('show');
});
document.addEventListener('click', e => {
  if (!notifPanel.contains(e.target)) notifPanel.classList.remove('show');
  if (!healthPanel.contains(e.target)) healthPanel.classList.remove('show');
});
document.getElementById('np-clear').addEventListener('click', () => {
  document.getElementById('notif-list').innerHTML = '<div class="empty">Nothing yet.</div>';
  notifPanel.classList.remove('show');
});
document.getElementById('hp-rerun').addEventListener('click', () => backend && backend.poll());
document.getElementById('btn-retry').addEventListener('click', () => backend && backend.poll());

function updateBubble() {
  const el = document.getElementById('n-count');
  el.textContent = unread;
  el.classList.toggle('show', unread > 0);
  if (unread > 0) {
    bell.classList.add('ring');
    setTimeout(() => bell.classList.remove('ring'), 700);
  }
}

function notify(message, tone_) {
  const list = document.getElementById('notif-list');
  const empty = list.querySelector('.empty');
  if (empty) empty.remove();
  /* The reference's own .notif-item markup. I had invented .notif/.nt/.nm,
     which match nothing in app.css -- so every entry rendered as unstyled
     text with no dot, no separator and no severity. */
  const row = document.createElement('div');
  row.className = 'notif-item' +
    (tone_ === 'offline' ? ' err' : tone_ === 'active' ? ' ok' : '');
  const title = { offline: 'Problem', active: 'Done' }[tone_] || 'Update';
  row.innerHTML = `<span class="ni-dot"></span>
    <div><div class="ni-t">${esc(title)}</div>
      <div class="ni-m">${esc(message)}</div>
      <div class="ni-time">${clock()}</div></div>`;
  list.prepend(row);
  unread++;
  updateBubble();
  toast(message, tone_);
  (tone_ === 'offline' ? sfx.error : sfx.notify)();
}

function toast(message, tone_) {
  const stack = document.getElementById('toast-stack');
  const el = document.createElement('div');
  el.className = 'toast' + (tone_ === 'offline' ? ' err' : tone_ === 'active' ? ' ok' : '');
  el.textContent = message;
  stack.appendChild(el);
  requestAnimationFrame(() => el.classList.add('in'));
  setTimeout(() => {
    el.classList.remove('in');
    setTimeout(() => el.remove(), 300);
  }, 4200);
}

function logLine(message, tone_) {
  const log = document.getElementById('log');
  const row = document.createElement('div');
  const cls = tone_ === 'offline' ? 'err' : tone_ === 'active' ? 'ok' : tone_ === 'warn' ? 'warn' : '';
  row.innerHTML = `<span class="t">${clock()}</span> <span class="${cls}">${esc(message)}</span>`;
  log.prepend(row);
  while (log.children.length > 200) log.lastChild.remove();
}

/* ---------------- dashboard --------------------------------------------- */
function renderState(json) {
  const state = JSON.parse(json);
  const wrap = document.getElementById('instances');

  if (!state.instances.length) {
    wrap.innerHTML = '<div class="empty">No accounts yet — add one under Instances to start rendering.</div>';
  } else {
    wrap.innerHTML = state.instances.map(instanceCard).join('');
  }

  const online = state.instances.length;
  const running = state.instances.filter(i => i.worker && i.worker.state === 'running').length;
  /* GPUs ACTIVE means GPUs actually reporting telemetry right now -- not
     "how many this account had last time", which is what counting cached
     hardware would give and would read as activity that is not happening. */
  const gpus = state.instances.reduce(
    (n, i) => n + (i.live ? i.live.gpus.length : 0), 0);
  const frames = state.instances.reduce(
    (n, i) => n + (i.live && i.live.framesTotal
                   ? i.live.framesDone
                   : (i.worker ? i.worker.framesDone : 0)), 0);

  setStat('inst', online);
  setStat('run', running);
  setStat('gpus', gpus);
  setStat('frames', frames);

  const jobMeta = document.getElementById('job-meta');
  if (jobMeta) {
    jobMeta.textContent = state.job
      ? `${state.job.blend} · frames ${state.job.startFrame}-${state.job.endFrame}`
      : (state.blend ? `${state.blend.name} · not started` : 'no scene chosen');
  }
  document.getElementById('nav-running').textContent = running;
  document.getElementById('nav-inst').textContent = online;
  document.getElementById('nav-files').textContent = state.job ? 1 : 0;

  /* Every page is driven from the SAME payload on the same tick. A count
     in the sidebar that updates on a different schedule from the page it
     points at is worse than no count. */
  renderFleetTable(state);
  renderFrameGrid(state);
  renderFailures(state);
  renderDataset(state);
}

function instanceCard(inst) {
  const worker = inst.worker;
  const state = worker ? worker.state : 'idle';
  /* Symbol AND word, always. A dot with no word would put the whole
     meaning on a colour, which ~8% of men cannot reliably read. */
  const badge = {
    running:  ['rendering', 'rendering'],
    queued:   ['idle', 'queued'],
    complete: ['complete', 'complete'],
    error:    ['warn', 'error'],
    cancel_requested:    ['paused', 'cancelling'],
    cancel_acknowledged: ['paused', 'cancelled'],
  }[state] || ['idle', 'idle'];
  const dot = { running: 'active', error: 'offline', complete: 'active' }[state] || 'idle';

  /* Last-KNOWN hardware, with its age, or an honest blank. Never dressed
     up as what the account is running right now. */
  const hw = inst.hardware
    ? `${inst.hardware.gpus.map(g => esc(g.model || 'GPU')).join(', ')}`
      + (inst.hardware.cpuCount ? ` · ${inst.hardware.cpuCount} vCPU` : '')
      + (inst.hardware.ramTotal ? ` · ${inst.hardware.ramTotal.toFixed(1)} GB` : '')
      + ` <b>${fmtAge(inst.hardware.ageSeconds)}</b>`
    : 'never run — launch to see specs';

  /* Live beats polled. The 30s status poll can only say queued/running --
     it cannot say "installing Blender" or "frame 7 of 15", because Kaggle
     returns no logs until a kernel COMPLETES. The SSE stream can, so when
     it has reported, its numbers are the ones shown. */
  const live = inst.live;
  const done = live && live.framesTotal ? live.framesDone
             : (worker ? worker.framesDone : 0);
  const total = live && live.framesTotal ? live.framesTotal
              : (worker ? worker.frames.length : 0);
  const progress = total ? Math.round(100 * done / total) : 0;

  /* The hardware this session ACTUALLY got, reported seconds after start
     -- distinct from the cached "last known" line above it, which may be
     from a different allocation entirely. */
  /* PREFLIGHT arrives first (seconds after the kernel starts); the
     hardware banner follows in the same cell. Either can be the source,
     so both are used -- reading only preflight meant a session that
     reported its CPU/RAM but not its preflight line showed neither.

     RAM is TOTAL, never "in use": the notebook queries nvidia-smi only,
     so no live RAM-used sample exists anywhere in this app. Labelling it
     as usage would be inventing a reading. */
  const preflight = live && live.preflight;
  const gpuNames = preflight ? (preflight.gpu_names || []) : null;
  const cpuCount = (preflight && preflight.cpu_count) || (live && live.cpuCount);
  const ramTotal = (preflight && preflight.ram_total) || (live && live.ramTotal);
  const liveHw = (gpuNames || cpuCount || ramTotal)
    ? `<div class="hw-row"><span class="hw-chip live">This session <b>${
        gpuNames ? esc(gpuNames.join(', ') || 'CPU only') : 'running'}</b>${
        cpuCount ? ` · ${cpuCount} vCPU` : ''}${
        ramTotal ? ` · ${ramTotal.toFixed(1)} GB RAM` : ''
      }</span></div>`
    : '';

  /* One row per physical GPU, never combined -- an average across two
     cards hides one of them sitting idle. */
  const gpuRows = live && live.gpus.length
    ? live.gpus.map(g => `<div class="gpu-line">
        <span class="tag on">GPU ${g.index}</span>
        <div class="track rendering"><i style="width:${g.util || 0}%"></i></div>
        <span class="pct">${g.util == null ? '—' : g.util + '%'}</span>
      </div>
      <div class="gpu-line">
        <span class="tag">VRAM</span>
        <div class="track"><i style="width:${
          g.memTotal ? Math.round(100 * g.memUsed / g.memTotal) : 0}%"></i></div>
        <span class="pct">${g.memTotal
          ? Math.round(g.memUsed / 1024) + '/' + Math.round(g.memTotal / 1024) + 'G'
          : '—'}</span>
      </div>`).join('')
    : '';

  const phase = live && live.phase
    ? `<div class="inst-foot"><b>${esc(live.phase)}</b></div>`
    : (worker && worker.state === 'queued'
       ? '<div class="inst-foot">queued — waiting for Kaggle to allocate a machine</div>'
       : '');

  return `<div class="inst">
    <div class="inst-head">
      <span class="status-dot ${dot}"></span>
      <span class="name">${esc(inst.label)}</span>
      <span class="sub">${esc(inst.username || '')}</span>
      <span class="right">
        ${inst.revoked
          ? '<span class="badge bad"><i></i>token revoked</span>'
          : inst.verified ? '' : '<span class="badge warn"><i></i>not verified</span>'}
        ${inst.username ? '' : '<span class="badge warn"><i></i>needs username</span>'}
        <span class="badge ${badge[0]}"><i></i>${badge[1]}</span>
      </span>
    </div>
    <div class="inst-body">
      <div class="hw-row">
        <span class="hw-chip">Quota (API) <b>${esc(inst.quota || '—')}</b></span>
      </div>
      <div class="hw-row">
        <span class="hw-chip">${hw}</span>
        ${(!inst.revoked && inst.username && !worker)
          ? `<button class="btn sm" data-hwcheck="${esc(inst.label)}"
               title="Kaggle decides what hardware a session gets, and it varies run to run. This starts a one-minute check so you know before committing a render.">Check hardware</button>`
          : ''}
      </div>
      ${liveHw}
      ${worker ? `
      <div class="assign">
        <div class="wrapc">
          <div class="l1"><span class="flab">Frames</span>
            <span class="frange">${done} / ${total}</span></div>
          <div class="assign-progress"><i style="width:${progress}%"></i></div>
        </div>
      </div>` : ''}
      ${gpuRows}
      ${phase}
      ${worker && worker.message ? `<div class="inst-foot"><b>${esc(worker.message)}</b></div>` : ''}
    </div>
  </div>`;
}

function setStat(key, value) {
  document.getElementById('s-' + key).textContent = value;
  const series = history[key];
  const previous = series.length ? series[series.length - 1] : null;
  series.push(value);
  if (series.length > 48) series.shift();
  drawSpark('ss-' + key, series);

  const trend = document.getElementById('tr-' + key.replace('frames', 'frames'));
  if (trend && previous !== null) {
    const delta = value - previous;
    trend.textContent = delta === 0 ? '—' : (delta > 0 ? '+' : '') + delta;
    trend.className = 'trend' + (delta > 0 ? ' up' : delta < 0 ? ' down' : '');
  }
}

function drawSpark(id, values) {
  const canvas = document.getElementById(id);
  if (!canvas || values.length < 2) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = (hi - lo) || 1;
  ctx.strokeStyle = cssVar('--accent');
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = w * i / (values.length - 1);
    const y = h - 2 - ((v - lo) / span) * (h - 4);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
}

function renderHealth(json) {
  const h = JSON.parse(json);
  const ok = h.online;
  document.getElementById('hp-verdict').className = 'hp-verdict' + (ok ? '' : ' bad');
  document.getElementById('hp-verdict-txt').textContent = ok ? 'connected' : 'cannot reach Kaggle';
  document.getElementById('hp-net').textContent = ok ? 'reachable' : 'unreachable';
  document.getElementById('hp-lat').textContent = h.lastPollMs != null ? h.lastPollMs + ' ms' : '—';
  document.getElementById('hp-sync').textContent = h.lastPollAt || 'never';
  document.getElementById('hp-acct').textContent = `${h.accountsReachable}/${h.accountsTotal}`;
  document.getElementById('hp-sub').textContent = 'last checked ' + (h.lastPollAt || '—');
  document.getElementById('wifi-ms').textContent = h.lastPollMs != null ? h.lastPollMs + ' ms' : '— ms';
  wifiBtn.classList.toggle('bad', !ok);
  document.getElementById('offline-banner').classList.toggle('show', !ok);
  document.getElementById('ob-sub').textContent = ok ? '' : 'retrying on the next poll…';
}

/* ---------------- helpers ----------------------------------------------- */
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function clock() {
  return new Date().toTimeString().slice(0, 8);
}
function fmtAge(seconds) {
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
  if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
  return Math.floor(seconds / 86400) + 'd ago';
}
function esc(text) {
  return String(text).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

setInterval(() => {
  const now = clock();
  document.getElementById('clock').textContent = now;
  document.getElementById('side-clock').textContent = now;
}, 1000);

/* ---------------- files page -------------------------------------------- */
function renderOptions() {
  return {
    startFrame: +document.getElementById('f-start').value,
    endFrame: +document.getElementById('f-end').value,
    resX: +document.getElementById('f-rx').value,
    resY: +document.getElementById('f-ry').value,
    samples: +document.getElementById('f-spp').value,
    format: document.getElementById('f-fmt').value,
  };
}

function refreshEta() {
  if (!backend) return;
  const o = renderOptions();
  backend.estimateRender(o.startFrame, o.endFrame, json => {
    const e = JSON.parse(json);
    /* The basis travels with the number, always: this is an extrapolation
       from one measured scene, not a prediction about yours. */
    document.getElementById('eta').innerHTML =
      `${e.frames} frames across ${e.accounts} account(s) ≈ <b>${e.hours.toFixed(1)} h</b> each `
      + `<span style="opacity:.8">(${esc(e.basis)})</span>`;
  });
}
['f-start', 'f-end'].forEach(id =>
  document.getElementById(id).addEventListener('input', refreshEta));

document.getElementById('dropzone').addEventListener('click', () => {
  if (!backend) return;
  backend.pickBlend(json => {
    const picked = JSON.parse(json);
    if (!picked.name) return;
    document.getElementById('dz-title').textContent = picked.name;
    document.getElementById('dz-sub').textContent = picked.path;
    document.getElementById('nav-files').textContent = '1';
    backend.state(renderState);
  });
});

document.getElementById('btn-upload').onclick = () =>
  backend && backend.syncDataset();

/* Getting a scene onto Kaggle is four different waits, and the byte
   counter stops moving during three of them. Without naming the stage,
   "sharing with two friends" and "stuck" look identical. */
const UPLOAD_STAGES = {
  checking:            d => ['checking Kaggle', `for ${d}`],
  'already-uploaded':  d => ['already on Kaggle', 'skipping the upload'],
  uploading:           d => [`uploading to ${d}`, 'this is the slow one'],
  verifying:           d => [`checking Kaggle stored it`, `as ${d}`],
  sharing:             d => ['granting access', `to ${d}`],
  'verifying-access':  d => ['confirming access', `for ${d}`],
  ready:               d => ['ready', d],
};

function showUploadStage(p) {
  const describe = UPLOAD_STAGES[p.stage];
  const [title, detail] = describe ? describe(p.detail || '') : [p.stage, ''];
  const pct = p.total ? Math.round(100 * p.uploaded / p.total) : null;

  document.getElementById('up-name').textContent =
    pct === null ? title : `${title}`;
  document.getElementById('up-pct').textContent =
    pct === null ? detail : `${pct}% · ${fmtBytes(p.uploaded)} of ${fmtBytes(p.total)}`;
  /* On a stage with no byte count, the bar holds its width rather than
     snapping back to zero -- a bar that resets reads as "it failed and
     started over". */
  if (pct !== null) document.getElementById('up-bar').style.width = pct + '%';
  if (p.stage === 'ready') {
    document.getElementById('up-bar').style.width = '100%';
    logLine(`dataset ready: ${p.detail}`, 'active');
  } else {
    logLine(`${title} ${detail}`.trim(), '');
  }
}

function renderDataset(state) {
  const ds = state.dataset;
  document.getElementById('ds-blend').textContent =
    state.blend ? state.blend.name : '—';
  document.getElementById('ds-slug').textContent =
    ds ? ds.slug : 'not uploaded this session';
  document.getElementById('ds-size').textContent =
    ds ? fmtBytes(ds.sizeBytes) : '—';
  /* Says plainly whether the next render will reuse this or re-upload.
     "Not uploaded this session" is not the same claim as "not on Kaggle" --
     we only know what we put there ourselves. */
  const note = document.getElementById('ds-note');
  if (!state.blend) {
    note.textContent = 'Choose a scene first.';
  } else if (ds && ds.blendName === state.blend.name) {
    note.textContent = `Ready — rendering will reuse this (uploaded ${ds.at}).`;
  } else if (ds) {
    note.textContent = `This dataset holds ${ds.blendName}, not the scene you have chosen — rendering would upload again.`;
  } else {
    note.textContent = 'Rendering will upload it first. Uploading here instead keeps a failed upload from taking a render attempt with it.';
  }
}

function fmtBytes(bytes) {
  if (!bytes) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let n = bytes, i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${units[i]}`;
}

document.getElementById('btn-render').onclick = () =>
  backend && backend.launch(JSON.stringify(renderOptions()));
document.getElementById('btn-cancel').onclick = () => backend && backend.cancelAll();
document.getElementById('btn-collect').onclick = () => backend && backend.collect('');

function renderFrameGrid(state) {
  const grid = document.getElementById('fgrid');
  const meta = document.getElementById('fg-meta');
  if (!state.job) {
    grid.innerHTML = '';
    meta.textContent = 'no job yet';
    return;
  }
  /* Which frames are done is INFERRED, not reported: each account is
     assumed to have finished the first N of its own stride. That holds
     until a frame fails, which is why the legend says approximate. */
  const done = new Set();
  state.instances.forEach(i => {
    if (!i.worker) return;
    const n = i.live && i.live.framesTotal ? i.live.framesDone
            : i.worker.framesDone;
    i.worker.frames.slice(0, n).forEach(f => done.add(f));
  });
  const cells = [];
  for (let f = state.job.startFrame; f <= state.job.endFrame; f++) {
    cells.push(`<div class="fcell${done.has(f) ? ' d' : ''}" title="frame ${f}"></div>`);
  }
  grid.innerHTML = cells.join('');
  const total = state.job.endFrame - state.job.startFrame + 1;
  meta.textContent = `${done.size}/${total} frames · ${esc(state.job.blend)}`;
}

/* ---------------- instances page ---------------------------------------- */
function renderFleetTable(state) {
  const body = document.getElementById('inst-tbody');
  document.getElementById('inst-meta').textContent =
    `${state.instances.length} account(s)`;
  /* Say what warm costs, rather than letting it look free. A machine
     waiting for work bills quota at the same rate as one rendering. */
  const warmCount = state.instances.filter(
    i => i.worker && ['running','queued'].includes(i.worker.state)
         && !i.worker.frames.length).length;
  document.getElementById('warm-note').textContent = warmCount
    ? `${warmCount} machine(s) warm — spending quota while they wait, and shutting themselves down after 10 idle minutes.`
    : 'Starting a machine spends quota from that moment. It reports its hardware first, so you can decide before sending work.';
  body.innerHTML = state.instances.map(i => {
    const hw = i.hardware
      ? `${esc(i.hardware.gpus.map(g => g.model || 'GPU').join(', ') || 'no GPU seen')} <span class="dim">${fmtAge(i.hardware.ageSeconds)}</span>`
      : '<span class="dim">never run</span>';
    const state_ = i.worker ? i.worker.state : 'idle';
    const stoppable = i.worker && ['running', 'queued'].includes(i.worker.state);
    const warm = i.worker && ['running','queued'].includes(i.worker.state)
               && !i.worker.frames.length;
  return `<tr>
      <td>${esc(i.label)}${i.revoked
        ? ' <span class="badge bad"><i></i>token revoked</span>'
        : i.verified ? '' : ' <span class="badge warn"><i></i>unverified</span>'}${warm ? ' <span class="badge accent"><i></i>warm</span>' : ''}</td>
      <td>${i.username
        ? esc(i.username)
        : `<button class="btn sm danger" data-username="${esc(i.label)}">Set username</button>`}</td>
      <td>${esc(i.quota || '—')}</td>
      <td>${hw}</td>
      <td>${esc(state_)}</td>
      <td style="text-align:right;white-space:nowrap">
        ${stoppable
          ? `<button class="btn sm" data-cancel="${esc(i.label)}">Stop</button>`
          : `<button class="btn sm" data-start="${esc(i.label)}">Start</button>`}
        <button class="btn sm" data-download="${esc(i.label)}">Download</button>
        <button class="btn sm danger" data-remove="${esc(i.label)}">Remove</button>
      </td></tr>`;
  }).join('');
}

document.getElementById('inst-tbody').addEventListener('click', e => {
  const button = e.target.closest('button');
  if (!button || !backend) return;
  if (button.dataset.username) {
    /* Kaggle exposes no "who am I": the handle is read off something the
       account owns, and an account that has never made a notebook or a
       dataset has nothing to read. Asking is the only way. */
    const name = window.prompt(
      `Kaggle username for ${button.dataset.username}?

`
      + 'Kaggle only reveals a handle through something the account owns, '
      + 'and this one owns no notebook or dataset yet. It is the name in '
      + 'your profile URL: kaggle.com/<username>.');
    if (name) backend.setUsername(button.dataset.username, name);
    return;
  }
  if (button.dataset.start) backend.startInstances(JSON.stringify([button.dataset.start]));
  if (button.dataset.hwcheck) backend.checkHardware(button.dataset.hwcheck);
  if (button.dataset.cancel) backend.cancelInstance(button.dataset.cancel);
  if (button.dataset.download) backend.collect(button.dataset.download);
  if (button.dataset.remove) backend.removeAccount(button.dataset.remove);
});

document.getElementById('btn-start-all').onclick = () =>
  backend && backend.startInstances(JSON.stringify([]));
document.getElementById('btn-stop-all').onclick = () =>
  backend && backend.cancelAll();
document.getElementById('btn-send-job').onclick = () =>
  backend && backend.sendJob(JSON.stringify(renderOptions()));
document.getElementById('btn-forget').onclick = () => {
  /* Confirmed, and worded as what it actually is. Abandoning a running
     session while sounding like a cancel would be the worst lie this app
     could tell -- somebody else's quota keeps draining either way. */
  const ok = window.confirm(
    'Stop tracking this job?\n\n'
    + 'This does NOT cancel anything. Any kernels still running on Kaggle '
    + 'keep running and keep spending quota, and this app will no longer '
    + 'be able to stop them or collect their frames.\n\n'
    + 'Use this only when Kaggle refuses to cancel and you are stuck. '
    + 'Then stop them by hand at kaggle.com.');
  if (ok && backend) backend.forgetJob();
};

document.getElementById('btn-add').onclick = () => {
  const label = document.getElementById('ni-label').value.trim();
  const token = document.getElementById('ni-token').value.trim();
  const err = document.getElementById('add-err');
  if (!label || !token) {
    err.textContent = 'Both a name and a token are needed.';
    return;
  }
  err.textContent = '';
  backend.addAccount(label, token);
  document.getElementById('ni-token').value = '';
};

/* ---------------- logs page --------------------------------------------- */
function renderFailures(state) {
  const list = document.getElementById('err-list');
  const failures = state.instances.filter(i => i.worker && i.worker.message);
  document.getElementById('err-empty').style.display =
    failures.length ? 'none' : '';
  document.getElementById('err-meta').textContent =
    `${failures.length} ${failures.length === 1 ? 'entry' : 'entries'}`;
  document.getElementById('nav-errs').textContent = failures.length;
  list.innerHTML = failures.map(i => `<div class="err-row">
      <span class="ed"></span>
      <div><div class="em"><b>${esc(i.label)}</b></div>
        <div class="es">${esc(i.worker.message)}</div></div>
    </div>`).join('');
}

/* ---------------- settings page ----------------------------------------- */
function bindToggle(id, key) {
  const el = document.getElementById(id);
  const flip = () => {
    const next = !el.classList.contains('on');
    el.classList.toggle('on', next);
    el.setAttribute('aria-checked', String(next));
    if (backend) backend.setPreference(key, JSON.stringify(next));
    if (key === 'sound') prefs.sound = next;
    if (key === 'translucent') document.body.classList.toggle('glass', next);
  };
  el.addEventListener('click', flip);
  el.addEventListener('keydown', e => {
    if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); flip(); }
  });
}
bindToggle('tgl-glass', 'translucent');
bindToggle('tgl-sound', 'sound');

document.getElementById('seg-theme').addEventListener('click', e => {
  const value = e.target.dataset.v;
  if (!value) return;
  applyPrefs({ theme: value });
  backend && backend.setPreference('theme', JSON.stringify(value));
  syncSettingsControls();
});
document.getElementById('swatches').addEventListener('click', e => {
  const value = e.target.dataset.v;
  if (!value) return;
  applyPrefs({ accent: value });
  backend && backend.setPreference('accent', JSON.stringify(value));
  syncSettingsControls();
});
document.getElementById('min-gpus').addEventListener('change', e => {
  backend && backend.setPreference('minGpus', JSON.stringify(+e.target.value));
});

function syncSettingsControls() {
  document.querySelectorAll('#seg-theme button').forEach(b =>
    b.classList.toggle('on', b.dataset.v === prefs.theme));
  document.querySelectorAll('#swatches .swatch').forEach(b =>
    b.classList.toggle('on', b.dataset.v === prefs.accent));
  document.getElementById('tgl-glass').classList.toggle('on', !!prefs.translucent);
  document.getElementById('tgl-sound').classList.toggle('on', prefs.sound !== false);
  if (prefs.minGpus !== undefined) {
    document.getElementById('min-gpus').value = prefs.minGpus;
  }
}

/* ---------------- bridge ------------------------------------------------ */
new QWebChannel(qt.webChannelTransport, channel => {
  backend = channel.objects.backend;

  backend.preferences(json => { applyPrefs(JSON.parse(json)); syncSettingsControls(); });
  backend.settingsChanged.connect(json => { applyPrefs(JSON.parse(json)); syncSettingsControls(); });

  /* Upload and download report as bytes, not as a spinner: a 400 MB
     .blend on a slow line is the one moment the app looks frozen, and a
     percentage is the difference between waiting and worrying. */
  backend.uploadProgress.connect(json => showUploadStage(JSON.parse(json)));
  backend.downloadProgress.connect(json => {
    const p = JSON.parse(json);
    const pct = p.total ? Math.round(100 * p.downloaded / p.total) : 0;
    logLine(`${p.label}: downloading ${pct}%`, '');
  });

  backend.busyChanged.connect((key, busy) => {
    const map = { launch: 'btn-render', cancel: 'btn-cancel',
                  'collect:': 'btn-collect', dataset: 'btn-upload' };
    const id = map[key];
    if (id) document.getElementById(id).disabled = busy;
    if (key.indexOf('verify:') === 0) document.getElementById('btn-add').disabled = busy;
  });

  backend.state(renderState);
  backend.stateChanged.connect(renderState);

  backend.health(renderHealth);
  backend.healthChanged.connect(renderHealth);

  backend.logLine.connect((message, tone_) => {
    logLine(message, tone_);
    if (tone_ === 'active' && message.indexOf('reconnected') === 0) sfx.reconnect();
  });
  backend.notification.connect(notify);

  backend.refreshQuota();
  backend.poll();
});
