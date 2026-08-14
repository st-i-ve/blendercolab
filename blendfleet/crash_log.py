"""Make the next crash say something.

This app has vanished off the user's screen three times mid-render with no
message, no dialog and no file. Windows Event Viewer recorded the only
evidence there was: BEX64 in Qt6Core.dll, exception c0000409 sub-code 7 --
`__fastfail(FAST_FAIL_FATAL_APP_EXIT)`, which is Qt deliberately calling
abort() after printing a fatal message. Qt printed a diagnosis. Nobody
ever saw it, because `console=False` in the PyInstaller spec means the
process has no stderr to print to, and the app installed no message
handler, no faulthandler, no excepthook and no logging of any kind.

So this module is not a fix. It is the instrument that lets the NEXT
occurrence name itself. Four independent capture paths, because each one
catches a class of death the others miss:

  * qInstallMessageHandler -- the important one. Qt's own qWarning/
    qCritical/qFatal text, including the fatal line emitted immediately
    before the abort we are chasing.
  * sys.excepthook -- an unhandled Python exception on the main thread.
  * threading.excepthook -- the same on a background thread, which
    currently disappears in total silence.
  * faulthandler -- a genuine SIGSEGV/SIGABRT dumps every thread's Python
    stack. Note it will NOT catch the __fastfail above: fast-fail bypasses
    structured exception handling by design, which is exactly why the Qt
    message handler is the one that matters here.

Everything writes to one plain-text file per run under the user's own
data directory (see platform_paths.log_dir), never inside the PyInstaller
bundle -- a log that lives in the temp directory the bundle unpacks to
would be deleted by the very exit we are trying to explain.
"""
from __future__ import annotations

import datetime as dt
import faulthandler
import os
import platform
import sys
import threading
import traceback
from pathlib import Path

from blendfleet.platform_paths import log_dir

# How many previous runs to keep. A crash log is worthless if the user's
# instinctive "let me try launching it again" erases it, and the report
# that finally explains this bug may need two runs side by side -- the one
# that died and the one before it that did not.
KEEP_RUNS = 6

# Per-run and whole-directory ceilings. A Qt message handler is attached to
# a Chromium-hosting app that can emit thousands of lines a minute, so
# "just append forever" is a way to fill a laptop's disk.
MAX_RUN_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024

_FILENAME_PREFIX = "blendfleet-"
_FILENAME_SUFFIX = ".log"

_lock = threading.Lock()
_file = None            # held open for the process lifetime: faulthandler
_path: Path | None = None    # writes to this fd from a signal handler
_written = 0
_capped = False


def current_log_path() -> Path | None:
    """The file this run is writing to, or None before install()."""
    return _path


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def _write(text: str, *, critical: bool = False) -> None:
    """Append and flush immediately.

    Flushing every line is not paranoia here: the death we are chasing is
    abort(), which throws away anything still sitting in a userspace
    buffer. An unflushed log of a crash is an empty log of a crash.

    `critical=True` marks the lines that are the whole point of the file --
    fatal Qt messages and tracebacks. Those keep being written past the
    per-run size cap, where routine chatter gets dropped, up to a hard
    ceiling that still bounds the file.
    """
    global _written, _capped
    with _lock:
        if _file is None:
            return
        if _written >= MAX_RUN_BYTES * 2:
            return
        if _written >= MAX_RUN_BYTES and not critical:
            if not _capped:
                _capped = True
                _emit(
                    f"\n[log capped at {MAX_RUN_BYTES} bytes -- routine "
                    "messages are being dropped from here on. Errors and "
                    "fatal messages are still recorded.]\n")
            return
        _emit(text)


def _emit(text: str) -> None:
    """Raw append. Caller holds _lock and has already checked the caps."""
    global _written
    try:
        _file.write(text)
        _file.flush()
        _written += len(text)
    except (OSError, ValueError):
        # A logger that raises during a crash turns one problem into two,
        # and the exception would surface from inside a Qt message handler
        # where there is nothing sane to do with it.
        pass


def _stamp() -> str:
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def record(message: str, *, critical: bool = False) -> None:
    """Write one timestamped line. Safe to call before install() (no-op)."""
    _write(f"{_stamp()} {message}\n", critical=critical)


# ---------------------------------------------------------------------------
# rotation
# ---------------------------------------------------------------------------

