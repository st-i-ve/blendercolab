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
  const row = document.createElement('div');
  row.className = 'notif' + (tone_ === 'offline' ? ' err' : tone_ === 'active' ? ' ok' : '');
  row.innerHTML = `<div class="nt">${esc(message)}</div><div class="nm">${clock()}</div>`;
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
  const gpus = state.instances.reduce(
    (n, i) => n + (i.worker && i.hardware ? i.hardware.gpus.length : 0), 0);
  const frames = state.instances.reduce(
    (n, i) => n + (i.worker ? i.worker.framesDone : 0), 0);

  setStat('inst', online);
  setStat('run', running);
  setStat('gpus', gpus);
  setStat('frames', frames);

  document.getElementById('nav-running').textContent = running;
  document.getElementById('nav-inst').textContent = online;
  document.getElementById('nav-files').textContent = state.job ? 1 : 0;

  /* Every page is driven from the SAME payload on the same tick. A count
     in the sidebar that updates on a different schedule from the page it
     points at is worse than no count. */
  renderFleetTable(state);
  renderFrameGrid(state);
  renderFailures(state);
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

  const progress = worker && worker.frames.length
    ? Math.round(100 * worker.framesDone / worker.frames.length) : 0;

  return `<div class="inst">
    <div class="inst-head">
      <span class="status-dot ${dot}"></span>
      <span class="name">${esc(inst.label)}</span>
      <span class="sub">${esc(inst.username || '')}</span>
      <span class="right">
        ${inst.verified ? '' : '<span class="badge warn"><i></i>not verified</span>'}
        <span class="badge ${badge[0]}"><i></i>${badge[1]}</span>
      </span>
    </div>
    <div class="inst-body">
      <div class="hw-row">
        <span class="hw-chip">Quota (API) <b>${esc(inst.quota || '—')}</b></span>
      </div>
      <div class="hw-row"><span class="hw-chip">${hw}</span></div>
      ${worker ? `
      <div class="assign">
        <div class="wrapc">
          <div class="l1"><span class="flab">Frames</span>
            <span class="frange">${worker.framesDone} / ${worker.frames.length}</span></div>
          <div class="assign-progress"><i style="width:${progress}%"></i></div>
        </div>
      </div>` : ''}
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
  });
});

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
    i.worker.frames.slice(0, i.worker.framesDone).forEach(f => done.add(f));
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
  body.innerHTML = state.instances.map(i => {
    const hw = i.hardware
      ? `${esc(i.hardware.gpus.map(g => g.model || 'GPU').join(', ') || 'no GPU seen')} <span class="dim">${fmtAge(i.hardware.ageSeconds)}</span>`
      : '<span class="dim">never run</span>';
    const state_ = i.worker ? i.worker.state : 'idle';
    const stoppable = i.worker && ['running', 'queued'].includes(i.worker.state);
    return `<tr>
      <td>${esc(i.label)}${i.verified ? '' : ' <span class="badge warn"><i></i>unverified</span>'}</td>
      <td>${esc(i.username || '—')}</td>
      <td>${esc(i.quota || '—')}</td>
      <td>${hw}</td>
      <td>${esc(state_)}</td>
      <td style="text-align:right;white-space:nowrap">
        ${stoppable ? `<button class="btn sm" data-cancel="${esc(i.label)}">Cancel</button>` : ''}
        <button class="btn sm" data-download="${esc(i.label)}">Download</button>
        <button class="btn sm danger" data-remove="${esc(i.label)}">Remove</button>
      </td></tr>`;
  }).join('');
}

document.getElementById('inst-tbody').addEventListener('click', e => {
  const button = e.target.closest('button');
  if (!button || !backend) return;
  if (button.dataset.cancel) backend.cancelInstance(button.dataset.cancel);
  if (button.dataset.download) backend.collect(button.dataset.download);
  if (button.dataset.remove) backend.removeAccount(button.dataset.remove);
});

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
  backend.uploadProgress.connect(json => {
    const p = JSON.parse(json);
    const pct = p.totalBytes ? Math.round(100 * p.sentBytes / p.totalBytes) : 0;
    document.getElementById('up-name').textContent = `uploading · ${p.label}`;
    document.getElementById('up-pct').textContent = `${pct}%`;
    document.getElementById('up-bar').style.width = pct + '%';
  });
  backend.downloadProgress.connect(json => {
    const p = JSON.parse(json);
    const pct = p.totalBytes ? Math.round(100 * p.receivedBytes / p.totalBytes) : 0;
    logLine(`${p.label}: downloading ${pct}%`, '');
  });

  backend.busyChanged.connect((key, busy) => {
    const map = { launch: 'btn-render', cancel: 'btn-cancel', 'collect:': 'btn-collect' };
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
