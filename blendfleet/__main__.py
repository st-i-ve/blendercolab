import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from blendfleet.accounts import AccountStore
from blendfleet.fleet import Fleet
from blendfleet.kaggle_client import KaggleClient, verify_token
from blendfleet.platform_paths import cache_dir
from blendfleet.ui.dashboard import Dashboard
from blendfleet.ui.setup_dialog import SetupDialog


def main() -> int:
    app = QApplication(sys.argv)
    store = AccountStore.load()
    if not store.list():
        SetupDialog(store, verify_token).exec()

    def fleet_factory(accounts):
        return Fleet(accounts, lambda t: KaggleClient(t), cache_dir() / "work")

    win = Dashboard(store, fleet_factory, verify_token)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
