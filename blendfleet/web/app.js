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
    // The scene library has no polling timer of its own -- refreshed on
    // every visit instead, so it is never more stale than "since you last
    // looked", not "since the app started".
    if (page === 'files' && backend) backend.scenes();
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
/* In-flight downloads, keyed by instance label: {downloaded, total, rate}.
   A download used to be a single log line ("acct0: downloading 42%"), which
   scrolled away and told you nothing about size or speed -- so a 36 MB
   fetch over a slow link was indistinguishable from a stuck one. Cleared
   when the collect finishes, so a finished card is not left showing a bar
   at 100% for ever. */
const downloads = {};

/* The last payload rendered, so a download tick can repaint the cards
   without waiting for the next 30-second poll. */
let lastStateJson = null;

/* The payload the cards ON SCREEN were built from -- which is not the same
   question as `lastStateJson`, the newest payload received whether or not
   it changed anything. The live tick fires every 2 seconds and re-emits a
   byte-identical payload whenever nothing moved; rebuilding every card for
   one of those replaced a whole grid of composited layers to arrive back
   at the same pixels. */
let lastRenderedJson = null;

function renderState(json) {
  lastStateJson = json;
  const state = JSON.parse(json);
  const wrap = document.getElementById('instances');

  /* Only the card grid is guarded, and only against a payload that has
     not changed by a single byte -- so anything that genuinely moved
     (GPU load, frame counts, which is most ticks) still repaints. The
     rest of this function writes numbers and text into elements that
     already exist, which is cheap; THIS discards and rebuilds every
     .inst in the fleet, which is not. */
  if (json !== lastRenderedJson) {
    if (!state.instances.length) {
      wrap.innerHTML = '<div class="empty">No accounts yet — add one under Instances to start rendering.</div>';
    } else {
      wrap.innerHTML = renderJobSections(state);
    }
    lastRenderedJson = json;
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

  const jobs = state.jobs || [];
  const jobMeta = document.getElementById('job-meta');
  if (jobMeta) {
    jobMeta.textContent = jobs.length
      ? `${jobs.length} scene${jobs.length === 1 ? '' : 's'} rendering`
      : (state.blend ? `${state.blend.name} · not started` : 'no scene chosen');
  }
  document.getElementById('nav-running').textContent = running;
  document.getElementById('nav-inst').textContent = online;
  document.getElementById('nav-files').textContent = jobs.length ? 1 : 0;

  /* Every page is driven from the SAME payload on the same tick. A count
     in the sidebar that updates on a different schedule from the page it
     points at is worse than no count. */
  renderFleetTable(state);
  renderAssignList(state);
  renderUnreadable(state);
  renderUnshared(state);
  renderFailures(state);
  renderDataset(state);
}

/* Rebuild the cards from the payload already on screen.
   A card also reads `downloads`, which is NOT part of the payload, so a
   download tick changes what the card should say without changing a byte
   of state -- exactly the case renderState's guard above is built to
   ignore. Callers that changed `downloads` say so by coming through
   here rather than by calling renderState directly. */
function repaintCards() {
  if (!lastStateJson) return;
  lastRenderedJson = null;
  renderState(lastStateJson);
}

/* Groups state.instances by the job they belong to -- one section per
   SCENE, each with its own frame grid and its own collect/cancel
   controls (carrying data-job, so a click can never be misrouted to the
   wrong scene), then a final section for accounts that are idle. Two
   concurrent renders used to be indistinguishable: every card landed in
   one flat grid with no heading saying which scene it belonged to. */
function renderJobSections(state) {
  const byJob = {};
  const idle = [];
  state.instances.forEach(inst => {
    if (inst.jobId) (byJob[inst.jobId] = byJob[inst.jobId] || []).push(inst);
    else idle.push(inst);
  });
  const jobsById = new Map((state.jobs || []).map(j => [j.jobId, j]));
  const sections = Object.keys(byJob).map(jobId => {
    /* A job a running account points at but that is missing from
       `jobs[]` (a payload inconsistency, never expected in practice) is
       still shown -- its own instances must not silently vanish -- but
       honestly, as an unknown scene rather than a guessed one. */
    const job = jobsById.get(jobId) || {
      jobId, scene: 'unknown scene', blend: 'unknown', startFrame: 1,
      endFrame: 0, labels: [], elapsed: null, finished: false,
    };
    return jobSectionHtml(job, byJob[jobId]);
  });
  return sections.join('') + idleSectionHtml(idle);
}

function jobSectionHtml(job, instances) {
  const elapsed = job.elapsed != null
    ? `<span class="job-sub">${job.finished ? 'finished in ' : ''}${
        fmtDuration(job.elapsed)}</span>`
    : '';
  return `<section class="job-group">
    <div class="job-head">
      <h3 class="job-title">${esc(job.scene)}</h3>
      <span class="job-sub">${esc(job.blend)} · frames ${job.startFrame}-${job.endFrame}</span>
      ${elapsed}
      <div class="job-actions">
        <button class="btn sm" data-job-collect="${esc(job.jobId)}"
          title="Download this scene's rendered frames">Collect frames…</button>
        <button class="btn sm danger" data-job-cancel="${esc(job.jobId)}"
          title="Cancel every account rendering this scene">Cancel</button>
      </div>
    </div>
    <div class="instances">${instances.map(instanceCard).join('')}</div>
    ${renderFrameGrid(job, instances)}
  </section>`;
}

function idleSectionHtml(instances) {
  if (!instances.length) return '';
  return `<section class="job-group idle">
    <div class="job-head">
      <h3 class="job-title">Idle</h3>
      <span class="job-sub">not currently rendering</span>
    </div>
    <div class="instances">${instances.map(instanceCard).join('')}</div>
  </section>`;
}

/* Status icons for the instance cards. Inline SVG, matching index.html's
   own idiom exactly: 16x16 box, stroke="currentColor", stroke-width 1.4,
   so each one inherits its badge's colour and needs no asset file.
   Deliberately four unmistakable silhouettes -- arc, tick, clock, triangle
   -- so the state survives being read in monochrome. */
const ICON = {
  spinner: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<path d="M8 1.8a6.2 6.2 0 1 1-6.2 6.2" stroke="currentColor"'
    + ' stroke-width="1.8" stroke-linecap="round"/></svg>',
  tick: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<path d="M3 8.4l3.2 3.2L13 4.8" stroke="currentColor"'
    + ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  clock: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5"/>'
    + '<path d="M8 4.6V8l2.4 1.6" stroke="currentColor" stroke-width="1.5"'
    + ' stroke-linecap="round" stroke-linejoin="round"/></svg>',
  warning: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<path d="M8 5.5v3.2M8 11.2v.1" stroke="currentColor" stroke-width="1.6"'
    + ' stroke-linecap="round"/><path d="M6.6 2.4c.6-1.1 2.2-1.1 2.8 0l4.4 8.1'
    + 'c.6 1.1-.2 2.5-1.4 2.5H3.6c-1.2 0-2-1.4-1.4-2.5l4.4-8.1z"'
    + ' stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
  cross: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<path d="M4.6 4.6l6.8 6.8M11.4 4.6l-6.8 6.8" stroke="currentColor"'
    + ' stroke-width="1.7" stroke-linecap="round"/></svg>',
  dash: '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<path d="M4.4 8h7.2" stroke="currentColor" stroke-width="1.8"'
    + ' stroke-linecap="round"/></svg>',
};

function instanceCard(inst) {
  const worker = inst.worker;
  const state = worker ? worker.state : 'idle';
  /* An icon, not a word -- but a DISTINCT SHAPE per state, never a bare
     coloured dot. The original rule here was "symbol AND word, always",
     because meaning carried by colour alone is lost to the ~8% of men
     with a colour vision deficiency. A spinner, a tick, a clock and a
     warning triangle are different shapes in monochrome, so that rule
     still holds; the word was what made the pill overflow its card and
     get clipped to "RENDERI…". The word survives as the tooltip. */
  const badge = {
    running:  ['rendering', 'rendering', ICON.spinner],
    queued:   ['idle', 'queued', ICON.clock],
    complete: ['complete', 'complete', ICON.tick],
    error:    ['warn', 'error', ICON.warning],
    cancel_requested:    ['paused', 'cancelling', ICON.cross],
    cancel_acknowledged: ['paused', 'cancelled', ICON.cross],
  }[state] || ['idle', 'idle', ICON.dash];
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
  const liveFrames = !!(live && live.framesTotal);
  const done = liveFrames ? live.framesDone
             : (worker ? worker.framesDone : 0);
  const total = liveFrames ? live.framesTotal
              : (worker ? worker.frames.length : 0);
  const progress = total ? Math.round(100 * done / total) : 0;

  /* Where that number came from, said out loud whenever it is NOT live.
     After a restart the count is whatever the last stream managed to save
     before the window closed -- the render kept going on Kaggle in the
     meantime, so it is a floor, not a reading. Presenting it bare, next
     to a bar, would claim a measurement nobody took. It carries its age,
     the same way the cached hardware line does. */
  const savedFrames = (!liveFrames && worker && worker.framesDoneAge != null)
    ? `<span class="frange dim" title="Saved by the last live connection.${
         ''} Kaggle's status API reports no frame count, so this is the${
         ''} newest number this app could have; the render has carried on${
         ''} since.">saved ${fmtAge(worker.framesDoneAge)}</span>`
    : '';

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
  /* "GPU" over one bar and "VRAM" over the next read as two names for the
     same thing. They are not: the first is how BUSY the chip is, the
     second is how much of its MEMORY is occupied -- a card can be 99%
     busy on 3 GB, or idle holding 14. Naming both after the card they
     belong to, and the quantity they measure, says so without a legend. */
  const gpuRows = live && live.gpus.length
    ? live.gpus.map(g => `<div class="gpu-line">
        <span class="tag on" title="How busy this GPU is right now">GPU ${
          g.index} load</span>
        <div class="track rendering"><i style="width:${g.util || 0}%"></i></div>
        <span class="pct">${g.util == null ? '—' : g.util + '%'}</span>
      </div>
      <div class="gpu-line">
        <span class="tag" title="Memory in use on this GPU, of its total">GPU ${
          g.index} memory</span>
        <div class="track"><i style="width:${
          g.memTotal ? Math.round(100 * g.memUsed / g.memTotal) : 0}%"></i></div>
        <span class="pct">${g.memTotal
          ? Math.round(g.memUsed / 1024) + '/' + Math.round(g.memTotal / 1024) + 'G'
          : '—'}</span>
      </div>`).join('')
    : '';

  /* System RAM, live, in the same shape as the GPU rows -- the machine's
     memory is what an out-of-memory kill actually exhausts, and until now
     only its TOTAL was ever shown (once, in the session chip). A session
     could be seconds from being killed while the card read a reassuring
     "31.3 GB RAM". Absent until the first SYSTEM sample: a bar at zero
     would claim a measurement that has not been taken. */
  const ramUsedGb = live && live.ramUsed ? live.ramUsed / (1024 ** 3) : null;
  const sysRow = ramUsedGb && ramTotal
    ? `<div class="gpu-line">
        <span class="tag" title="System RAM in use on this machine, of its total">System RAM</span>
        <div class="track"><i style="width:${
          Math.min(100, Math.round(100 * ramUsedGb / ramTotal))}%"></i></div>
        <span class="pct">${ramUsedGb.toFixed(1)}/${ramTotal.toFixed(0)}G</span>
      </div>`
    : '';

  /* A download in flight, shown the way the render is: a bar, a size and
     a rate. The frames arrive as ONE zip per worker (the notebook builds
     it as each frame finishes), so this is normally a single file moving,
     and "12.4 / 36.1 MB · 1.8 MB/s" is what says whether it is moving at
     all. Bytes, never a bare percentage: a percentage of an unknown total
     is how a stalled transfer looks healthy. */
  const dl = downloads[inst.label];
  const dlRow = dl
    ? `<div class="gpu-line dl-row" data-dl="${esc(inst.label)}">
        <span class="tag on">downloading</span>
        <div class="track rendering"><i data-dl-bar style="width:${
          dl.total ? Math.min(100, Math.round(100 * dl.downloaded / dl.total)) : 0
        }%"></i></div>
        <span class="pct" data-dl-text>${fmtDownload(dl)}</span>
      </div>`
    : '';

  /* Elapsed time, and after it stops, the total. "finished in 5:20" is
     the benchmark; while it runs the same number is the stopwatch, so
     one field serves both and they cannot disagree. */
  const elapsed = worker && worker.elapsed != null
    ? `<span class="elapsed">${worker.finished ? 'finished in ' : ''}${
        fmtDuration(worker.elapsed)}</span>`
    : '';
  /* The window after a restart, before the replayed log has rebuilt
     anything. The render never stopped -- only this app's view of it did
     -- and a card that simply sat blank there was read as a stuck render
     and is the whole reason this text exists. It says what is happening,
     why there is nothing to show yet, and that there is nothing to do. */
  const phase = live && live.phase
    ? `<div class="inst-foot"><b>${esc(live.phase)}</b>${elapsed}</div>`
    : inst.reconnecting
      ? `<div class="inst-foot"><b>reconnecting — replaying this session's`
        + ` log from Kaggle to catch up</b>${elapsed}<span class="sub">The`
        + ` render never stopped; nothing to restart.</span></div>`
    : (worker && worker.state === 'queued'
       ? '<div class="inst-foot">queued — waiting for Kaggle to allocate a machine</div>'
       : (elapsed ? `<div class="inst-foot">${elapsed}</div>` : ''));

  return `<div class="inst">
    <div class="inst-head">
      <span class="status-dot ${dot}"></span>
      <span class="name">${esc(inst.label)}</span>
      ${/* Shown ONLY when it differs from the label. For most accounts the
            two are identical, and printing "sudaouserwithani
            sudaouserwithani" was both noise and what pushed the status
            pill off the edge of the card. A renamed account still needs
            its real Kaggle identity visible, so it is not simply
            deleted. */
        inst.username && inst.username !== inst.label
          ? `<span class="sub">${esc(inst.username)}</span>` : ''}
      <span class="right">
        ${/* The account holding the upload everyone else reads from.
              Worth naming: it is the only one that pays the upload, and
              when a share goes wrong it is the one to check first. */
          inst.owner
            ? '<span class="badge accent" title="This account holds the'
              + ' uploaded scene; the others read it from here">'
              + '<i></i>owner</span>'
            : ''}
        ${inst.revoked
          ? '<span class="badge bad"><i></i>token revoked</span>'
          : inst.verified ? '' : '<span class="badge warn"><i></i>not verified</span>'}
        ${inst.username ? '' : '<span class="badge warn"><i></i>needs username</span>'}
        <span class="badge ico ${badge[0]}" title="${esc(badge[1])}"
              role="img" aria-label="${esc(badge[1])}">${badge[2]}</span>
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
            <span class="frange">${done} / ${total}</span>${savedFrames}</div>
          <div class="assign-progress"><i style="width:${progress}%"></i></div>
        </div>
      </div>` : ''}
      ${gpuRows}
      ${sysRow}
      ${dlRow}
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
/* "12.4 / 36.1 MB · 1.8 MB/s". The total is omitted rather than faked
   when Kaggle sends no Content-Length -- "12.4 MB of 0" is worse than
   "12.4 MB", because one of them is obviously incomplete information and
   the other looks like a bug. */
function fmtDownload(d) {
  const rate = d.rate ? ` · ${fmtBytes(d.rate)}/s` : '';
  if (!d.total) return `${fmtBytes(d.downloaded)}${rate}`;
  return `${fmtBytes(d.downloaded)} / ${fmtBytes(d.total)}${rate}`;
}

/* How long a render took, read the way a person says it. Under an hour
   it is mm:ss, which is how you read a stopwatch; past an hour the bare
   "1:05:20" is ambiguous enough at a glance to be worth spelling out.
   Seconds are always shown -- "1h 5m" hides up to 59s of a benchmark. */
function fmtDuration(seconds) {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return '';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${m}m ${sec}s`;
  return `${m}:${String(sec).padStart(2, '0')}`;
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
/* Offered as a menu, not a gate -- an unlisted-but-valid version typed by
   hand elsewhere still renders; this list is only what the picker shows
   by default. A named function (not an inline callback) so it can be
   exercised directly with a hand-built payload, the same way instanceCard
   and renderFrameGrid are. */
function populateBlenderVersions(json) {
  const v = JSON.parse(json);
  const sel = document.getElementById('sel-blender');
  sel.innerHTML = v.versions.map(x =>
    `<option value="${esc(x)}"${x === v.current ? ' selected' : ''}>${esc(x)}</option>`
  ).join('');
}

/* `labels` is left OFF entirely unless the user has actually touched a
   checkbox in #assign-list -- launch()'s own contract (bridge.py) is that
   ABSENT means every free account, while an explicitly EMPTY list is
   REFUSED outright (Fix round 1, Critical: unticking every box must not
   fall back to rendering on everybody). assignTouched is what tells
   these two "nobody chosen yet" and "chose nobody on purpose" apart. */
let assignTouched = false;
/* Fix round 1, CRITICAL: renderAssignList used to rebuild #assign-list
   from scratch on every stateChanged (the 30s poll among them) with
   every free account hard-coded ` checked`, so a deliberate untick
   survived only until the NEXT tick -- measured, it silently came back
   ticked and launch() would then render on an account the user had just
   excluded. This set is the user's own choice, kept independent of
   whatever renderAssignList paints next: a label in it stays unticked
   across every re-render until the user ticks it again themselves. A
   label never added here (including one that only just became free)
   still defaults to ticked. */
const assignUnchecked = new Set();

function renderOptions() {
  const options = {
    startFrame: +document.getElementById('f-start').value,
    endFrame: +document.getElementById('f-end').value,
    resX: +document.getElementById('f-rx').value,
    resY: +document.getElementById('f-ry').value,
    samples: +document.getElementById('f-spp').value,
    format: document.getElementById('f-fmt').value,
    blenderVersion: document.getElementById('sel-blender').value,
  };
  if (assignTouched) {
    options.labels = Array.from(
      document.querySelectorAll('#assign-list input[data-assign]:checked')
    ).map(el => el.dataset.assign);
  }
  return options;
}

/* An account holds a real, ACTIVE session only while its worker is
   queued or running -- fleet.py's own ACTIVE_STATES, and free_accounts()
   says in as many words that a FINISHED job holds nobody. Fix round 1,
   CRITICAL: this used to be `!!inst.jobId`, which stays true long after
   the job behind it finished (a jobId is only cleared by forgetting the
   job) -- so a completed render's accounts showed disabled with a false
   "already rendering" title, and ticking the box anyway still produced
   labels: [], which launch() then refused with "no machines selected"
   and left no tickable account to fix it with. */
function isActivelyRendering(inst) {
  return !!(inst.worker
    && (inst.worker.state === 'running' || inst.worker.state === 'queued'));
}

/* One checkbox per configured account, in the render panel. A FREE
   account defaults to checked unless the user has UNticked it (tracked
   in assignUnchecked, independent of this function's own re-renders --
   see that set's own comment for the bug this fixes). An account
   actively rendering another scene is shown disabled, with the reason in
   its title: ticking it would spend that account's quota twice for the
   same output, which this app never offers as an option. */
function renderAssignList(state) {
  const el = document.getElementById('assign-list');
  if (!el) return;
  if (!state.instances.length) {
    el.innerHTML = '<div class="dz-sub" style="padding:6px 2px">No accounts yet — add one under Instances.</div>';
    return;
  }
  const jobsById = new Map((state.jobs || []).map(j => [j.jobId, j]));
  el.innerHTML = state.instances.map(inst => {
    const busy = isActivelyRendering(inst);
    const job = busy ? jobsById.get(inst.jobId) : null;
    const scene = job ? job.scene : 'another scene';
    const title = busy
      ? `${esc(inst.label)} is already rendering ${esc(scene)} — starting `
        + 'a second render on it would spend this account\'s quota twice '
        + 'for the same output.'
      : '';
    const checked = !busy && !assignUnchecked.has(inst.label);
    return `<label class="assign-opt"${title ? ` title="${title}"` : ''}>
      <input type="checkbox" data-assign="${esc(inst.label)}"${busy ? ' disabled' : ''}${checked ? ' checked' : ''}>
      <span class="an">${esc(inst.label)}</span>
      ${busy ? `<span class="ad">rendering ${esc(scene)}</span>` : ''}
    </label>`;
  }).join('');
}
document.getElementById('assign-list').addEventListener('change', e => {
  if (!e.target.matches('[data-assign]')) return;
  assignTouched = true;
  const label = e.target.dataset.assign;
  if (e.target.checked) assignUnchecked.delete(label);
  else assignUnchecked.add(label);
});

/* Job records this app could no longer read at all -- see
   Fleet.unreadable_jobs and bridge.py's _unreadable_jobs_payload(). Never
   silently dropped: any kernels named in `message` may still be running
   on Kaggle and billing quota, with nothing here able to cancel or
   collect them any more. "Forget this record" is explicit that it is NOT
   a cancel -- it only stops this app from being able to warn about it. */
function renderUnreadable(state) {
  const el = document.getElementById('unreadable-banner');
  const entries = state.unreadableJobs || [];
  el.classList.toggle('show', entries.length > 0);
  if (!entries.length) { el.innerHTML = ''; return; }
  el.innerHTML = entries.map(u => `<div class="unreadable-row">
      <span class="warn-ico">${ICON.warning}</span>
      <div class="unreadable-body">
        <div>${esc(u.message)}</div>
        ${u.kernelUrls && u.kernelUrls.length
          ? `<div class="unreadable-links">${u.kernelUrls.map(url =>
              `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>`
            ).join(' ')}</div>`
          : ''}
      </div>
      <button class="btn sm danger" data-forget-unreadable="${u.index}"
        data-fingerprint="${esc(u.fingerprint)}"
        title="Stops this app from warning about this record. Does NOT cancel anything still running on Kaggle.">Forget this record</button>
    </div>`).join('');
}
document.getElementById('unreadable-banner').addEventListener('click', e => {
  const btn = e.target.closest('[data-forget-unreadable]');
  if (!btn || !backend) return;
  /* Fix round 1, Minor: this used to fire on the first click, with the
     "not a cancel" honesty confined to a hover title nobody has to read.
     It permanently discards what may be the only surviving trace of
     kernels still billing on Kaggle -- the same stakes as btn-forget's
     own confirm, worded the same way. */
  const ok = window.confirm(
    'Forget this record?\n\n'
    + 'This does NOT cancel anything. If it named any kernels and they '
    + 'are still running on Kaggle, they keep running and keep spending '
    + 'quota, and this app will no longer be able to warn about them.\n\n'
    + 'Check kaggle.com and stop them by hand first if you have not.');
  if (!ok) return;
  backend.forgetUnreadableJob(Number(btn.dataset.forgetUnreadable),
                              btn.dataset.fingerprint);
});

/* Accounts the LAST scene upload could not be shared with (bridge.py's
   `unshared`). `note` always travels with it, because this is a snapshot
   of that one upload -- never a live check of the dataset in use right
   now -- and showing the accounts without that scope would read as a
   current, ongoing failure.

   Fix round 1, Minor: `null` (nothing recorded this session -- no upload
   has happened yet) and `{accounts:{}}` (a real upload that reached
   EVERY account) used to both render as no banner at all, so "we do not
   know" and "we checked and it is fine" were indistinguishable -- on a
   page whose whole rule is that an unknown must never read as a fine.
   `null` still shows nothing (there is genuinely nothing to report);
   `{accounts:{}}` now shows a real, positive confirmation instead of
   silence standing in for one. */
function renderUnshared(state) {
  const el = document.getElementById('unshared-banner');
  const unshared = state.unshared;
  if (unshared == null) {
    el.className = 'unshared-banner';
    el.innerHTML = '';
    return;
  }
  const accounts = unshared.accounts || {};
  const names = Object.keys(accounts);
  if (!names.length) {
    el.className = 'unshared-banner show ok';
    el.innerHTML = `<div class="unshared-head"><b>Last upload was shared with every account.</b></div>
      <div class="unshared-note">${esc(unshared.note)}</div>`;
    return;
  }
  el.className = 'unshared-banner show';
  el.innerHTML = `<div class="unshared-head"><b>Not everyone can see the last uploaded scene.</b></div>`
    + names.map(name => `<div class="unshared-row"><b>${esc(name)}</b>: ${esc(accounts[name])}</div>`).join('')
    + `<div class="unshared-note">${esc(unshared.note)}</div>`;
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

/* Who has the scene, and who is still waiting for it.
   Two separate steps hid behind one progress bar: the scene is uploaded
   ONCE by the owner, then every other account is granted READER on that
   one dataset and has to be confirmed able to actually see it. When two
   accounts appeared to render nothing (2026-08-12) the first question
   was "did they ever get the file?", and nothing on screen could answer
   it. `owner` is the account that holds the upload; everyone else is a
   row that starts pending and is ticked by its own confirmation. */
const share = { owner: null, rows: {} };

function resetShare() {
  share.owner = null;
  share.rows = {};
  renderShareList();
}

function renderShareList() {
  const list = document.getElementById('share-list');
  const names = Object.keys(share.rows);
  if (!share.owner && !names.length) {
    list.hidden = true;
    list.innerHTML = '';
    return;
  }
  list.hidden = false;
  const row = (name, state, isOwner) => `<li class="share-row ${state}">
      <span class="share-mark">${
        state === 'ok' ? ICON.tick : state === 'bad' ? ICON.warning : ICON.clock
      }</span>
      <span class="share-who">${esc(name)}</span>
      <span class="share-note">${
        isOwner ? 'owner — holds the upload'
                : state === 'ok' ? 'has the scene'
                : state === 'bad' ? 'cannot see it'
                : 'waiting for access'
      }</span>
    </li>`;
  list.innerHTML =
    (share.owner ? row(share.owner, 'ok', true) : '')
    + names.map(n => row(n, share.rows[n], false)).join('');
}

function showUploadStage(p) {
  /* Stage names are the contract with fleet.prepare_dataset's on_stage --
     see the stage() calls there. */
  const stageDetail = p.detail || '';
  if (p.stage === 'checking') resetShare();
  if (p.stage === 'verifying') {
    share.owner = stageDetail;       // the account that uploaded
    renderShareList();
  }
  if (p.stage === 'sharing') {
    stageDetail.split(',').map(s => s.trim()).filter(Boolean)
      .forEach(name => { share.rows[name] = 'pending'; });
    renderShareList();
  }
  if (p.stage === 'verifying-access') {
    share.rows[stageDetail] = 'ok';
    renderShareList();
  }
  if (p.stage === 'ready') {
    Object.keys(share.rows).forEach(n => { share.rows[n] = 'ok'; });
    renderShareList();
  }
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

/* ---------------- scene library (Files page) ----------------------------
   Browse scenes already on Kaggle, re-render one without re-uploading, or
   delete one for good. Every figure here comes from bridge.py's scenes()
   -- nothing is invented, and nothing here may claim a listed scene is
   confirmed to hold a .blend: that "-blend" suffix is only ever a naming
   CONVENTION (scenes.py's own docstring), proven true or false only once
   renderScene() actually lists the dataset's real files. */

/* Kaggle reports last_updated as an ISO string, or not at all
   (Scene.updated: datetime | None) -- fmtAge already says "an age, never
   a missing reading is a zero" for hardware; reused here for the same
   reason, with its own honest text for the genuinely-unknown case rather
   than falling through to some default date. */
function fmtSceneUpdated(iso) {
  const UNKNOWN = 'unknown — no usable timestamp for it';
  if (!iso) return UNKNOWN;
  const ageSeconds = (Date.now() - new Date(iso).getTime()) / 1000;
  /* Fix round 1, Minor: `Math.max(ageSeconds, 0)` used to clamp a FUTURE
     timestamp (clock skew between this machine and Kaggle) into a false
     "just now" reading, and an unparseable string produced "NaNd ago" --
     both are fabricated readings the rest of this app never allows
     itself. Routed to the same honest-unknown text already used for a
     genuinely absent timestamp, rather than letting fmtAge dress up
     either one as a real age. */
  if (!Number.isFinite(ageSeconds) || ageSeconds < 0) return UNKNOWN;
  return fmtAge(ageSeconds);
}

/* Pure, like instanceCard()/renderFrameGrid() -- reads only the one scene
   handed to it, so test_web_page.py can drive it directly with a
   hand-built payload. */
function sceneRowHtml(scene) {
  return `<div class="fs-row">
    <div class="fi scene">.blend</div>
    <div class="grow">
      <div class="fn2">${esc(scene.name)}</div>
      <div class="fs2">${esc(scene.owner)} · ${fmtBytes(scene.sizeBytes)}
        · updated ${fmtSceneUpdated(scene.updated)}
        · guessed file ${esc(scene.blendName)}
        <span class="lock" title="The '-blend' name is a naming convention this app applies to its own uploads, not proof this dataset actually contains a .blend. That is only confirmed by listing its real files, which happens automatically right before rendering it.">(unverified)</span>
      </div>
    </div>
    <button class="btn sm" data-scene-render="${esc(scene.slug)}"
      title="Renders this scene straight from Kaggle, with no re-upload. Sharing is re-checked for every account before anything starts.">Render this</button>
    <button class="btn sm danger" data-scene-delete="${esc(scene.slug)}"
      data-scene-name="${esc(scene.name)}" data-scene-size="${scene.sizeBytes}"
      title="Permanently deletes this dataset from Kaggle.">Delete…</button>
  </div>`;
}

/* Accounts scenes() could not reach -- their error is attached, and every
   OTHER account's scenes are still shown (bridge.py's own discipline,
   mirroring CollectReport.worker_errors: one worker's failure never hides
   the rest). Reuses .unshared-banner's own markup shape (WARN severity, a
   list of account: reason rows) rather than inventing a second one. */
function renderScenes(json) {
  const payload = JSON.parse(json);
  const scenes = payload.scenes || [];
  const errors = payload.errors || {};
  const errNames = Object.keys(errors);

  const errBox = document.getElementById('scene-errors');
  errBox.classList.toggle('show', errNames.length > 0);
  errBox.innerHTML = errNames.length
    ? '<div class="unshared-head"><b>Could not reach every account.</b></div>'
      + errNames.map(name =>
          `<div class="unshared-row"><b>${esc(name)}</b>: ${esc(errors[name])}</div>`
        ).join('')
      + '<div class="unshared-note">Scenes owned by the account(s) above '
        + 'may be missing from the list below — every other account\'s '
        + 'own scenes are still shown.</div>'
    : '';

  const list = document.getElementById('scene-list');
  if (scenes.length) {
    list.innerHTML = scenes.map(sceneRowHtml).join('');
  } else if (errNames.length) {
    /* Fix round 1, Important 1: "No scenes on Kaggle yet" is a claim
       about what IS on Kaggle -- true only once every account was
       actually reached. With every account (or the only configured one)
       failing, what is really there is UNKNOWN, not empty; saying so
       instead of the "nothing yet" sentence is the whole point of this
       branch existing separately from the one below. The banner above
       already names who failed and why -- this just makes sure the
       empty-list sentence itself cannot be misread as "your Kaggle is
       empty" when it might just be a rate limit or a stale token. */
    list.innerHTML = '<div class="empty">Could not read the scene library '
      + `for ${errNames.length} account(s) — see above. What is on Kaggle `
      + 'right now is unknown until that is fixed. Retry, or check those '
      + 'accounts under Manage accounts…</div>';
  } else {
    list.innerHTML = '<div class="empty">No scenes on Kaggle yet — upload a .blend above to add one.</div>';
  }
}

/* The delete confirmation, factored out so it can be exercised directly
   (test_web_page.py) without simulating a click. Names the scene and its
   size, and says plainly that deleting is permanent and that any account
   this scene was shared with loses access -- Kaggle has no undo, trash or
   recycle bin for a deleted dataset (KaggleClient.delete_dataset's own
   docstring).

   Fix round 1, Important 2: used to close with "-- and remember: any
   account...", which put the literal word "remember" into EVERY one of
   these messages regardless of the scene's actual name -- and "remember"
   is itself a name this exact app's own tests and docs use for an
   example scene throughout. `"remember" in confirmMessage` therefore
   passed even with the name dropped entirely (verified: it still passes
   with `scene.name` undefined, rendering `Delete "undefined"?`). Reworded
   to drop that word -- the scene's name only ever appears once, inside
   the quoted title, which is where a test must look for it. */
function deleteConfirmMessage(scene) {
  return `Delete "${scene.name}"?\n\n`
    + `This permanently deletes the ${fmtBytes(scene.sizeBytes)} dataset `
    + `${scene.slug} from Kaggle. This cannot be undone — Kaggle keeps no `
    + 'undo, trash or recycle bin for a deleted dataset, and any account '
    + 'this scene was shared with loses access to it the moment it is '
    + 'gone.';
}

document.getElementById('scene-list').addEventListener('click', e => {
  const renderBtn = e.target.closest('[data-scene-render]');
  if (renderBtn && backend) {
    backend.renderScene(renderBtn.dataset.sceneRender,
                        JSON.stringify(renderOptions()));
    return;
  }
  const delBtn = e.target.closest('[data-scene-delete]');
  if (delBtn && backend) {
    const scene = {
      name: delBtn.dataset.sceneName,
      slug: delBtn.dataset.sceneDelete,
      sizeBytes: Number(delBtn.dataset.sceneSize),
    };
    if (window.confirm(deleteConfirmMessage(scene))) {
      backend.deleteScene(delBtn.dataset.sceneDelete);
    }
  }
});

/* One job's own frame grid, as an HTML fragment -- never the whole page's
   state. Pure, like instanceCard(): it reads only the ONE job and the
   instances already known to belong to it (the caller, renderJobSections,
   is what does that filtering), so two concurrent scenes can never bleed
   frames into each other's grid. */
function renderFrameGrid(job, instances) {
  /* Which frames are done is INFERRED, not reported: each account is
     assumed to have finished the first N of its own stride. That holds
     until a frame fails, which is why the legend says approximate. */
  const done = new Set();
  instances.forEach(i => {
    if (!i.worker) return;
    const n = i.live && i.live.framesTotal ? i.live.framesDone
            : i.worker.framesDone;
    i.worker.frames.slice(0, n).forEach(f => done.add(f));
  });
  const cells = [];
  for (let f = job.startFrame; f <= job.endFrame; f++) {
    /* A finished frame is clickable: one image is fetched on demand
       rather than collecting the whole job to look at a picture. An
       unfinished one is not -- there is nothing on Kaggle to fetch. */
    const isDone = done.has(f);
    cells.push(`<div class="fcell${isDone ? ' d' : ''}"${
      isDone ? ` data-frame="${f}" role="button" tabindex="0"` : ''
    } title="frame ${f}${isDone ? ' — click to preview' : ''}"></div>`);
  }
  const total = job.endFrame - job.startFrame + 1;
  return `<div class="fgrid-wrap show">
    <div class="fgrid" data-job="${esc(job.jobId || '')}">${cells.join('')}</div>
    <div class="fgrid-legend">
      <span><i style="background:var(--accent)"></i>done <b>(approximate)</b></span>
      <span><i style="background:var(--fill)"></i>not yet</span>
    </div>
    <div class="fg-meta">${done.size}/${total} frames · ${esc(job.blend)}
      — a failed frame shifts every later cell for that account; Collect
      frames is the authoritative list of what exists.</div>
  </div>`;
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

/* Fix round 2: startInstances('') -- an empty STRING, not '[]' -- now
   means "every configured account". '[]' means the caller explicitly
   asked for nobody and is refused, matching launch()'s own
   absent-vs-explicitly-empty distinction (bridge.py). This button
   still means "start everyone", unchanged; only the wire signal for
   that moved. */
document.getElementById('btn-start-all').onclick = () =>
  backend && backend.startInstances('');
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

/* ---------------- one rendered frame ------------------------------------ */
function openPreview(frame, url, label) {
  document.getElementById('lb-title').textContent = `frame ${frame}`;
  document.getElementById('lb-sub').textContent = label ? `rendered by ${label}` : '';
  const img = document.getElementById('lb-img');
  img.src = url;
  img.alt = `Rendered frame ${frame}`;
  document.getElementById('lightbox').hidden = false;
  document.getElementById('lb-close').focus();
}

function closePreview() {
  document.getElementById('lightbox').hidden = true;
  /* Dropped so the next open cannot flash the previous frame while the
     new one decodes. */
  document.getElementById('lb-img').removeAttribute('src');
}

document.getElementById('lb-close').addEventListener('click', closePreview);
document.getElementById('lightbox').addEventListener('click', e => {
  if (e.target.id === 'lightbox') closePreview();   // click the backdrop
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !document.getElementById('lightbox').hidden) {
    closePreview();
  }
});

/* Delegated on #instances, not on any one grid: renderJobSections rebuilds
   its whole innerHTML on every state tick, and with several scenes running
   there is no longer a single static #fgrid to bind to -- a listener
   attached to a grid div would not survive the very next tick, or the
   very next SECOND job either. #instances itself is never replaced, only
   its contents, so a listener bound here outlives every repaint. */
/* The jobId a clicked frame cell belongs to, from the .fgrid it lives in
   -- Fix round 1, IMPORTANT: frame numbers are not unique across jobs
   (two scenes both rendering frames 1-4 is the ordinary case, not an
   edge case), and previewFrame() used to take only a number, which the
   bridge resolved via Fleet.load() -- "the most recent job" -- so
   clicking scene A's frame 2 silently previewed scene B's frame 2
   whenever B was the more recently launched of the two. The grid has
   carried data-job since this task's first draft; only the click
   handler was never taught to read it. */
function jobIdOfCell(cell) {
  const grid = cell.closest('.fgrid');
  return (grid && grid.dataset.job) || '';
}

document.getElementById('instances').addEventListener('click', e => {
  const cell = e.target.closest('[data-frame]');
  if (cell && backend) {
    backend.previewFrame(Number(cell.dataset.frame), jobIdOfCell(cell));
    return;
  }
  const collectBtn = e.target.closest('[data-job-collect]');
  if (collectBtn && backend) {
    backend.collect('', collectBtn.dataset.jobCollect);
    return;
  }
  const cancelBtn = e.target.closest('[data-job-cancel]');
  if (cancelBtn && backend && lastStateJson) {
    /* backend.cancelJob(jobId) stops exactly this job's own accounts via
       Fleet.cancel_job(), which searches every tracked job -- unlike
       looping cancelInstance() per account, which only ever reaches
       Fleet.cancel_worker() -> load()'s single newest job, so cancelling
       any OLDER of two live scenes cancelled nothing and reported
       "already stopped" while that account's kernel kept running and
       billing (must-fix 1). */
    const jobId = cancelBtn.dataset.jobCancel;
    const job = (JSON.parse(lastStateJson).jobs || [])
      .find(j => j.jobId === jobId);
    const labels = job ? job.labels : [];
    if (!labels.length) return;
    /* Fix round 1, IMPORTANT: several identical red Cancel buttons now
       sit in a list that reshuffles every 30 seconds, and one misclick
       ends work other people's quota already paid for, with no undo --
       restarting spends that quota again. btn-forget confirms for an
       action that is strictly LESS destructive (forgetting stops
       nothing); this is strictly more, so it must confirm too, and name
       exactly who is about to be stopped. */
    const ok = window.confirm(
      `Cancel ${job.scene}?\n\n`
      + `This stops ${labels.length} account(s) rendering it: `
      + `${labels.join(', ')}.\n`
      + 'Restarting later spends that quota again.');
    if (ok) backend.cancelJob(jobId);
  }
});
document.getElementById('instances').addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const cell = e.target.closest('[data-frame]');
  if (cell && backend) {
    e.preventDefault();
    backend.previewFrame(Number(cell.dataset.frame), jobIdOfCell(cell));
  }
});

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
document.getElementById('sel-blender').addEventListener('change', e => {
  // Remembered the same way every other preference is -- picking it once
  // and having it reset next launch would be worse than not offering the
  // choice at all.
  backend && backend.setPreference('blenderVersion', JSON.stringify(e.target.value));
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

  backend.blenderVersions(populateBlenderVersions);

  /* Asked for once at startup: the path is fixed for the life of the run,
     and a user only goes looking for it after something has gone wrong. */
  backend.diagnostics(json => {
    const d = JSON.parse(json);
    const el = document.getElementById('log-path');
    if (el) el.textContent = d.logFile || d.logDir || 'not available';
  });

  /* Upload and download report as bytes, not as a spinner: a 400 MB
     .blend on a slow line is the one moment the app looks frozen, and a
     percentage is the difference between waiting and worrying. */
  backend.uploadProgress.connect(json => showUploadStage(JSON.parse(json)));
  backend.downloadProgress.connect(json => {
    const p = JSON.parse(json);
    downloads[p.label] = p;
    /* Patched in place, not re-rendered: these arrive every megabyte, and
       rebuilding every card that often would fight the user for the
       scroll position. The card's own markup is rebuilt on the next state
       tick anyway, and reads the same `downloads` entry. */
    /* Matched on the dataset value rather than built into a selector: a
       label is free text, and a quote or bracket in one would make
       querySelector throw rather than simply not match. */
    const row = Array.from(document.querySelectorAll('[data-dl]'))
      .find(el => el.dataset.dl === p.label);
    if (row) {
      const bar = row.querySelector('[data-dl-bar]');
      const text = row.querySelector('[data-dl-text]');
      if (bar) {
        bar.style.width = (p.total
          ? Math.min(100, Math.round(100 * p.downloaded / p.total)) : 0) + '%';
      }
      if (text) text.textContent = fmtDownload(p);
    } else {
      repaintCards();               // first tick: the row does not exist yet
    }
  });

  backend.busyChanged.connect((key, busy) => {
    const map = { launch: 'btn-render', cancel: 'btn-cancel',
                  'collect:': 'btn-collect', dataset: 'btn-upload' };
    const id = map[key];
    if (id) document.getElementById(id).disabled = busy;
    if (key.indexOf('verify:') === 0) document.getElementById('btn-add').disabled = busy;
    /* A finished collect must not leave a bar frozen at whatever it
       reached -- including a failed one, which would otherwise sit at 68%
       for ever, looking like it was still going. */
    if (key.indexOf('collect:') === 0 && !busy) {
      Object.keys(downloads).forEach(k => delete downloads[k]);
      repaintCards();
    }
    /* The dataset step has stopped. Anything still waiting was never
       confirmed -- prepare_dataset raises rather than continuing past an
       account that cannot see the file -- so it is not still pending, it
       failed. Leaving it on a clock would say "any moment now" for ever. */
    if (key === 'dataset' && !busy) {
      Object.keys(share.rows).forEach(n => {
        if (share.rows[n] === 'pending') share.rows[n] = 'bad';
      });
      renderShareList();
    }
    /* Per-scene buttons, matched on the slug carried in the button's own
       dataset value -- same idiom as the download-row match above, since
       a slug is free text ("owner/name") and could contain a character a
       hand-built CSS selector would choke on. */
    if (key.indexOf('launch-scene:') === 0) {
      const slug = key.slice('launch-scene:'.length);
      const btn = Array.from(document.querySelectorAll('[data-scene-render]'))
        .find(el => el.dataset.sceneRender === slug);
      if (btn) btn.disabled = busy;
    }
    if (key.indexOf('delete-scene:') === 0) {
      const slug = key.slice('delete-scene:'.length);
      const btn = Array.from(document.querySelectorAll('[data-scene-delete]'))
        .find(el => el.dataset.sceneDelete === slug);
      if (btn) btn.disabled = busy;
    }
  });

  backend.framePreview.connect(json => {
    const p = JSON.parse(json);
    openPreview(p.frame, p.path, p.label);
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

  backend.scenesChanged.connect(renderScenes);
  backend.scenes();

  backend.refreshQuota();
  backend.poll();

  /* LAST, and it has to stay last. This is how the page says "every
     handler above is wired up now", and it is what makes the backend
     re-attach its log streams to renders that were still running on
     Kaggle when the app was last closed. Those streams announce
     themselves through notification/stateChanged -- both connected
     above -- so calling this any earlier (the `backend.state(...)` call
     on the line above the stateChanged connect, say) would emit into
     handlers that do not exist yet and the user would be told nothing. */
  backend.ready();
});
