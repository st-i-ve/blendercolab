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
              sound: true, frameThumbnails: true, font: 'heebo',
              closeAction: 'ask' };
const history = { inst: [], run: [], gpus: [], frames: [] };

/* kaggle_client.TERMINAL_STATES, mirrored here because the page has to
   answer the same question the backend does: is this worker's kernel done
   with, or could it still be doing something? "complete"/"error"/
   "cancel_acknowledged" are the states after which nothing further happens
   without a brand new push -- see the constant's own comment in
   kaggle_client.py for why "not in ACTIVE_STATES" is NOT the same test (a
   kernel Kaggle has accepted but not started yet answers "not_started",
   and one whose session has not run its first cell answers "new_script";
   neither is active and neither is finished).

   Everything on the dashboard that claims something is HAPPENING is
   computed through this: a tile, a heading or a Cancel button that counts
   a finished job is the same untruth as a card that says a completed
   render is still waiting. */
const TERMINAL_STATES = ['complete', 'error', 'cancel_acknowledged'];
function isTerminal(worker) {
  return !!worker && TERMINAL_STATES.includes(worker.state);
}
/* An account whose kernel could still be doing something on Kaggle: it has
   a worker and that worker has not reached a terminal state. Idle accounts
   (worker: null) are not "live" -- there is no session at all. */
function isLive(inst) {
  return !!(inst && inst.worker && !isTerminal(inst.worker));
}

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
  /* The reference's own "tiny toggle click", value for value (its
     sfx.switch): two very short tones, the second lower, with an 8ms
     attack so it reads as a click rather than a note. */
  toggle:   () => { tone(740, 0.08, 0.016, 0, 0.008);
                    tone(555, 0.12, 0.014, 0.05, 0.008); },
  error:    () => { tone(300, 0.26, 0.03); tone(220, 0.3, 0.025, 0.12); },
  reconnect:() => { tone(520, 0.16); tone(780, 0.2, 0.018, 0.08); },
  notify:   () => { tone(720, 0.14, 0.016); },
};

