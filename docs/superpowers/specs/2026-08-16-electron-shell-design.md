# An Electron shell for BlendFleet

*2026-08-16*

## What this is

BlendFleet's dashboard is HTML, CSS and JavaScript rendered inside a Qt
window (`ui/web_host.py`) and talking to Python through QWebChannel. This
replaces the Qt window with an Electron one, and the QWebChannel bridge
with a JSON-RPC sidecar, so the same dashboard runs on a shell that is
easier to build for Windows, Linux and macOS.

Two facts make it affordable, and both were verified before this was
written:

- **The render core is already Qt-free.** `fleet.py`, `kaggle_client.py`,
  `uploader.py`, `log_stream.py`, `collector.py`, `notebook_builder.py`,
  `settings.py`, `accounts.py`, `sharing.py` import no PySide6. Qt
  appears only in the two entry points, `crash_log.py`, and `ui/`.
- **The page talks to exactly one object.** `backend`, with 35 callable
  methods and 16 signals, every payload a JSON string. Nothing else in
  the dashboard knows Qt exists.

So this is a second *adapter* over an unchanged core, not a rewrite. The
dashboard ships unchanged.

## What it is not

Not smaller, and not faster. Electron plus a Python sidecar is a larger
download than Qt plus Python. What it buys is a shell that builds for
three platforms without Qt, a better development loop, and an installer
story.

## Rules this design is held to

1. **The current app does not change.** `ui/bridge.py` is not edited.
   Not one line. The Qt build keeps working, and its 1391 tests keep
   passing, throughout.
2. **`dist/blendfleetweb/` is never written to.** Electron output goes to
   `dist/electron/`, the sidecar to `dist/backend/<platform>/`.
3. **The dashboard is not edited for Electron.** If `blendfleet/web/`
   needs a change to work under Electron, that is a bug in the adapter,
   not a job for the page -- with one deliberate exception, noted under
   *The accent ground* below.
4. **No new honesty.** Every payload the page receives means exactly what
   it means today: a cached reading still says how old it is, an unknown
   count still refuses to draw a bar. The adapter is a transport, not a
   place to reinterpret.

## Cost of rule 1

The payload-shaping logic in `bridge.py` -- which is where the honesty
rules live -- is duplicated into `rpc/session.py` rather than shared,
because sharing it means editing `bridge.py`. Until the Qt shell is
retired, a fix in one has to be made in the other.

That is a real cost, accepted deliberately: a subtle threading regression
in the app being used for real renders is worse than visible, temporary
duplication. The two collapse into one adapter as the last step of stage
2, when Electron is proven and the fallback is obvious rather than
urgent.

---

## Stage 1 -- the sidecar and its protocol

A new package, `blendfleet/rpc/`, with no Qt import anywhere in it.

### `emitter.py`

`Emitter` -- `connect(fn)`, `disconnect(fn)`, `emit(*args)`. Thread-safe
(a lock around the handler list, handlers called outside it). This is
what replaces `Signal`. Handlers run on the calling thread; the sidecar
serialises everything through one writer, so no handler ever races
another.

### `session.py`

`Session` owns the 35 methods and 16 events, ported from `bridge.py` with
Qt removed:

| Qt | replacement |
|---|---|
| `QObject` / `@Slot` | plain methods |
| `Signal` | `Emitter` |
| `QThread` worker | `concurrent.futures.ThreadPoolExecutor`, one future per key, keyed exactly as `_start` keys today |
| `QTimer` poll | a `threading.Timer` chain that can be cancelled |
| `QApplication.instance()` | nothing -- theme application is the shell's job |

`_start(key, fn, action, on_ok)` keeps its contract exactly: a second
call under a live key is skipped, not queued, and `busyChanged` is
emitted true then false around it. That contract is what the page's
budget accounting and the preview modal both read.

### `protocol.py`

Newline-delimited JSON, one object per line, both directions.

```jsonc
// page -> sidecar
{"id": 7, "call": "launch", "args": ["{\"startFrame\":1}"]}
// sidecar -> page, in reply
{"id": 7, "ok": true, "result": null}
{"id": 7, "ok": false, "error": "no machines selected"}
// sidecar -> page, unsolicited
{"event": "stateChanged", "args": ["{...}"]}
```

Rules: an unknown `call` answers `ok: false` rather than closing the
pipe; an exception inside a method answers `ok: false` with the message
scrubbed of tokens (`_tokenless`, as today); `id` is echoed untouched; a
line that will not parse is answered with `ok: false, id: null` and the
stream continues.

