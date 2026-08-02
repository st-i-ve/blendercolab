from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QListWidget, QLabel, QMessageBox)

from blendfleet.accounts import Account, AccountStore, TokenFormatError


class SetupDialog(QDialog):
    """Add one Kaggle account per person who is lending you quota."""

    def __init__(self, store: AccountStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
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
        add = QPushButton("Add"); add.clicked.connect(self._add)
        rm = QPushButton("Remove"); rm.clicked.connect(self._remove)
        for wdg in (self.label, self.token, add, rm):
            row.addWidget(wdg)
        v.addLayout(row)

        done = QPushButton("Done"); done.clicked.connect(self.accept)
        v.addWidget(done)
        self._refresh()

    def _refresh(self) -> None:
        self.list.clear()
        for a in self.store.list():
            self.list.addItem(f"{a.label}   ({a.username or 'not yet identified'})")

    def _add(self) -> None:
        try:
            self.store.add(Account(label=self.label.text().strip() or "account",
                                   token=self.token.text().strip()))
            self.store.save()
            self.label.clear(); self.token.clear()
            self._refresh()
        except (TokenFormatError, ValueError) as e:
            QMessageBox.warning(self, "Cannot add account", str(e))

    def _remove(self) -> None:
        item = self.list.currentItem()
        if item:
            self.store.remove(item.text().split()[0])
            self.store.save()
            self._refresh()
