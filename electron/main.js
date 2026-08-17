/* The window, and the Python behind it.
 *
 * BlendFleet's backend is Python -- the Kaggle client, the resumable
 * upload, the SSE log stream, the frame collector -- and none of that is
 * being rewritten. It runs as a headless sidecar (python -m blendfleet.rpc)
 * speaking one JSON object per line over stdio, and this process is the
 * relay between that pipe and the page.
 *
 * WHY STDIO AND NOT A LOCAL SERVER: a pipe has no port to bind, no token
 * to invent, and no way for anything else on the machine to talk to it.
 * It also dies with its parent, which is what stops an orphaned backend
 * holding a render's state with no window left to show it.
 */
const { app, BrowserWindow, Menu, Notification, Tray, dialog, ipcMain,
        nativeImage, nativeTheme, powerMonitor, powerSaveBlocker, session,
        shell } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const readline = require('readline');

const ROOT = path.join(__dirname, '..');
const PACKAGED = app.isPackaged;

/* Where the page, the icon and the sidecar live differs between "run it
   from the repo" and "installed". Named once, here, rather than guessed
   at each use. */
const paths = {
  /* resources/BLENDFLEET/WEB, not resources/web. app.css asks for its
     fonts and the logo as `../../assets/...` -- two levels up from
     blendfleet/web/, which is the repo root. Flattening the page to
     resources/web/ made that resolve one level above resources/, so in
     the first packaged build every @font-face and the sidebar mark
     silently 404'd: the app looked almost right, in fallback fonts, and
     the tell was a card label wrapping onto two lines. The page's
     relative paths are part of its contract with whatever hosts it, so
     the host mirrors the layout instead of the page changing shape. */
  page: PACKAGED
    ? path.join(process.resourcesPath, 'blendfleet', 'web', 'index.html')
    : path.join(ROOT, 'blendfleet', 'web', 'index.html'),
  icon: PACKAGED
    ? path.join(process.resourcesPath, 'assets', 'logo', 'app-icon-256.png')
    : path.join(ROOT, 'assets', 'logo', 'app-icon-256.png'),
  /* BESIDE THE EXE, not inside resources/ -- `extraFiles` in
     package.json, not `extraResources`. The built folder is meant to read
     the way dist/blendfleetweb/ does: the program, then the parts it
     runs. A sidecar buried two levels down in resources/ is the same
     bytes and a worse answer to "where is the backend". The page and the
     icon stay in resources/, which is this shell's `_internal`. */
  backend: PACKAGED
    ? path.join(path.dirname(app.getPath('exe')), 'backend',
                process.platform === 'win32'
                  ? 'blendfleet-backend.exe' : 'blendfleet-backend')
    : null,
  python: process.platform === 'win32'
    ? path.join(ROOT, '.venv', 'Scripts', 'python.exe')
    : path.join(ROOT, '.venv', 'bin', 'python'),
};

let win = null;
let tray = null;
let backend = null;
let quitting = false;
let saidWhereItWent = false;
const pending = new Map();      // call id -> {resolve}
let nextId = 1;

/* ---- the sidecar ------------------------------------------------------ */

