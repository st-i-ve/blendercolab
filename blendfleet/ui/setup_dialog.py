from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QListWidget, QListWidgetItem, QLabel,
                               QMessageBox)

from blendfleet.accounts import Account, AccountStore
from blendfleet.ui.messages import explain
from blendfleet.ui.theme import ACCENT, TEXT_SECONDARY, WARNING

# Verification state colours, pulled from the one app-wide palette
# (blendfleet.ui.theme) so this dialog is never a separate look from the
# rest of the app. Every state also carries a distinct symbol/word so the
# list never relies on colour alone -- roughly 8% of men cannot reliably
# tell red from green, which is why "not verified" is amber rather than red.
def _wash(hex_color: str, alpha: int = 60) -> QColor:
    """A translucent tint over the dialog's own dark surface, rather than
    a solid pastel block -- the palette is warm-dark, so a full-strength
    background fill would fight the rest of the theme instead of reading
    as a status."""
    c = QColor(hex_color)
    c.setAlpha(alpha)
    return c


COLOR_VERIFIED = _wash(ACCENT)
COLOR_UNVERIFIED = _wash(WARNING)
COLOR_CHECKING = _wash(TEXT_SECONDARY)


class _VerifyWorker(QThread):
    """Runs one blocking callable off the UI thread.

    whoami() is a real HTTP round trip, and the UI thread freezing on Kaggle
    calls is a known problem elsewhere in this app (see dashboard.py). A
    QThread is used here rather than QApplication.processEvents() so the
    dialog stays genuinely interactive (movable/closable) for however long
    the network call takes, not just repainted before/after it.

    `fn` must do NETWORK WORK ONLY and return its result -- it must not
    touch the AccountStore. The store is a plain list plus a file write
    (save() also chmods), read on the UI thread by _refresh()/store.list()
    every time the dialog repaints; mutating and saving it from in here
    raced that, and could persist a half-updated accounts.json. Whatever
    is returned comes back on `succeeded` and is applied to the store by
    the UI thread.
    """

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], object], parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as e:  # noqa: BLE001 -- surfaced verbatim to the user
            self.failed.emit(str(e))
        else:
            self.succeeded.emit(result)