/* ---------------- chrome ------------------------------------------------ */
function applyPrefs(p) {
  prefs = Object.assign(prefs, p);
  document.documentElement.dataset.theme = prefs.theme;
  document.documentElement.dataset.accent = prefs.accent;
  document.documentElement.dataset.font = prefs.font || 'heebo';
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

/* ONE way in and out of a theme change, so the sound, the transition
   and the saved preference cannot get out of step with each other.
   `theming` puts every element on the same transition for the length of
   the switch (see app.css) and is taken straight off again -- without it
   the page ground faded over .3s under panels that had already snapped,
   which is what "some parts switch faster than others" was. */
function setTheme(next) {
  if (!next || next === prefs.theme) return;
  const root = document.documentElement;
  /* Suppress every transition, change the theme, force the style to be
     recalculated while they are still off, and only then release them.
     Without the forced reflow the browser is free to batch the class
     removal with the theme change and animate anyway. */
  root.classList.add('theme-snap');
  sfx.toggle();
  applyPrefs({ theme: next });
  void root.offsetWidth;
  requestAnimationFrame(() => root.classList.remove('theme-snap'));
  /* Persisted through Settings, not localStorage: the Qt host reads the
     same file to decide the window's own chrome (Mica, dark title bar). */
  if (backend) backend.setPreference('theme', JSON.stringify(next));
  syncSettingsControls();
}

const themeBtn = document.getElementById('btn-theme');
themeBtn.addEventListener('click', () => {
  themeBtn.classList.add('spin');
  setTheme(prefs.theme === 'dark' ? 'light' : 'dark');
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

/* ---------------- tooltips ----------------------------------------------
 *
 * Two problems this solves, both visible on screen before it existed:
 *
 *  1. THE BLACK BOX. A `title=` attribute is drawn by the operating
 *     system. It ignores the theme, arrives as a black slab in the
 *     middle of a warm light UI, and lands ON TOP of ours because
 *     nothing in CSS can out-rank a native widget. So a title is now
 *     MOVED onto the element (data-tip) the first time the pointer
 *     reaches it and the attribute removed -- the words survive, the
 *     black box is never drawn again.
 *
 *  2. THE CLIPPED ONE. The tooltip used to be a ::after on the control
 *     itself, so it lived inside whatever the control lived inside --
 *     and .panel is overflow:hidden, which cut the tooltip of every
 *     button near a panel edge in half. There is exactly one tooltip
 *     element now, it is a child of <body>, and it is positioned in
 *     viewport coordinates: nothing can clip it and nothing can cover
 *     it (z-index 200 -- see STACKING ORDER in app.css).
 */
const tipEl = document.createElement('div');
tipEl.className = 'tip';
tipEl.setAttribute('role', 'tooltip');
tipEl.setAttribute('aria-hidden', 'true');
document.body.appendChild(tipEl);
let tipFor = null;

/* The words, wherever they were written -- and the native tooltip
   defused on the way past. */
function tipTextFor(el) {
  const native = el.getAttribute('title');
  if (native !== null) {
    el.removeAttribute('title');
    const text = native.replace(/\s+/g, ' ').trim();
    if (text && !el.dataset.tip) el.dataset.tip = text;
  }
  return el.dataset.tip || '';
}

/* Every title in a freshly built subtree, defused before the pointer
   ever reaches it. Hovering is too late on its own: the cards are
   rebuilt under a resting pointer on every live tick, and a rebuilt
   element arrives carrying a fresh title= with no pointerover to
   follow -- which is exactly when the black box used to appear. */
function defuseTitles(root) {
  if (!root || root.nodeType !== 1) return;
  if (root.hasAttribute('title')) tipTextFor(root);
  root.querySelectorAll('[title]').forEach(tipTextFor);
}
defuseTitles(document.body);
new MutationObserver(records => {
  records.forEach(r => r.addedNodes.forEach(defuseTitles));
}).observe(document.body, { childList: true, subtree: true });

function showTip(el) {
  const text = tipTextFor(el);
  if (!text) return;
  tipFor = el;
  tipEl.textContent = text;
  tipEl.classList.add('show');
  const r = el.getBoundingClientRect();
  const t = tipEl.getBoundingClientRect();
  const pad = 8;
  /* Beside a collapsed sidebar item, below anything else -- and above
     instead when there is no room below, which is where a button in the
     last row of a long list always is. */
  const aside = el.closest('.side.collapsed');
  let left, top;
  if (aside) {
    left = r.right + 10;
    top = r.top + (r.height - t.height) / 2;
  } else {
    left = r.left + (r.width - t.width) / 2;
    top = r.bottom + 8;
    if (top + t.height > window.innerHeight - pad) top = r.top - t.height - 8;
  }
  tipEl.style.left = Math.max(pad, Math.min(left, window.innerWidth - t.width - pad)) + 'px';
  tipEl.style.top = Math.max(pad, top) + 'px';
}

function hideTip() {
  tipFor = null;
  tipEl.classList.remove('show');
}

document.addEventListener('pointerover', e => {
  const el = e.target.closest && e.target.closest('[data-tip],[title]');
  if (!el) { if (tipFor) hideTip(); return; }
  if (el !== tipFor) showTip(el);
});
document.addEventListener('pointerout', e => {
  if (tipFor && (!e.relatedTarget || !tipFor.contains(e.relatedTarget))) hideTip();
});
/* Keyboard reaches the same tips: they are the only name an icon-only
   button has on screen. :focus-visible, not :focus -- the app moves
   focus itself when a dialog opens, and a tip that popped up next to a
   button nobody pointed at is noise. */
document.addEventListener('focusin', e => {
  const el = e.target.closest && e.target.closest('[data-tip],[title]');
  if (el && el.matches(':focus-visible')) showTip(el);
});
document.addEventListener('focusout', hideTip);
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideTip(); });
/* Anything that moves the control out from under the tooltip. */
window.addEventListener('scroll', hideTip, true);
window.addEventListener('resize', hideTip);
document.addEventListener('click', hideTip, true);

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
      /* The sections were just discarded and rebuilt, taking every
         loaded thumbnail's <img src> with them. The pictures themselves
         are held outside the payload, so they go straight back rather
         than being fetched again. */
      hydrateFrameStrips();
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
  /* Scoped to workers that have NOT reached a terminal state, exactly like
     "Rendering now" and "GPUs active" beside it. It used to sum every
     tracked worker's framesDone, so a fleet whose renders had all finished
     hours ago showed "FRAMES DONE 4" -- part live figure, part leftover
     from whatever the log stream last saved before the window closed, and
     true of nothing at all. This tile answers "what is happening right
     now", so with nothing rendering the honest answer is 0; the finished
     scenes' own totals live on their frame grids, where they carry the
     scene they belong to. */
  const frames = state.instances.reduce(
    (n, i) => n + (!isLive(i) ? 0
                   : (i.live && i.live.framesTotal
                      ? i.live.framesDone
                      : (i.worker.framesDone || 0))), 0);

  setStat('inst', online);
  setStat('run', running);
  setStat('gpus', gpus);
  setStat('frames', frames);

  const jobs = state.jobs || [];
  /* Jobs with at least one worker still capable of doing something. The
     heading counted TRACKED jobs, which is why a dashboard on which every
     render had finished still read "6 scenes rendering" -- and a job stays
     tracked until the user forgets it, so that number only ever grew.
     Counted from the instances rather than from job.finished because a job
     whose account was removed has no worker left to finish it, and the
     cards are what the heading sits above. */
  const liveJobIds = new Set(state.instances.filter(isLive)
                             .map(i => i.jobId).filter(Boolean));
  const jobMeta = document.getElementById('job-meta');
  if (jobMeta) {
    jobMeta.textContent = liveJobIds.size
      ? `${liveJobIds.size} scene${liveJobIds.size === 1 ? '' : 's'} rendering`
      : jobs.length
      ? `${jobs.length} scene${jobs.length === 1 ? '' : 's'} tracked · none`
        + ` rendering — use “Collect frames…” on a scene to download what`
        + ` it made`
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
  /* Cancel is offered while ANY of this scene's workers is short of a
     terminal state, not merely while one is ACTIVE. Those are different
     tests and the difference is the whole point: a kernel Kaggle has
     accepted but not started yet answers "not_started", and one whose
     session has not run its first cell answers "new_script" -- neither is
     active, both are about to start spending somebody's quota, and that is
     exactly when cancelling matters most (see kaggle_client's
     ACTIVE_STATES / PENDING_STATES / TERMINAL_STATES comments).

     Once every worker IS terminal there is no session left to stop, and
     offering to "cancel" invites the user to try to stop work that is
     already done -- so the button is disabled and loses its
     data-job-cancel, which is what routes the click, so a stray click can
     never reach Fleet.cancel_job() for this scene at all. Disabled rather
     than removed: a control that vanishes reads as a bug, and the title is
     where the reason lives. */
  const cancellable = instances.some(isLive);
  const cancelBtn = cancellable
    ? `<button class="btn ico sm danger" data-job-cancel="${esc(job.jobId)}"
         aria-label="Cancel" data-tip="Cancel this render"
         title="Cancel every account rendering this scene">${ACT.stop}</button>`
    : `<button class="btn ico sm danger" disabled aria-label="Cancel"
         title="Nothing left to cancel: every account on this scene has
                already stopped on Kaggle, so no session is still spending
                quota. Their frames are waiting there — use “Collect
                frames…” to download them.">${ACT.stop}</button>`;
  return `<section class="job-group">
    <div class="job-head">
      <h3 class="job-title">${esc(job.scene)}</h3>
      <span class="job-sub">${esc(job.blend)} · frames ${job.startFrame}-${job.endFrame}</span>
      ${elapsed}
      <div class="job-actions">
        <button class="btn ico sm" data-job-collect="${esc(job.jobId)}"
          aria-label="Collect frames" data-tip="Collect frames…"
          title="Collect frames — download this scene's rendered frames">${
            ACT.download}</button>
        ${cancelBtn}
      </div>
    </div>
    <!-- The whole scene's download, alongside the per-account bytes each
         card already shows. Collect now writes ONE zip per render, so
         "how far along is my zip" is a question the per-card figures
         cannot answer between them -- see rollupDownload for why it is
         bytes, and why it declines to show a percentage until every
         account has reported a real size. Empty when nothing is
         downloading, so an idle section is unchanged. -->
    <div class="job-dl" data-jdl="${esc(job.jobId)}">${
      jobDownloadInnerHtml(job.jobId)}</div>
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

/* The mark that stands in front of a hardware reading.
 *
 * Deliberately OURS and not the manufacturer's: NVIDIA's logo is a
 * trademark, it is not in this repo, and vendoring it off the web into a
 * shipped binary is not a thing to do quietly. This is a die-on-a-board
 * silhouette drawn in currentColor, so it inherits the accent, follows
 * the theme, and stays legible on both grounds -- which the wordmark, an
 * image with its own fixed colours, would not.
 */
const GPU_MARK = '<svg class="gpu-mark" width="13" height="13"'
  + ' viewBox="0 0 16 16" fill="none" aria-hidden="true">'
  + '<rect x="1.4" y="3.6" width="13.2" height="9" rx="1.8"'
  + ' stroke="currentColor" stroke-width="1.3"/>'
  + '<rect x="4.6" y="6.4" width="6.8" height="4.2" rx="1"'
  + ' stroke="currentColor" stroke-width="1.2"/>'
  + '<path d="M4.6 3.6V2.2M11.4 3.6V2.2M4.6 13.8v-1.2M11.4 13.8v-1.2"'
  + ' stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg>';

/* Icons for the ACTIONS a row offers, as against the states above.
   Same 16x16 stroked idiom, so a button drawn from one of these sits
   at the same weight as the status glyph beside it. Every use pairs
   the glyph with an aria-label and a tooltip carrying the same words
   the button used to print. */
const ACT = {
  play: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<path d="M5.2 3.4l7 4.6-7 4.6V3.4z" stroke="currentColor"'
    + ' stroke-width="1.5" stroke-linejoin="round"/></svg>',
  trash: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<path d="M3.2 4.6h9.6M6.4 4.6V3.2h3.2v1.4M4.6 4.6l.5 8.2h5.8l.5-8.2"'
    + ' stroke="currentColor" stroke-width="1.4" stroke-linecap="round"'
    + ' stroke-linejoin="round"/></svg>',
  download: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<path d="M8 2.6v7M8 9.6L5.2 6.8M8 9.6l2.8-2.8M2.8 11.4v1.2c0 .6.5 1 1 1h8.4c.6 0 1-.4 1-1v-1.2"'
    + ' stroke="currentColor" stroke-width="1.4" stroke-linecap="round"'
    + ' stroke-linejoin="round"/></svg>',
  chip: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<rect x="4.6" y="4.6" width="6.8" height="6.8" rx="1.4" stroke="currentColor"'
    + ' stroke-width="1.4"/><path d="M6.6 2.2v2.4M9.4 2.2v2.4M6.6 11.4v2.4M9.4 11.4v2.4'
    + 'M2.2 6.6h2.4M2.2 9.4h2.4M11.4 6.6h2.4M11.4 9.4h2.4" stroke="currentColor"'
    + ' stroke-width="1.3" stroke-linecap="round"/></svg>',
  stop: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<rect x="4.4" y="4.4" width="7.2" height="7.2" rx="1.5" stroke="currentColor"'
    + ' stroke-width="1.5"/></svg>',
  refresh: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<path d="M13.4 8a5.4 5.4 0 11-1.6-3.8M13.4 2.6v2.6h-2.6" stroke="currentColor"'
    + ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  user: '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
    + '<circle cx="8" cy="5.6" r="2.6" stroke="currentColor" stroke-width="1.4"/>'
    + '<path d="M3 13.4c.6-2.4 2.6-3.6 5-3.6s4.4 1.2 5 3.6" stroke="currentColor"'
    + ' stroke-width="1.4" stroke-linecap="round"/></svg>',
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
    ? `${GPU_MARK}${inst.hardware.gpus.map(g => esc(g.model || 'GPU')).join(', ')}`
      + (inst.hardware.cpuCount ? ` · ${inst.hardware.cpuCount} vCPU` : '')
      + (inst.hardware.ramTotal ? ` · ${inst.hardware.ramTotal.toFixed(1)} GB` : '')
      + ` <span class="age">${fmtAge(inst.hardware.ageSeconds)}</span>`
    : 'never run — launch to see specs';

  /* Live beats polled. The 30s status poll can only say queued/running --
     it cannot say "installing Blender" or "frame 7 of 15", because Kaggle
     returns no logs until a kernel COMPLETES. The SSE stream can, so when
     it has reported, its numbers are the ones shown. */
  const live = inst.live;
  const liveFrames = !!(live && live.framesTotal);
  /* WHAT KIND of number framesDone is, straight from the payload
     (bridge._frames_done_source). "final" was read from this worker's own
     kernel log once it stopped and is the render's last word; "unknown" is
     a stopped render whose log could NOT be read, where framesDone is only
     a floor from some moment before the end. A live stream outranks all of
     them while one is connected. */
  const source = liveFrames ? 'live'
               : (worker ? (worker.framesDoneSource || 'saved') : 'none');
  const unknown = source === 'unknown';
  const done = liveFrames ? live.framesDone
             : (worker ? worker.framesDone : 0);
  const total = liveFrames ? live.framesTotal
              : (worker ? worker.frames.length : 0);
  const doneText = unknown ? '—' : done;

  /* Where that number came from, said out loud whenever it is NOT live.
     After a restart the count is whatever the last stream managed to save
     before the window closed -- the render kept going on Kaggle in the
     meantime, so it is a floor, not a reading. Presenting it bare, next
     to a bar, would claim a measurement nobody took. It carries its age,
     the same way the cached hardware line does. */
  const savedFrames = unknown
    ? `<span class="frange dim" title="This render has stopped, but its own${
         ''} log on Kaggle could not be read, so how many frames it${
         ''} finished is not known. The last number a live connection${
         ''} saved is from before it ended and would be wrong to show as a${
         ''} count. “Collect frames…” is the authoritative list of what${
         ''} exists.">count not known</span>`
    /* A count read from the finished kernel's own log needs no label:
       it is the render's last word, and the state icon in the head
       already says the render ended. Only a number that is NOT a
       current reading -- a stale saved one, or none at all -- has to
       say so, which is what the other two branches are for. */
    : source === 'final'
    ? ''
    : (!liveFrames && worker && worker.framesDoneAge != null)
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
    ? `<div class="m wide live"><span class="k">This session</span>
        <span class="v">${gpuNames || cpuCount ? GPU_MARK : ''}${
          gpuNames ? esc(gpuNames.join(', ') || 'CPU only') : 'running'}${
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

  /* Elapsed time, and after it stops, the total -- one field serving
     both, so the stopwatch and the benchmark can never disagree.
     It rides in the HEAD, beside the state icon: the two together are
     the whole of "how did this machine do", and it is two units at most
     (fmtDurationShort) because that is what fits beside a 14px glyph.
     The exact figure, to the second, is in its tooltip. */
  const dur = worker && worker.elapsed != null
    ? `<span class="dur" title="${
        worker.finished ? 'This render took ' : 'Running for '}${
        fmtDuration(worker.elapsed)}">${fmtDurationShort(worker.elapsed)}</span>`
    : '';
  /* A session that has ENDED says nothing here at all: the state icon
     and the duration beside it, in the head, are what report how it
     went, and the frame count is on its own line above. The card used
     to close with four lines of prose (state, elapsed, and a sentence
     about collecting) which repeated all three.
     `ended` is still checked FIRST, and that is not cosmetic: a
     finished worker whose replayed log left "rendering · 15/15 frames"
     behind was the "everything is stuck" report -- a card that had in
     fact finished hours ago. A phase belongs to a session that is still
     going, so a finished one is never allowed to print one. */
  const ended = !!(worker && worker.finished);
  /* The window after a restart, before the replayed log has rebuilt
     anything. The render never stopped -- only this app's view of it did
     -- and a card that simply sat blank there was read as a stuck render
     and is the whole reason this text exists. It says what is happening,
     why there is nothing to show yet, and that there is nothing to do. */
  const phase = ended
    ? ''
    : live && live.phase
    ? `<div class="inst-foot"><b>${esc(live.phase)}</b></div>`
    : inst.reconnecting
      ? `<div class="inst-foot"><b>reconnecting — replaying this session's`
        + ` log from Kaggle to catch up</b><span class="sub">The`
        + ` render never stopped; nothing to restart.</span></div>`
    : (worker && worker.state === 'queued'
       ? '<div class="inst-foot">queued — waiting for Kaggle to allocate a machine</div>'
       : '');

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
        ${dur}
        <span class="badge ico ${badge[0]}" title="${esc(badge[1])}"
              role="img" aria-label="${esc(badge[1])}">${badge[2]}</span>
        ${/* An icon, in the head, beside the state it is about to
              change -- it used to be a full-width "CHECK HARDWARE"
              button wedged next to the hardware line, which made the
              cached reading look like a control rather than a fact. */
          (!inst.revoked && inst.username && !worker)
          ? `<button class="btn ico sm" data-hwcheck="${esc(inst.label)}"
               aria-label="Check hardware" data-tip="Check hardware"
               title="Check hardware — Kaggle decides what a session gets, and it varies run to run. This starts a one-minute check so you know before committing a render.">${ACT.chip}</button>`
          : ''}
      </span>
    </div>
    <div class="inst-body">
      <div class="meta">
        <div class="m"><span class="k">Quota (API)</span>
          <span class="v">${esc(inst.quota || '—')}</span></div>
        <div class="m"><span class="k">Last known hardware</span>
          <span class="v">${hw}</span></div>
        ${liveHw}
      </div>
      ${worker ? `
      <div class="assign">
        <div class="wrapc">
          <div class="l1"><span class="flab">Frames</span>
            <span class="frange">${doneText} / ${total}</span>${savedFrames}</div>
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
/* The same duration, at a glance, in TWO units at most.
 *
 * fmtDuration above is the long form ("1h 5m 20s"), which is right in a
 * sentence and wrong beside a status icon: three units read as a serial
 * number at 10px. Renders here run for hours, so the seconds in "1h 37m
 * 44s" are noise -- what is being judged is roughly how long a machine
 * took. Under an hour the seconds ARE the interesting half, so the pair
 * shifts down rather than dropping a unit. Under a minute there is only
 * one honest unit and it says so, rather than padding to "0m 9s".
 */
function fmtDurationShort(seconds) {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return '';
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
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
  /* The upload is done and the byte counter is frozen at 100% while
     Kaggle finishes indexing a dataset it has only just received -- 1s
     was far too soon for a 499 MB scene (2026-08-15). Named so the wait
     reads as progress rather than a hang. */
  'waiting-for-kaggle': d => ['waiting for Kaggle to index the upload',
                              `${d} — the file is already there`],
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

  /* Something is uploading now, so the bar has something to measure. */
  document.getElementById('up-item').classList.remove('idle');
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
    <div class="row-actions">
      <button class="btn ico sm" data-scene-render="${esc(scene.slug)}"
        aria-label="Render this" data-tip="Render this"
        title="Render this — renders this scene straight from Kaggle, with no re-upload. Sharing is re-checked for every account before anything starts.">${ACT.play}</button>
      <button class="btn ico sm danger" data-scene-delete="${esc(scene.slug)}"
        data-scene-name="${esc(scene.name)}" data-scene-size="${scene.sizeBytes}"
        aria-label="Delete scene" data-tip="Delete…"
        title="Delete — permanently deletes this dataset from Kaggle.">${ACT.trash}</button>
    </div>
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

/* ---------------- renders / packed outputs (Files page) -----------------
   Every render THIS app has run, newest first, each with a Download that
   packs its frames into one zip. Distinct from the scene library above:
   a scene is a .blend waiting to be rendered, an output is what came back
   from rendering one, and folding them into one list would make "delete"
   and "download" sit on rows that mean opposite things.

   The list itself is instant -- bridge.outputs() reads the tracked-job
   file and nothing else. Whether Kaggle STILL has each render's frames is
   a slower question answered afterwards by checkOutputs(), which is why
   `availability` has four values and why "unchecked" is one of them. */

/* Held between renders so a row that is mid-download, or that has just
   finished one, does not lose that the moment an availability check
   re-emits the whole list. Keyed by jobId. */
const collectResults = {};

/* The last outputs payload, so a download tick or a finished collect can
   redraw the rows without waiting for another bridge call -- the same
   reason `lastStateJson` exists for the dashboard. */
let lastOutputsJson = null;

/* Every download tick seen for one render. `downloads` is keyed by
   ACCOUNT because that is what the instance cards show; a job-level
   figure is those same entries added up. */
function jobDownloadEntries(jobId) {
  return Object.keys(downloads).map(k => downloads[k])
    .filter(d => d && d.jobId === jobId);
}

/* Per-account bytes rolled up into ONE figure for the whole render.
 *
 * Bytes, not "3 of 5 accounts done": the accounts are wildly unequal --
 * one may hold 200 frames and another 2 -- so counting finished accounts
 * would jump from 20% to 80% while most of the data was still arriving.
 * Bytes are the thing actually being waited for, and summing them is
 * exactly what the eventual zip is made of.
 *
 * The total is the honesty problem, and it is why `totalKnown` exists.
 * Kaggle does not always send a Content-Length, so a worker's `total` can
 * be 0 meaning NOT KNOWN (never "zero bytes"); and collect() fetches the
 * accounts one after another, so an account that has not started yet has
 * reported no size at all. In either case the sum of the totals is a
 * FLOOR, not a total -- and dividing by a floor gives a percentage that
 * climbs to 100% while bytes are still coming in, which is the one
 * reading this app must never show. So a percentage is offered only when
 * every account in the render has reported and every one of those
 * reports carried a real size; otherwise `percent` is null and the caller
 * shows the bytes so far and says the total is not yet known.
 */
function rollupDownload(entries, workerCount) {
  const downloaded = entries.reduce((n, e) => n + (e.downloaded || 0), 0);
  const rate = entries.reduce((n, e) => n + (e.rate || 0), 0);
  const everyoneReported = workerCount > 0 && entries.length >= workerCount;
  const everySizeKnown = entries.length > 0 && entries.every(e => e.total > 0);
  const totalKnown = everyoneReported && everySizeKnown;
  const total = totalKnown ? entries.reduce((n, e) => n + e.total, 0) : 0;
  return {
    downloaded, rate, total, totalKnown,
    reported: entries.length,
    workerCount,
    /* null, never 0 and never 100: "no percentage can honestly be given
       yet" is a different statement from "0% done". */
    percent: totalKnown && total
      ? Math.min(100, Math.round(100 * downloaded / total)) : null,
  };
}

/* The inside of a job-level download indicator, shared by the Files row
   and the dashboard's per-scene section so the two can never disagree.
   Returns '' when this render has no download in flight. */
function jobDownloadInnerHtml(jobId) {
  const entries = jobDownloadEntries(jobId);
  if (!entries.length) return '';
  const roll = rollupDownload(entries, entries[0].jobWorkers || 0);
  if (roll.percent === null) {
    /* No bar at all. A bar drawn at 0% -- which is what an unknown total
       forces -- reads as "not started" on a download that is plainly
       moving, and a bar drawn against the sum-so-far reads as nearly
       finished throughout. The byte counter is the honest instrument
       here, and it is visibly ticking. */
    return `<span class="tag on">downloading</span>
      <span class="pct" data-jdl-text>${fmtDownload(
        { downloaded: roll.downloaded, total: 0, rate: roll.rate })}</span>
      <div class="out-note">Total size not known yet — ${roll.reported} of ${
        roll.workerCount || '?'} account(s) have reported one, so there is no
        honest percentage to show. The figure above is what has actually
        arrived.</div>`;
  }
  return `<span class="tag on">downloading</span>
    <div class="track rendering"><i data-jdl-bar style="width:${roll.percent}%"></i></div>
    <span class="pct" data-jdl-text>${fmtDownload(
      { downloaded: roll.downloaded, total: roll.total, rate: roll.rate })}</span>`;
}

/* Availability, in the row's own words. Four states, and the difference
   between them is the whole point of this section:

   available -- Kaggle still lists render files for at least one of this
                render's accounts, so Download has something to fetch.
   gone      -- every account was reached and none of them still has any.
                The render DID happen; Kaggle expires kernel output after
                a while and has deleted it. Nothing was lost by this app
                and there is nothing left for it to download.
   unknown   -- this app could not ask (no token, offline, rate limited).
                Not the same as gone, and never shown as it.
   unchecked -- nothing has asked yet. Claims neither way. */
function outputAvailabilityHtml(o) {
  if (o.availability === 'available') {
    const partial = o.availableAccounts < o.workerCount
      ? ` — ${o.availableAccounts} of ${o.workerCount} account(s)`
      : '';
    return `<span class="badge active"><i></i>on Kaggle${esc(partial)}</span>
      <span class="lock">Download packs what is there into one zip.</span>`;
  }
  if (o.availability === 'gone') {
    return `<span class="badge warn"><i></i>output deleted by Kaggle</span>
      <span class="lock">This render did happen — Kaggle removes a kernel's
      output after a while, and all ${o.workerCount} account(s) now list
      none. Nothing is left to download; re-render the scene if you need
      the frames again.</span>`;
  }
  if (o.availability === 'unknown') {
    const why = Object.keys(o.availabilityErrors || {})
      .map(k => `${k}: ${o.availabilityErrors[k]}`).join(' · ');
    return `<span class="badge idle"><i></i>could not check</span>
      <span class="lock">BlendFleet could not ask Kaggle whether these
      frames are still there${why ? ' — ' + esc(why) : ''}. They may well
      be. Press Re-check above once that is fixed.</span>`;
  }
  return `<span class="badge idle"><i></i>not checked yet</span>
    <span class="lock">Whether Kaggle still has these frames has not been
    asked yet — checking runs in the background. Download works if they
    are there.</span>`;
}

/* Pure, like sceneRowHtml/instanceCard: reads only the one output handed
   to it (plus the shared download/result maps), so it can be driven
   directly from a test with a hand-built payload. */
function outputRowHtml(o) {
  const when = o.ageSeconds == null
    ? 'when it ran is not recorded'
    : `ran ${fmtAge(o.ageSeconds)}`;
  /* The frame RANGE is a fact about the job. How many frames actually
     came out is only known once every account's own kernel log has been
     read back for its final count -- short of that, framesDone is
     whatever a live stream last saved, which is a floor. */
  const done = o.framesDoneKnown
    ? ` · ${o.framesDone} frame(s) reported rendered`
    : ' · frames rendered not confirmed for every account';
  const state = o.finished
    ? 'every account finished'
    : 'not every account has finished';
  const accounts = o.accounts.length
    ? o.accounts.map(esc).join(', ')
    : 'no accounts recorded';
  const dl = jobDownloadInnerHtml(o.jobId);
  const result = collectResults[o.jobId];
  /* Where the zip went, kept on the row rather than only in a toast that
     fades. `archivePath` is empty when collect() wrote no zip at all --
     which is a real outcome (nothing had rendered) and must not be
     dressed up as a saved file. */
  const resultHtml = !result ? ''
    : result.archivePath
      ? `<div class="out-note ok">Saved to ${esc(result.archivePath)} — ${
          result.copied} frame(s). Unzip it to get the frames.${
          result.wantedName
            ? ` A ${esc(result.wantedName)} was already in that folder, so this
               download was saved beside it rather than replacing it.` : ''}${
          result.missing
            ? ` ${result.missing} frame(s) are still missing — not rendered,
               or that account failed.` : ''}</div>`
      : `<div class="out-note bad">No zip was written to ${
          esc(result.destination)} — nothing came back to put in one. ${
          esc(result.message)}</div>`;
  return `<div class="fs-row out-row" data-out="${esc(o.jobId)}">
    <div class="fi frames">ZIP</div>
    <div class="grow">
      <div class="fn2">${esc(o.scene)}</div>
      <div class="fs2">${esc(o.blend)} · frames ${o.startFrame}-${o.endFrame}
        (${o.frameCount})${esc(done)} · ${esc(when)} · ${esc(state)}
        · ${accounts}</div>
      <div class="out-state">${outputAvailabilityHtml(o)}</div>
      <div class="out-prog" data-jdl="${esc(o.jobId)}">${dl}</div>
      ${resultHtml}
    </div>
    <div class="row-actions">
      <button class="btn ico sm" data-out-download="${esc(o.jobId)}"
        aria-label="Download" data-tip="Download"
        title="Download — packs every frame this render produced into one zip
               in a folder you choose.">${ACT.download}</button>
    </div>
  </div>`;
}

function renderOutputs(json) {
  const payload = JSON.parse(json);
  const outputs = payload.outputs || [];
  lastOutputsJson = json;
  const list = document.getElementById('output-list');
  list.innerHTML = outputs.length
    ? outputs.map(outputRowHtml).join('')
    : '<div class="empty">No renders yet — BlendFleet lists the renders it '
      + 'has run itself, so this fills up once you render a scene. Renders '
      + 'started outside BlendFleet are not tracked here.</div>';
}

function repaintOutputs() {
  if (lastOutputsJson) renderOutputs(lastOutputsJson);
}

document.getElementById('output-list').addEventListener('click', e => {
  const btn = e.target.closest('[data-out-download]');
  /* Routed by the jobId carried on the button itself, never by row
     position: the list is re-rendered whenever an availability check
     lands, and a click resolved against "the nth row" would collect a
     different render than the one pressed. */
  if (btn && backend) backend.collect('', btn.dataset.outDownload);
});

document.getElementById('btn-recheck-outputs').onclick = () =>
  backend && backend.checkOutputs();

/* LIVE PREVIEWS: what each machine is producing RIGHT NOW.
 *
 * Kaggle releases a session's output only once that session ends, so
 * until then there is no rendered frame to fetch -- the notebook pushes a
 * small JPEG of every finished frame down the same log stream that
 * carries progress, and this draws the newest one per account.
 *
 * IT IS NOT THE RENDERED FRAME, and the markup says so three times over:
 * in the heading tag, in the note under the tiles, and in the lightbox it
 * opens. A 320-pixel JPEG next to a 1920-pixel render is a glance, not a
 * judgement -- the full-resolution image is still what Collect brings
 * back, and still what clicking a finished cell in the grid below
 * fetches once the session has ended.
 *
 * An account with no preview yet contributes NOTHING here: no tile, no
 * placeholder. An empty frame-shaped box would imply a frame had
 * rendered, which is the one thing this must never say. */
function livePreviewStrip(instances) {
  const tiles = instances.filter(
    i => i.live && i.live.thumb && i.live.thumb.dataUrl).map(i => {
    const t = i.live.thumb;
    return `<figure class="lp-tile" data-live-frame="${t.frame}"
        data-live-label="${esc(i.label)}" role="button" tabindex="0"
        title="Frame ${t.frame}, as ${esc(i.label)} finished it moments ago — a small preview sent over the log, not the full-resolution render. Click to see it larger.">
      <img src="${esc(t.dataUrl)}" alt="Small live preview of frame ${t.frame}, rendered by ${esc(i.label)}">
      <figcaption><b>${esc(i.label)}</b> · frame ${t.frame}</figcaption>
    </figure>`;
  });
  if (!tiles.length) return '';
  return `<div class="lp-strip">
    <div class="lp-head">Live preview
      <span class="lp-tag">small JPEG, not the render</span></div>
    <div class="lp-tiles">${tiles.join('')}</div>
    <p class="lp-note">Each machine sends a small picture of every frame
      it finishes down the same log this page reads progress from, so a
      render can be watched without stopping it. Kaggle only releases the
      full-resolution frames once a session ends — until then, clicking a
      finished cell below will tell you so. Set BR_THUMBS=0 on a render to
      turn these off.</p>
  </div>`;
}

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
  /* Accounts whose count this app does NOT know: a stopped render whose
     own log could not be read (bridge._frames_done_source === 'unknown').
     Their cells are left unshaded rather than shaded from the last number
     a live stream happened to save, and the meta line below names them --
     an unshaded cell that is merely unknown must not read as "not
     rendered". */
  const unknownLabels = [];
  instances.forEach(i => {
    if (!i.worker) return;
    const liveFrames = !!(i.live && i.live.framesTotal);
    if (!liveFrames && i.worker.framesDoneSource === 'unknown') {
      unknownLabels.push(i.label);
      return;
    }
    const n = liveFrames ? i.live.framesDone : i.worker.framesDone;
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
  const jobId = job.jobId || '';
  const view = frameView(jobId);
  const open = !frameSectionCollapsed(jobId);
  /* Both views are built every time and one is shown, rather than one
     being built on demand: this whole section is discarded and rebuilt on
     every live tick, so there is no "on demand" to hold on to. What has
     to survive a rebuild lives in frameThumbs[jobId], and
     hydrateFrameStrips() puts the pictures back afterwards. */
  return `<div class="fgrid-wrap show" data-fg="${esc(jobId)}" data-view="${view}"${
      open ? '' : ' data-collapsed'}>
    <div class="fg-head">
      <button class="fg-collapse" data-fg-collapse="${esc(jobId)}"
        aria-expanded="${open}"
        title="${open ? 'Hide' : 'Show'} this scene's frames">
        <svg class="chev" width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M3 4.5L6 7.5l3-3" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
        <span class="fg-count">${done.size}/${total} frames</span>
      </button>
      ${prefs.frameThumbnails === false ? '' : `<div class="seg sm fg-views">
        <button data-fg-view="grid" data-job="${esc(jobId)}"${
          view === 'grid' ? ' class="on"' : ''} aria-label="Grid" data-tip="Grid">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><rect x="2" y="2" width="4.6" height="4.6" rx="1" stroke="currentColor" stroke-width="1.4"/><rect x="9.4" y="2" width="4.6" height="4.6" rx="1" stroke="currentColor" stroke-width="1.4"/><rect x="2" y="9.4" width="4.6" height="4.6" rx="1" stroke="currentColor" stroke-width="1.4"/><rect x="9.4" y="9.4" width="4.6" height="4.6" rx="1" stroke="currentColor" stroke-width="1.4"/></svg>
        </button>
        <button data-fg-view="thumbs" data-job="${esc(jobId)}"${
          view === 'thumbs' ? ' class="on"' : ''} aria-label="Thumbnails"
          data-tip="Thumbnails">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none"><rect x="1.6" y="3.4" width="12.8" height="9.2" rx="1.4" stroke="currentColor" stroke-width="1.4"/><path d="M1.6 10.6l3.2-2.9 2.6 2.2 2.8-2.6 4.2 3.7" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>
        </button>
      </div>`}
    </div>
    <div class="fg-body">
      ${livePreviewStrip(instances)}
      <div class="fgrid" data-job="${esc(jobId)}">${cells.join('')}</div>
      ${frameStripHtml(job, instances, done)}
      <div class="fgrid-legend">
        <span><i style="background:var(--accent)"></i>done <b>(approximate)</b></span>
        <span><i style="background:var(--fill)"></i>not yet</span>
      </div>
      <div class="fg-meta">${done.size}/${total} frames · ${esc(job.blend)}
        — a failed frame shifts every later cell for that account; Collect
        frames is the authoritative list of what exists.${unknownLabels.length
          ? ` ${unknownLabels.length} account(s) are not counted here at all
              (${unknownLabels.map(esc).join(', ')}): they have stopped, but
              their logs on Kaggle could not be read, so how many frames they
              finished is not known — their cells are left blank rather than
              guessed, and Collect frames will show what they actually made.`
          : ''}</div>
    </div>
  </div>`;
}

/* ---------------- the thumbnail view of a job's frames -------------------
 *
 * The grid says HOW MANY frames are done; this says what they look like.
 *
 * The cost is the thing to understand before reading the rest: Kaggle
 * serves no thumbnail. The only file it will hand over is the frame
 * itself, ~2 MB of PNG, so a wall of 250 tiles is half a gigabyte and
 * being lazy here is the design rather than an optimisation. Hence: a
 * tile is fetched only once it is actually scrolled to, one fetch at a
 * time, and the queue stops and asks after THUMB_BUDGET network fetches.
 * A frame already in the app's own cache costs nothing (previewFrame
 * answers it without touching the network) and is deliberately not
 * counted against that budget.
 *
 * What is HELD is a shrunk copy, not the frame: each picture is redrawn
 * to a canvas at THUMB_WIDTH and kept as a small JPEG, and the
 * full-resolution original is dropped. Twenty-four 1080p PNGs decoded in
 * a scroller is ~190 MB of bitmap; the same twenty-four at 320px is a
 * few hundred KB. Clicking a tile opens the REAL frame in the preview
 * modal, read back from the cache -- so the strip never passes a scaled
 * copy off as the thing itself, and its own caption says as much.
 */
const THUMB_BUDGET = 24;      /* network fetches before it stops to ask */
const THUMB_WIDTH = 320;      /* the kept copy's width, in pixels */

/* Per job, and deliberately OUTSIDE the payload: the card grid is rebuilt
   from scratch on every live tick, and the pictures must not be fetched
   again every two seconds. */
const frameThumbs = {};

function thumbState(jobId) {
  if (!frameThumbs[jobId]) {
    frameThumbs[jobId] = {
      view: 'grid', collapsed: false,
      small: {},        /* frame -> shrunk data URL, what a tile shows */
      full: {},         /* frame -> file URL, what the modal opens */
      failed: {},       /* frame -> true: asked, and Kaggle had nothing */
      budget: THUMB_BUDGET,
      capped: false,    /* budget ran out with tiles still unfetched */
    };
  }
  return frameThumbs[jobId];
}

function frameView(jobId) {
  /* Turning the preference off does not discard the choice, it stops
     honouring it -- turning it back on returns to whatever was open. */
  if (prefs.frameThumbnails === false) return 'grid';
  return thumbState(jobId).view;
}

function frameSectionCollapsed(jobId) {
  return thumbState(jobId).collapsed;
}

/* Which account owns each frame, and whether that account's session has
   ENDED. Kaggle releases a kernel's output only once its session stops,
   so a frame belonging to a still-running account cannot be fetched at
   all -- those tiles say so rather than queueing a request that could
   only ever fail. */
function frameOwners(instances) {
  const owners = {};
  instances.forEach(i => {
    if (!i.worker || !i.worker.frames) return;
    i.worker.frames.forEach(f => {
      owners[f] = { label: i.label, finished: !!i.worker.finished };
    });
  });
  return owners;
}

/* Pure, like renderFrameGrid itself: reads the job, its instances and the
   same `done` set the grid shades, so a test can drive it directly. */
function frameStripHtml(job, instances, done) {
  const jobId = job.jobId || '';
  const state = thumbState(jobId);
  const owners = frameOwners(instances);
  const tiles = [];
  let fetchable = 0;
  for (let f = job.startFrame; f <= job.endFrame; f++) {
    if (!done.has(f)) continue;          /* nothing rendered, nothing to show */
    const owner = owners[f];
    if (owner && !owner.finished) {
      tiles.push(`<figure class="fthumb waiting" title="Frame ${f} is on ${
        esc(owner.label)}, whose session is still running — Kaggle releases a session's frames only once it ends."><figcaption>${
        f}<span class="fw">when the session ends</span></figcaption></figure>`);
      continue;
    }
    if (state.failed[f]) {
      tiles.push(`<figure class="fthumb gone" title="Kaggle had no file for frame ${
        f} — that session's output may have expired."><figcaption>${
        f}<span class="fw">not on Kaggle</span></figcaption></figure>`);
      continue;
    }
    fetchable++;
    tiles.push(`<figure class="fthumb" data-thumb-frame="${f}"
      data-thumb-job="${esc(jobId)}" role="button" tabindex="0"
      title="Frame ${f} — click for the full-resolution frame">
      <div class="skel"></div><img alt="Frame ${f}, reduced">
      <figcaption>${f}</figcaption></figure>`);
  }
  if (!tiles.length) {
    return '<div class="fstrip-wrap"><div class="fstrip-empty">'
      + 'Nothing to show yet — a picture exists here once an account has'
      + ' finished a frame AND its session has ended, which is when Kaggle'
      + ' releases the file.</div></div>';
  }
  const loaded = Object.keys(state.small).length;
  return `<div class="fstrip-wrap">
    <button class="fstrip-nav prev" data-strip-slide="-1" aria-label="Earlier frames" data-tip="Earlier frames" disabled>
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M10 3.5L5.5 8l4.5 4.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    <button class="fstrip-nav next" data-strip-slide="1" aria-label="Later frames" data-tip="Later frames">
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M6 3.5L10.5 8 6 12.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    <div class="fstrip">${tiles.join('')}</div>
    <div class="fstrip-note">Reduced copies — click one for the full-resolution frame.</div>
    <div class="fstrip-more"${state.capped && fetchable > loaded ? '' : ' hidden'}>
      <span>${loaded} frame(s) fetched. Loading is paused so a long render cannot quietly pull hundreds of megabytes.</span>
      <button class="btn sm" data-thumb-more="${esc(jobId)}">Keep loading</button>
    </div>
  </div>`;
}

/* ---- putting the pictures back after a rebuild ---- */
let thumbObserver = null;

/* An arrow is only offered while there is something that way to go. */
function syncStripNav(rail) {
  const wrap = rail.closest('.fstrip-wrap');
  if (!wrap) return;
  const max = rail.scrollWidth - rail.clientWidth;
  const prev = wrap.querySelector('.fstrip-nav.prev');
  const next = wrap.querySelector('.fstrip-nav.next');
  if (prev) prev.disabled = rail.scrollLeft <= 2;
  if (next) next.disabled = rail.scrollLeft >= max - 2;
}

function hydrateFrameStrips() {
  document.querySelectorAll('.fthumb[data-thumb-frame]').forEach(tile => {
    const jobId = tile.dataset.thumbJob;
    const frame = Number(tile.dataset.thumbFrame);
    const small = thumbState(jobId).small[frame];
    if (!small) return;
    const img = tile.querySelector('img');
    if (img && img.getAttribute('src') !== small) img.src = small;
    tile.classList.add('has-image');
  });
  observeThumbs();
  document.querySelectorAll('.fstrip').forEach(rail => {
    syncStripNav(rail);
    if (rail.dataset.navBound) return;
    rail.dataset.navBound = '1';
    /* Scrolling is also how tiles come into view, so the same handler
       keeps the arrows honest and lets the observer do its work. */
    rail.addEventListener('scroll', () => syncStripNav(rail), { passive: true });
  });
}

/* Fetch what is actually looked at, and nothing else. */
function observeThumbs() {
  if (!('IntersectionObserver' in window)) return;
  if (!thumbObserver) {
    thumbObserver = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return;
        queueThumb(entry.target.dataset.thumbJob,
                   Number(entry.target.dataset.thumbFrame));
      });
    }, { rootMargin: '200px' });
  }
  thumbObserver.disconnect();
  document.querySelectorAll('.fthumb[data-thumb-frame]:not(.has-image)')
    .forEach(tile => thumbObserver.observe(tile));
}

/* ---- the queue: one fetch at a time, and a budget ---- */
const thumbQueue = [];

function queueThumb(jobId, frame) {
  if (!backend || !jobId || !frame) return;
  const state = thumbState(jobId);
  if (state.small[frame] || state.failed[frame]) return;
  if (thumbQueue.some(q => q.jobId === jobId && q.frame === frame)) return;
  thumbQueue.push({ jobId, frame });
  pumpThumbs();
}

function pumpThumbs() {
  if (!backend || frameFetchInFlight()) return;
  while (thumbQueue.length) {
    const next = thumbQueue[0];
    const state = thumbState(next.jobId);
    if (state.small[next.frame] || state.failed[next.frame]) {
      thumbQueue.shift();
      continue;
    }
    if (state.budget <= 0) {
      /* Stop, say so, and wait to be asked again. Nothing is dropped:
         the queue simply is not pumped until Keep loading tops it up. */
      if (!state.capped) {
        state.capped = true;
        showThumbCap(next.jobId);
      }
      return;
    }
    thumbQueue.shift();
    askForFrame(next.frame, next.jobId, false);
    return;
  }
}

function showThumbCap(jobId) {
  const wraps = Array.from(document.querySelectorAll('.fgrid-wrap[data-fg]'))
    .filter(el => el.dataset.fg === jobId);
  wraps.forEach(wrap => {
    const more = wrap.querySelector('.fstrip-more');
    if (more) more.hidden = false;
  });
}

/* ---- one frame arrives ---- */
function takeFrameThumb(jobId, frame, url) {
  const state = thumbState(jobId);
  state.full[frame] = url;
  shrinkFrame(url, small => {
    state.small[frame] = small || url;
    hydrateFrameStrips();
  });
}

/* The picture, redrawn small, so the page holds a thumbnail rather than a
   1080p bitmap. Falls back to the original URL if the canvas refuses (a
   tainted canvas throws on toDataURL) -- a working tile at the wrong size
   beats no tile at all. */
function shrinkFrame(url, done) {
  const img = new Image();
  img.onload = () => {
    try {
      const w = THUMB_WIDTH;
      const h = Math.max(1, Math.round(img.naturalHeight * w / img.naturalWidth));
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      canvas.getContext('2d').drawImage(img, 0, 0, w, h);
      done(canvas.toDataURL('image/jpeg', 0.72));
    } catch (e) {
      done(url);
    }
  };
  img.onerror = () => done(null);
  img.src = url;
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
        : `<button class="btn ico sm danger" data-username="${esc(i.label)}"
             aria-label="Set username" data-tip="Set username"
             title="Set username — Kaggle only reveals a handle through something the account owns.">${ACT.user}</button>`}</td>
      <td>${esc(i.quota || '—')}</td>
      <td>${hw}</td>
      <td>${esc(state_)}</td>
      <td style="text-align:right;white-space:nowrap">
        <div class="row-actions">
          ${stoppable
            ? `<button class="btn ico sm" data-cancel="${esc(i.label)}"
                 aria-label="Stop" data-tip="Stop"
                 title="Stop this account's session on Kaggle">${ACT.stop}</button>`
            : `<button class="btn ico sm" data-start="${esc(i.label)}"
                 aria-label="Start" data-tip="Start"
                 title="Start a warm machine on this account">${ACT.play}</button>`}
          <button class="btn ico sm" data-download="${esc(i.label)}"
            aria-label="Download" data-tip="Download"
            title="Download this account's rendered frames">${ACT.download}</button>
          <button class="btn ico sm danger" data-remove="${esc(i.label)}"
            aria-label="Remove account" data-tip="Remove"
            title="Remove this account from the fleet">${ACT.trash}</button>
        </div>
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
/* `live` marks the small JPEG the notebook pushed down the log stream
   while the render was still going, as opposed to the full-resolution
   file fetched from a finished session. The two are NEVER titled the
   same: they are different pictures of different quality, and a viewer
   who mistook one for the other would judge a render by a thumbnail.
   The live one is also shown at its own size and never stretched up to
   fill the stage -- see .lb-stage img in app.css for the same rule
   applied to the real thing. */
/* WHICH FRAME THIS BOX IS CURRENTLY WAITING FOR.
   `{frame, jobId, started}` while a fetch is outstanding, null once the
   picture has arrived or the wait has been given up on. `started` is
   set by busyChanged: previewFrame answers some cases (no job, a frame
   assigned to nobody, an account since removed) with a notification and
   no worker at all, so "the request never even started" is a distinct
   outcome from "it started and failed", and only one of them is worth
   offering Try again for. */
let previewWait = null;
/* What Try again would ask for. Kept past the failure that offers it --
   `previewWait` is cleared the moment the wait ends, one way or the
   other, and a retry button has to outlive that. */
let previewAsked = null;
let previewTimers = [];

function clearPreviewTimers() {
  previewTimers.forEach(clearTimeout);
  previewTimers = [];
}

/* loading | ready | failed -- the stage shows exactly one of the three. */
function setPreviewState(state) {
  const stage = document.getElementById('lb-stage');
  stage.dataset.state = state;
}

/* Open NOW, on the click, with the box empty and a placeholder running.
   The picture follows through framePreview whenever Kaggle hands it
   over; this is what makes the click feel answered. */
function openPreviewLoading(frame, jobId) {
  clearPreviewTimers();
  previewWait = { frame, jobId, started: false };
  previewAsked = { frame, jobId };
  document.getElementById('lb-title').textContent = `frame ${frame}`;
  document.getElementById('lb-sub').textContent = 'fetching from Kaggle…';
  const img = document.getElementById('lb-img');
  img.removeAttribute('src');
  img.classList.remove('lb-live');
  setPreviewState('loading');
  const box = document.getElementById('lightbox');
  box.setAttribute('aria-label', 'Rendered frame');
  box.hidden = false;
  document.querySelector('.lb-box').focus();
  /* A request that never reached a worker has nothing left to report,
     so the placeholder must not run for ever waiting on it. */
  previewTimers.push(setTimeout(() => {
    if (previewWait && !previewWait.started) {
      failPreview(`Frame ${frame} could not be requested — the message that `
                  + 'just appeared says why.', false);
    }
  }, 1500));
}

function failPreview(message, retryable = true) {
  clearPreviewTimers();
  previewWait = null;
  if (document.getElementById('lightbox').hidden) return;
  document.getElementById('lb-fail-msg').textContent = message;
  document.getElementById('lb-retry').hidden = !retryable;
  document.getElementById('lb-sub').textContent = '';
  setPreviewState('failed');
}

/* `live` marks the small JPEG the notebook pushed down the log stream
   while the render was still going, as opposed to the full-resolution
   file fetched from a finished session. */
function openPreview(frame, url, label, live) {
  clearPreviewTimers();
  previewWait = null;
  document.getElementById('lb-title').textContent =
    live ? `frame ${frame} · live preview` : `frame ${frame}`;
  document.getElementById('lb-sub').textContent = live
    ? `a small picture ${label || 'that machine'} sent while it was still `
      + 'rendering — not the full-resolution frame, which Kaggle releases '
      + 'only once that session ends'
    : (label ? `rendered by ${label}` : '');
  const img = document.getElementById('lb-img');
  /* The placeholder stays up until the picture has actually DECODED --
     a 2 MB PNG is not on screen the instant its src is set, and an
     empty stage in that gap reads as a frame that came back blank. */
  setPreviewState('loading');
  img.onload = () => setPreviewState('ready');
  img.onerror = () => failPreview(
    `Frame ${frame} arrived but could not be displayed.`);
  img.src = url;
  img.alt = live
    ? `Small live preview of frame ${frame}`
    : `Rendered frame ${frame}`;
  img.classList.toggle('lb-live', !!live);
  if (img.complete && img.naturalWidth) setPreviewState('ready');
  /* The dialog's own name, not just its contents: a screen reader
     announcing "Rendered frame" over a thumbnail would make exactly the
     mistake the visible labels are here to prevent. */
  const box = document.getElementById('lightbox');
  box.setAttribute('aria-label', live ? 'Live frame preview' : 'Rendered frame');
  box.hidden = false;
  document.querySelector('.lb-box').focus();
}

function closePreview() {
  clearPreviewTimers();
  previewWait = null;
  document.getElementById('lightbox').hidden = true;
  /* Dropped so the next open cannot flash the previous frame while the
     new one decodes. */
  document.getElementById('lb-img').removeAttribute('src');
}

document.getElementById('lb-close').addEventListener('click', closePreview);
document.getElementById('lb-retry').addEventListener('click', () => {
  if (!backend || !previewAsked) return;
  const { frame, jobId } = previewAsked;
  /* A frame that answered "not on Kaggle" is remembered as such, and Try
     again is precisely the request to find out whether that is still
     true -- so the memory is dropped rather than short-circuiting it. */
  delete thumbState(jobId).failed[frame];
  openPreviewLoading(frame, jobId);
  askForFrame(frame, jobId, true);
});
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
/* ---- every frame this page has asked for and not yet heard back about,
   keyed "<jobId>|<frame>" --------------------------------------------
   One record per frame, carrying who is waiting for it. Two things ask
   for frames now -- the preview modal and the thumbnail strip -- and
   previewFrame() is a NO-OP when a fetch for the same frame is already
   running: bridge._start skips it and emits nothing at all, so a second
   asker would wait for a signal that never comes. Going through one
   record means the second asker joins the first instead. */
const frameFetches = new Map();

function frameKey(jobId, frame) { return `${jobId || ''}|${frame}`; }

/* The strip pauses while anything is in flight -- one frame at a time is
   what keeps a scroll from firing forty parallel downloads at Kaggle. */
function frameFetchInFlight() { return frameFetches.size > 0; }

function frameFetchFor(frame) {
  for (const rec of frameFetches.values()) {
    if (rec.frame === frame) return rec;
  }
  return null;
}

/* `forModal` says whether the answer should also open the preview box.
   A tile scrolling into view wants the picture but not the dialog. */
function askForFrame(frame, jobId, forModal) {
  if (!backend) return;
  const key = frameKey(jobId, frame);
  const existing = frameFetches.get(key);
  if (existing) {
    existing.forModal = existing.forModal || forModal;
    return;
  }
  const rec = { frame, jobId, forModal, started: false, timers: [] };
  frameFetches.set(key, rec);
  /* A request that never reaches a worker (no job, a frame assigned to
     nobody, an account since removed) answers with a notification and
     no signals at all, so it needs its own way of ending. */
  rec.timers.push(setTimeout(() => {
    if (frameFetches.get(key) === rec && !rec.started) {
      failFrameFetch(rec, `Frame ${frame} could not be requested — the message`
                          + ' that just appeared says why.', false);
    }
  }, 1500));
  backend.previewFrame(frame, jobId);
}

function clearFrameFetch(rec) {
  rec.timers.forEach(clearTimeout);
  rec.timers = [];
  frameFetches.delete(frameKey(rec.jobId, rec.frame));
}

/* Kaggle handed the picture over. It fills the tile it belongs to
   whether or not a tile asked for it -- a frame fetched for the modal is
   already paid for, so the strip may as well show it. */
function settleFrameFetch(rec, url, label) {
  clearFrameFetch(rec);
  /* Only a fetch that actually went to the network spends the budget.
     A frame already on disk answers with no busyChanged at all, which is
     exactly what `started` records. */
  if (rec.started) thumbState(rec.jobId).budget -= 1;
  takeFrameThumb(rec.jobId, rec.frame, url);
  if (rec.forModal) openPreview(rec.frame, url, label);
  pumpThumbs();
}

function failFrameFetch(rec, message, retryable) {
  clearFrameFetch(rec);
  if (rec.started) thumbState(rec.jobId).budget -= 1;
  thumbState(rec.jobId).failed[rec.frame] = true;
  markThumbGone(rec.jobId, rec.frame);
  if (rec.forModal) failPreview(message, retryable);
  pumpThumbs();
}

/* The tile, patched where it stands. A full rebuild would work too, but
   it would also throw away every other tile's picture for one that came
   back empty. */
function markThumbGone(jobId, frame) {
  document.querySelectorAll('.fthumb[data-thumb-frame]').forEach(tile => {
    if (tile.dataset.thumbJob !== jobId
        || Number(tile.dataset.thumbFrame) !== frame) return;
    tile.classList.add('gone');
    tile.removeAttribute('data-thumb-frame');
    tile.title = `Kaggle had no file for frame ${frame} — that session's`
      + ' output may have expired.';
    tile.innerHTML = `<figcaption>${frame}<span class="fw">not on`
      + ' Kaggle</span></figcaption>';
  });
}

