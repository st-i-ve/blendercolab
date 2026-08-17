"""Suite-wide safety nets.

Five of them, all autouse, all there for the same reason: the merge gate
must be deterministic and must never hang, touch the network, or touch
the user's real data.

The first two guards' bug was real. tests/test_dashboard.py's
launch-success test did not stub `stream_progress`, so Dashboard's
`_start_progress_threads` spawned real daemon threads that opened real
HTTPS connections to kaggle.com with fake `KGAT_000…` tokens. Nothing
joined them, so they outlived the test module and were still inside
`ssl.do_handshake` while a later module ran -- 2 of 14 otherwise clean runs
died with `Fatal Python error: Aborted`. A test that only *sometimes*
reaches the network is worse than one that always does, because it passes
in review and fails in CI, so these guards make both failure modes loud and
immediate rather than probabilistic.

The third guard's bug was also real, and more recent (Task 5's fix round
1): a previously-always-succeeding dashboard test started failing once a
new pre-launch check was added, its `QMessageBox.critical` failure path
was never stubbed, and a REAL modal dialog under
QT_QPA_PLATFORM=offscreen has no click to dismiss it -- the suite hung
for 10+ minutes instead of reporting one failing test. Only 3 of that
module's ~20 launch tests stubbed message boxes; the other ~17 relied on
the success path never failing. `no_unstubbed_dialogs` below turns the
NEXT such gap into an immediate, named assertion failure instead of a
repeat of that hang.

The fourth and fifth guards exist because of a fourth real bug, found
during Task 5's own triage rather than caused by it: tests/test_dashboard.py's
`_seed_job` helper drives a real `Fleet.launch()` -> `Fleet._save()` ->
`state_dir()/"fleet.json"`, and that module's per-test isolation happened
to cover every OTHER call site but not that one reliably enough -- a
suite run left a synthetic `alpha.blend` job sitting in the user's REAL
`%APPDATA%\\BlendFleet\\state\\fleet.json`, overwriting the only record of
which Kaggle kernels this app had actually started (real jobs, still
billing someone's GPU quota, become uncancellable and uncollectable from
the app the moment that file no longer names them). `redirect_app_dirs`
fixes the cause; `guard_real_app_dir_untouched` fails the whole session,
loudly, if any test -- this one or a future one -- ever does it again.

That guard then produced its own real bug: it cannot distinguish "a test
wrote here" from "the user's own BlendFleet app wrote here", and the app
legitimately rewrites state/fleet.json and state/instance_state.json
every 30s while it runs. Whenever the user had the app open while running
this suite -- exactly when someone is testing a build -- the guard failed
and blamed the tests: three false failures in one working session, each
one investigated as if it were real, before this fixture learned to check
for a live BlendFleet process first and only fail outright when none is
running.

That fix still had a race, and it produced a fourth false failure the
same day: the app was running mid-session -- it rewrote fleet.json on its
own 30s poll timer at 17:04:37 -- but had already exited by the time the
guard sampled `tasklist` at teardown, so the process check found nothing
and the guard took the hard-failure branch anyway, blaming the tests for
the app's own write. It was only provably a false alarm because the
file's contents were the user's real jobs (real .blend filenames, real
Kaggle usernames -- nothing this suite generates) and a later, unrelated
subset run left the file's mtime untouched. The fix: the verdict is now
based on whether a live app was seen at ANY point during the session --
sampled at session setup, at session teardown, and periodically while
tests run (see `_AppSeenTracker` and `pytest_runtest_teardown` below) --
instead of only "is it running right now, at the exact instant teardown
happens to check". Four false failures in one working session, each
chased down as if it were a real regression, is the bar this guard now
has to clear: one more and nobody will trust it, which is worse than not
having it at all.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import warnings

import pytest
from PySide6.QtWidgets import QMessageBox

import blendfleet.accounts as accounts_mod
import blendfleet.crash_log as crash_log_mod
import blendfleet.fleet as fleet_mod
import blendfleet.instance_state as instance_state_mod
import blendfleet.platform_paths as platform_paths_mod
import blendfleet.settings as settings_mod
import blendfleet.ui.bridge as bridge_mod


class NetworkAccessInTestError(RuntimeError):
    """A test tried to open a socket."""


class LeakedThreadError(RuntimeError):
    """A test left a thread running past its own scope."""


class UnstubbedDialogError(RuntimeError):
    """A test reached a real QMessageBox instead of stubbing it.

    Raised instead of letting the dialog actually show: under
    QT_QPA_PLATFORM=offscreen a real modal dialog blocks forever waiting
    for a click nothing will ever deliver, which is a hang, not a failure
    -- see this module's docstring.
    """


# How long a thread started during a test may take to unwind after the test
# body returns before it counts as leaked. Generous enough for a QThread's
# finished-signal bookkeeping, short enough that a genuinely stuck network
# thread is caught rather than waited on.
_LEAK_GRACE_SECONDS = 5.0
_LEAK_POLL_SECONDS = 0.05


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Make every outbound socket connection raise, loudly and by name.

    Blocks at the socket layer rather than at `requests`/`kagglesdk` on
    purpose: it then holds for any library any future test happens to
    reach for, and it names the address so the offending call is obvious
    instead of surfacing as a timeout somewhere unrelated.

    Note this blocks *connecting*, not the socket module itself -- Qt does
    its own networking in C++ and never goes through here, and pytest's own
    machinery does not open sockets in this suite.
    """
    def blocked(*args, **kwargs):
        address = args[1] if len(args) > 1 else kwargs.get("address", "?")
        raise NetworkAccessInTestError(
            f"a test tried to open a network connection to {address!r}. "
            "No test in this suite may touch the network: stub the call "
            "(see tests/test_dashboard.py's stubbed stream_progress) rather "
            "than letting it reach kaggle.com.")

    def blocked_create_connection(address=None, *args, **kwargs):
        raise NetworkAccessInTestError(
            f"a test tried to open a network connection to {address!r}. "
            "No test in this suite may touch the network.")

    def blocked_getaddrinfo(host=None, *args, **kwargs):
        # DNS is network traffic too, and blocking it here means a stray
        # call fails before any packet leaves the machine rather than after
        # a resolver round trip.
        raise NetworkAccessInTestError(
            f"a test tried to resolve the hostname {host!r}. No test in "
            "this suite may touch the network.")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", blocked_getaddrinfo)
    yield


