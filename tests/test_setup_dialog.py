import os
import threading

# Must be set before PySide6 creates any platform integration -- this is the
# only test module in the suite that touches Qt, so setting it here (rather
# than relying on the invoker to export it) keeps `pytest tests/` runnable
# in a headless agent sandbox with no display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

import blendfleet.platform_paths as pp
import blendfleet.ui.theme as theme
from blendfleet.accounts import Account, AccountStore
from blendfleet.ui.setup_dialog import SetupDialog, _wash

VALID = "KGAT_" + "a" * 32


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_active_accent(qapp):
    """test_verified_row_background_tracks_the_live_accent below calls
    theme.apply() with every non-default accent -- see test_theme.py's
    fixture of the same name for why that process-global state must not
    leak into other test modules run later in the same session."""
    original = theme._active_accent_name
    yield
    theme._active_accent_name = original
    theme.apply(qapp, original)


# Every SetupDialog a test builds. A dialog owns its _VerifyWorker QThread
# as a Qt child; if the dialog is left to Python's garbage collector the
# C++ QDialog (and that QThread with it) is destroyed at whatever arbitrary
# later allocation happens to trigger a collection -- which is how an abort
# ends up reported against a completely unrelated line. Closing them here
# makes destruction happen at a known point, with the event loop available.
_LIVE_DIALOGS: list = []


def make_dialog(store, verifier) -> SetupDialog:
    dlg = SetupDialog(store, verifier=verifier)
    _LIVE_DIALOGS.append(dlg)
    return dlg


@pytest.fixture(autouse=True)
def close_dialogs(qapp):
    yield
    while _LIVE_DIALOGS:
        dlg = _LIVE_DIALOGS.pop()
        # reject(), not close(): QDialog::closeEvent only calls reject()
        # when the dialog is visible, and these are never shown. Every real
        # close path (Done, Esc, the window's X) funnels through done()
        # either way, which is what waits for the verification thread.
        dlg.reject()
        dlg.deleteLater()
    for _ in range(20):
        QCoreApplication.processEvents()


def stub_warnings(monkeypatch):
    """Stand in for QMessageBox.warning: a real one blocks on a modal event
    loop waiting for a click, which never comes headless. Returns the list
    of (title, message) pairs it was called with."""
    calls = []
    monkeypatch.setattr(
        "blendfleet.ui.setup_dialog.QMessageBox.warning",
        lambda parent, title, message: calls.append((title, message)))
    return calls


def pump(worker, timeout=2000) -> None:
    """Block until the worker's run() has returned, then drain the Qt event
    queue so the queued cross-thread succeeded/failed signal is actually
    delivered to its slot -- standing in for the app.exec() loop that would
    do this in the real app."""
    assert worker is not None
    assert worker.wait(timeout), "verify worker did not finish in time"
    for _ in range(10):
        QCoreApplication.processEvents()


def failing_verifier(token: str) -> str:
    raise ValueError("revoked token")


# ---------------- add ----------------

def test_add_with_passing_verifier_stores_username_and_shows_verified(qapp, monkeypatch):
    stub_warnings(monkeypatch)
    store = AccountStore()
    dlg = make_dialog(store, verifier=lambda t: "stivestivewithani")
    dlg.label.setText("james")
    dlg.token.setText(VALID)

    dlg._add()
    assert dlg.add_btn.text() == "Verifying…"
    assert not dlg.add_btn.isEnabled()

    pump(dlg._worker)

    acct = store.list()[0]
    assert acct.username == "stivestivewithani"
    assert acct.verified is True
    assert dlg.add_btn.text() == "Add"
    assert dlg.add_btn.isEnabled()
    assert dlg.label.text() == "" and dlg.token.text() == ""   # cleared on success
    row = dlg.list.item(0).text()
    assert "✓" in row and "stivestivewithani" in row


def test_add_with_failing_verifier_does_not_add_and_keeps_entered_values(qapp, monkeypatch):
    warned = stub_warnings(monkeypatch)
    store = AccountStore()
    dlg = make_dialog(store, verifier=failing_verifier)
    dlg.label.setText("james")
    dlg.token.setText(VALID)

    dlg._add()
    pump(dlg._worker)

    assert store.list() == []
    assert warned and "revoked" in warned[0][1]
    assert dlg.label.text() == "james"    # kept so the user can correct, not retype
    assert dlg.token.text() == VALID
    assert dlg.add_btn.text() == "Add"
    assert dlg.add_btn.isEnabled()


