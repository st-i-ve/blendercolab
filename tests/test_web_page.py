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
    # Labelled "GPU <n> memory", not "VRAM": stacked under a bar labelled
    # "GPU", the old wording read as a second name for the same quantity
    # rather than a different one (asked directly, 2026-08-12).
    assert "GPU 0 memory" in html
    assert "GPU 0 load" in html, "the two bars must name what they measure"
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


# ---------------------------------------------------------------------------
# The status pill became an icon.
#
# On a real dashboard (2026-08-12) it was clipped to "RENDERI…" and
# "COMP…": .inst-head is a flex row, its children default to
# min-width:auto so none of them could shrink, and .inst's overflow:hidden
# cut the overflowing badge off. The word moved to the tooltip.
# ---------------------------------------------------------------------------

def test_the_status_badge_is_an_icon_not_a_clippable_word(card):
    html = card()
    assert '<svg' in html, "the state must render as an icon"
    assert "badge ico rendering" in html
    # The word must not come back as visible pill text -- that is what
    # overflowed. It survives only as the accessible name.
    assert ">rendering<" not in html


def test_the_icon_still_says_what_it_means(card):
    """An icon with no accessible name is worse than the word it replaced."""
    html = card()
    assert 'title="rendering"' in html
    assert 'aria-label="rendering"' in html
    assert 'role="img"' in html


def test_each_state_gets_its_own_shape_not_just_its_own_colour(card):
    """Meaning carried by colour alone is lost to a colour vision
    deficiency -- which is exactly why this used to be a word. Distinct
    silhouettes are what make the icon a fair replacement, so a shared
    glyph between two states would quietly undo that."""
    def icon_for(state):
        html = card(LIVE_INSTANCE.replace("state: 'running'", f"state: '{state}'"))
        start = html.index("<svg")
        return html[start:html.index("</svg>", start)]

    shapes = {s: icon_for(s) for s in
              ("running", "queued", "complete", "error", "cancel_acknowledged")}
    assert len(set(shapes.values())) == len(shapes), \
        f"two states share a glyph: {shapes}"


def test_a_username_identical_to_the_label_is_not_printed_twice(card):
    """"sudaouserwithani sudaouserwithani" was noise, and it was what
    pushed the pill off the edge of the card."""
    same = LIVE_INSTANCE.replace("username: 'friend'", "username: 'acct0'")
    html = card(same)
    assert html.count("acct0") == 1, "the name must appear once, not twice"
    assert 'class="sub"' not in html


def test_a_renamed_account_still_shows_its_kaggle_username(card):
    """Dropping the duplicate must not hide the real identity of an
    account the user has given their own nickname."""
    html = card()          # label 'acct0', username 'friend'
    assert 'class="sub"' in html
    assert "friend" in html


# ---------------------------------------------------------------------------
# Live system RAM.
#
# Only the machine's TOTAL was ever shown, once, in the session chip -- so
# a session minutes from an out-of-memory kill displayed a reassuring
# "31.3 GB RAM" the whole way down. ("i thought weed see the system ram
# also", 2026-08-12.)
# ---------------------------------------------------------------------------

LIVE_WITH_RAM = LIVE_INSTANCE.replace(
    "cpuCount: 4, ramTotal: 31.3,",
    "cpuCount: 4, ramTotal: 31.3, ramUsed: 12884901888, cpuPct: 63,")


def test_a_card_shows_system_ram_in_use_not_only_its_size(card):
    html = card(LIVE_WITH_RAM)
    assert "System RAM" in html
    assert "12.0/31G" in html, "used against total, both visible"


def test_system_ram_is_absent_until_it_has_actually_been_sampled(card):
    """A bar at zero would claim a reading nobody took. The card must show
    nothing at all until the first SYSTEM line arrives."""
    html = card()          # no ramUsed in the payload
    assert "System RAM" not in html


# ---------------------------------------------------------------------------
# "finished in 5:20" -- how long a render actually took.
# ---------------------------------------------------------------------------

