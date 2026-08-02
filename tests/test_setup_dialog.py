import os

# Must be set before PySide6 creates any platform integration -- this is the
# only test module in the suite that touches Qt, so setting it here (rather
# than relying on the invoker to export it) keeps `pytest tests/` runnable
# in a headless agent sandbox with no display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

import blendfleet.platform_paths as pp
from blendfleet.accounts import Account, AccountStore
from blendfleet.ui.setup_dialog import SetupDialog

VALID = "KGAT_" + "a" * 32


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


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
    dlg = SetupDialog(store, verifier=lambda t: "stivestivewithani")
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
    dlg = SetupDialog(store, verifier=failing_verifier)
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


def test_add_accepts_valid_token_with_no_notebooks(qapp, monkeypatch):
    stub_warnings(monkeypatch)
    store = AccountStore()
    dlg = SetupDialog(store, verifier=lambda t: None)
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
    dlg = SetupDialog(store, verifier=lambda t: "stivestivewithani")
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
    dlg = SetupDialog(store, verifier=failing_verifier)
    dlg.list.setCurrentRow(0)

    dlg._reverify()
    pump(dlg._worker)

    assert store.list()[0].verified is False
    assert warned and "revoked" in warned[0][1]
    assert "✗" in dlg.list.item(0).text()