@pytest.fixture(autouse=True)
def no_leaked_threads():
    """Fail a test that leaves a thread of its own still running.

    "daemon=True" is not a substitute for joining: a daemon thread inside
    OpenSSL when the interpreter starts tearing down is exactly what
    produced `Fatal Python error: Aborted`. Threads are identified by
    ident, and threading._DummyThread instances are ignored -- those are
    the placeholder objects the threading module fabricates for threads it
    did not create (a PySide6 QThread that calls into Python), not
    something a test can join.
    """
    def real_threads():
        return [t for t in threading.enumerate()
                if not isinstance(t, threading._DummyThread)]

    before = {t.ident for t in real_threads()}
    yield

    deadline = time.monotonic() + _LEAK_GRACE_SECONDS
    leaked: list[threading.Thread] = []
    while True:
        leaked = [t for t in real_threads()
                  if t.ident not in before and t.is_alive()]
        if not leaked or time.monotonic() > deadline:
            break
        time.sleep(_LEAK_POLL_SECONDS)

    if leaked:
        names = ", ".join(f"{t.name} (daemon={t.daemon})" for t in leaked)
        raise LeakedThreadError(
            f"{len(leaked)} thread(s) were still running when the test "
            f"returned: {names}. Every thread a test starts must be joined "
            "or stopped inside that test -- one still in flight during the "
            "next module's teardown is how this suite used to abort.")


