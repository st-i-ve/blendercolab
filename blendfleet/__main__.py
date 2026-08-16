import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from blendfleet.accounts import AccountStore
from blendfleet.fleet import Fleet
from blendfleet.kaggle_client import KaggleClient, verify_token
from blendfleet.platform_paths import cache_dir
from blendfleet.settings import Settings
from blendfleet.ui.dashboard import Dashboard
from blendfleet.ui.setup_dialog import SetupDialog
from blendfleet.ui.theme import apply


def _icon_path() -> Path | None:
    """The window icon, whether running from source or from a frozen bundle.

    PyInstaller unpacks --add-data into sys._MEIPASS at runtime, so the
    source-tree location does not exist in the packaged exe.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / "logo" / "app-icon-256.png")
    candidates.append(
        Path(__file__).parent.parent / "assets" / "logo" / "app-icon-256.png")
    for c in candidates:
        if c.exists():
            return c
    return None


def main() -> int:
    app = QApplication(sys.argv)
    # Settings loaded once, here, and the SAME instance handed to Dashboard
    # below -- so the accent applied to the QApplication before any window
    # or dialog shows (see theme.apply()'s own docstring on why that order
    # matters: SetupDialog can appear before Dashboard does, on a fresh
    # install with no accounts yet) is never at risk of drifting from
    # whatever Dashboard's own settings-driven window state later reads.
    settings = Settings.load()
    apply(app, settings.accent, settings.theme, settings.font)
    icon = _icon_path()
    if icon is not None:
        app.setWindowIcon(QIcon(str(icon)))
    store = AccountStore.load()
    if not store.list():
        SetupDialog(store, verify_token).exec()

    def fleet_factory(accounts):
        # Tokens are unique per account (AccountStore.add enforces it), so
        # this recovers the human label for whichever token the fleet asks
        # for -- which is what lets KaggleClient's identity check name the
        # account ("james") rather than a masked token.
        labels = {a.token: a.label for a in accounts}
        return Fleet(accounts,
                     lambda t: KaggleClient(t, label=labels.get(t)),
                     cache_dir() / "work")

    win = Dashboard(store, fleet_factory, verify_token, settings=settings)
    win.show_at_startup()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