/* Open the box, THEN ask. The order is the whole point: fetching a
   ~2 MB frame off Kaggle takes seconds, and a click that shows nothing
   for those seconds reads as a click that did nothing. */
function requestPreview(frame, jobId) {
  openPreviewLoading(frame, jobId);
  askForFrame(frame, jobId, true);
}

function jobIdOfCell(cell) {
  const grid = cell.closest('.fgrid');
  return (grid && grid.dataset.job) || '';
}

/* A tile shows a reduced copy; the modal must show the frame. If this
   page still has the full-resolution URL for it there is nothing to
   fetch -- the file is on disk -- so it opens instantly. Otherwise it
   goes through the ordinary request, box first. */
function openThumb(tile) {
  const jobId = tile.dataset.thumbJob;
  const frame = Number(tile.dataset.thumbFrame);
  const full = thumbState(jobId).full[frame];
  if (full) {
    openPreview(frame, full);
    return;
  }
  requestPreview(frame, jobId);
}

/* A live tile opens the picture it is ALREADY showing, larger -- it never
   asks the backend for anything. Mid-render there is nothing on Kaggle to
   fetch (the session has not ended, so its output does not exist yet), so
   routing this through previewFrame would only ever produce an apology. */
function openLiveTile(tile) {
  const img = tile.querySelector('img');
  if (!img || !img.src) return;
  openPreview(Number(tile.dataset.liveFrame), img.src,
              tile.dataset.liveLabel, true);
}

