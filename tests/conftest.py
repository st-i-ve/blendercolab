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
"""
from __future__ import annotations

import socket
import threading
import time

import pytest
from PySide6.QtWidgets import QMessageBox

import blendfleet.accounts as accounts_mod
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

    def fake_config_dir():
        fake_config.mkdir(parents=True, exist_ok=True)
        return fake_config

    def fake_state_dir():
        fake_state.mkdir(parents=True, exist_ok=True)
        return fake_state

    for module, name, fake in (
        (platform_paths_mod, "config_dir", fake_config_dir),
        (platform_paths_mod, "state_dir", fake_state_dir),
        (accounts_mod, "config_dir", fake_config_dir),
        (settings_mod, "config_dir", fake_config_dir),
        (fleet_mod, "state_dir", fake_state_dir),
        (instance_state_mod, "state_dir", fake_state_dir),
        (bridge_mod, "state_dir", fake_state_dir),
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


@pytest.fixture(scope="session", autouse=True)
def guard_real_app_dir_untouched():
    """Fail the whole session, loudly, if any test -- this one or a future
    one that forgets redirect_app_dirs applies to it -- wrote to the real
    BlendFleet config/state directory.

    Session-scoped so its setup runs before the first test's function-
    scoped fixtures (redirect_app_dirs included) and its teardown runs
    after the last test's have already been undone -- both snapshots see
    the real, unpatched directory, never a redirected tmp path.
    """
    before = _snapshot_real_app_dir()
    yield
    after = _snapshot_real_app_dir()
    assert after == before, (
        "a test just wrote to the REAL BlendFleet config/state directory "
        f"({platform_paths_mod.config_dir()}) instead of a redirected "
        "tmp path -- this is the exact defect that once overwrote the "
        "user's only record of which Kaggle kernels were actually "
        "running with a synthetic test job. Find whichever test "
        "constructed a Fleet/Settings/AccountStore/InstanceStore/Backend "
        "without redirect_app_dirs in effect for it (e.g. via a session- "
        "or module-scoped fixture that runs before autouse function-"
        "scoped fixtures do) and make it go through the normal per-test "
        "tmp_path instead. Do NOT clear this failure by deleting or "
        "overwriting the real files it points at -- back them up first, "
        "the same way the previous occurrence was recovered (see "
        ".superpowers/sdd/2026-08-12-scene-library-and-multi-scene/"
        "state-isolation-report.md).")