# ---------------- accent: resolved at use time, not import time ----------
# THE bug the Task 6 brief calls out by name: this module used to compute
# `COLOR_VERIFIED = _wash(ACCENT)` once, at ITS OWN import time, via
# `from ... import ACCENT`. _verified_color() now calls current_accent()
# every time _refresh() runs, so a live accent switch is reflected the
# next time this dialog's list repaints.

@pytest.mark.parametrize("name", list(theme.ACCENTS))
def test_verified_row_background_tracks_the_live_accent(qapp, monkeypatch, name):
    stub_warnings(monkeypatch)
    theme.apply(qapp, name)
    store = AccountStore()
    store.add(Account(label="james", token=VALID), verifier=lambda t: "j")
    dlg = make_dialog(store, verifier=lambda t: "j")

    expected = _wash(theme.ACCENTS[name].base)
    actual = dlg.list.item(0).background().color()
    assert (actual.red(), actual.green(), actual.blue(), actual.alpha()) == \
        (expected.red(), expected.green(), expected.blue(), expected.alpha())


def test_add_accepts_valid_token_with_no_notebooks(qapp, monkeypatch):
    stub_warnings(monkeypatch)
    store = AccountStore()
    dlg = make_dialog(store, verifier=lambda t: None)
    dlg.label.setText("newbie")
    dlg.token.setText(VALID)

    dlg._add()
    pump(dlg._worker)

    acct = store.list()[0]
    assert acct.verified is True
    assert acct.username is None
    row = dlg.list.item(0).text()
    assert "✓" in row   # accepted + verified, despite no resolvable username


# ---------------- re-verify ----------------

def test_reverify_updates_status_to_verified(qapp, monkeypatch):
    stub_warnings(monkeypatch)
    store = AccountStore()
    store.add(Account(label="james", token=VALID))
    dlg = make_dialog(store, verifier=lambda t: "stivestivewithani")
    dlg.list.setCurrentRow(0)
    assert "✗" in dlg.list.item(0).text()

    dlg._reverify()
    assert "…" in dlg.list.item(0).text()   # checking state shown immediately

    pump(dlg._worker)

    assert store.list()[0].verified is True
    assert "✓" in dlg.list.item(0).text()


def test_reverify_failure_shows_error_and_marks_unverified(qapp, monkeypatch):
    warned = stub_warnings(monkeypatch)
    store = AccountStore()
    store.add(Account(label="james", token=VALID), verifier=lambda t: "stivestivewithani")
    dlg = make_dialog(store, verifier=failing_verifier)
    dlg.list.setCurrentRow(0)

    dlg._reverify()
    pump(dlg._worker)

    assert store.list()[0].verified is False
    assert warned and "revoked" in warned[0][1]
    assert "✗" in dlg.list.item(0).text()


# ---------------- thread discipline ----------------
# Three separate leaks lived in this dialog:
#   * _run_worker overwrote self._worker without ever waiting, so every
#     _manage() left a running QThread whose last Python reference had just
#     been dropped;
#   * do_add mutated AND saved the AccountStore from inside that thread,
#     racing _refresh()/store.list() on the UI thread and able to leave a
#     half-written accounts.json behind;
#   * Remove stayed enabled during a verification, so the account a
#     verification was in flight for could be deleted underneath it.

class RecordingStore(AccountStore):
    """Records which thread every mutating call arrived on."""

    def __init__(self) -> None:
        super().__init__()
        self.mutating_threads: set = set()

    def add(self, account, verifier=None) -> None:
        self.mutating_threads.add(threading.get_ident())
        super().add(account, verifier=verifier)

    def reverify(self, label, verifier) -> None:
        self.mutating_threads.add(threading.get_ident())
        super().reverify(label, verifier=verifier)

    def save(self) -> None:
        self.mutating_threads.add(threading.get_ident())
        super().save()