document.getElementById('instances').addEventListener('click', e => {
  const collapse = e.target.closest('[data-fg-collapse]');
  if (collapse) {
    const jobId = collapse.dataset.fgCollapse;
    const state = thumbState(jobId);
    state.collapsed = !state.collapsed;
    const wrap = collapse.closest('.fgrid-wrap');
    if (wrap) {
      wrap.toggleAttribute('data-collapsed', state.collapsed);
      collapse.setAttribute('aria-expanded', String(!state.collapsed));
    }
    /* Nothing loads while it is shut: the observer only ever sees tiles
       that are on screen, and a collapsed section has none. */
    if (!state.collapsed) observeThumbs();
    return;
  }
  const viewBtn = e.target.closest('[data-fg-view]');
  if (viewBtn) {
    const jobId = viewBtn.dataset.job;
    thumbState(jobId).view = viewBtn.dataset.fgView;
    const wrap = viewBtn.closest('.fgrid-wrap');
    if (wrap) {
      wrap.dataset.view = viewBtn.dataset.fgView;
      wrap.querySelectorAll('[data-fg-view]').forEach(b =>
        b.classList.toggle('on', b === viewBtn));
    }
    /* Only now do any tiles have a size, so only now can the observer
       tell which of them are actually on screen. */
    observeThumbs();
    return;
  }
  const slide = e.target.closest('[data-strip-slide]');
  if (slide) {
    const rail = slide.closest('.fstrip-wrap').querySelector('.fstrip');
    /* One viewport of tiles, less a sliver, so the tile at the edge is
       not left half-shown and forgotten. */
    rail.scrollBy({ left: Number(slide.dataset.stripSlide)
                          * Math.max(120, rail.clientWidth - 40),
                    behavior: 'smooth' });
    return;
  }
  const more = e.target.closest('[data-thumb-more]');
  if (more) {
    const state = thumbState(more.dataset.thumbMore);
    state.budget = THUMB_BUDGET;
    state.capped = false;
    const box = more.closest('.fstrip-more');
    if (box) box.hidden = true;
    observeThumbs();
    pumpThumbs();
    return;
  }
  const thumb = e.target.closest('[data-thumb-frame]');
  if (thumb) {
    openThumb(thumb);
    return;
  }
  const tile = e.target.closest('[data-live-frame]');
  if (tile) {
    openLiveTile(tile);
    return;
  }
  const cell = e.target.closest('[data-frame]');
  if (cell && backend) {
    requestPreview(Number(cell.dataset.frame), jobIdOfCell(cell));
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
  const thumb = e.target.closest('[data-thumb-frame]');
  if (thumb) {
    e.preventDefault();
    openThumb(thumb);
    return;
  }
  const tile = e.target.closest('[data-live-frame]');
  if (tile) {
    e.preventDefault();
    openLiveTile(tile);
    return;
  }
  const cell = e.target.closest('[data-frame]');
  if (cell && backend) {
    e.preventDefault();
    requestPreview(Number(cell.dataset.frame), jobIdOfCell(cell));
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
    if (key === 'frameThumbnails') {
      prefs.frameThumbnails = next;
      /* The sections carry the view switch, so they have to be rebuilt
         for it to appear or go. Turning it off does not discard what a
         strip already loaded -- that lives in frameThumbs, and turning
         it back on shows it again without re-fetching a thing. */
      repaintCards();
    }
  };
  el.addEventListener('click', flip);
  el.addEventListener('keydown', e => {
    if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); flip(); }
  });
}
bindToggle('tgl-glass', 'translucent');
bindToggle('tgl-sound', 'sound');
bindToggle('tgl-thumbs', 'frameThumbnails');

