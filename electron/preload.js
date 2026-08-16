/* The page's `backend`, rebuilt on this side of the pipe.
 *
 * blendfleet/web/app.js was written against QWebChannel, where `backend`
 * is an object whose methods you call and whose signals you .connect()
 * to. Nothing in those 5,700 lines knows or cares what is behind that
 * object -- so this hands it the same shape, and the page runs unchanged.
 * That is the whole reason an Electron variation was affordable at all.
 *
 * The two halves of the shape:
 *
 *   backend.state(cb)        a call. QWebChannel passes the answer to a
 *                            trailing callback, so that is what happens
 *                            here too -- and the same call also returns a
 *                            promise, which is friendlier and which
 *                            nothing existing relies on.
 *   backend.stateChanged     an event, with .connect(fn).
 *
 * Native UI is NOT the sidecar's job (it is headless and has no window to
 * parent a dialog to), so pickBlend and collect are completed here: show
 * the shell's own dialog, then call the sidecar with the answer. From the
 * page's side they behave exactly as they always did.
 */
const { contextBridge, ipcRenderer } = require('electron');

/* Every method the page may call. Listed rather than proxied blindly:
   a typo in app.js should fail loudly here, not travel down the pipe to
   be refused by a process that cannot say which line sent it. */
const CALLS = [
  'accounts', 'addAccount', 'blenderVersions', 'cancelAll', 'cancelInstance',
  'cancelJob', 'checkHardware', 'checkOutputs', 'collect', 'deleteScene',
  'diagnostics', 'estimateRender', 'forgetJob', 'forgetUnreadableJob',
  'health', 'launch', 'outputs', 'poll', 'preferences', 'previewFrame',
  'ready', 'refreshQuota', 'removeAccount', 'renderScene', 'scenes',
  'sendJob', 'setBlend', 'setPreference', 'setUsername', 'startInstances',
  'state', 'syncDataset',
];

/* Every event it may listen for. Same list as rpc/protocol.EVENTS, and
   the parity test in tests/test_rpc_session.py is what keeps them equal. */
const EVENTS = [
  'stateChanged', 'accountsChanged', 'settingsChanged', 'telemetry',
  'uploadProgress', 'downloadProgress', 'framePreview', 'logLine',
  'notification', 'healthChanged', 'busyChanged', 'scenesChanged',
  'outputsChanged', 'collectFinished',
];

const handlers = {};
EVENTS.forEach(name => { handlers[name] = []; });

ipcRenderer.on('backend:event', (_event, message) => {
  (handlers[message.event] || []).forEach(fn => {
    try {
      fn(...(message.args || []));
    } catch (e) {
      /* One bad listener must not stop the render's progress reaching
         the rest of the page -- the same rule rpc/emitter.py keeps on
         the other side of the pipe. */
      console.error(`a handler for ${message.event} raised`, e);
    }
  });
});

function call(name, args) {
  return ipcRenderer.invoke('backend:call', { call: name, args });
}

const backend = {};

CALLS.forEach(name => {
  backend[name] = (...args) => {
    /* QWebChannel's own calling convention: a trailing function is the
       callback for the answer, not an argument for the method. app.js
       uses it for state(), health(), preferences(), diagnostics() and
       blenderVersions(). */
    let callback = null;
    if (typeof args[args.length - 1] === 'function') callback = args.pop();
    const answer = call(name, args);
    if (callback) answer.then(result => callback(result));
    return answer;
  };
});

EVENTS.forEach(name => {
  backend[name] = {
    connect: fn => handlers[name].push(fn),
    disconnect: fn => {
      const at = handlers[name].indexOf(fn);
      if (at >= 0) handlers[name].splice(at, 1);
    },
  };
});

/* ---- the two calls that need a window ---------------------------------
   The page still calls backend.pickBlend() and backend.collect(); it is
   this side that knows a dialog is involved. */
backend.pickBlend = async (...args) => {
  let callback = null;
  if (typeof args[args.length - 1] === 'function') callback = args.pop();
  const chosen = await ipcRenderer.invoke('shell:pickBlend');
  const answer = await call('setBlend', [chosen || '']);
  if (callback) callback(answer);
  return answer;
};

backend.collect = async (label = '', jobId = '') => {
  const destination = await ipcRenderer.invoke('shell:chooseDirectory');
  /* A dismissed chooser is not a collect. The sidecar refuses an empty
     destination too, but asking it to would put a pointless round trip
     between the click and nothing happening. */
  if (!destination) return null;
  return call('collect', [label, jobId, destination]);
};

contextBridge.exposeInMainWorld('backend', backend);

/* The shell's own affordances, for the parts of the app that are the
   WINDOW rather than the render: the title bar's buttons and the frame
   preview's Save image. */
contextBridge.exposeInMainWorld('shell', {
  minimize: () => ipcRenderer.send('shell:minimize'),
  toggleMaximize: () => ipcRenderer.send('shell:toggleMaximize'),
  close: () => ipcRenderer.send('shell:close'),
  saveImage: url => ipcRenderer.invoke('shell:saveImage', url),
});

/* The page is told which shell it is in, and that is all this does with
   the DOM. The stylesheet that goes with the class (electron/shell.css:
   the accent ground and the drag region) is injected by the main process
   with insertCSS -- a preload is sandboxed and has no `fs`, and pushing
   that boundary open to read one file would be a poor trade. */
window.addEventListener('DOMContentLoaded', () => {
  document.documentElement.classList.add('electron');
});