_DIALOG_KINDS = ("critical", "warning", "information", "question")


@pytest.fixture(autouse=True)
def no_unstubbed_dialogs(request, monkeypatch):
    """Fail immediately, by name, if a real QMessageBox is about to show.

    Patches the QMessageBox class itself (not a particular module's import
    of it), so it holds regardless of which module reaches for
    critical/warning/information/question -- same reasoning as no_network
    blocking at the socket layer rather than at one caller.

    A test that legitimately expects a dialog opts in with the
    stub_message_boxes fixture below, which overrides these guards for
    that test only (fixtures share one monkeypatch, so the later
    monkeypatch.setattr wins and is unwound first when the test ends).
    """
    test_name = request.node.name

    def make_guard(kind):
        def guard(*args, **kwargs):
            raise UnstubbedDialogError(
                f"{test_name} triggered a real QMessageBox.{kind}(...) "
                "without stubbing it. A real modal dialog blocks forever "
                "under QT_QPA_PLATFORM=offscreen -- there is no click to "
                "dismiss it, so this would hang the whole suite instead "
                "of failing this one test. Add the stub_message_boxes "
                "fixture to this test if a dialog is actually expected.")
        return guard

    for kind in _DIALOG_KINDS:
        monkeypatch.setattr(QMessageBox, kind, make_guard(kind))


@pytest.fixture
def stub_message_boxes(monkeypatch):
    """Sanctioned opt-in for a test that legitimately expects a dialog --
    silences no_unstubbed_dialogs above for exactly this test.

    Returns {"warning": [...], "critical": [...], "information": [...]} of
    (title, message) pairs recorded instead of shown. question() always
    answers Yes -- the only answer any current caller needs (Dashboard's
    confirm-before-cancel-all).
    """
    calls = {"warning": [], "critical": [], "information": []}
    for kind in calls:
        monkeypatch.setattr(
            QMessageBox, kind,
            lambda parent, title, message, k=kind: calls[k].append((title, message)))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **kw: QMessageBox.StandardButton.Yes)
    return calls


# ---------------------------------------------------------------------------
# state_dir()/config_dir() isolation. See this module's docstring for the
# fleet.json corruption that made this necessary.
# ---------------------------------------------------------------------------

# tests/test_platform_paths.py exercises _base()/state_dir()/config_dir()
# themselves (via sys.platform and the real APPDATA/XDG_CONFIG_HOME/HOME
# env vars) -- pre-redirecting those functions here, before that module's
# own test body runs, would make it assert against THIS fixture's fake
# path instead of the production _base() logic it exists to cover.
_EXEMPT_MODULES = frozenset({"test_platform_paths"})


@pytest.fixture(autouse=True)
def redirect_app_dirs(request, tmp_path, monkeypatch):
    """Force every state_dir()/config_dir() call in the process to resolve
    under this test's own tmp_path, never under the user's real
    %APPDATA%\\BlendFleet (or ~/.config/blendfleet).

    Patching blendfleet.platform_paths.state_dir/config_dir alone is not
    enough: `from blendfleet.platform_paths import state_dir` -- used by
    fleet.py, instance_state.py and ui/bridge.py, same for config_dir in
    accounts.py and settings.py -- binds a name in THAT module's own
    namespace at import time. Patching platform_paths.state_dir never
    touches fleet.state_dir once fleet.py has already imported it, so
    every one of those already-bound aliases has to be patched too, or
    exactly the gap that once let a test overwrite the user's real
    fleet.json reopens the moment a new call site is added.
    """
    if request.module.__name__ in _EXEMPT_MODULES:
        yield
        return

    fake_config = tmp_path / "blendfleet-appdata"
    fake_state = fake_config / "state"
    fake_logs = fake_config / "logs"

    def fake_config_dir():
        fake_config.mkdir(parents=True, exist_ok=True)
        return fake_config

    def fake_state_dir():
        fake_state.mkdir(parents=True, exist_ok=True)
        return fake_state

    def fake_log_dir():
        fake_logs.mkdir(parents=True, exist_ok=True)
        return fake_logs

    for module, name, fake in (
        (platform_paths_mod, "config_dir", fake_config_dir),
        (platform_paths_mod, "state_dir", fake_state_dir),
        (platform_paths_mod, "log_dir", fake_log_dir),
        (accounts_mod, "config_dir", fake_config_dir),
        (settings_mod, "config_dir", fake_config_dir),
        (fleet_mod, "state_dir", fake_state_dir),
        (instance_state_mod, "state_dir", fake_state_dir),
        (bridge_mod, "state_dir", fake_state_dir),
        (bridge_mod, "log_dir", fake_log_dir),
        (crash_log_mod, "log_dir", fake_log_dir),
    ):
        monkeypatch.setattr(module, name, fake)

    yield