### `__main__.py`

Reads stdin until EOF, dispatches on a single thread, writes replies and
events to stdout through one lock. Stdout carries protocol only --
every log line goes to the crash-log file, never to the pipe. **EOF on
stdin means the parent is gone: stop the session and exit**, which is
what stops an orphaned sidecar outliving the window.

### Stage 1 is done when

`echo '{"id":1,"call":"state"}' | python -m blendfleet.rpc` prints the
real fleet state, and the new tests pass beside the existing 1391.

### Tests

`tests/test_rpc_protocol.py` -- framing, unknown method, exception
scrubbing, malformed line, id echo, EOF exit.
`tests/test_rpc_session.py` -- the payload parity that matters: for a
seeded fleet, `Session.state()` and `Backend.state()` return the same
JSON. That test is the whole defence against duplication drift, and it
runs against both adapters until one of them is deleted.

---

## Stage 2 -- the Electron shell

`electron/` at the repo root: `main.js`, `preload.js`, `package.json`.

### The bridge shim

`preload.js` exposes `window.backend` through `contextBridge` with the
same shape QWebChannel gave it: every method a function returning a
promise, every signal an object with `.connect(fn)`. The page's own
`backend.state(cb)` callback style is preserved by the shim, so
`app.js` is untouched.

`main.js` spawns the sidecar, matches replies by `id`, and forwards
events to the renderer. If the sidecar dies, the window says so through
the app's own notification channel rather than silently going inert.

### Window parity

| today (Qt) | Electron |
|---|---|
| frameless + our own title bar | `frame: false`, our title bar is already HTML |
| Mica backdrop | `backgroundMaterial: 'mica'` (Windows 11) |
| painted accent ground | back to CSS -- see below |
| tray + keep-running on close | `Tray`, `dialog.showMessageBox`, `close` event |
| frame context menu | `Menu.buildFromTemplate` on `context-menu` |
| native .blend picker | `dialog.showOpenDialog` |
| save image / save zip | `dialog.showSaveDialog` |
| crash log | `app.on('render-process-gone')` + the sidecar's own log |
| single instance | `app.requestSingleInstanceLock()` |

### The accent ground

The wash is painted by the Qt window today because the title bar sits
*outside* the web view, and a wash that starts at the view's top edge
leaves a seam across the window. Under Electron there is no strip outside
the page: the whole window is the page. So the wash returns to CSS as
`body::before`, exactly where it began, and the seam cannot occur.

This is the one edit to `blendfleet/web/` this project makes, and it is
additive -- the rule is restored, not changed, and the Qt build keeps
painting its own ground underneath a page that no longer draws one.
Both shells are checked for the seam before this is called done.

### Stage 2 is done when

The dashboard runs under Electron with a real fleet: cards update, a
render launches, the log streams, a frame previews, the tray keeps it
running.

---

## Stage 3 -- packaging

- `electron-builder` config in `electron/package.json`, output to
  `dist/electron/`.
- The sidecar built by PyInstaller (`packaging/blendfleet-backend.spec`,
  console, no Qt) to `dist/backend/<platform>/`, and bundled as an
  `extraResource`.
- Windows: NSIS installer. Linux: AppImage. macOS: dmg.
- **PyInstaller cannot cross-compile.** Each platform's sidecar has to be
  built on that platform, so this stage delivers a GitHub Actions matrix
  that does it, plus documented local commands. Artifacts for Linux and
  macOS come from CI, not from this machine.

### Stage 3 is done when

`npm run dist` on Windows produces an installer that installs, launches,
renders, and uninstalls -- and the workflow file builds the other two.

---

## Risks, named

**Duplication drift** (stage 1). Two copies of the payload logic. Guarded
by the parity test above, and time-boxed to the life of the Qt shell.

**Sidecar lifetime** (stage 2). An orphaned Python process holding a
render's state is the worst failure here. Three guards: EOF on stdin
exits, Electron kills the child on `will-quit`, and the single-instance
lock stops a second window adopting a first sidecar.

**Token safety** (stage 1). The pipe carries Kaggle API tokens in
`addAccount`. It is a pipe between two processes owned by the same user,
never a socket -- that is why stdio was chosen over a localhost server --
and error strings stay scrubbed by `_tokenless`.

**Size.** Electron (~150 MB) plus sidecar (~200 MB) against today's
~200 MB. Named here so it is not discovered at install time.
