"""The crash log has to survive the crash.

This app has closed itself mid-render three times leaving nothing behind
but a Windows Event Viewer line. blendfleet/crash_log.py exists so the
next occurrence explains itself, which means these tests care about
exactly three things: that a file appears where a human can find it, that
it cannot grow or multiply without bound, and that each capture path
installs and writes without raising.

Deliberately NOT tested: a real abort()/SIGSEGV. Provoking one would take
the pytest process down with it.
"""
from __future__ import annotations

import faulthandler
import sys
import threading

import pytest

from blendfleet import crash_log


@pytest.fixture(autouse=True)
def restore_process_wide_hooks():
    """install() deliberately mutates process-global state -- sys.excepthook,
    threading.excepthook, Qt's message handler, faulthandler's target fd.
    Left in place, a later test in the same session would be reporting into
    a tmp file this one already deleted."""
    saved_excepthook = sys.excepthook
    saved_thread_hook = threading.excepthook
    saved_faulthandler = faulthandler.is_enabled()
    yield
    crash_log.shutdown()
    sys.excepthook = saved_excepthook
    threading.excepthook = saved_thread_hook
    try:
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(None)        # None restores Qt's default
    except ImportError:
        pass
    if saved_faulthandler:
        faulthandler.enable()
    else:
        faulthandler.disable()


def _text(path):
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# where the file goes
# ---------------------------------------------------------------------------

def test_install_creates_a_log_in_the_given_directory(tmp_path):
    path = crash_log.install(tmp_path)
    assert path.parent == tmp_path
    assert path.exists()
    assert crash_log.current_log_path() == path


def test_install_defaults_to_the_app_log_dir(tmp_path):
    """No argument means platform_paths.log_dir() -- outside the
    PyInstaller bundle, so it outlives the process that wrote it.
    (conftest's redirect_app_dirs points log_dir at a tmp path here.)"""
    from blendfleet.platform_paths import log_dir

    path = crash_log.install()
    assert path.parent == log_dir()


def test_header_names_the_build_qt_and_the_start_time(tmp_path):
    from blendfleet import __version__

    path = crash_log.install(tmp_path)
    body = _text(path)
    assert __version__ in body, "a log that cannot name its build is unactionable"
    assert "commit" in body
    assert "Qt " in body or "Qt unavailable" in body
    assert "started" in body
    assert "pid" in body


def test_back_to_back_installs_never_share_a_file(tmp_path):
    """Two launches inside one second -- a relaunch straight after a crash
    is exactly that -- must not interleave into one file."""
    paths = [crash_log.install(tmp_path) for _ in range(4)]
    assert len(set(paths)) == 4
    assert all(p.exists() for p in paths)


# ---------------------------------------------------------------------------
# rotation: the next launch must not erase the evidence
# ---------------------------------------------------------------------------

def test_previous_runs_survive_the_next_launch(tmp_path):
    older = crash_log.install(tmp_path)
    crash_log.install(tmp_path)
    assert older.exists(), (
        "relaunching after a crash is the user's first instinct -- it must "
        "not delete the log of the crash")


def test_rotation_keeps_only_the_last_few_runs(tmp_path):
    for i in range(crash_log.KEEP_RUNS + 8):
        (tmp_path / f"blendfleet-20260101-{i:06d}.log").write_text("x")

    crash_log.install(tmp_path)
    remaining = sorted(tmp_path.glob("blendfleet-*.log"))
    assert len(remaining) == crash_log.KEEP_RUNS


def test_rotation_drops_oldest_first(tmp_path):
    for i in range(crash_log.KEEP_RUNS + 3):
        (tmp_path / f"blendfleet-20260101-{i:06d}.log").write_text("x")

    crash_log.install(tmp_path)
    names = sorted(p.name for p in tmp_path.glob("blendfleet-*.log"))
    assert "blendfleet-20260101-000000.log" not in names
    assert "blendfleet-20260101-000008.log" in names


