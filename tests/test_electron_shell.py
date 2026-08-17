"""The Electron shell's side of the contract, checked from Python.

`electron/` had no tests at all until 2026-08-17, and the first bug the
shell shipped was one a two-line assertion would have caught: the page
sets `html,body{background:transparent}` because the QT window paints the
ground underneath it, and the Electron shell was not painting one. Theme
switching repainted every card and left the ground alone, showing Mica --
which follows Windows' light/dark setting, not BlendFleet's.

These read the JS and CSS as text rather than running them. A Node test
runner for three files would be a second toolchain in the repo for less
coverage than this; what actually goes wrong here is drift between two
languages' copies of one list, and text is enough to catch that.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from blendfleet.rpc.protocol import EVENTS
from blendfleet.rpc.session import Session

ELECTRON = Path(__file__).resolve().parents[1] / "electron"
WEB = Path(__file__).resolve().parents[1] / "blendfleet" / "web"


def _js_string_list(source: str, name: str) -> list[str]:
    """The strings in `const <name> = [ ... ];`."""
    match = re.search(rf"const {name} = \[(.*?)\];", source, re.S)
    assert match, f"{name} is not declared as a literal array any more"
    return re.findall(r"'([^']+)'", match.group(1))


# ---- the ground -------------------------------------------------------

def test_the_shell_paints_a_ground_that_follows_the_theme():
    """The regression itself: a ground, from the theme's own token.

    Asserting `var(--bg)` rather than any colour is the point -- a
    hard-coded hex here would look right in one theme and be wrong in the
    other seven accents and the dark theme.
    """
    css = (ELECTRON / "shell.css").read_text(encoding="utf-8")
    ground = re.search(r"html\.electron\s*\{([^}]*)\}", css)
    assert ground, "nothing paints the window's ground"
    assert "background" in ground.group(1)
    assert "var(--bg)" in ground.group(1), (
        "the ground must come from the theme token, or switching theme "
        "leaves it behind")


def test_the_page_still_expects_its_host_to_paint_the_ground():
    """Why the rule above has to exist, pinned in the page it depends on.

    If this ever fails, `blendfleet/web/` has started painting its own
    ground and the shell's rule is now fighting it rather than filling a
    gap -- so this failure means "go re-read shell.css", not "fix the
    page".
    """
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert re.search(r"html,body\{background:transparent\}", css), (
        "app.css no longer leaves the ground to its host")


def test_translucency_thins_the_ground_instead_of_removing_it():
    """`body.glass` is the page's translucency (app.js). Mica reads
    through a thinned ground; it cannot read through an opaque one, and a
    ground removed entirely is the bug above coming back for one
    preference."""
    css = (ELECTRON / "shell.css").read_text(encoding="utf-8")
    glass = re.search(r"html\.electron:has\(body\.glass\)\s*\{([^}]*)\}", css)
    assert glass, "translucency does not reach the ground"
    assert "var(--bg)" in glass.group(1)


# ---- what only a shell can do -----------------------------------------
#
# Asserted as TEXT, and the limits of that are worth stating: these check
# that the shell still contains the mechanism, not that Windows suspends or
# that a toast appears. A Node harness driving Electron with a fake sidecar
# would check the behaviour, and would be a second toolchain in this repo
# for one file. What text catches is the realistic regression -- somebody
# refactors main.js and a hook quietly stops being wired.

def _main() -> str:
    return (ELECTRON / "main.js").read_text(encoding="utf-8")


def test_the_machine_is_kept_awake_only_while_work_is_in_flight():
    """A laptop that suspends mid-upload loses the upload. One that never
    sleeps because a render farm is installed is a worse neighbour, so the
    blocker is held against a set of in-flight keys and released when it
    empties."""
    source = _main()
    assert "powerSaveBlocker.start('prevent-app-suspension')" in source, (
        "sleep is not being blocked, or is blocking the display too")
    assert "powerSaveBlocker.stop(" in source, "the blocker is never released"
    assert "busy.size" in source, (
        "the blocker is not tied to whether anything is actually running")


def test_waking_from_sleep_polls_instead_of_waiting_out_the_timer():
    """The status poll is on a 30-second timer, so a lid opened after two
    hours shows two-hour-old readings that look current."""
    source = _main()
    resumed = re.search(r"powerMonitor\.on\('resume',(.*?)\}\);", source, re.S)
    assert resumed, "nothing happens when the machine wakes up"
    assert "callBackend('poll')" in resumed.group(1)


def test_taskbar_progress_is_cleared_rather_than_left_full():
    """setProgressBar(-1) is 'no bar'. A bar left at 100% reads as a render
    that never finished, which is the opposite of what happened."""
    source = _main()
    assert "setProgressBar(" in source
    assert "-1" in source[source.index("setProgressBar("):
                          source.index("setProgressBar(") + 120], (
        "the progress bar is never cleared")


def test_an_os_notification_is_only_raised_when_the_window_cannot_show_one():
    """Duplicating an on-screen toast as an OS notification is noise; the
    point is the window being hidden in the tray, which is exactly when a
    render finishing is news."""
    source = _main()
    guard = re.search(r"function notifyOutside\((.*?)\n\}", source, re.S)
    assert guard, "notifications are not routed through one place"
    assert "isVisible()" in guard.group(1), (
        "a notification would fire even with the window in front")
    assert "Notification.isSupported()" in guard.group(1), (
        "an unsupported platform must not throw on a render finishing")


def test_a_chosen_blend_joins_the_operating_system_s_recent_files():
    source = _main()
    assert "app.addRecentDocument(" in source


# ---- the two copies of one list ---------------------------------------

def test_preload_listens_for_exactly_the_events_the_protocol_sends():
    """preload.js says this list is protocol.EVENTS. It now is.

    An event in Python and not in JS is a card that never updates; one in
    JS and not in Python is a handler that never fires. Both fail silently
    in a window that otherwise looks perfectly fine, which is what makes
    them worth a test rather than a comment.
    """
    listed = _js_string_list(
        (ELECTRON / "preload.js").read_text(encoding="utf-8"), "EVENTS")
    assert listed == list(EVENTS)


def test_every_call_the_page_may_make_exists_on_the_session():
    listed = _js_string_list(
        (ELECTRON / "preload.js").read_text(encoding="utf-8"), "CALLS")
    missing = [name for name in listed
               if not callable(getattr(Session, name, None))]
    assert not missing, f"preload offers calls the sidecar does not have: {missing}"


# ---- where the built folder puts things -------------------------------

def _package_json() -> dict:
    return json.loads((ELECTRON / "package.json").read_text(encoding="utf-8"))


def test_the_built_app_keeps_the_sidecar_beside_the_exe():
    """The shape asked for on 2026-08-17: one folder that reads like
    dist/blendfleetweb/ -- the program, then the parts it runs.

    `extraFiles` puts a directory next to the executable; `extraResources`
    buries it in resources/. Both ship the same bytes, and only one
    answers "where is the backend" by looking.
    """
    build = _package_json()["build"]
    beside = [entry["to"] for entry in build.get("extraFiles", [])]
    inside = [entry["to"] for entry in build.get("extraResources", [])]
    assert "backend" in beside
    assert "backend" not in inside, (
        "the sidecar moved back into resources/ -- main.js resolves it "
        "beside the exe and will not find it there")


def test_main_resolves_the_sidecar_where_the_build_puts_it():
    """The other half of the pair above. These two files have to agree,
    and they are in different languages with no compiler between them: a
    packaged app whose main.js still looks in resources/ starts, shows the
    whole dashboard, and every card sits empty forever."""
    source = (ELECTRON / "main.js").read_text(encoding="utf-8")
    resolved = re.search(r"backend: PACKAGED\s*\?(.*?):\s*null", source, re.S)
    assert resolved, "the packaged backend path is no longer resolved here"
    assert "app.getPath('exe')" in resolved.group(1), (
        "the packaged build looks for its sidecar somewhere other than "
        "beside the exe")


def test_the_packaged_layout_keeps_the_pages_relative_assets_reachable():
    """The first packaged build's real bug, and the one hardest to see.

    app.css asks for its fonts and the sidebar mark as `../../assets/...`
    -- relative to blendfleet/web/, so two levels up is the repo root.
    The build flattened the page into resources/web/, which made every one
    of those resolve one level ABOVE resources/. Nothing errored: the
    window opened, the layout held, and the whole page quietly rendered in
    fallback fonts with an empty circle where the logo goes.

    Computed from the page's own references rather than from a remembered
    path, so a new asset folder is covered the day it is added.
    """
    import posixpath

    css = (WEB / "app.css").read_text(encoding="utf-8")
    referenced = sorted(set(re.findall(r"\.\./\.\./(assets/[\w./-]+)", css)))
    assert referenced, "app.css no longer reaches outside its own folder"

    resources = _package_json()["build"]["extraResources"]
    page_to = next(entry["to"] for entry in resources
                   if entry["from"].endswith("blendfleet/web"))
    shipped = {entry["to"].rstrip("/") for entry in resources}

    for reference in referenced:
        # Where the browser will look, given where the page is put.
        resolved = posixpath.normpath(posixpath.join(page_to, "..", "..",
                                                     reference))
        assert not resolved.startswith(".."), (
            f"{reference} resolves outside resources/ from {page_to}/")
        assert any(resolved == to or resolved.startswith(to + "/")
                   for to in shipped), (
            f"the page asks for {reference}; from {page_to}/ that is "
            f"{resolved}, which this build does not ship")


def test_main_loads_the_page_from_where_the_build_puts_it():
    source = (ELECTRON / "main.js").read_text(encoding="utf-8")
    page_to = next(entry["to"] for entry
                   in _package_json()["build"]["extraResources"]
                   if entry["from"].endswith("blendfleet/web"))
    expected = ", ".join(f"'{part}'" for part in page_to.split("/"))
    assert f"process.resourcesPath, {expected}, 'index.html'" in source, (
        "main.js loads the page from somewhere the build does not write it")


def test_the_build_writes_where_the_qt_build_does_not():
    """Rule 2 of the design: dist/blendfleetweb/ is never written to. An
    output directory pointed at it would be electron-builder cleaning the
    Qt app away before every build."""
    output = _package_json()["build"]["directories"]["output"]
    assert "blendfleetweb" not in output
    assert output == "../dist/fleet-electron"


def test_the_calls_the_shell_completes_itself_are_not_offered_raw():
    """pickBlend needs a file dialog and collect needs a directory, and
    the sidecar is headless. preload builds both on top of the plain
    calls, so the plain ones must not also be handed to the page under the
    dialog's name -- and `stop` must not be reachable at all: the page has
    no business ending the process that outlives its window."""
    source = (ELECTRON / "preload.js").read_text(encoding="utf-8")
    listed = _js_string_list(source, "CALLS")
    assert "pickBlend" not in listed
    assert "stop" not in listed
    assert "backend.pickBlend = " in source
    assert "backend.collect = " in source