def _snapshot_real_app_dir():
    """(path -> (size, mtime_ns)) for every file already on disk under the
    genuine, UNPATCHED BlendFleet base directory.

    config_dir() IS that base -- state_dir() and cache_dir() both nest
    under it -- so one recursive walk from here already covers every real
    call site redirect_app_dirs above patches. Must only ever be called
    outside any test's monkeypatch (session fixture setup/teardown, never
    mid-test), or it would snapshot the redirected tmp path instead of the
    real one and this guard would pass no matter what a test did.
    """
    base = platform_paths_mod.config_dir()
    snapshot = {}
    for path in base.rglob("*"):
        if path.is_file():
            stat = path.stat()
            snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def _diff_snapshot(before: dict, after: dict) -> list[str]:
    """Paths whose (size, mtime_ns) differ between two
    _snapshot_real_app_dir() calls, or that only exist on one side --
    sorted so the failure/warning message below is stable and readable
    instead of dict-iteration-order soup.
    """
    return sorted(p for p in before.keys() | after.keys()
                  if before.get(p) != after.get(p))


# Process names the PACKAGED build runs under (see dist/blendfleetweb/).
# Deliberately narrow: matching every python.exe on the box would turn a
# guard meant to name a specific innocent writer into one that excuses
# anything, which is just a slower way of deleting it. A dev-mode run via
# `python -m blendfleet.web_main` will NOT be detected by this and will
# still be (correctly) treated as "no app running" below -- documented
# gap, not silently assumed away.
#
# The Electron shell is on this list because on 2026-08-17 it walked
# straight into the gap: `npm start` was running during a full-suite run,
# its sidecar polled Kaggle on the same 30s loop and rewrote the real
# fleet.json, and because the only names here were the Qt build's, the
# guard reported "no app process was observed at ANY point" and escalated
# to a hard error. The writer was the app, exactly as this fixture's
# warning path describes -- it just was not wearing a name the fixture
# knew. `electron.exe` covers a dev run (whose sidecar is a bare
# python.exe, which stays off this list for the reason above -- pytest
# itself is one), `BlendFleet.exe` the installed shell, and
# `blendfleet-backend.exe` the frozen sidecar either may spawn.
_APP_PROCESS_NAMES = (
    "blendfleetweb.exe", "blendfleetweb",
    "electron.exe", "electron",
    "BlendFleet.exe",
    "blendfleet-backend.exe", "blendfleet-backend",
)