def _run_logs(directory: Path) -> list[Path]:
    return sorted(directory.glob(f"{_FILENAME_PREFIX}*{_FILENAME_SUFFIX}"))


def prune(directory: Path, keep: Path | None = None) -> list[Path]:
    """Delete oldest logs until at most KEEP_RUNS remain and the directory
    is under MAX_TOTAL_BYTES. `keep` is never deleted.

    Filenames sort chronologically because they carry a zero-padded
    timestamp, so lexical order is age order -- no stat() per file, which
    matters because this runs on the startup path.

    Returns what was deleted, so a caller (and a test) can see it happen.
    """
    deleted: list[Path] = []
    candidates = [p for p in _run_logs(directory) if p != keep]

    while len(candidates) + (1 if keep else 0) > KEEP_RUNS:
        _unlink(candidates.pop(0), deleted)

    def total() -> int:
        files = candidates + ([keep] if keep else [])
        return sum(_size(p) for p in files)

    while candidates and total() > MAX_TOTAL_BYTES:
        _unlink(candidates.pop(0), deleted)
    return deleted


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _unlink(path: Path, deleted: list[Path]) -> None:
    try:
        path.unlink()
        deleted.append(path)
    except OSError:
        # Most likely another instance of the app has it open. Leaving one
        # stale file behind is better than failing to start.
        pass


# ---------------------------------------------------------------------------
# the handlers
# ---------------------------------------------------------------------------

def _install_excepthooks() -> None:
    previous = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        _write(f"\n{_stamp()} UNHANDLED EXCEPTION on the main thread\n"
               + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
               critical=True)
        previous(exc_type, exc_value, exc_tb)

    sys.excepthook = hook

    previous_thread_hook = threading.excepthook

    def thread_hook(args):
        # Without this, an exception in one of the render-progress or
        # upload threads is printed to a stderr that does not exist in the
        # packaged app and is then forgotten -- the thread just stops and
        # the UI waits forever for data that will never arrive.
        name = getattr(args.thread, "name", "?")
        _write(f"\n{_stamp()} UNHANDLED EXCEPTION on background thread {name!r}\n"
               + "".join(traceback.format_exception(
                   args.exc_type, args.exc_value, args.exc_traceback)),
               critical=True)
        previous_thread_hook(args)

    threading.excepthook = thread_hook


# Qt severities, lowest to highest. Kept as plain strings rather than the
# QtMsgType repr so a log line reads the same across Qt versions.
_QT_SEVERITY = {0: "DEBUG", 1: "WARNING", 2: "CRITICAL", 3: "FATAL", 4: "INFO"}
_QT_CRITICAL = {2, 3}

# qInstallMessageHandler hands the callable to C++. Keeping our own
# reference here means Python cannot collect it out from under Qt, which
# would turn the crash reporter into a second crash.
_qt_handler = None


def _qt_level(mode) -> int:
    """QtMsgType -> int, across the several shapes PySide6 has used for
    enums (plain int, IntEnum, shiboken enum with .value)."""
    try:
        return int(mode)
    except (TypeError, ValueError):
        return int(getattr(mode, "value", -1))


def qt_message_to_line(mode, context, message) -> str:
    """The exact text one Qt message contributes to the log."""
    level = _QT_SEVERITY.get(_qt_level(mode), f"LEVEL{_qt_level(mode)}")
    where = ""
    filename = getattr(context, "file", None)
    if filename:
        where = f"  [{filename}:{getattr(context, 'line', '?')}]"
    return f"{_stamp()} Qt {level}: {message}{where}\n"


def install_qt_message_handler() -> bool:
    """Route every Qt message into the log. Returns False if Qt is absent.

    This is the capture path that should finally name the mid-render
    crash: qFatal prints its reason through here and only THEN aborts, so
    the last line in the file will be the reason.
    """
    global _qt_handler
    try:
        from PySide6.QtCore import qInstallMessageHandler
    except ImportError:
        return False

    def handler(mode, context, message):
        try:
            _write(qt_message_to_line(mode, context, message),
                   critical=_qt_level(mode) in _QT_CRITICAL)
        except Exception:       # noqa: BLE001 -- see _emit's comment
            pass

    _qt_handler = handler
    qInstallMessageHandler(handler)
    return True


# ---------------------------------------------------------------------------
# the header
# ---------------------------------------------------------------------------

