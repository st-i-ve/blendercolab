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
from PySide6.QtCore import QEventLoop, QTimer, QUrl
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
    # A nested QEventLoop, never app.exec()/app.quit(): quitting the
    # PRIMARY loop leaves the application in a state where later nested
    # loops return immediately, so every card test came back empty with no
    # error to explain it.
    loop = QEventLoop()

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
            lambda r: (result.update(json_loads(r)), loop.quit()))

    QTimer.singleShot(LOAD_MS, probe)
    QTimer.singleShot(LOAD_MS + 8000, loop.quit)    # never hang
    loop.exec()
    # The view stays ALIVE for the whole module: the card tests below run
    # JavaScript in this same page, and a deleteLater()d page never calls
    # its runJavaScript callback -- which hangs the test run rather than
    # failing it.
    yield page, result
    view.deleteLater()
    for _ in range(20):
        QApplication.processEvents()


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


# ---------------------------------------------------------------------------
# The other half of "live GPU and RAM work": the payload can be perfect and
# still show nothing if the card does not read it. These call the real
# instanceCard() in a real browser with a real-shaped payload.
# ---------------------------------------------------------------------------

LIVE_INSTANCE = """({
  label: 'acct0', username: 'friend', verified: true,
  quota: '2.0 / 30.0 h',
  worker: { state: 'running', frames: [1,2,3,4], framesDone: 1, message: '' },
  hardware: { gpus: [{index:0, memTotal:16280, model:'Tesla P100'}],
              cpuCount: 4, ramTotal: 31.3, observedAt: 0, ageSeconds: 7200 },
  live: {
    phase: 'rendering · 2/4 frames', framesDone: 2, framesTotal: 4,
    gpus: [{index:0, util:91, memUsed:4096, memTotal:15360},
           {index:1, util:12, memUsed:1024, memTotal:15360}],
    cpuCount: 4, ramTotal: 31.3,
    preflight: {gpu_count:2, gpu_names:['Tesla T4','Tesla T4'],
                cpu_count:4, ram_total:31.3}
  }
})"""


def _card_html(page, instance_js=LIVE_INSTANCE):
    """Render one card by calling the page's own instanceCard().

    Bailed out by a timer as well as by the callback: a page that cannot
    answer must fail the test, never hang the suite.
    """
    out = {}
    # A NESTED loop, not app.exec(): the module fixture has already run and
    # quit the application loop once, and re-entering it returns
    # immediately, so the callback is never waited for and every card comes
    # back empty. QEventLoop is what nests correctly.
    loop = QEventLoop()

    def done(result):
        out["html"] = result or ""
        loop.quit()

    # Wrapped in an IIFE that stringifies and catches: a bare call
    # expression came back empty here, and an exception inside the card
    # would otherwise be indistinguishable from "rendered nothing".
    page.runJavaScript(
        "(() => { try { return String(instanceCard(" + instance_js + "));"
        " } catch (e) { return 'THREW ' + e; } })()", done)
    QTimer.singleShot(5000, loop.quit)      # never hang, only fail
    loop.exec()
    assert "html" in out, "the page never answered runJavaScript"
    assert not out["html"].startswith("THREW"), out["html"]
    return out["html"]


@pytest.fixture
def card(loaded_page):
    page, _ = loaded_page
    return lambda js=LIVE_INSTANCE: _card_html(page, js)


def test_a_card_shows_one_row_per_gpu_with_its_utilisation(card):
    html = card()
    assert "GPU 0" in html and "GPU 1" in html
    assert "91%" in html and "12%" in html


def test_a_card_shows_vram_used_against_total(card):
    """VRAM matters more than utilisation for a render that is about to
    fail: a card at 100% is working, a card at 15/15G is about to die."""
    html = card()
    assert "VRAM" in html
    assert "4/15G" in html


def test_a_card_shows_the_ram_this_session_got(card):
    html = card()
    assert "31.3 GB RAM" in html
    assert "4 vCPU" in html


def test_a_card_shows_the_live_phase_and_live_frame_count(card):
    html = card()
    assert "rendering · 2/4 frames" in html
    assert "2 / 4" in html, "the frame counter ignored the live count"


def test_live_hardware_is_shown_separately_from_cached_hardware(card):
    """Kaggle reallocates between runs, so the cached line and this
    session's line can legitimately disagree -- and both are shown, with
    the cached one carrying its age."""
    html = card()
    assert "Tesla T4" in html, "this session's hardware is missing"
    assert "Tesla P100" in html, "the cached hardware line is missing"
    assert "ago" in html, "the cached line lost its age"


def test_ram_and_cpu_still_show_without_a_preflight_line(card):
    """The hardware banner and PREFLIGHT are separate lines from the same
    cell; either may arrive alone. Reading only preflight meant a session
    that reported CPU/RAM showed neither."""
    html = card("""({
      label:'a', username:'u', verified:true, quota:'', worker:null,
      hardware:null,
      live:{phase:'installing Blender', framesDone:0, framesTotal:0, gpus:[],
            cpuCount:4, ramTotal:31.3, preflight:null}
    })""")
    assert "31.3 GB RAM" in html
    assert "4 vCPU" in html


def test_a_card_with_no_live_data_claims_nothing(card):
    """Between renders there is no session. The card must not fall back to
    the last run's numbers and present them as current."""
    html = card("""({
      label:'a', username:'u', verified:true, quota:'1.0 / 30.0 h',
      worker:null,
      hardware:{gpus:[{index:0,memTotal:16280,model:'Tesla P100'}],
                cpuCount:4, ramTotal:31.3, observedAt:0, ageSeconds:100},
      live:null
    })""")
    assert "GPU 0" not in html, "invented a live GPU row with no live data"
    assert "VRAM" not in html
    assert "This session" not in html
    assert "Tesla P100" in html and "ago" in html
