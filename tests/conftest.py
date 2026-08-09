"""Suite-wide safety nets.

Three of them, all autouse, all there for the same reason: the merge gate
must be deterministic and must never hang or touch the network.

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
"""
from __future__ import annotations

import socket
import threading
import time

import pytest
from PySide6.QtWidgets import QMessageBox


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