def _blendfleet_app_is_running() -> bool:
    """Best-effort: is the packaged BlendFleet app alive right now?

    Exists because this guard cannot otherwise tell "a test wrote to the
    real app dir" from "the user's own running app did", and the second
    one is not a bug -- BlendFleet polls Kaggle every 30s and rewrites
    state/fleet.json, state/instance_state.json and cache/work/... as it
    goes. Verified live on 2026-08-15: fleet.json's mtime advanced while
    blendfleetweb.exe was the only relevant process running and no test
    had touched it -- that is the false positive this function exists to
    catch, not a hole to patch over the real one.

    A missed detection (app running but not found here, e.g. dev-mode)
    just makes the guard fail loudly instead of warn -- the safe
    direction, since the caller can then check by hand. Any exception
    finding processes (tasklist/ps missing, permissions, ...) is treated
    the same way: assume no app, let the real check run.
    """
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-eo", "comm"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
    except Exception:
        return False
    out_lower = out.lower()
    return any(name.lower() in out_lower for name in _APP_PROCESS_NAMES)


# How often pytest_runtest_teardown below is allowed to actually shell out
# to tasklist, in wall-clock seconds -- NOT once per test. A per-test check
# would scale cost with suite size (1298 tests today, more tomorrow); a
# wall-clock throttle instead bounds the added cost by session duration,
# which is what makes "sample periodically" affordable without a dedicated
# polling thread (and a thread is exactly the kind of thing no_leaked_threads
# above exists to catch -- not a place to introduce one to fix a different
# guard).
_APP_SEEN_POLL_INTERVAL_SECONDS = 5.0


class _AppSeenTracker:
    """Session-wide memory of "was a live BlendFleet process seen at ANY
    point during this session", not just "is one running right now".

    That distinction is the whole fix: a live app that exits before
    teardown checks used to be indistinguishable from no app ever having
    run, which produced the fourth false failure described in this
    module's docstring. `seen` only ever goes False -> True; nothing
    resets it once a live process has been observed, because the write
    that changed the real directory could have happened at any point
    while that app was up, not only at the instant it was last checked.
    """

    def __init__(self, initial_seen: bool) -> None:
        self.seen = initial_seen
        self._last_poll = time.monotonic()

    def sample(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_poll) < _APP_SEEN_POLL_INTERVAL_SECONDS:
            return
        self._last_poll = now
        if _blendfleet_app_is_running():
            self.seen = True


# Set by guard_real_app_dir_untouched's setup, read by pytest_runtest_teardown
# below. A module global, not a fixture, on purpose: it needs to be poked
# once per test regardless of that test's own fixtures, and adding a sixth
# autouse fixture just to piggyback a periodic side-effect onto every test
# would misrepresent this module's docstring count of five guards -- this
# is bookkeeping in service of guard #5, not a new guard in its own right.
_app_seen_tracker: "_AppSeenTracker | None" = None


def pytest_runtest_teardown(item, nextitem):
    """Between every test, not just at session start/end -- give the app
    another chance to be caught running before it exits mid-session, same
    as it did in the false failure this fixes. Throttled by
    _AppSeenTracker.sample()'s wall-clock check, so this is a cheap
    time.monotonic() comparison on ~1298 of 1298 calls and an actual
    tasklist shell-out on only the handful where enough real time has
    passed -- overhead bounded by wall-clock session length, not test count.
    """
    if _app_seen_tracker is not None:
        _app_seen_tracker.sample()