class SetupDialog(QDialog):
    """Add one Kaggle account per person who is lending you quota."""

    def __init__(self, store: AccountStore, verifier: Callable[[str], "str | None"],
                 parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.verifier = verifier
        self._checking: set[str] = set()   # labels currently being re-verified
        self._worker: _VerifyWorker | None = None
        self.setWindowTitle("BlendFleet — accounts")
        self.resize(560, 380)
        v = QVBoxLayout(self)

        v.addWidget(QLabel(
            "<b>Add a Kaggle account for each person contributing GPU time.</b><br>"
            "Each person generates their own token at "
            "<a href='https://www.kaggle.com/settings'>kaggle.com/settings</a> "
            "→ API → Generate New Token.<br><br>"
            "<b>A token grants full access to that Kaggle account.</b> Only accept "
            "tokens from people who understand that, and tell them they can revoke "
            "it at any time from the same page."))

        self.list = QListWidget()
        v.addWidget(self.list)

        row = QHBoxLayout()
        self.label = QLineEdit(); self.label.setPlaceholderText("label, e.g. 'james'")
        self.token = QLineEdit(); self.token.setPlaceholderText("KGAT_…")
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.add_btn = QPushButton("Add"); self.add_btn.clicked.connect(self._add)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self._remove)
        self.reverify_btn = QPushButton("Re-verify")
        self.reverify_btn.clicked.connect(self._reverify)
        for wdg in (self.label, self.token, self.add_btn, self.remove_btn,
                    self.reverify_btn):
            row.addWidget(wdg)
        v.addLayout(row)

        done = QPushButton("Done"); done.clicked.connect(self.accept)
        v.addWidget(done)
        self._refresh()

    # ---------------- rendering ----------------
    def _refresh(self) -> None:
        self.list.clear()
        for a in self.store.list():
            item = QListWidgetItem()
            if a.label in self._checking:
                item.setText(f"…  {a.label}   (checking…)")
                item.setBackground(QBrush(COLOR_CHECKING))
            elif a.verified:
                item.setText(
                    f"✓  {a.label}   verified "
                    f"({a.username or 'username unresolved -- no notebooks yet'})")
                item.setBackground(QBrush(COLOR_VERIFIED))
            else:
                item.setText(f"✗  {a.label}   not verified")
                item.setBackground(QBrush(COLOR_UNVERIFIED))
            self.list.addItem(item)

    def _set_busy(self, busy: bool) -> None:
        self.add_btn.setEnabled(not busy)
        self.reverify_btn.setEnabled(not busy)
        # Remove has to go too. Removing the very account a verification is
        # in flight for meant the worker came back and applied its result to
        # a label that no longer exists -- reverify() then raised "no account
        # labeled ..." over the top of the real outcome, and an add landing
        # after a remove could resurrect the row the user just deleted.
        self.remove_btn.setEnabled(not busy)
        self.add_btn.setText("Verifying…" if busy else "Add")

    # ---------------- actions ----------------
    def _add(self) -> None:
        label = self.label.text().strip() or "account"
        token = self.token.text().strip()
        account = Account(label=label, token=token)

        # Format and duplicate rules are pure, I/O-free checks -- run them
        # here, on the UI thread, before any thread is started. This is the
        # same order AccountStore.add() used internally, so a malformed or
        # duplicate token still costs zero network calls.
        try:
            self.store.validate(account)
        except Exception as e:  # noqa: BLE001 -- shown to the user
            QMessageBox.warning(self, "Cannot add account",
                                explain("Adding this account", e))
            return

        self._set_busy(True)

        def verify() -> "str | None":
            # Background thread: the network call, and nothing else. The
            # explanation is baked into the exception here because
            # _VerifyWorker only ever forwards str(exc) to on_failure.
            try:
                return self.verifier(account.token)
            except Exception as e:
                raise RuntimeError(explain("Adding this account", e)) from e

        def on_success(username) -> None:
            # UI thread: the only place the store is mutated or written.
            account.username = username
            account.verified = True
            try:
                self.store.add(account)
                self.store.save()
            except Exception as e:  # noqa: BLE001 -- e.g. an unwritable config dir
                self._set_busy(False)
                QMessageBox.warning(self, "Cannot add account",
                                    explain("Adding this account", e))
                return
            self._set_busy(False)
            self.label.clear(); self.token.clear()
            self._refresh()

        def on_failure(message: str) -> None:
            self._set_busy(False)
            # deliberately do NOT clear label/token: keep what the user
            # entered so they can correct it rather than retype everything
            QMessageBox.warning(self, "Cannot add account", message)

        self._run_worker(verify, on_success, on_failure)

    def _remove(self) -> None:
        item = self.list.currentItem()
        if item:
            self.store.remove(self._label_from_item(item))
            self.store.save()
            self._refresh()

    def _reverify(self) -> None:
        item = self.list.currentItem()
        if not item:
            return
        label = self._label_from_item(item)
        account = next((a for a in self.store.list() if a.label == label), None)
        if account is None:
            return
        token = account.token
        self._checking.add(label)
        self._set_busy(True)
        self._refresh()

        def verify() -> "str | None":
            # Background thread: network only, never the store.
            try:
                return self.verifier(token)
            except Exception as e:
                raise RuntimeError(
                    explain("Re-verifying this account", e)) from e

        def on_success(username) -> None:
            self._checking.discard(label)
            self._set_busy(False)
            self._apply_reverify(label, username=username)
            self._refresh()

        def on_failure(message: str) -> None:
            self._checking.discard(label)
            self._set_busy(False)
            # AccountStore.reverify() is what defines "a failed re-verify
            # marks the account unverified"; feeding it an already-decided
            # failure keeps that rule in one place instead of duplicating
            # it here.
            self._apply_reverify(label, error=message)
            self._refresh()
            QMessageBox.warning(self, "Re-verification failed", message)

        self._run_worker(verify, on_success, on_failure)

    def _apply_reverify(self, label: str, username: "str | None" = None,
                        error: str | None = None) -> None:
        """Write an already-completed verification result into the store,
        on the UI thread. The 'verifier' handed to reverify() here does no
        I/O at all -- it just replays the outcome the worker returned.
        """
        def resolved(_token):
            if error is not None:
                raise RuntimeError(error)
            return username

        try:
            self.store.reverify(label, verifier=resolved)
        except Exception:   # noqa: BLE001 -- already reported to the user
            pass
        self.store.save()

    def _run_worker(self, fn, on_success, on_failure) -> None:
        # A dialog used to leak one running QThread per verification: each
        # call overwrote self._worker, so the previous QThread lost its last
        # Python reference while still running. Wait the old one out first
        # (the buttons are disabled during a verify, so in practice it has
        # already finished) rather than letting it be collected mid-flight.
        self._wait_for_worker()
        self._worker = _VerifyWorker(fn, self)
        self._worker.succeeded.connect(on_success)
        self._worker.failed.connect(on_failure)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _wait_for_worker(self, timeout_ms: int = 5000) -> None:
        """Block until the in-flight verification thread has finished.

        Bounded, so a genuinely stuck network call cannot wedge the dialog
        shut forever.
        """
        worker = self._worker
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.wait(timeout_ms)
        except RuntimeError:
            # finished + deleteLater already processed: the C++ QThread is
            # gone, which is the "not running any more" state we wanted.
            pass

    def done(self, result: int) -> None:   # noqa: D102 -- Qt override
        # Every close path (Done, Esc, the window's X) funnels through
        # QDialog.done(), so this is the one place that guarantees no
        # QThread outlives the dialog that owns it.
        self._wait_for_worker()
        super().done(result)

    @staticmethod
    def _label_from_item(item: QListWidgetItem) -> str:
        # rows read "<symbol>  <label>   <status...>"
        return item.text().split(None, 2)[1]
