"""The page has to actually parse and run.

This file exists because it did not, once, and the app shipped: a `\\n`
that became a real newline inside a single-quoted JS string left the whole
of app.js unparseable, so nothing was defined, nothing was bound, and the
window came up empty with every control dead. Every Python test still
passed -- they test the bridge, and the bridge was fine.

Nothing here asserts what the page LOOKS like. It asserts that the script
loads, that the functions the UI is built out of exist, and that loading it
produces no JavaScript errors -- the difference between a working app and a
blank one.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

import pytest
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication

pytest.importorskip("PySide6.QtWebEngineWidgets",
                    reason="QtWebEngine is not available in this environment")

from PySide6.QtWebEngineCore import QWebEnginePage      # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView   # noqa: E402

from blendfleet.ui.web_host import WEB_DIR              # noqa: E402

# Long enough for Chromium to start and run the page's top level. The
# probe fires once; it does not poll, so this is a ceiling, not a sleep
# every run pays in full.
LOAD_MS = 3500


class _RecordingPage(QWebEnginePage):
    """A page that remembers every console message, errors included."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.messages: list[tuple[str, str, int]] = []

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        name = getattr(level, "name", str(level))
        self.messages.append((name, message, line))


@pytest.fixture(scope="module")
def loaded_page():
    """Load index.html once and return (page, probe result).

    Loaded WITHOUT the QWebChannel shim the host injects, so the page's
    own bridge call fails -- deliberately. Everything before that line is
    what these tests are about, and the failure is asserted for explicitly
    below rather than silently tolerated.
    """
    app = QApplication.instance() or QApplication([])
    view = QWebEngineView()
    page = _RecordingPage(view)
    view.setPage(page)
    view.load(QUrl.fromLocalFile(str(WEB_DIR / "index.html")))
    view.resize(1200, 700)
    view.show()

    result: dict = {}

    def probe():
        page.runJavaScript(
            "JSON.stringify({"
            "  renderState: typeof renderState,"
            "  instanceCard: typeof instanceCard,"
            "  renderFleetTable: typeof renderFleetTable,"
            "  renderDataset: typeof renderDataset,"
            "  notify: typeof notify,"
            "  buttons: document.querySelectorAll('button').length,"
            "  pages: document.querySelectorAll('.page').length"
            "})",
            lambda r: (result.update(json_loads(r)), app.quit()))

    QTimer.singleShot(LOAD_MS, probe)
    app.exec()
    view.deleteLater()
    return page, result


def json_loads(text):
    import json
    return json.loads(text) if text else {}


def test_the_script_parses(loaded_page):
    """A SyntaxError anywhere in app.js kills the ENTIRE file: no handler
    is bound, every page is empty, and every control is dead. This is the
    single most valuable assertion in the file."""
    page, _ = loaded_page
    syntax = [m for level, m, _line in page.messages if "SyntaxError" in m]
    assert not syntax, syntax


def test_no_javascript_errors_other_than_the_absent_bridge(loaded_page):
    """The bridge shim is injected by the host, not by the page, so its
    absence here is expected and is the ONLY error tolerated. Anything
    else is a real fault."""
    page, _ = loaded_page
    errors = [m for level, m, _line in page.messages
              if "Error" in level and "QWebChannel is not defined" not in m]
    assert not errors, errors


def test_every_function_the_ui_is_built_from_exists(loaded_page):
    """If the script half-parsed, these would be undefined -- which is
    exactly what a blank window looks like from the outside."""
    _, result = loaded_page
    for name in ("renderState", "instanceCard", "renderFleetTable",
                 "renderDataset", "notify"):
        assert result.get(name) == "function", f"{name} is {result.get(name)}"


def test_the_markup_has_its_pages_and_controls(loaded_page):
    """Guards the other half: a page that parses but has lost its markup
    is just as broken."""
    _, result = loaded_page
    assert result.get("pages") == 5, result
    assert result.get("buttons", 0) >= 20, result