function startBackend() {
  const [command, args] = PACKAGED
    ? [paths.backend, []]
    : [paths.python, ['-m', 'blendfleet.rpc']];

  backend = spawn(command, args, {
    cwd: PACKAGED ? process.resourcesPath : ROOT,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  });

  /* One line at a time, whatever the OS decides to chunk the pipe into.
     Reading raw and splitting by hand is how half a payload gets parsed
     as a whole one. */
  readline.createInterface({ input: backend.stdout }).on('line', line => {
    let message;
    try {
      message = JSON.parse(line);
    } catch (e) {
      console.error('unreadable line from the backend:', line);
      return;
    }
    if (message.event) {
      /* The shell listens to the same events the page does, for the things
         only a shell can do: keep the machine awake, put progress on the
         taskbar, raise an OS notification when the window is not there to
         show one. Before the page, and in a try, because none of it may
         cost the dashboard an update. */
      try {
        observe(message);
      } catch (e) {
        console.error('shell could not act on', message.event, e);
      }
      if (win && !win.isDestroyed()) {
        win.webContents.send('backend:event', message);
      }
      return;
    }
    const waiting = pending.get(message.id);
    if (!waiting) return;
    pending.delete(message.id);
    /* An error is ANSWERED, not thrown: the page's call sites are fire
       and forget, and an unhandled rejection in a renderer is a silent
       failure. The sidecar has already written the reason to the
       diagnostic log. */
    if (message.ok === false) console.error('backend refused:', message.error);
    waiting(message.ok === false ? null : message.result);
  });

  /* stderr is the sidecar's own crash channel -- Python tracebacks that
     never reached the protocol. Kept out of the page and put where the
     rest of this app's diagnostics go. */
  readline.createInterface({ input: backend.stderr }).on('line', line => {
    console.error('[backend]', line);
  });

  backend.on('exit', (code, signal) => {
    backend = null;
    if (quitting) return;
    /* The backend dying under a live window is the one failure that must
       not be silent: every card would simply stop updating. */
    console.error(`the backend exited (${code} ${signal || ''})`);
    if (win && !win.isDestroyed()) {
      win.webContents.send('backend:event', {
        event: 'notification',
        args: ['BlendFleet’s backend stopped. Renders on Kaggle are '
               + 'unaffected — reopen the app to follow them again.',
               'offline'],
      });
    }
  });
}

/* ---- what a shell can do that a page cannot --------------------------- */

/* THE MACHINE MUST NOT SLEEP WHILE WORK IS IN FLIGHT.
   A laptop that suspends mid-upload loses the upload -- the resumable
   uploader has to start that part again -- and mid-collect loses the
   download. Held only while something is actually running, released the
   moment it is not: a render farm that permanently prevented sleep would
   be a worse neighbour than one that occasionally lost a transfer. */
const busy = new Set();
let sleepBlocker = null;

function holdSleep() {
  if (sleepBlocker !== null || busy.size === 0) return;
  /* 'prevent-app-suspension', not 'prevent-display-sleep': the screen is
     welcome to turn off. What must not happen is the process being
     suspended while an HTTPS request is open. */
  sleepBlocker = powerSaveBlocker.start('prevent-app-suspension');
}

function releaseSleep() {
  if (sleepBlocker === null || busy.size > 0) return;
  powerSaveBlocker.stop(sleepBlocker);
  sleepBlocker = null;
}

/* PROGRESS WHERE IT CAN BE SEEN WITH THE WINDOW HIDDEN.
   The same fraction the card's edge draws, on the taskbar icon, because
   "keep running in the background" is a feature of this app and a render
   in the tray otherwise reports nothing at all. Cleared -- not left at
   100% -- when nothing is rendering: a full bar that never goes away
   reads as a render that never finished. */
function showProgress(payload) {
  if (!win || win.isDestroyed()) return;
  let done = 0;
  let total = 0;
  let running = 0;
  for (const instance of (payload.instances || [])) {
    const worker = instance.worker;
    if (!worker) continue;
    if (worker.state === 'running' || worker.state === 'queued') running += 1;
    /* Only frames a render actually claims. framesDone with no frames at
       all is not 0% of anything, and drawing it as such is the "bar at 0%"
       this app already refuses elsewhere. */
    if (Array.isArray(worker.frames) && worker.frames.length) {
      total += worker.frames.length;
      done += Math.min(worker.framesDone || 0, worker.frames.length);
    }
  }
  win.setProgressBar(running && total ? done / total : -1);
  if (process.platform === 'win32') {
    /* A count, not a percentage: the taskbar badge is 16 pixels and a
       number of machines is the one thing that fits. */
    win.setOverlayIcon(running ? badgeFor(running) : null,
                       running ? `${running} rendering` : '');
  }
}

/* Drawn rather than shipped as a file: one asset per possible count is
   silly, and this is two shapes and a digit. */