document.getElementById('seg-theme').addEventListener('click', e => {
  setTheme(e.target.dataset.v);
});
document.getElementById('faces').addEventListener('click', e => {
  const value = e.target.closest('[data-v]') && e.target.closest('[data-v]').dataset.v;
  if (!value || value === prefs.font) return;
  /* Type changes the size of everything, so it lands in one frame like a
     theme change does rather than reflowing under a transition. */
  const root = document.documentElement;
  root.classList.add('theme-snap');
  sfx.toggle();
  applyPrefs({ font: value });
  backend && backend.setPreference('font', JSON.stringify(value));
  void root.offsetWidth;
  requestAnimationFrame(() => root.classList.remove('theme-snap'));
  syncSettingsControls();
});
document.getElementById('swatches').addEventListener('click', e => {
  const value = e.target.dataset.v;
  if (!value) return;
  applyPrefs({ accent: value });
  backend && backend.setPreference('accent', JSON.stringify(value));
  syncSettingsControls();
});
document.getElementById('seg-close').addEventListener('click', e => {
  const value = e.target.dataset.v;
  if (!value) return;
  prefs.closeAction = value;
  backend && backend.setPreference('closeAction', JSON.stringify(value));
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
  document.querySelectorAll('#faces .face').forEach(b =>
    b.classList.toggle('on', b.dataset.v === (prefs.font || 'heebo')));
  document.querySelectorAll('#seg-close button').forEach(b =>
    b.classList.toggle('on', b.dataset.v === (prefs.closeAction || 'ask')));
  document.getElementById('tgl-glass').classList.toggle('on', !!prefs.translucent);
  document.getElementById('tgl-sound').classList.toggle('on', prefs.sound !== false);
  document.getElementById('tgl-thumbs')
    .classList.toggle('on', prefs.frameThumbnails !== false);
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
    /* The JOB-level figure, on the dashboard section and on the Files
       row alike. Rebuilt rather than patched field by field because the
       indicator legitimately CHANGES SHAPE mid-download: it carries no
       bar until every account has reported a real size, and grows one
       the moment they have (see jobDownloadInnerHtml). */
    const jobRows = Array.from(document.querySelectorAll('[data-jdl]'))
      .filter(el => el.dataset.jdl === p.jobId);
    jobRows.forEach(el => { el.innerHTML = jobDownloadInnerHtml(p.jobId); });
    /* A Files row that has never been drawn for this render (the section
       was rendered before the download began) has no [data-jdl] to patch,
       so the whole list is redrawn once to grow one. */
    if (!jobRows.length) repaintOutputs();
  });

  /* A collect has finished. The row that started it says where the zip
     went -- the message already names the path -- instead of leaving a
     bar behind or sending the user to hunt for the file. */
  backend.collectFinished.connect(json => {
    const result = JSON.parse(json);
    if (result.jobId) collectResults[result.jobId] = result;
    repaintOutputs();
  });

  backend.busyChanged.connect((key, busy) => {
    const map = { launch: 'btn-render', cancel: 'btn-cancel',
                  'collect:': 'btn-collect', dataset: 'btn-upload' };
    const id = map[key];
    if (id) document.getElementById(id).disabled = busy;
    if (key.indexOf('verify:') === 0) document.getElementById('btn-add').disabled = busy;
    /* A frame fetch, told what became of it. bridge's _start emits
       busy=false BEFORE the handler that emits the picture, so
       "finished" is not yet "failed" -- the short wait is for the
       framePreview that normally lands immediately after and settles the
       record. busy=true is also the only signal that says a request
       actually went to the NETWORK rather than being served from the
       cache, which is what the strip's budget counts. */
    if (key.indexOf('preview:') === 0) {
      const rec = frameFetchFor(Number(key.slice('preview:'.length)));
      if (rec) {
        if (busy) {
          rec.started = true;
          rec.timers.forEach(clearTimeout);
          rec.timers = [];
        } else {
          rec.timers.push(setTimeout(() => {
            if (frameFetches.get(frameKey(rec.jobId, rec.frame)) === rec) {
              failFrameFetch(rec, `Frame ${rec.frame} did not come back — the `
                             + 'message that just appeared says what Kaggle '
                             + 'answered.', true);
            }
          }, 400));
        }
      }
    }
    /* A finished collect must not leave a bar frozen at whatever it
       reached -- including a failed one, which would otherwise sit at 68%
       for ever, looking like it was still going. */
    if (key.indexOf('collect:') === 0 && !busy) {
      const label = key.slice('collect:'.length);
      /* Scoped to the account that finished, so a second collect running
         alongside does not lose its bars. The fleet-wide button sends an
         empty label and does mean "all of them". */
      Object.keys(downloads).forEach(k => {
        if (!label || k === label) delete downloads[k];
      });
      repaintCards();
      repaintOutputs();
    }
    /* The Files page's per-render Download button, keyed by jobId (see
       bridge.collect). Its own key, not a third segment of "collect:", so
       the exact-match entries above keep matching. */
    if (key.indexOf('collect-job:') === 0) {
      const jobId = key.slice('collect-job:'.length);
      const btn = Array.from(document.querySelectorAll('[data-out-download]'))
        .find(el => el.dataset.outDownload === jobId);
      if (btn) btn.disabled = busy;
      const jobBtn = Array.from(document.querySelectorAll('[data-job-collect]'))
        .find(el => el.dataset.jobCollect === jobId);
      if (jobBtn) jobBtn.disabled = busy;
      if (!busy) {
        Object.keys(downloads).forEach(k => {
          if (downloads[k] && downloads[k].jobId === jobId) delete downloads[k];
        });
        repaintCards();
        repaintOutputs();
      }
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
    const rec = frameFetchFor(p.frame);
    if (rec) {
      settleFrameFetch(rec, p.path, p.label);
      return;
    }
    /* Nothing on this page is waiting for it -- which today means it was
       asked for before a reload. Showing it is still the right answer:
       the user asked for this frame. */
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

  /* Two calls, in this order, and the order is the point. outputs() is
     pure disk and answers immediately, so the renders list is on screen
     before anything touches the network. checkOutputs() then asks Kaggle
     which of them it still has and re-emits; until it answers, every row
     honestly says it has not been checked. Nothing here blocks the page
     -- the startup poll already taught us what that feels like. */
  backend.outputsChanged.connect(renderOutputs);
  backend.outputs();
  backend.checkOutputs();

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