def test_rotation_caps_total_bytes_even_below_the_run_count(tmp_path):
    """Three enormous logs are still too much disk, even though three is
    fewer than KEEP_RUNS."""
    big = "z" * (crash_log.MAX_TOTAL_BYTES // 2)
    for i in range(3):
        (tmp_path / f"blendfleet-20260101-{i:06d}.log").write_text(big)

    crash_log.install(tmp_path)
    total = sum(p.stat().st_size for p in tmp_path.glob("blendfleet-*.log"))
    assert total <= crash_log.MAX_TOTAL_BYTES


def test_prune_never_deletes_the_current_run(tmp_path):
    current = tmp_path / "blendfleet-20260101-000000.log"
    current.write_text("y" * (crash_log.MAX_TOTAL_BYTES + 1))
    for i in range(1, 4):
        (tmp_path / f"blendfleet-20260101-{i:06d}.log").write_text("y")

    crash_log.prune(tmp_path, keep=current)
    assert current.exists()


def test_prune_survives_a_file_it_cannot_delete(tmp_path, monkeypatch):
    """A second instance holding a log open must not stop this one
    starting."""
    for i in range(crash_log.KEEP_RUNS + 4):
        (tmp_path / f"blendfleet-20260101-{i:06d}.log").write_text("x")

    def refuse(self, *a, **kw):
        raise PermissionError("held open by another process")

    monkeypatch.setattr(crash_log.Path, "unlink", refuse)
    assert crash_log.prune(tmp_path) == []


# ---------------------------------------------------------------------------
# per-run size cap
# ---------------------------------------------------------------------------

def test_one_run_cannot_grow_without_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(crash_log, "MAX_RUN_BYTES", 2000)
    path = crash_log.install(tmp_path)

    for i in range(5000):
        crash_log.record(f"routine chatter {i}")

    assert path.stat().st_size < 2000 * 3
    assert "log capped" in _text(path)


def test_errors_still_reach_a_capped_log(tmp_path, monkeypatch):
    """Dropping routine chatter is fine. Dropping the line that names the
    crash would defeat the whole file."""
    monkeypatch.setattr(crash_log, "MAX_RUN_BYTES", 2000)
    path = crash_log.install(tmp_path)

    for i in range(3000):
        crash_log.record(f"routine chatter {i}")
    crash_log.record("QWidget: Must construct a QApplication first",
                     critical=True)

    assert "Must construct a QApplication first" in _text(path)


def test_sharing_narration_cannot_evict_the_crash_report(tmp_path, monkeypatch):
    """The sharing path (fleet.prepare_dataset) now narrates every step for
    every account. That is a lot more routine volume than this file used to
    carry, and the one thing it must never do is push the line explaining a
    crash off the end -- so the routine cap absorbs it and the hard ceiling
    still bounds the file."""
    monkeypatch.setattr(crash_log, "MAX_RUN_BYTES", 2000)
    path = crash_log.install(tmp_path)

    for i in range(4000):
        crash_log.record(
            f"share user_0/scene-blend: a{i} (user_{i}): step 3/4 reachable "
            "-- yes, in 41 ms")
    crash_log.record("UNHANDLED EXCEPTION on the main thread", critical=True)

    body = _text(path)
    assert "UNHANDLED EXCEPTION on the main thread" in body
    assert "log capped" in body
    assert path.stat().st_size < crash_log.MAX_RUN_BYTES * 2 + 500
    assert sum(p.stat().st_size for p in tmp_path.glob("blendfleet-*.log"))         <= crash_log.MAX_TOTAL_BYTES


def test_writing_before_install_is_a_no_op(tmp_path):
    crash_log.shutdown()
    crash_log.record("nobody is listening yet")     # must not raise


# ---------------------------------------------------------------------------
# the capture paths
# ---------------------------------------------------------------------------

def test_faulthandler_is_enabled_after_install(tmp_path):
    crash_log.install(tmp_path)
    assert faulthandler.is_enabled()


def test_qt_message_handler_installs_and_records(tmp_path):
    from PySide6.QtCore import qWarning

    path = crash_log.install(tmp_path)
    assert crash_log.install_qt_message_handler() is True
    qWarning("a Qt warning that must not vanish")
    assert "a Qt warning that must not vanish" in _text(path)


def test_qt_fatal_messages_are_marked_critical(tmp_path):
    """qFatal prints through the handler and only THEN aborts, so this
    line is what the packaged app's silent death will finally leave
    behind -- it must survive the routine-message cap."""
    line = crash_log.qt_message_to_line(3, None, "the reason it aborted")
    assert "Qt FATAL" in line
    assert "the reason it aborted" in line


def test_qt_message_line_includes_source_location_when_qt_gives_one(tmp_path):
    class Context:
        file = "qwidget.cpp"
        line = 1234

    assert "[qwidget.cpp:1234]" in crash_log.qt_message_to_line(
        2, Context(), "boom")


def test_unhandled_main_thread_exception_is_recorded(tmp_path):
    path = crash_log.install(tmp_path)
    try:
        raise RuntimeError("main thread blew up")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())

    body = _text(path)
    assert "UNHANDLED EXCEPTION on the main thread" in body
    assert "main thread blew up" in body


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_unhandled_background_thread_exception_is_recorded(tmp_path):
    """Currently these vanish entirely: the thread dies, the UI keeps
    waiting for data that will never arrive, and nothing is written."""
    path = crash_log.install(tmp_path)

    def explode():
        raise RuntimeError("stream thread blew up")

    worker = threading.Thread(target=explode, name="stream-worker")
    worker.start()
    worker.join()

    body = _text(path)
    assert "UNHANDLED EXCEPTION on background thread 'stream-worker'" in body
    assert "stream thread blew up" in body


def test_a_handler_that_hits_a_closed_file_does_not_raise(tmp_path):
    crash_log.install(tmp_path)
    crash_log.shutdown()
    crash_log.record("after the file is gone", critical=True)   # must not raise


# ---------------------------------------------------------------------------
# telling the user where it is
# ---------------------------------------------------------------------------

def test_announce_names_the_path_and_what_to_do_with_it(tmp_path):
    path = crash_log.install(tmp_path)
    message = crash_log.announce()
    assert str(path) in message
    assert "closes unexpectedly" in message


def test_announce_survives_a_windowed_build_with_no_stdio(tmp_path,
                                                          monkeypatch):
    """PyInstaller's console=False sets sys.stdout and sys.stderr to None,
    and print(file=None) raises -- the crash reporter must not be the thing
    that crashes the app on line one."""
    crash_log.install(tmp_path)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdout", None)
    assert crash_log.announce()


def test_bridge_reports_the_log_location_to_the_page(tmp_path):
    """Settings shows this, so a user whose app vanished can find the file
    without being read a %APPDATA% path over the phone."""
    import json

    # Called unbound, with any object for self, because this answer comes
    # from the log module rather than from any adapter state. Since the
    # 2026-08-17 collapse the method lives on Session; the Qt Backend's
    # slot of the same name forwards to exactly this.
    from blendfleet.rpc.session import Session

    path = crash_log.install(tmp_path)
    payload = json.loads(Session.diagnostics(object()))
    assert payload["logFile"] == str(path)
    assert payload["logDir"]
