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


# ---------------------------------------------------------------------------
# Previewing one frame.
#
# "can we see one rendered image in our app instead of having to download
# it" (2026-08-12). The notebook writes loose per-frame images alongside
# the archive, and Kaggle gives a URL per file, so exactly one ~2 MB PNG is
# fetched rather than the whole job's output.
# ---------------------------------------------------------------------------

def _grid_html(page, job_js, instances_js):
    """Render one job's own frame grid by calling the page's own
    renderFrameGrid(job, instances) -- PURE now, like instanceCard(), so
    the concurrent-scenes rewrite (Task 7) can call it once per job
    without each call fighting the others over a single shared #fgrid
    element. Bailed out by a timer as well as by the callback, same as
    _card_html.
    """
    out = {}
    loop = QEventLoop()
    page.runJavaScript(
        "(() => { try { return String(renderFrameGrid("
        f"{job_js}, {instances_js}));"
        " } catch (e) { return 'THREW ' + e; } })()",
        lambda r: (out.__setitem__("v", r or ""), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert "v" in out and not out["v"].startswith("THREW"), out.get("v")
    return out["v"]


FOUR_FRAME_JOB = "{blend:'x.blend', startFrame:1, endFrame:4}"
TWO_DONE = ("[{worker:{state:'running', frames:[1,2,3,4], framesDone:2},"
            " live:{framesDone:2, framesTotal:4}}]")


def test_a_finished_frame_is_clickable(loaded_page):
    page, _ = loaded_page
    html = _grid_html(page, FOUR_FRAME_JOB, TWO_DONE)
    assert 'data-frame="1"' in html and 'data-frame="2"' in html
    assert "click to preview" in html


def test_an_unfinished_frame_is_not_clickable(loaded_page):
    """There is nothing on Kaggle to fetch for a frame that has not been
    rendered, so it must not offer."""
    page, _ = loaded_page
    html = _grid_html(page, FOUR_FRAME_JOB, TWO_DONE)
    assert 'data-frame="3"' not in html and 'data-frame="4"' not in html


def test_a_finished_frame_is_reachable_by_keyboard(loaded_page):
    page, _ = loaded_page
    html = _grid_html(page, FOUR_FRAME_JOB, TWO_DONE)
    assert 'role="button"' in html and 'tabindex="0"' in html


# ---------------------------------------------------------------------------
# Several scenes rendering at once (Task 7).
#
# Tasks 3-6 made the backend track several concurrent jobs and put
# `jobId` on every instance; before this, renderState/renderFrameGrid still
# only ever read the single, most-recent `state.job`, so a second scene's
# accounts landed in the SAME flat card grid with no heading saying which
# scene they belonged to, and its frames were never shown at all.
# ---------------------------------------------------------------------------

def _state_html(page, payload_js, element_id="instances"):
    """Drive the real renderState(json) and return one element's innerHTML.

    Mirrors _share_html's own shape: build the state through the page's
    own function rather than hand-assembling markup, then read back
    whatever DOM it produced.
    """
    out = {}
    loop = QEventLoop()
    page.runJavaScript(
        "(() => { try {"
        f" renderState(JSON.stringify({payload_js}));"
        f" return document.getElementById({element_id!r}).innerHTML;"
        " } catch (e) { return 'THREW ' + e; } })()",
        lambda r: (out.__setitem__("v", r or ""), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert "v" in out and not out["v"].startswith("THREW"), out.get("v")
    return out["v"]


def _two_scene_state(extra_job_fields=""):
    """Two jobs (alpha/beta), one account each, nothing idle."""
    return f"""({{
      job: null,
      jobs: [
        {{jobId:'j-alpha', scene:'alpha', blend:'alpha.blend',
          startFrame:1, endFrame:4, labels:['acct0'],
          elapsed: 12, finished: false{extra_job_fields}}},
        {{jobId:'j-beta', scene:'beta', blend:'beta.blend',
          startFrame:1, endFrame:4, labels:['acct1'],
          elapsed: 8, finished: false{extra_job_fields}}}
      ],
      instances: [
        {{label:'acct0', username:'acct0', verified:true, revoked:false,
          owner:false, jobId:'j-alpha', quota:'', hardware:null,
          worker:{{state:'running', frames:[1,2,3,4], framesDone:2,
                  message:'', elapsed:12, finished:false}}, live:null}},
        {{label:'acct1', username:'acct1', verified:true, revoked:false,
          owner:false, jobId:'j-beta', quota:'', hardware:null,
          worker:{{state:'running', frames:[1,2,3,4], framesDone:1,
                  message:'', elapsed:8, finished:false}}, live:null}}
      ],
      dataset: null, unshared: null, unreadableJobs: [], blend: null,
      approximate: true
    }})"""


def test_instances_are_grouped_by_the_scene_they_are_rendering(loaded_page):
    """Two scenes at once, and no way to tell which card belongs to which,
    would be worse than not having the feature."""
    page, _ = loaded_page
    html = _state_html(page, _two_scene_state())
    assert "alpha" in html and "beta" in html
    assert html.index("alpha") < html.index("acct0")


def test_each_job_gets_its_own_frame_grid(loaded_page):
    page, _ = loaded_page
    html = _state_html(page, _two_scene_state())
    assert html.count('class="fgrid"') == 2


def test_an_account_already_rendering_cannot_be_assigned_to_a_second_scene(
        loaded_page):
    """It would spend that account's quota twice for the same output."""
    page, _ = loaded_page
    payload = """({
      job: null, jobs: [{jobId:'j-alpha', scene:'alpha', blend:'alpha.blend',
        startFrame:1, endFrame:4, labels:['acct0'], elapsed:null,
        finished:false}],
      instances: [
        {label:'acct0', username:'acct0', verified:true, revoked:false,
         owner:false, jobId:'j-alpha', quota:'', hardware:null,
         worker:{state:'running', frames:[1,2,3,4], framesDone:1,
                 message:'', elapsed:5, finished:false}, live:null},
        {label:'acct1', username:'acct1', verified:true, revoked:false,
         owner:false, jobId:null, quota:'', hardware:null,
         worker:null, live:null}
      ],
      dataset: null, unshared: null, unreadableJobs: [], blend: null,
      approximate: true
    })"""
    checkbox_html = _state_html(page, payload, element_id="assign-list")
    assert "disabled" in checkbox_html
    # The free account must still be OFFERED, not merely absent from the
    # list -- unticking is a choice the user makes, not one made for them.
    assert "acct1" in checkbox_html and "checked" in checkbox_html


def _preview_state(page, script):
    out = {}
    loop = QEventLoop()
    page.runJavaScript(
        "(() => { try { " + script + " } catch (e) { return 'THREW ' + e; } })()",
        lambda r: (out.__setitem__("v", r or ""), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert "v" in out and not str(out["v"]).startswith("THREW"), out.get("v")
    return out["v"]


def test_opening_a_preview_shows_the_frame_and_who_rendered_it(loaded_page):
    page, _ = loaded_page
    result = _preview_state(page,
        "openPreview(7, 'file:///tmp/f_0007.png', 'stive');"
        " return JSON.stringify({"
        "  hidden: document.getElementById('lightbox').hidden,"
        "  title: document.getElementById('lb-title').textContent,"
        "  sub: document.getElementById('lb-sub').textContent,"
        "  src: document.getElementById('lb-img').getAttribute('src')});")
    import json as _json
    got = _json.loads(result)
    assert got["hidden"] is False
    assert got["title"] == "frame 7"
    assert "stive" in got["sub"]
    assert got["src"].endswith("f_0007.png")


def test_the_page_offers_a_blender_version_picker(loaded_page):
    page, _ = loaded_page
    out = {}
    loop = QEventLoop()
    page.runJavaScript(
        "String(document.getElementById('sel-blender') !== null)",
        lambda r: (out.__setitem__("v", r), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert out["v"] == "true"


def test_the_picker_is_populated_from_the_bridge_payload(loaded_page):
    """A static <select> proves nothing on its own: the element could
    exist while the code that fills it from the bridge had been deleted
    entirely, and this same fixture (loaded without the QWebChannel shim)
    would still show it as present. Calling the page's own
    populateBlenderVersions() with a hand-built payload is what actually
    exercises that code."""
    page, _ = loaded_page
    result = _preview_state(page,
        "populateBlenderVersions(JSON.stringify({"
        "  versions: ['5.2.0', '4.5.3', '4.2.9'], current: '4.2.9'}));"
        " const sel = document.getElementById('sel-blender');"
        " return JSON.stringify({"
        "  options: Array.from(sel.options).map(o => o.value),"
        "  value: sel.value});")
    import json as _json
    got = _json.loads(result)
    assert got["options"] == ["5.2.0", "4.5.3", "4.2.9"]
    assert got["value"] == "4.2.9", "the current version must be preselected"


def test_the_launch_options_carry_the_chosen_blender_version(loaded_page):
    """renderOptions() is what launch() and sendJob() both read -- if it
    never picked up the select's value, the version chosen on the page
    would never reach a render."""
    page, _ = loaded_page
    result = _preview_state(page,
        "populateBlenderVersions(JSON.stringify({"
        "  versions: ['5.2.0', '4.2.9'], current: '5.2.0'}));"
        " document.getElementById('sel-blender').value = '4.2.9';"
        " return JSON.stringify(renderOptions());")
    import json as _json
    got = _json.loads(result)
    assert got["blenderVersion"] == "4.2.9"


def test_closing_a_preview_drops_the_image(loaded_page):
    """Left in place, the previous frame flashes up while the next one is
    still decoding -- which reads as the wrong frame having been fetched."""
    page, _ = loaded_page
    result = _preview_state(page,
        "openPreview(7, 'file:///tmp/f_0007.png', 'stive'); closePreview();"
        " return JSON.stringify({"
        "  hidden: document.getElementById('lightbox').hidden,"
        "  src: document.getElementById('lb-img').getAttribute('src')});")
    import json as _json
    got = _json.loads(result)
    assert got["hidden"] is True
    assert got["src"] is None


# ---------------------------------------------------------------------------
# Task 7, Fix round 1: two Criticals, two Importants, two Minors caught by
# review. Each test below reassigns the module-level `backend` (a plain
# `let` at the top of app.js, reachable from any later runJavaScript call
# in the same page realm -- confirmed empirically before writing these) to
# a recorder, so the real click handlers run for real without needing an
# actual QWebChannel connection.
# ---------------------------------------------------------------------------

def _idle_pair_state():
    return """({
      job: null, jobs: [],
      instances: [
        {label:'acct0', username:'acct0', verified:true, revoked:false,
         owner:false, jobId:null, quota:'', hardware:null, worker:null,
         live:null},
        {label:'acct1', username:'acct1', verified:true, revoked:false,
         owner:false, jobId:null, quota:'', hardware:null, worker:null,
         live:null}
      ],
      dataset: null, unshared: null, unreadableJobs: [], blend: null,
      approximate: true
    })"""


def test_an_unticked_account_stays_unticked_across_a_state_tick(loaded_page):
    """CRITICAL: renderAssignList used to rebuild #assign-list from
    scratch on every stateChanged (the 30s poll among them) with every
    free account hard-coded `checked`, so a deliberate untick survived
    only until the next tick and launch() would then render on an
    account the user had just excluded."""
    page, _ = loaded_page
    state = _idle_pair_state()
    result = _preview_state(page,
        f"renderState(JSON.stringify({state}));"
        " const box = document.querySelector("
        "   '#assign-list input[data-assign=\"acct0\"]');"
        " box.checked = false;"
        " box.dispatchEvent(new Event('change', {bubbles: true}));"
        # A later poll tick, re-rendering from an identical payload.
        f" renderState(JSON.stringify({state}));"
        " const after = document.querySelector("
        "   '#assign-list input[data-assign=\"acct0\"]');"
        " return JSON.stringify({"
        "   checked: after.checked, labels: renderOptions().labels});")
    import json as _json
    got = _json.loads(result)
    assert got["checked"] is False, "the untick did not survive the next tick"
    assert "acct0" not in got["labels"]
    assert "acct1" in got["labels"]


def test_a_finished_jobs_accounts_are_not_shown_as_busy(loaded_page):
    """CRITICAL: fleet.py's ACTIVE_STATES is {queued, running} and
    free_accounts() says a completed job holds nobody -- but the render
    panel used to treat `!!inst.jobId` as busy, which stays true long
    after the job behind it finished (jobId is only cleared by forgetting
    the job). A completed render's accounts showed disabled with a false
    "already rendering" title, and there was no tickable account left to
    fix it with."""
    page, _ = loaded_page
    # A label not used by any other test in this module: assignUnchecked
    # (the fix for the CRITICAL above) is page-side state that persists
    # for the whole module-scoped fixture, so reusing "acct0" here would
    # pick up whatever an earlier test left unticked and fail for a
    # reason unrelated to what THIS test checks.
    payload = """({
      job: null,
      jobs: [{jobId:'j-alpha', scene:'alpha', blend:'alpha.blend',
        startFrame:1, endFrame:4, labels:['acct-finished'], elapsed:20,
        finished:true}],
      instances: [
        {label:'acct-finished', username:'acct-finished', verified:true,
         revoked:false, owner:false, jobId:'j-alpha', quota:'',
         hardware:null,
         worker:{state:'complete', frames:[1,2,3,4], framesDone:4,
                 message:'', elapsed:20, finished:true}, live:null}
      ],
      dataset: null, unshared: null, unreadableJobs: [], blend: null,
      approximate: true
    })"""
    html = _state_html(page, payload, element_id="assign-list")
    assert "disabled" not in html
    assert "already rendering" not in html
    assert "checked" in html


def test_frame_preview_is_scoped_to_the_grid_it_was_clicked_in(loaded_page):
    """IMPORTANT: previewFrame() used to take only a frame number, which
    the bridge resolved via Fleet.load() -- "the most recent job". With
    two jobs both showing a done frame 2, clicking scene alpha's cell
    must record alpha's own job id, not whichever job happens to be more
    recent -- the grid has carried data-job since this task's first
    draft; only the click handler was never taught to read it."""
    page, _ = loaded_page
    payload = """({
      job: null,
      jobs: [
        {jobId:'j-alpha', scene:'alpha', blend:'alpha.blend',
         startFrame:1, endFrame:4, labels:['acct0'], elapsed:null,
         finished:false},
        {jobId:'j-beta', scene:'beta', blend:'beta.blend',
         startFrame:1, endFrame:4, labels:['acct1'], elapsed:null,
         finished:false}
      ],
      instances: [
        {label:'acct0', username:'acct0', verified:true, revoked:false,
         owner:false, jobId:'j-alpha', quota:'', hardware:null,
         worker:{state:'running', frames:[1,2,3,4], framesDone:2,
                 message:'', elapsed:null, finished:false}, live:null},
        {label:'acct1', username:'acct1', verified:true, revoked:false,
         owner:false, jobId:'j-beta', quota:'', hardware:null,
         worker:{state:'running', frames:[1,2,3,4], framesDone:2,
                 message:'', elapsed:null, finished:false}, live:null}
      ],
      dataset: null, unshared: null, unreadableJobs: [], blend: null,
      approximate: true
    })"""
    result = _preview_state(page,
        f"renderState(JSON.stringify({payload}));"
        " const calls = [];"
        " backend = { previewFrame: (f, j) => calls.push([f, j]) };"
        " document.querySelectorAll('.fgrid[data-job]').forEach("
        "   g => g.querySelector('[data-frame=\"2\"]').click());"
        " backend = null;"
        " return JSON.stringify(calls);")
    import json as _json
    calls = _json.loads(result)
    assert [2, "j-alpha"] in calls, calls
    assert [2, "j-beta"] in calls, calls


def test_cancelling_a_job_asks_for_confirmation_first(loaded_page):
    """IMPORTANT: several identical red Cancel buttons sit in a list that
    reshuffles every 30 seconds; one misclick used to end work other
    people's quota already paid for, immediately, with no undo.
    btn-forget confirms for an action that is strictly LESS destructive
    (forgetting stops nothing) -- this must confirm too.

    Must-fix 1: this now drives backend.cancelJob(jobId) -- the per-job
    Cancel button used to loop cancelInstance() per account instead,
    which only ever reaches Fleet.cancel_worker() -> load()'s single
    newest job, so cancelling any OLDER of two live scenes cancelled
    nothing at all and reported "already stopped" while its kernels kept
    running and billing.

    Clicks the SECOND job's button (j-beta, jobs[1]), not the first: a
    handler rewired to always use jobs[0].jobId -- beta's Cancel button
    silently stopping alpha's kernels instead -- would still pass a
    version of this test that only ever clicked jobs[0]'s own button,
    since jobs[0].jobId and the clicked button's id would coincide."""
    page, _ = loaded_page
    state = _two_scene_state()
    result = _preview_state(page,
        f"renderState(JSON.stringify({state}));"
        " const cancelled = [];"
        " const confirmMessages = [];"
        " backend = { cancelJob: jobId => cancelled.push(jobId) };"
        " window.confirm = msg => { confirmMessages.push(msg); return false; };"
        " document.querySelector('[data-job-cancel=\"j-beta\"]').click();"
        " const declined = cancelled.slice();"
        " window.confirm = msg => { confirmMessages.push(msg); return true; };"
        " document.querySelector('[data-job-cancel=\"j-beta\"]').click();"
        " const accepted = cancelled.slice();"
        " backend = null;"
        " return JSON.stringify({declined, accepted, confirmMessages});")
    import json as _json
    got = _json.loads(result)
    assert got["declined"] == [], "declining must not cancel anything"
    assert got["accepted"] == ["j-beta"], \
        "accepting must cancel exactly this job, by id, not every job"
    assert all("beta" in m and "acct1" in m for m in got["confirmMessages"]), \
        "the confirmation must name the SECOND job, not jobs[0]"


def test_confirmed_shared_and_never_checked_are_not_the_same_banner(
        loaded_page):
    """Minor: `unshared: null` (nothing recorded this session) and
    `unshared: {accounts: {}}` (a real upload that reached everyone) used
    to both render as no banner at all, so "we do not know" and "we
    checked and it is fine" were indistinguishable -- on a page whose own
    rule is that an unknown must never read as fine."""
    page, _ = loaded_page
    common = ("job: null, jobs: [], instances: [], unreadableJobs: [], "
              "blend: null, approximate: true, dataset: null")
    never_checked = _state_html(
        page, "({" + common + ", unshared: null})",
        element_id="unshared-banner")
    confirmed_fine = _state_html(
        page,
        "({" + common + ", unshared: {accounts: {}, "
        "note: 'the last upload reached everyone'}})",
        element_id="unshared-banner")
    assert never_checked.strip() == ""
    assert "shared with every account" in confirmed_fine
    assert never_checked != confirmed_fine


def test_forgetting_an_unreadable_record_asks_for_confirmation_first(
        loaded_page):
    """Minor: this used to fire on the first click, with the "not a
    cancel" honesty confined to a hover title nobody has to read. It
    permanently discards what may be the only surviving trace of kernels
    still billing on Kaggle -- the same stakes as btn-forget's own
    confirm.

    Two unreadable records, not one (same single-row hole must-fix 6
    fixed for scenes): a one-row fixture makes "the button clicked" and
    "the first row" the same element, so a handler rebuilt around a
    hard-coded row 0 would still pass unnoticed. Clicking the SECOND
    row's button must reach the SECOND record's own index/fingerprint."""
    page, _ = loaded_page
    payload = """({
      job: null, jobs: [], instances: [],
      unreadableJobs: [
        {index:0, jobId:'j1', blend:'x.blend',
         kernels:['a/b'], kernelUrls:['https://www.kaggle.com/code/a/b'],
         message:'could not read it', fingerprint:'fp1'},
        {index:1, jobId:'j2', blend:'y.blend',
         kernels:['c/d'], kernelUrls:['https://www.kaggle.com/code/c/d'],
         message:'could not read it either', fingerprint:'fp2'}
      ],
      blend: null, approximate: true, dataset: null, unshared: null
    })"""
    result = _preview_state(page,
        f"renderState(JSON.stringify({payload}));"
        " const forgotten = [];"
        " backend = { forgetUnreadableJob: (i, fp) => forgotten.push([i, fp]) };"
        " window.confirm = () => false;"
        " document.querySelectorAll('[data-forget-unreadable]')[1].click();"
        " const declined = forgotten.slice();"
        " window.confirm = () => true;"
        " document.querySelectorAll('[data-forget-unreadable]')[1].click();"
        " const accepted = forgotten.slice();"
        " backend = null;"
        " return JSON.stringify({declined, accepted});")
    import json as _json
    got = _json.loads(result)
    assert got["declined"] == [], "declining must not forget the record"
    assert got["accepted"] == [[1, "fp2"]], (
        "clicking the SECOND row's Forget button must forget the SECOND "
        f"record, not the first: {got['accepted']}")


# ---------------------------------------------------------------------------
# The scene library (Task 11): browse scenes already on Kaggle, re-render
# one with no re-upload, or delete one for good.
# ---------------------------------------------------------------------------

def _scene_confirm_html(page, scene_js):
    """Drive the page's own deleteConfirmMessage(scene) and return it."""
    out = {}
    loop = QEventLoop()
    page.runJavaScript(
        "(() => { try { return String(deleteConfirmMessage("
        + scene_js + ")); } catch (e) { return 'THREW ' + e; } })()",
        lambda r: (out.__setitem__("v", r or ""), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert "v" in out and not out["v"].startswith("THREW"), out.get("v")
    return out["v"]


def test_the_delete_confirm_names_the_scene_and_says_it_is_permanent(
        loaded_page):
    """Fix round 1, Important 2: the brief's own `"remember" in html`
    assertion is vacuous -- the message's own boilerplate used to contain
    the literal word "remember" ("-- and remember: any account...")
    regardless of the scene's actual name, so this passed even with
    `scene.name` dropped entirely (verified pre-fix: it still passed with
    no `name` at all, rendering `Delete "undefined"?`). Asserted here
    where the name actually has to appear -- the quoted title -- and the
    wording no longer uses that word at all (see deleteConfirmMessage's
    own comment)."""
    page, _ = loaded_page
    html = _scene_confirm_html(
        page,
        "{name:'remember', slug:'user0/remember-blend', sizeBytes:52428800}")
    assert '"remember"' in html
    assert "cannot be undone" in html


def test_the_delete_confirm_names_a_distinctive_scene_name(loaded_page):
    """A name chosen so it cannot collide with any word already in the
    message's own boilerplate -- pins that the NAME shown is the one
    passed in, not an accident of shared vocabulary with the wording
    around it."""
    page, _ = loaded_page
    html = _scene_confirm_html(
        page,
        "{name:'xyzzy-plugh-42', slug:'user0/xyzzy-plugh-42-blend', "
        "sizeBytes:1024}")
    assert '"xyzzy-plugh-42"' in html


def test_the_delete_confirm_never_says_the_word_remember(loaded_page):
    """Pins Important 2's actual fix, not just the reworded test above:
    the boilerplate itself must not reintroduce a word that happens to
    collide with a real scene name used throughout this app's own tests
    and docs."""
    page, _ = loaded_page
    html = _scene_confirm_html(
        page,
        "{name:'xyzzy-plugh-42', slug:'user0/xyzzy-plugh-42-blend', "
        "sizeBytes:1024}")
    assert "remember" not in html.lower()


def test_the_delete_confirm_says_sharing_accounts_lose_access(loaded_page):
    page, _ = loaded_page
    html = _scene_confirm_html(
        page,
        "{name:'remember', slug:'user0/remember-blend', sizeBytes:1024}")
    assert "loses access" in html or "lose access" in html


def test_the_delete_confirm_names_the_size(loaded_page):
    page, _ = loaded_page
    html = _scene_confirm_html(
        page,
        "{name:'remember', slug:'user0/remember-blend', sizeBytes:1048576}")
    assert "1 MB" in html or "1.0 MB" in html


def _scenes_html(page, payload_js, element_id="scene-list"):
    """Drive the page's own renderScenes(json) and return one element's
    innerHTML. Mirrors _state_html's own shape."""
    out = {}
    loop = QEventLoop()
    page.runJavaScript(
        "(() => { try {"
        f" renderScenes(JSON.stringify({payload_js}));"
        f" return document.getElementById({element_id!r}).innerHTML;"
        " } catch (e) { return 'THREW ' + e; } })()",
        lambda r: (out.__setitem__("v", r or ""), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert "v" in out and not out["v"].startswith("THREW"), out.get("v")
    return out["v"]


ONE_SCENE = """({
  scenes: [{slug:'user0/remember-blend', name:'remember', owner:'user0',
            sizeBytes:1048576, updated:null, blendName:'remember.blend'}],
  errors: {}
})"""

# Must-fix 6: a SECOND scene, distinct from the first in every field a
# handler could plausibly read from -- slug, name, size. A single-row
# fixture (ONE_SCENE above) makes "the button I clicked" and "the first
# row" the same element, so a handler rebuilt around a hard-coded first
# row would still pass every ONE_SCENE-driven test. Tests that must prove
# ROUTING (not just that render/delete work at all) click the SECOND row
# here and assert on the SECOND scene's own slug/name -- the same pattern
# test_frame_preview_is_scoped_to_the_grid_it_was_clicked_in already uses
# for frame grids.
TWO_SCENES = """({
  scenes: [
    {slug:'user0/remember-blend', name:'remember', owner:'user0',
     sizeBytes:1048576, updated:null, blendName:'remember.blend'},
    {slug:'user1/second-scene-blend', name:'second scene', owner:'user1',
     sizeBytes:2097152, updated:null, blendName:'second-scene.blend'}
  ],
  errors: {}
})"""


def test_a_scene_row_offers_render_and_delete(loaded_page):
    page, _ = loaded_page
    html = _scenes_html(page, ONE_SCENE)
    assert 'data-scene-render="user0/remember-blend"' in html
    assert 'data-scene-delete="user0/remember-blend"' in html
    assert "Render this" in html
    assert "Delete" in html


def test_an_undated_scene_never_shows_a_default_date(loaded_page):
    """Scene.updated is datetime | None -- absent must read as unknown,
    never as some default (e.g. epoch) date standing in for it."""
    page, _ = loaded_page
    html = _scenes_html(page, ONE_SCENE)
    assert "unknown" in html
    assert "1970" not in html and "Jan 1" not in html


def _scene_updated_text(page, iso_js):
    """Drive the page's own fmtSceneUpdated(iso) directly."""
    out = {}
    loop = QEventLoop()
    page.runJavaScript(
        "(() => { try { return String(fmtSceneUpdated("
        + iso_js + ")); } catch (e) { return 'THREW ' + e; } })()",
        lambda r: (out.__setitem__("v", r or ""), loop.quit()))
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert "v" in out and not out["v"].startswith("THREW"), out.get("v")
    return out["v"]


def test_a_future_timestamp_is_not_dressed_up_as_just_now(loaded_page):
    """Fix round 1, Minor: `Math.max(age, 0)` used to clamp clock skew
    (a timestamp in the future) into a false "just now" reading -- one of
    the fabricated readings this app forbids everywhere else."""
    page, _ = loaded_page
    text = _scene_updated_text(
        page, "new Date(Date.now() + 3600000).toISOString()")
    assert "just now" not in text
    assert "unknown" in text


def test_an_unparseable_timestamp_is_not_shown_as_nan(loaded_page):
    """Fix round 1, Minor: an unparseable string used to produce
    "NaNd ago" -- also a fabricated reading, not an honest unknown."""
    page, _ = loaded_page
    text = _scene_updated_text(page, "'not-a-real-date'")
    assert "NaN" not in text
    assert "unknown" in text


def test_a_scene_never_claims_to_be_verified(loaded_page):
    """The "-blend" suffix is a naming CONVENTION, not proof (scenes.py's
    own docstring) -- the page must say so, not present it as confirmed."""
    page, _ = loaded_page
    html = _scenes_html(page, ONE_SCENE)
    assert "unverified" in html


def test_no_scenes_shows_an_empty_state_not_a_blank_panel(loaded_page):
    page, _ = loaded_page
    html = _scenes_html(page, "({scenes: [], errors: {}})")
    assert "No scenes" in html


def test_no_scenes_because_every_account_failed_says_unknown_not_empty(
        loaded_page):
    """Fix round 1, Important 1: with EVERY account erroring (a single
    misconfigured account is the common case this hits), "No scenes on
    Kaggle yet" is a false positive claim about what IS on Kaggle -- the
    truth is that nothing could be READ, which is a different, weaker
    claim. index.html's own comment on #scene-errors already says this
    absence "must never be mistaken for 'nothing on Kaggle'"; this pins
    that the empty-list sentence itself honours that, not just the
    banner above it."""
    page, _ = loaded_page
    html = _scenes_html(page, "({scenes: [], "
                         "errors: {acct0: 'rate limited'}})")
    assert "No scenes on Kaggle yet" not in html
    assert "unknown" in html


def test_one_unreachable_account_does_not_hide_the_others_scenes_on_the_page(
        loaded_page):
    """The same payload drives both halves of the page: the reachable
    scene must still show even though one account's own listing failed."""
    page, _ = loaded_page
    payload = """({
      scenes: [{slug:'user0/remember-blend', name:'remember', owner:'user0',
                sizeBytes:1024, updated:null, blendName:'remember.blend'}],
      errors: {acct1: 'could not list datasets: rate limited'}
    })"""
    list_html = _scenes_html(page, payload, element_id="scene-list")
    err_html = _scenes_html(page, payload, element_id="scene-errors")
    assert "remember" in list_html
    assert "acct1" in err_html and "rate limited" in err_html


def test_no_errors_shows_no_error_banner(loaded_page):
    page, _ = loaded_page
    err_html = _scenes_html(page, ONE_SCENE, element_id="scene-errors")
    assert err_html.strip() == ""


def test_clicking_render_this_calls_render_scene_with_the_slug(loaded_page):
    """Two scenes, not one (must-fix 6): with a single-row fixture, "the
    button that was clicked" and "the first row" are the same element, so
    a handler rebuilt around a hard-coded first row would still pass this
    test unnoticed. Clicking the SECOND row's button must reach the
    SECOND scene's own slug, never the first."""
    page, _ = loaded_page
    result = _preview_state(page,
        f"renderScenes(JSON.stringify({TWO_SCENES}));"
        " const calls = [];"
        " backend = { renderScene: (slug, opts) => calls.push([slug, opts]) };"
        " document.querySelectorAll('[data-scene-render]')[1].click();"
        " backend = null;"
        " return JSON.stringify(calls);")
    import json as _json
    calls = _json.loads(result)
    assert len(calls) == 1
    slug, opts_json = calls[0]
    assert slug == "user1/second-scene-blend"
    opts = _json.loads(opts_json)
    assert "startFrame" in opts and "endFrame" in opts


def test_clicking_delete_asks_for_confirmation_first(loaded_page):
    """Deletion is irreversible -- see deleteConfirmMessage's own tests
    above -- so declining must leave deleteScene uncalled, exactly like
    every other irreversible action on this page (cancel, forget).

    Two scenes, not one (must-fix 6): clicking the SECOND row's Delete
    button must both confirm-and-delete the SECOND scene's own slug and
    name it (not the first scene's) in the confirmation dialog -- a
    single-row fixture cannot tell "the row clicked" from "the first
    row", which is exactly the hole that would let every Delete button
    silently act on scene #1 with a confirm that confidently names scene
    #1."""
    page, _ = loaded_page
    result = _preview_state(page,
        f"renderScenes(JSON.stringify({TWO_SCENES}));"
        " const deleted = [];"
        " let confirmMessage = '';"
        " backend = { deleteScene: slug => deleted.push(slug) };"
        " window.confirm = m => { confirmMessage = m; return false; };"
        " document.querySelectorAll('[data-scene-delete]')[1].click();"
        " const declined = deleted.slice();"
        " window.confirm = m => { confirmMessage = m; return true; };"
        " document.querySelectorAll('[data-scene-delete]')[1].click();"
        " const accepted = deleted.slice();"
        " backend = null;"
        " return JSON.stringify({declined, accepted, confirmMessage});")
    import json as _json
    got = _json.loads(result)
    assert got["declined"] == [], "declining must not delete anything"
    assert got["accepted"] == ["user1/second-scene-blend"], (
        "clicking the SECOND row's Delete button must delete the SECOND "
        f"scene, not the first: {got['accepted']}")
    assert "second scene" in got["confirmMessage"], (
        "the confirmation must name the scene actually clicked, not "
        f"whichever scene happens to be first: {got['confirmMessage']!r}")


# ---------------------------------------------------------------------------
# Long messages must WRAP, not get cut off.
#
# Reported from the field with a screenshot: the notification panel showed
#
#     Uploading the scene failed: could not list the files in datase|
#     'sudaouserwithani/stranger-blend': 403 Client Error: Forbid|
#
# with the right-hand side of every line sliced off. The cause is a flex
# item's default min-width:auto -- the text column refused to shrink below
# its longest unbreakable word, and our error messages quote Kaggle URLs
# like api.kaggle.com/v1/datasets.DatasetApiService/ListDatasetFiles. That
# one token pushed the column wider than the panel, which is
# overflow:hidden.
#
# Measured in the real Chromium layout rather than asserted against the CSS
# text, because the property that matters is whether anything overflows.
# ---------------------------------------------------------------------------

_CLIPPING_MESSAGE = (
    "Uploading the scene failed: could not list the files in dataset "
    "'sudaouserwithani/stranger-blend': 403 Client Error: Forbidden for "
    "url: https://api.kaggle.com/v1/datasets.DatasetApiService/"
    "ListDatasetFiles. The most likely cause is that the dataset has since "
    "been deleted or renamed on kaggle.com.")


def _js_string(text):
    """A JS string literal. repr() is not one: the message itself quotes a
    dataset slug, and swapping quote characters breaks the literal."""
    import json
    return json.dumps(text)


def _overflow_of(page, js):
    out = {}
    loop = QEventLoop()

    def done(result):
        out["v"] = result or ""
        loop.quit()

    page.runJavaScript(
        "(() => { try { return String(" + js + "); }"
        " catch (e) { return 'THREW ' + e; } })()", done)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    assert "v" in out, "the page never answered runJavaScript"
    assert not out["v"].startswith("THREW"), out["v"]
    return out["v"]


def test_a_long_notification_does_not_overflow_its_panel(loaded_page):
    page, _ = loaded_page
    js = (
        "(() => {"
        "  notify(" + _js_string(_CLIPPING_MESSAGE) + ", 'offline');"
        "  const panel = document.getElementById('notif-panel');"
        "  panel.classList.add('show');"
        "  const item = document.querySelector('#notif-list .notif-item');"
        "  const msg = item.querySelector('.ni-m');"
        "  const over = msg.getBoundingClientRect().right"
        "               - panel.getBoundingClientRect().right;"
        "  panel.classList.remove('show');"
        "  return Math.round(over);"
        "})()")
    overflow = int(_overflow_of(page, js))
    assert overflow <= 0, (
        f"the message runs {overflow}px past the right edge of the "
        "notification panel, which is overflow:hidden -- so that much of "
        "every long error is invisible to the user")


def test_a_long_notification_wraps_to_several_lines(loaded_page):
    """The other half: not overflowing could also be achieved by clipping
    the text to one line, which would be just as unreadable."""
    page, _ = loaded_page
    js = (
        "(() => {"
        "  notify(" + _js_string(_CLIPPING_MESSAGE) + ", 'offline');"
        "  const panel = document.getElementById('notif-panel');"
        "  panel.classList.add('show');"
        "  const msg = document.querySelector('#notif-list .ni-m');"
        "  const lines = msg.getBoundingClientRect().height"
        "                / parseFloat(getComputedStyle(msg).lineHeight);"
        "  panel.classList.remove('show');"
        "  return Math.round(lines);"
        "})()")
    assert int(_overflow_of(page, js)) >= 5, (
        "a 250-character message that fits in fewer than five lines of a "
        "330px panel is being truncated, not wrapped")


# ---------------------------------------------------------------------------
# The frame preview flickering while a render runs (2026-08-15).
#
# Three separate faults sat behind the report, and each is pinned here on
# its own terms. NONE of these tests can see a flicker -- that needs a
# human, a running render and an open preview. What they assert is that
# the three conditions that made the compositor redo work it did not need
# to do are gone, and stay gone.
# ---------------------------------------------------------------------------


def _flicker_state(frames_done):
    """One running account, carrying a number that can be varied so two
    payloads can be made to differ in exactly one place."""
    return f"""({{
      job: null,
      jobs: [{{jobId:'j-flick', scene:'flick', blend:'flick.blend',
               startFrame:1, endFrame:4, labels:['acct0'],
               elapsed:5, finished:false}}],
      instances: [
        {{label:'acct0', username:'acct0', verified:true, revoked:false,
          owner:false, jobId:'j-flick', quota:'', hardware:null,
          worker:{{state:'running', frames:[1,2,3,4],
                  framesDone:{frames_done}, message:'', elapsed:5,
                  finished:false}}, live:null}}
      ],
      dataset: null, unshared: null, unreadableJobs: [], blend: null,
      approximate: true
    }})"""


def test_the_preview_sits_on_its_own_layer_above_the_modal(loaded_page):
    """.lightbox and .modal were both z-index 120, which left "which of
    the two covers the other" to DOM order rather than to a decision.

    Read back through the real CSS engine rather than by grepping the
    stylesheet: a later rule can always override an earlier one, so the
    text of the file is not the answer -- the computed value is."""
    page, _ = loaded_page
    result = _preview_state(page,
        "const modal = document.createElement('div');"
        " modal.className = 'modal';"
        " const tipped = document.createElement('div');"
        " tipped.setAttribute('data-tip', 'x');"
        " document.body.appendChild(modal);"
        " document.body.appendChild(tipped);"
        " const lb = document.getElementById('lightbox');"
        " const was = lb.hidden; lb.hidden = false;"
        " const out = {"
        "   modal: getComputedStyle(modal).zIndex,"
        "   lightbox: getComputedStyle(lb).zIndex,"
        "   toasts: getComputedStyle("
        "     document.getElementById('toast-stack')).zIndex,"
        "   tooltip: getComputedStyle(tipped, '::after').zIndex};"
        " lb.hidden = was; modal.remove(); tipped.remove();"
        " return JSON.stringify(out);")
    import json as _json
    got = _json.loads(result)
    assert "auto" not in got.values(), got
    layers = {k: int(v) for k, v in got.items()}
    assert layers["lightbox"] > layers["modal"], (
        "the preview must cover a modal, not tie with it: " + str(layers))
    assert layers["modal"] > layers["toasts"], layers
    assert layers["tooltip"] > layers["lightbox"], (
        "a tooltip belongs to controls at every level, including the "
        "preview's own Close button: " + str(layers))


def _alpha_of(css_colour):
    """Alpha out of a computed colour, in EITHER syntax Chromium hands
    back: legacy `rgba(r, g, b, a)`, or CSS Color 4's
    `color(srgb r g b / a)` -- which is what a color-mix() resolves to,
    and is exactly the translucent value this must not miss."""
    if "/" in css_colour:
        return float(css_colour.rsplit("/", 1)[1].strip(" )"))
    if css_colour.startswith("rgba("):
        return float(css_colour[css_colour.index("(") + 1:-1].split(",")[3])
    return 1.0


def test_the_open_preview_has_nothing_left_to_re_blur(loaded_page):
    """A backdrop-filter must be re-sampled by the compositor whenever
    anything BEHIND it repaints -- and behind this sits the whole
    dashboard, every card of it rebuilt on each 2-second live tick of a
    running render. An opaque background has nothing behind it to sample.

    Swept over every mode the app actually ships -- light, dark, and each
    of those with the `glass` translucency preference on -- because --bg
    is a per-theme token and `body.glass` re-blurs surfaces in bulk. A
    background that is only opaque in the theme that happened to be
    loaded would be no fix at all for the user running the other one."""
    page, _ = loaded_page
    result = _preview_state(page,
        "const root = document.documentElement;"
        " const lb = document.getElementById('lightbox');"
        " const wasHidden = lb.hidden, wasTheme = root.dataset.theme;"
        " const wasGlass = document.body.classList.contains('glass');"
        " lb.hidden = false;"
        " const out = {};"
        " ['light', 'dark'].forEach(theme => {"
        "   [false, true].forEach(glass => {"
        "     root.dataset.theme = theme;"
        "     document.body.classList.toggle('glass', glass);"
        "     const cs = getComputedStyle(lb);"
        "     out[theme + (glass ? '+glass' : '')] = {"
        "       bg: cs.backgroundColor,"
        "       filter: cs.backdropFilter || cs.webkitBackdropFilter"
        "               || 'none'};"
        "   });"
        " });"
        " root.dataset.theme = wasTheme;"
        " document.body.classList.toggle('glass', wasGlass);"
        " lb.hidden = wasHidden;"
        " return JSON.stringify(out);")
    import json as _json
    got = _json.loads(result)
    assert set(got) == {"light", "light+glass", "dark", "dark+glass"}, got
    for mode, seen in got.items():
        assert seen["filter"] == "none", \
            f"the preview still carries a backdrop-filter in {mode}: {seen}"
        assert _alpha_of(seen["bg"]) == 1.0, \
            f"the preview backdrop is still translucent in {mode} "\
            f"({seen['bg']}), so the dashboard behind it still forces a "\
            "re-composite"


def test_an_unchanged_payload_does_not_rebuild_every_card(loaded_page):
    """renderState replaced the entire card grid on EVERY state emit, and
    the live tick emits every 2 seconds throughout a render whether or
    not anything moved. Node identity is the assertion: identical HTML
    would look the same either way, but only a skipped rebuild leaves the
    same element in place."""
    page, _ = loaded_page
    result = _preview_state(page,
        f"renderState(JSON.stringify({_flicker_state(1)}));"
        " const first = document.querySelector('#instances .inst');"
        f" renderState(JSON.stringify({_flicker_state(1)}));"
        " const afterSame = document.querySelector('#instances .inst');"
        f" renderState(JSON.stringify({_flicker_state(3)}));"
        " const afterChange = document.querySelector('#instances .inst');"
        " return JSON.stringify({"
        "   present: first !== null,"
        "   kept: first === afterSame,"
        "   replaced: afterChange !== null && afterChange !== first});")
    import json as _json
    got = _json.loads(result)
    assert got["present"], "the first render produced no card at all"
    assert got["kept"], \
        "a byte-identical payload still replaced the card grid"
    assert got["replaced"], \
        "a payload that DID change left the stale card on screen -- the "\
        "skip is now swallowing real updates, which is far worse than "\
        "the flicker it was added to fix"


def test_a_download_tick_still_repaints_the_cards(loaded_page):
    """The other side of the skip. A card also reads `downloads`, which
    is not part of the state payload, so a collect starting or finishing
    changes what the card should say without changing a byte of state --
    precisely the case the skip above is built to ignore. That path goes
    through repaintCards(), which must force the rebuild anyway."""
    page, _ = loaded_page
    result = _preview_state(page,
        f"renderState(JSON.stringify({_flicker_state(2)}));"
        " const first = document.querySelector('#instances .inst');"
        " repaintCards();"
        " const after = document.querySelector('#instances .inst');"
        " return JSON.stringify({"
        "   present: after !== null, rebuilt: first !== after});")
    import json as _json
    got = _json.loads(result)
    assert got["present"], "repaintCards() left the grid empty"
    assert got["rebuilt"], (
        "repaintCards() was skipped as an unchanged payload -- a download "
        "bar would then never appear until the state happened to change")