def _commit() -> str:
    """The checked-out commit, read straight out of .git rather than by
    shelling out to git -- this runs on the startup path we just spent a
    packaging change making fast, and a frozen build has no git anyway."""
    env = os.environ.get("BLENDFLEET_COMMIT")
    if env:
        return env.strip()
    head = Path(__file__).resolve().parents[1] / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            target = head.parent / ref[5:]
            return target.read_text(encoding="utf-8").strip()[:12]
        return ref[:12]
    except OSError:
        return "unknown"


def _header() -> str:
    from blendfleet import __version__

    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion
        qt = f"Qt {qVersion()} / PySide6 {pyside_version}"
    except ImportError:
        qt = "Qt unavailable"

    meipass = getattr(sys, "_MEIPASS", None)
    lines = [
        "=" * 78,
        f"BlendFleet {__version__} (commit {_commit()})",
        f"started  {dt.datetime.now().isoformat(timespec='seconds')}",
        f"{qt}",
        f"Python   {sys.version.split()[0]} on {platform.platform()}",
        f"exe      {sys.executable}",
        f"bundle   {meipass or 'not frozen (running from source)'}",
        f"pid      {os.getpid()}",
        "=" * 78,
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def install(directory: Path | None = None) -> Path:
    """Start logging this run, and return the file it writes to.

    Called as early as possible in the entry point -- before QApplication,
    so a failure constructing Qt itself is still captured.
    """
    global _file, _path, _written, _capped

    directory = Path(directory) if directory is not None else log_dir()
    directory.mkdir(parents=True, exist_ok=True)

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"{_FILENAME_PREFIX}{stamp}{_FILENAME_SUFFIX}"
    # Two launches inside the same second (a relaunch after a crash is
    # exactly that) must not share one file and interleave.
    counter = 1
    while path.exists():
        path = directory / f"{_FILENAME_PREFIX}{stamp}-{counter}{_FILENAME_SUFFIX}"
        counter += 1

    with _lock:
        _close_locked()
        # Line-buffered text, opened once and never closed: faulthandler
        # keeps this file descriptor and writes to it from a signal
        # handler, so closing it would leave a dangling fd for the one
        # moment it is needed.
        _file = open(path, "w", encoding="utf-8", errors="replace")
        _path = path
        _written = 0
        _capped = False
        _emit(_header())

    prune(directory, keep=path)
    faulthandler.enable(file=_file, all_threads=True)
    _register_dump_signal()
    install_qt_message_handler()
    _install_excepthooks()
    return path


def _register_dump_signal() -> None:
    """On Linux, `kill -USR1 <pid>` dumps every thread's stack into the log.

    The Windows build has no equivalent (faulthandler.register is
    POSIX-only), hence the guard -- but on Linux it turns "the app is
    frozen" into a question that can be answered without a debugger.
    """
    if not hasattr(faulthandler, "register"):
        return
    try:
        import signal
        faulthandler.register(signal.SIGUSR1, file=_file, all_threads=True,
                              chain=True)
    except (AttributeError, OSError, ValueError):
        pass


def announce() -> str:
    """Tell whoever is watching where this run's log is, and return the
    same text so a caller can show it somewhere else too.

    Printed to stderr when there is one -- a windowed PyInstaller build has
    sys.stdout and sys.stderr set to None, and printing to None raises,
    which would mean the crash reporter itself crashed the app on line one.
    """
    message = (
        f"BlendFleet is writing a diagnostic log for this run to:\n"
        f"    {_path}\n"
        "If the app closes unexpectedly, that file records why -- send it "
        "along with the report.")
    stream = sys.stderr or sys.stdout
    if stream is not None:
        try:
            print(message, file=stream, flush=True)
        except (OSError, ValueError):
            pass
    return message


def shutdown() -> None:
    """Stop logging and release the file.

    A real run never calls this -- the file staying open until the process
    dies is the entire point, since the interesting deaths are the ones
    that skip every orderly shutdown path. It exists so the test suite can
    unwind an install() instead of leaving faulthandler pointed at a
    deleted tmp file for the rest of the session.
    """
    global _path
    with _lock:
        _close_locked()
        _path = None


def _close_locked() -> None:
    global _file
    if _file is not None:
        try:
            faulthandler.disable()
            _file.close()
        except (OSError, ValueError):
            pass
        _file = None