def _duration(page, seconds):
    out = {}
    loop = QEventLoop()
    page.runJavaScript(
        f"(() => {{ try {{ return String(fmtDuration({seconds}));"
        f" }} catch (e) {{ return 'THREW ' + e; }} }})()",
        lambda r: (out.__setitem__("v", r or ""), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert "v" in out, "the page never answered"
    return out["v"]


def test_a_duration_reads_like_a_stopwatch_under_an_hour(loaded_page):
    page, _ = loaded_page
    assert _duration(page, 320) == "5:20"
    assert _duration(page, 9) == "0:09", "seconds must stay two digits"


def test_a_duration_past_an_hour_is_spelled_out(loaded_page):
    """"1:05:20" is ambiguous at a glance; hours deserve their unit."""
    page, _ = loaded_page
    assert _duration(page, 3920) == "1h 5m 20s"


def test_an_unmeasured_duration_shows_nothing_rather_than_zero(loaded_page):
    """A job launched before start times were recorded has no duration.
    "0:00" would be a claim; blank is the truth."""
    page, _ = loaded_page
    assert _duration(page, "null") == ""


def test_a_finished_card_says_how_long_it_took(card):
    done = LIVE_INSTANCE.replace(
        "framesDone: 1, message: ''",
        "framesDone: 4, message: '', elapsed: 320, finished: true")
    html = card(done)
    assert "finished in 5:20" in html


def test_a_running_card_shows_the_same_field_as_a_stopwatch(card):
    running = LIVE_INSTANCE.replace(
        "framesDone: 1, message: ''",
        "framesDone: 1, message: '', elapsed: 95, finished: false")
    html = card(running)
    assert "1:35" in html
    assert "finished in" not in html


# ---------------------------------------------------------------------------
# A download you can watch.
#
# Progress was emitted all along, but only as a log line that scrolled
# away ("acct0: downloading 42%") -- so a 36 MB fetch over a slow link was
# indistinguishable from a stuck one. ("when i download a file i want to
# see the progress", 2026-08-12.)
# ---------------------------------------------------------------------------

def _download_html(page, label, entry):
    """Render a card with `downloads` primed, then clean up after."""
    out = {}
    loop = QEventLoop()
    page.runJavaScript(
        "(() => { try {"
        f"  downloads[{label!r}] = {entry};"
        f"  const html = String(instanceCard({LIVE_INSTANCE}));"
        f"  delete downloads[{label!r}];"
        "   return html;"
        " } catch (e) { return 'THREW ' + e; } })()",
        lambda r: (out.__setitem__("v", r or ""), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert "v" in out, "the page never answered"
    assert not out["v"].startswith("THREW"), out["v"]
    return out["v"]


def test_a_downloading_card_shows_bytes_and_rate_not_just_a_percent(loaded_page):
    page, _ = loaded_page
    html = _download_html(page, "acct0",
                          "{downloaded: 13002343, total: 37855928, rate: 1887436}")
    assert "downloading" in html
    # fmtBytes is the page's shared formatter: one decimal only below ten
    # units, so 13002343 B reads "12 MB" and a 1.8 MB/s rate keeps its
    # decimal. Asserted in its terms rather than bending a formatter the
    # upload display also uses.
    assert "12 MB" in html and "36 MB" in html, html[-400:]
    assert "1.8 MB/s" in html, "a rate is what says whether it is still moving"


def test_a_download_with_no_declared_total_does_not_invent_one(loaded_page):
    """Kaggle does not always send Content-Length. "12.4 MB of 0" reads as
    a bug; "12.4 MB" reads as incomplete information, which is the truth."""
    page, _ = loaded_page
    html = _download_html(page, "acct0",
                          "{downloaded: 13002343, total: 0, rate: 0}")
    assert "12 MB" in html
    assert "/ 0" not in html and "0 B" not in html
    # fmtBytes renders a zero as an em dash; a rate of nothing must be
    # omitted entirely rather than shown as "· —/s". (Not asserted as
    # "/s" -- that substring is in every closing </span> on the card.)
    assert "MB/s" not in html and "B/s" not in html


def test_a_card_with_no_download_shows_no_download_row(loaded_page):
    page, _ = loaded_page
    out = {}
    loop = QEventLoop()
    page.runJavaScript(
        f"String(instanceCard({LIVE_INSTANCE}))",
        lambda r: (out.__setitem__("v", r or ""), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert "data-dl=" not in out["v"]


# ---------------------------------------------------------------------------
# Who has the scene.
#
# Uploading and SHARING are two steps and only the first was ever visible.
# When two accounts appeared to render nothing (2026-08-12) the first
# question was "did they ever get the file?" and nothing on screen could
# answer it. ("we need the upload process 1, then we need the file sharing
# process next so we should see a check list of instances with the shared
# file.")
# ---------------------------------------------------------------------------

def _share_html(page, stages):
    """Drive showUploadStage through `stages` and return the checklist."""
    calls = "".join(
        f"showUploadStage({{stage: {s!r}, detail: {d!r}}});" for s, d in stages)
    out = {}
    loop = QEventLoop()
    page.runJavaScript(
        "(() => { try {"
        f" resetShare(); {calls}"
        " return document.getElementById('share-list').outerHTML;"
        " } catch (e) { return 'THREW ' + e; } })()",
        lambda r: (out.__setitem__("v", r or ""), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert "v" in out and not out["v"].startswith("THREW"), out.get("v")
    return out["v"]


def test_the_checklist_names_the_owner_and_everyone_waiting(loaded_page):
    page, _ = loaded_page
    html = _share_html(page, [
        ("checking", "sudao/remember-blend"),
        ("verifying", "sudaouserwithani"),
        ("sharing", "worpstudios, johnokadah"),
    ])
    assert "sudaouserwithani" in html and "owner" in html
    assert "worpstudios" in html and "johnokadah" in html
    assert html.count("waiting for access") == 2


def test_each_account_is_ticked_as_it_confirms_it_can_see_the_file(loaded_page):
    page, _ = loaded_page
    html = _share_html(page, [
        ("checking", "x"), ("verifying", "sudaouserwithani"),
        ("sharing", "worpstudios, johnokadah"),
        ("verifying-access", "worpstudios"),
    ])
    assert "has the scene" in html
    assert "waiting for access" in html, "johnokadah has not confirmed yet"
    assert html.count("has the scene") == 1


def test_ready_means_every_account_has_it(loaded_page):
    page, _ = loaded_page
    html = _share_html(page, [
        ("checking", "x"), ("verifying", "owner-acct"),
        ("sharing", "a, b"), ("verifying-access", "a"),
        ("ready", "owner-acct/remember-blend"),
    ])
    assert "waiting for access" not in html
    assert html.count("has the scene") == 2


def test_the_checklist_is_hidden_when_nothing_is_being_shared(loaded_page):
    page, _ = loaded_page
    html = _share_html(page, [])
    assert "hidden" in html


def test_the_owner_card_is_badged(card):
    owned = LIVE_INSTANCE.replace("verified: true", "verified: true, owner: true")
    html = card(owned)
    assert ">owner<" in html


def test_a_non_owner_card_is_not_badged(card):
    html = card()
    assert ">owner<" not in html