def test_add_never_touches_the_store_from_the_worker_thread(qapp, monkeypatch):
    stub_warnings(monkeypatch)
    store = RecordingStore()
    verifier_threads = []

    def verifier(token):
        verifier_threads.append(threading.get_ident())
        return "stivestivewithani"

    dlg = make_dialog(store, verifier=verifier)
    dlg.label.setText("james")
    dlg.token.setText(VALID)

    ui_thread = threading.get_ident()
    dlg._add()
    pump(dlg._worker)

    assert store.list()[0].username == "stivestivewithani"
    assert store.mutating_threads == {ui_thread}, \
        "the store must only ever be mutated/saved on the UI thread"
    assert verifier_threads and ui_thread not in verifier_threads, \
        "the network call must still happen OFF the UI thread"


def test_reverify_never_touches_the_store_from_the_worker_thread(qapp, monkeypatch):
    stub_warnings(monkeypatch)
    store = RecordingStore()
    store.add(Account(label="james", token=VALID))
    verifier_threads = []

    def verifier(token):
        verifier_threads.append(threading.get_ident())
        return "stivestivewithani"

    dlg = make_dialog(store, verifier=verifier)
    dlg.list.setCurrentRow(0)

    ui_thread = threading.get_ident()
    store.mutating_threads.clear()
    dlg._reverify()
    pump(dlg._worker)

    assert store.list()[0].verified is True
    assert store.mutating_threads == {ui_thread}
    assert verifier_threads and ui_thread not in verifier_threads


def test_remove_is_disabled_while_a_verification_is_in_flight(qapp, monkeypatch):
    """Removing the account a verification is running for meant the result
    landed on a label that no longer existed."""
    stub_warnings(monkeypatch)
    store = AccountStore()
    dlg = make_dialog(store, verifier=lambda t: "stivestivewithani")
    dlg.label.setText("james")
    dlg.token.setText(VALID)

    assert dlg.remove_btn.isEnabled()
    dlg._add()
    assert not dlg.remove_btn.isEnabled(), \
        "Remove must be disabled for the duration of a verification"

    pump(dlg._worker)
    assert dlg.remove_btn.isEnabled()


def test_a_malformed_token_is_rejected_without_any_network_call(qapp, monkeypatch):
    """Format and duplicate rules are I/O-free, so they must still be
    checked before a thread is started -- moving verification off the UI
    thread must not cost a round trip to find out the token is nonsense."""
    warned = stub_warnings(monkeypatch)
    called = []
    store = AccountStore()
    dlg = make_dialog(store, verifier=lambda t: called.append(t))
    dlg.label.setText("james")
    dlg.token.setText("not-a-kaggle-token")

    dlg._add()

    assert called == [], "no verification may be attempted for a bad token"
    assert dlg._worker is None, "no thread may be started either"
    assert store.list() == []
    assert warned and "KGAT_" in warned[0][1]
    assert dlg.add_btn.isEnabled(), "the dialog must not be left stuck in 'Verifying…'"


@pytest.mark.parametrize("close_path", ["accept", "reject"])
def test_closing_the_dialog_waits_for_its_verification_thread(qapp, monkeypatch,
                                                              close_path):
    """A QThread must never outlive the dialog that parents it.

    Both paths are covered because both are real: the Done button calls
    accept(), and Esc/the window's X call reject(). Both funnel through
    QDialog.done(), which is where the wait lives.
    """
    stub_warnings(monkeypatch)
    store = AccountStore()
    dlg = make_dialog(store, verifier=lambda t: "stivestivewithani")
    dlg.label.setText("james")
    dlg.token.setText(VALID)

    dlg._add()
    worker = dlg._worker
    getattr(dlg, close_path)()          # -> done() -> _wait_for_worker()
    assert not worker.isRunning(), \
        f"{close_path}() returned while the verification thread was still running"


def test_a_second_verification_does_not_abandon_the_first_thread(qapp, monkeypatch):
    """_run_worker used to overwrite self._worker, dropping the previous
    QThread's last reference while it was still running."""
    stub_warnings(monkeypatch)
    store = AccountStore()
    dlg = make_dialog(store, verifier=lambda t: "stivestivewithani")

    dlg.label.setText("james")
    dlg.token.setText(VALID)
    dlg._add()
    first = dlg._worker
    pump(first)

    dlg.label.setText("mary")
    dlg.token.setText("KGAT_" + "b" * 32)
    dlg._add()
    second = dlg._worker
    assert second is not first
    pump(second)

    assert [a.label for a in store.list()] == ["james", "mary"]
