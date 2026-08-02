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
    """

    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, fn: Callable[[], None], parent=None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self._fn()
        except Exception as e:  # noqa: BLE001 -- surfaced verbatim to the user
            self.failed.emit(str(e))
        else:
            self.succeeded.emit()


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
        rm = QPushButton("Remove"); rm.clicked.connect(self._remove)
        self.reverify_btn = QPushButton("Re-verify")
        self.reverify_btn.clicked.connect(self._reverify)
        for wdg in (self.label, self.token, self.add_btn, rm, self.reverify_btn):
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
        self.add_btn.setText("Verifying…" if busy else "Add")

    # ---------------- actions ----------------
    def _add(self) -> None:
        label = self.label.text().strip() or "account"
        token = self.token.text().strip()
        account = Account(label=label, token=token)

        self._set_busy(True)

        def do_add() -> None:
            try:
                self.store.add(account, verifier=self.verifier)
            except Exception as e:
                # Re-raised with the full what/why/next-step explanation
                # already built in -- _VerifyWorker only ever forwards
                # str(exc) to on_failure, so the friendly text has to be
                # baked into the exception here, not applied afterwards.
                raise RuntimeError(explain("Adding this account", e)) from e
            self.store.save()

        def on_success() -> None:
            self._set_busy(False)
            self.label.clear(); self.token.clear()
            self._refresh()

        def on_failure(message: str) -> None:
            self._set_busy(False)
            # deliberately do NOT clear label/token: keep what the user
            # entered so they can correct it rather than retype everything
            QMessageBox.warning(self, "Cannot add account", message)

        self._run_worker(do_add, on_success, on_failure)

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
        self._checking.add(label)
        self._set_busy(True)
        self._refresh()

        def do_reverify() -> None:
            try:
                self.store.reverify(label, verifier=self.verifier)
            except Exception as e:
                raise RuntimeError(
                    explain("Re-verifying this account", e)) from e
            self.store.save()

        def on_success() -> None:
            self._checking.discard(label)
            self._set_busy(False)
            self._refresh()

        def on_failure(message: str) -> None:
            self._checking.discard(label)
            self._set_busy(False)
            self._refresh()
            QMessageBox.warning(self, "Re-verification failed", message)

        self._run_worker(do_reverify, on_success, on_failure)

    def _run_worker(self, fn, on_success, on_failure) -> None:
        self._worker = _VerifyWorker(fn, self)
        self._worker.succeeded.connect(on_success)
        self._worker.failed.connect(on_failure)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    @staticmethod
    def _label_from_item(item: QListWidgetItem) -> str:
        # rows read "<symbol>  <label>   <status...>"
        return item.text().split(None, 2)[1]