function badgeFor(count) {
  const label = count > 9 ? '9+' : String(count);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32">
    <circle cx="16" cy="16" r="15" fill="#111"/>
    <circle cx="16" cy="16" r="15" fill="none" stroke="#fff" stroke-width="2"/>
    <text x="16" y="22" font-family="Segoe UI, sans-serif" font-size="17"
          font-weight="600" fill="#fff" text-anchor="middle">${label}</text>
  </svg>`;
  return nativeImage.createFromDataURL(
    'data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64'));
}

/* A NOTIFICATION THE USER CAN ACTUALLY SEE.
   The app already emits one for every event worth telling somebody about,
   and until now only the page could hear it -- which is no use at all when
   the window is hidden in the tray, i.e. exactly when a render finishing
   is news. Only when hidden: duplicating an on-screen toast as an OS
   notification is noise. */
function notifyOutside(message, tone) {
  if (!Notification.isSupported()) return;
  if (win && !win.isDestroyed() && win.isVisible() && !win.isMinimized()) return;
  const note = new Notification({
    title: tone === 'offline' ? 'BlendFleet — attention needed' : 'BlendFleet',
    body: message,
    silent: tone !== 'offline',
  });
  note.on('click', showFromTray);
  note.show();
}

/* THE THEME THE WINDOW ITSELF USES.
   The page paints its own surfaces, but the parts Chromium and Windows
   draw -- scrollbars, the context menu, the Mica backdrop -- take their
   cue from nativeTheme. Setting themeSource from the app's own preference
   keeps those in step, and has a second effect worth having: it is also
   what `prefers-color-scheme` reports to the page, so a preference of
   "system" resolves to the same answer in both halves rather than the page
   asking the OS while the window asks the app. */
function matchShellTheme(preferences) {
  const wanted = (preferences.theme === 'dark' || preferences.theme === 'light')
    ? preferences.theme
    : 'system';
  if (nativeTheme.themeSource !== wanted) nativeTheme.themeSource = wanted;
}

function observe(message) {
  const args = message.args || [];
  switch (message.event) {
    case 'settingsChanged':
      matchShellTheme(JSON.parse(args[0] || '{}'));
      break;
    case 'busyChanged': {
      const [key, inFlight] = args;
      if (inFlight) busy.add(key); else busy.delete(key);
      if (busy.size) holdSleep(); else releaseSleep();
      break;
    }
    case 'stateChanged':
      showProgress(JSON.parse(args[0] || '{}'));
      break;
    case 'notification':
      notifyOutside(args[0] || '', args[1] || '');
      break;
    default:
      break;
  }
}

function callBackend(name, args = []) {
  return new Promise(resolve => {
    if (!backend || backend.killed) {
      resolve(null);
      return;
    }
    const id = nextId++;
    pending.set(id, resolve);
    backend.stdin.write(JSON.stringify({ id, call: name, args }) + '\n');
  });
}

/* ---- the window ------------------------------------------------------- */

function createWindow() {
  win = new BrowserWindow({
    width: 1400,
    height: 900,
    show: false,
    frame: false,
    /* Electron draws the window controls over the page's own header; the
       page marks the draggable strip (see shell.css). Colours are set
       from the theme once the page has told us which one it is in. */
    titleBarStyle: 'hidden',
    titleBarOverlay: { color: '#00000000', symbolColor: '#A8AAA2', height: 44 },
    backgroundColor: '#00000000',
    backgroundMaterial: 'mica',
    icon: paths.icon,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });

  win.loadFile(paths.page);
  win.once('ready-to-show', () => win.show());

  /* What the page cannot know about itself: the ambient accent ground
     (which the Qt build paints in its window, because its title bar sits
     outside the page) and the drag region a frameless window needs.
     Injected from here rather than added to app.css, so blendfleet/web/
     stays byte for byte the file the Qt build serves -- and from MAIN
     rather than the preload, which is sandboxed and has no fs. */
  win.webContents.on('did-finish-load', () => {
    win.webContents.insertCSS(
      fs.readFileSync(path.join(__dirname, 'shell.css'), 'utf8'));
  });

  /* The page's own console, where the person running from source can
     see it. A renderer that throws on line one otherwise fails in
     complete silence -- the window renders, nothing populates, and
     nothing anywhere says why. */
  win.webContents.on('console-message', (_event, level, message, line,
                                         source) => {
    if (level >= 2) {
      console.error(`[page] ${message} (${source}:${line})`);
    } else if (process.env.BLENDFLEET_VERBOSE) {
      console.log(`[page] ${message}`);
    }
  });

  /* A development affordance, not a feature: BLENDFLEET_SHOT=<path>
     captures the window once it has settled and quits. Verifying a shell
     by describing it does not work, and this is how the screenshots in
     the design review were taken. Off unless the variable is set. */
  if (process.env.BLENDFLEET_SHOT) {
    const after = Number(process.env.BLENDFLEET_SHOT_DELAY || 4000);
    setTimeout(async () => {
      const image = await win.webContents.capturePage();
      fs.writeFileSync(process.env.BLENDFLEET_SHOT, image.toPNG());
      console.log('captured', process.env.BLENDFLEET_SHOT);
      quitNow();
    }, after);
  }

  /* Nothing in this app is a link to somewhere else; anything that tries
     to open one opens it in the real browser rather than replacing the
     dashboard with it. */
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  /* Right-clicking a rendered frame offers the two things worth
     offering. Everything else offers nothing -- Chromium's own menu has
     Back, Forward and Reload on it, and in a single-page app those are
     either meaningless or destructive. */
  win.webContents.on('context-menu', (_event, props) => {
    if (props.mediaType !== 'image') return;
    Menu.buildFromTemplate([
      {
        label: 'Save image as…',
        click: () => saveImage(props.srcURL),
      },
      {
        label: 'Copy image',
        click: () => win.webContents.copyImageAt(props.x, props.y),
      },
    ]).popup({ window: win });
  });

  win.on('close', event => {
    if (quitting) return;
    event.preventDefault();
    decideOnClose();
  });
}

/* ---- closing, while something is still rendering ---------------------- */

async function decideOnClose() {
  /* live_renders answers with an object; every other call answers with a
     JSON string, because that is what QWebChannel could carry and the
     page was written against. A null means the backend is gone -- and a
     window that cannot ask whether anything is rendering must not
     guess that nothing is, so it asks the question. */
  const live = await callBackend('live_renders');
  const preferences = JSON.parse(await callBackend('preferences') || '{}');
  const accounts = live === null ? -1 : Number(live.accounts || 0);
  if (accounts < 0) {
    const lost = await dialog.showMessageBox(win, {
      type: 'warning',
      title: 'BlendFleet',
      message: 'BlendFleet cannot reach its own backend.',
      detail: 'It cannot tell whether anything is still rendering. Any '
        + 'render already started is on Kaggle and unaffected either way.',
      buttons: ['Quit', 'Keep the window open'],
      defaultId: 1,
      cancelId: 1,
    });
    return lost.response === 0 ? quitNow() : null;
  }

  /* Nothing rendering always quits, whatever the preference says: a tray
     icon for an idle app is litter. */
  if (accounts <= 0) return quitNow();
  if (preferences.closeAction === 'quit') return quitNow();
  if (preferences.closeAction === 'background') return hideToTray();

  const scenes = live.scenes || [];
  const what = scenes.length === 1
    ? `${scenes[0]} is still rendering`
    : `${scenes.length} scenes are still rendering`;
  const answer = await dialog.showMessageBox(win, {
    type: 'question',
    title: 'BlendFleet',
    message: `${what} on ${accounts} account${accounts === 1 ? '' : 's'}.`,
    /* Never "quitting cancels your render", because it does not: the
       render is on Kaggle either way. What quitting loses is this app
       following it. */
    detail: 'The render itself is on Kaggle either way — quitting does '
      + 'not cancel it. What quitting loses is BlendFleet following it: the '
      + 'frame counts stop advancing, nothing chimes when it finishes, and '
      + 'its frames wait on Kaggle until you open this again.',
    buttons: ['Keep running', 'Quit'],
    defaultId: 0,
    cancelId: 0,
    checkboxLabel: 'Remember my choice',
    checkboxChecked: false,
  });
  if (answer.checkboxChecked) {
    callBackend('setPreference', ['closeAction',
                                  answer.response === 0 ? '"background"'
                                                        : '"quit"']);
  }
  return answer.response === 0 ? hideToTray() : quitNow();
}

function hideToTray() {
  ensureTray();
  win.hide();
  if (!saidWhereItWent) {
    saidWhereItWent = true;
    /* A new tray icon usually lands in Windows' overflow chevron, so an
       app that simply vanished would read as closed. */
    tray.displayBalloon({
      title: 'BlendFleet is still running',
      content: 'It is following your render from here. Open it again from '
        + 'this icon — or quit from its menu.',
      icon: nativeImage.createFromPath(paths.icon),
    });
  }
}

function ensureTray() {
  if (tray) return tray;
  tray = new Tray(nativeImage.createFromPath(paths.icon));
  tray.setToolTip('BlendFleet');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Open BlendFleet', click: showFromTray },
    { type: 'separator' },
    { label: 'Quit', click: quitNow },
  ]));
  tray.on('double-click', showFromTray);
  return tray;
}

function showFromTray() {
  if (!win || win.isDestroyed()) return;
  win.show();
  win.focus();
  if (tray) {
    tray.destroy();
    tray = null;
  }
}

function quitNow() {
  quitting = true;
  /* Let the machine sleep again. Electron would drop the blocker with the
     process anyway; releasing it here means the one place that ends the app
     also ends everything the app was holding. */
  busy.clear();
  releaseSleep();
  if (tray) {
    tray.destroy();
    tray = null;
  }
  /* Closing the pipe is how the sidecar learns to stop -- see
     rpc/__main__. It is given a moment to unwind its own workers before
     the process is ended outright. */
  if (backend) {
    try {
      backend.stdin.end();
    } catch (e) { /* already gone */ }
    setTimeout(() => { if (backend) backend.kill(); }, 3000);
  }
  app.quit();
}

/* ---- dialogs the sidecar cannot show --------------------------------- */

async function saveImage(url) {
  const suggested = decodeURIComponent(path.basename(new URL(url).pathname))
    || 'frame.png';
  const answer = await dialog.showSaveDialog(win, {
    title: 'Save image',
    defaultPath: suggested,
  });
  if (answer.canceled || !answer.filePath) return null;
  /* The frame is already on disk -- previewFrame cached it -- so this is
     a copy, not a download. */
  try {
    fs.copyFileSync(decodeURIComponent(new URL(url).pathname).replace(
      /^\/([a-zA-Z]:)/, '$1'), answer.filePath);
  } catch (e) {
    console.error('could not save the image', e);
  }
  return answer.filePath;
}

ipcMain.handle('backend:call', (_event, message) =>
  callBackend(message.call, message.args));

ipcMain.handle('shell:pickBlend', async () => {
  const answer = await dialog.showOpenDialog(win, {
    title: 'Select .blend',
    filters: [{ name: 'Blender', extensions: ['blend'] }],
    properties: ['openFile'],
  });
  if (answer.canceled) return '';
  /* The scene joins the taskbar's Recent list. A .blend is exactly the
     kind of file somebody comes back to for a week of renders, and this is
     the OS's own memory of it rather than a second one kept here. */
  app.addRecentDocument(answer.filePaths[0]);
  return answer.filePaths[0];
});

ipcMain.handle('shell:pickBlends', async () => {
  const answer = await dialog.showOpenDialog(win, {
    title: 'Select .blend files',
    filters: [{ name: 'Blender', extensions: ['blend'] }],
    /* multiSelections is the whole point; the sidecar still checks each
       path, because a multi-select is how a .blend1 backup gets swept up. */
    properties: ['openFile', 'multiSelections'],
  });
  if (answer.canceled) return [];
  answer.filePaths.forEach(path => app.addRecentDocument(path));
  return answer.filePaths;
});

ipcMain.handle('shell:chooseDirectory', async () => {
  const answer = await dialog.showOpenDialog(win, {
    title: 'Save frames to',
    properties: ['openDirectory', 'createDirectory'],
  });
  return answer.canceled ? '' : answer.filePaths[0];
});

ipcMain.handle('shell:saveImage', (_event, url) => saveImage(url));
ipcMain.on('shell:minimize', () => win && win.minimize());
ipcMain.on('shell:toggleMaximize', () => {
  if (!win) return;
  win.isMaximized() ? win.unmaximize() : win.maximize();
});
ipcMain.on('shell:close', () => win && win.close());

/* ---- lifetime --------------------------------------------------------- */

/* One window, one sidecar. A second launch raises the first rather than
   starting a second backend that would poll the same accounts twice and
   write the same state file underneath it. */
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', showFromTray);

  app.whenReady().then(() => {
    /* A Content-Security-Policy, set as a HEADER rather than a <meta> in
       index.html, because the page belongs to both shells and this is
       true of only one of them. The dashboard loads nothing from a
       network: its fonts, styles and scripts are all beside it on disk,
       and the only images it shows are file:// frames out of the app's
       own cache and the data: URLs it makes itself. Saying so costs
       nothing and means a compromised payload cannot phone anywhere. */
    session.defaultSession.webRequest.onHeadersReceived((details, done) => {
      done({
        responseHeaders: {
          ...details.responseHeaders,
          'Content-Security-Policy': [
            "default-src 'self'; "
            + "img-src 'self' file: data:; "
            + "style-src 'self' 'unsafe-inline'; "
            + "font-src 'self' file:; "
            + "script-src 'self'; "
            + "connect-src 'none'",
          ],
        },
      });
    });
    startBackend();
    createWindow();

    /* Asked once at startup, because settingsChanged only fires when
       something CHANGES -- without this, a saved preference of "dark"
       would leave the native parts light until the user touched a
       setting. */
    callBackend('preferences').then(json => {
      if (json) matchShellTheme(JSON.parse(json));
    }).catch(e => {
      /* Caught, because the native theme is cosmetic and an unhandled
         rejection in the main process is a warning nobody reads. It was one
         of these that caught `nativeTheme` missing from the require above,
         so the noise was worth something once. */
      console.error('could not match the shell theme at startup', e);
    });

    /* BACK FROM SLEEP, ASK AT ONCE.
       The status poll is on a 30-second timer, so a lid opened after two
       hours shows two-hour-old readings until that timer next fires -- and
       everything on screen looks current. A render can easily have
       finished, failed, or been evicted in the meantime. */
    powerMonitor.on('resume', () => {
      console.error('resumed from sleep — polling now');
      callBackend('poll');
    });

    /* The taskbar right-click, for the two things worth reaching without
       the window: start a render, or go and get finished frames. Both open
       the app on the page that does it -- a jump list that performed a
       render invisibly, with no window to report it, would be a way to
       spend somebody's quota by accident. */
    if (process.platform === 'win32') {
      app.setUserTasks([
        {
          program: process.execPath,
          arguments: '--page=dashboard',
          title: 'Open the dashboard',
          description: 'Show the fleet and what it is rendering',
          iconPath: process.execPath,
          iconIndex: 0,
        },
        {
          program: process.execPath,
          arguments: '--page=files',
          title: 'Collect frames',
          description: 'Open Files, where finished renders are downloaded',
          iconPath: process.execPath,
          iconIndex: 0,
        },
      ]);
    }
  });

  app.on('window-all-closed', () => {
    /* Deliberately NOT app.quit(): hiding to the tray closes the last
       window, and quitting there is exactly what "keep running" must not
       do. quitNow() is the only way out. */
  });

  app.on('will-quit', () => {
    quitting = true;
    if (backend) backend.kill();
  });
}