def _evaluate_real_app_dir_snapshots(before: dict, after: dict, *,
                                      app_seen: bool, base) -> None:
    """The guard's actual verdict, pulled out of the fixture so it can be
    exercised directly with synthetic snapshots and a forced app_seen
    value -- proving both branches without ever touching, or needing to
    kill, the real %APPDATA%\\BlendFleet directory or the user's live app.

    Same assertion either way (`after == before`); the only question this
    function answers is whether a mismatch is reported as a WARNING (a
    live app was seen at some point this session and is the plausible
    writer) or a hard failure (no app was ever seen, so a test is the only
    remaining explanation). `app_seen` means "seen at any point during the
    session", not "running right now" -- see _AppSeenTracker.
    """
    if after == before:
        return

    changed = "\n  ".join(_diff_snapshot(before, after))

    if app_seen:
        warnings.warn(
            "guard_real_app_dir_untouched: the REAL BlendFleet config/state "
            f"directory ({base}) changed during this test session, and a "
            "live blendfleetweb process WAS OBSERVED running at some point "
            "during this session (it may have exited before this check ran "
            "-- that no longer matters, see this module's docstring for "
            "why). This is almost certainly that app's own 30s Kaggle poll "
            "loop rewriting fleet.json / instance_state.json / cache files "
            "under you, NOT a test -- this exact situation produced 4 false "
            "failures in one working session before this check existed. "
            f"Changed path(s):\n  {changed}\n"
            "How to tell this apart from a real test bug: close BlendFleet "
            "completely and re-run `pytest tests/ -q`. If the real "
            "directory is then untouched, the app was the writer and this "
            "warning was correct. If changed paths are STILL reported with "
            "no BlendFleet process seen running at any point, that is a "
            "genuine regression -- treat it exactly as the hard failure "
            "below would.",
            stacklevel=2)
        return

    assert after == before, (
        "a test just wrote to the REAL BlendFleet config/state directory "
        f"({base}) instead of a redirected tmp path. No BlendFleet app "
        "process was observed running at ANY point during this session "
        "(checked at session start, session end, and periodically between "
        "tests), which rules out the known false positive (a live app's "
        "own 30s poll loop rewriting its own state -- see this fixture's "
        "WARNING path for that case) and leaves a test as the only "
        "explanation. Changed path(s):\n"
        f"  {changed}\n"
        "This is the exact defect that once overwrote the user's only "
        "record of which Kaggle kernels were actually running with a "
        "synthetic test job. Find whichever test constructed a "
        "Fleet/Settings/AccountStore/InstanceStore/Backend without "
        "redirect_app_dirs in effect for it (e.g. via a session- or "
        "module-scoped fixture that runs before autouse function-scoped "
        "fixtures do) and make it go through the normal per-test tmp_path "
        "instead. Do NOT clear this failure by deleting or overwriting "
        "the real files it points at -- back them up first, the same way "
        "the previous occurrence was recovered (see "
        ".superpowers/sdd/2026-08-12-scene-library-and-multi-scene/"
        "state-isolation-report.md).")


@pytest.fixture(scope="session", autouse=True)
def guard_real_app_dir_untouched():
    """Fail the whole session, loudly, if any test -- this one or a future
    one that forgets redirect_app_dirs applies to it -- wrote to the real
    BlendFleet config/state directory. Downgrades to a named WARNING
    instead of failing when a live BlendFleet process was seen at any
    point during the session, since then the app itself (polling Kaggle
    every 30s and rewriting its own state files) is the far more likely
    writer than a test -- see _evaluate_real_app_dir_snapshots,
    _AppSeenTracker and _blendfleet_app_is_running above for the
    mechanics and their limits.

    Session-scoped so its setup runs before the first test's function-
    scoped fixtures (redirect_app_dirs included) and its teardown runs
    after the last test's have already been undone -- both snapshots see
    the real, unpatched directory, never a redirected tmp path.

    The liveness check is NOT similarly a single point-in-time sample:
    `global _app_seen_tracker` is set here at setup (first sample, before
    any test runs) so pytest_runtest_teardown above can keep sampling
    between tests, and a forced final sample is taken here again at
    teardown -- "seen running at any point" is what decides warn-vs-fail
    below, not "running right now", which is what let a live app that
    exited mid-session get blamed on the tests instead.
    """
    global _app_seen_tracker
    before = _snapshot_real_app_dir()
    _app_seen_tracker = _AppSeenTracker(_blendfleet_app_is_running())
    yield
    _app_seen_tracker.sample(force=True)
    after = _snapshot_real_app_dir()
    _evaluate_real_app_dir_snapshots(
        before, after,
        app_seen=_app_seen_tracker.seen,
        base=platform_paths_mod.config_dir())
