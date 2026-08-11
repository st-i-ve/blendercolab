"""Entry point for the web-UI build.

A second entry point rather than a flag on __main__, because the two are
genuinely different applications while the port is in progress: this one
opens WebHost, the existing one opens the Qt Dashboard. Both share every
line of the backend. When the port finishes, __main__ switches to WebHost
and this file goes away along with the widget UI.
"""
from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from blendfleet.accounts import AccountStore
from blendfleet.fleet import Fleet
from blendfleet.kaggle_client import KaggleClient, verify_token
from blendfleet.platform_paths import cache_dir
from blendfleet.settings import Settings
from blendfleet.ui.setup_dialog import SetupDialog
from blendfleet.ui.theme import apply
from blendfleet.ui.web_host import WebHost


def main() -> int:
    app = QApplication(sys.argv)
    settings = Settings.load()
    # Still applied, even though the pages are HTML: the window chrome
    # around the web view (title bar, Mica, and any Qt dialog such as
    # SetupDialog or QFileDialog) is Qt, and must match the theme the page
    # is about to render itself in.
    apply(app, settings.accent, settings.theme)

    from blendfleet.__main__ import _icon_path
    from PySide6.QtGui import QIcon
    icon = _icon_path()
    if icon is not None:
        app.setWindowIcon(QIcon(str(icon)))

    store = AccountStore.load()
    if not store.list():
        SetupDialog(store, verify_token).exec()

    def fleet_factory(accounts):
        # Tokens are unique per account, so this recovers the human label
        # for whichever token the fleet asks for -- which is what lets
        # KaggleClient's identity check name the account rather than a
        # masked token.
        labels = {a.token: a.label for a in accounts}
        return Fleet(accounts,
                     lambda t: KaggleClient(t, label=labels.get(t)),
                     cache_dir() / "work")

    host = WebHost(store, fleet_factory, verify_token, settings)
    host.show_at_startup()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
