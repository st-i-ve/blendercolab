"""The window around the web view: what a right-click offers, and where a
saved frame goes.

Chromium's own context menu used to be what appeared: a black slab drawn
outside the app's stylesheet, offering Back / Forward / Reload / Save page
/ Copy image address. Every one of those is either meaningless in a
single-page app or actively destructive -- Back leaves the window blank,
Reload throws away the live render state and every thumbnail already
fetched. These tests are about the replacement: two useful actions on an
image, and nothing at all anywhere else.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

import pytest

pytest.importorskip("PySide6.QtWebEngineWidgets",
                    reason="QtWebEngine is not available in this environment")

from PySide6.QtWebEngineCore import (QWebEngineContextMenuRequest,  # noqa: E402
                                     QWebEnginePage)

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from blendfleet.ui import theme  # noqa: E402
from blendfleet.ui.web_host import (Shell, close_decision,  # noqa: E402
                                    close_question, context_actions,
                                    save_download, wash_geometry)

MEDIA = QWebEngineContextMenuRequest.MediaType


def test_a_right_click_on_a_frame_offers_the_two_things_worth_offering():
    """What a person right-clicks a rendered frame FOR."""
    actions = context_actions(MEDIA.MediaTypeImage)
    assert [label for label, _ in actions] == ["Save image as…", "Copy image"]
    assert [action for _, action in actions] == [
        QWebEnginePage.WebAction.DownloadImageToDisk,
        QWebEnginePage.WebAction.CopyImageToClipboard,
    ]


@pytest.mark.parametrize("media", [MEDIA.MediaTypeNone, MEDIA.MediaTypeVideo,
                                   MEDIA.MediaTypeAudio, MEDIA.MediaTypeFile,
                                   MEDIA.MediaTypeCanvas, None])
def test_a_right_click_on_anything_else_offers_no_menu_at_all(media):
    """Not a menu with the useless items greyed out -- no menu. A control
    that is offered and must not be pressed is worse than one that is not
    offered, and Reload is one keystroke from losing a running render's
    entire view state."""
    assert context_actions(media) == ()


class _FakeDownload:
    """The three setters and two verbs save_download actually uses."""

    def __init__(self, name="f_0007.png"):
        self._name = name
        self.directory = None
        self.filename = None
        self.accepted = False
        self.cancelled = False

    def downloadFileName(self):     # noqa: N802 - Qt's own spelling
        return self._name

    def setDownloadDirectory(self, value):      # noqa: N802
        self.directory = value

    def setDownloadFileName(self, value):       # noqa: N802
        self.filename = value

    def accept(self):
        self.accepted = True

    def cancel(self):
        self.cancelled = True


def test_a_saved_frame_goes_where_the_dialog_said(tmp_path):
    download = _FakeDownload()
    target = tmp_path / "renders" / "hero.png"
    assert save_download(download, chooser=lambda suggested: str(target))
    assert download.directory == str(target.parent)
    assert download.filename == "hero.png"
    assert download.accepted is True


def test_the_dialog_opens_on_the_name_the_page_suggested(tmp_path):
    """Frames come off Kaggle as f_0007.png, and that is the name worth
    keeping -- it is the frame number."""
    seen = {}

    def chooser(suggested):
        seen["suggested"] = suggested
        return str(tmp_path / suggested)

    save_download(_FakeDownload("f_0007.png"), chooser=chooser)
    assert seen["suggested"] == "f_0007.png"


def test_cancelling_the_dialog_cancels_the_download():
    """A download Qt is never told what to do with is dropped in silence,
    which would leave a half-started transfer with nowhere to go."""
    download = _FakeDownload()
    assert save_download(download, chooser=lambda suggested: "") is False
    assert download.cancelled is True
    assert download.accepted is False


# ---------------------------------------------------------------------------
# The window's ground.
#
# The accent wash used to live in the page (body::before), which meant it
# could only start at the top of the web view -- and the title bar above
# it is painted by Qt in the flat shell colour. The result was a hard
# horizontal seam straight across the window, exactly where the two met.
# It is painted by the window now, behind both.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_the_washes_bleed_in_from_outside_the_window():
    """A glow whose centre is ON the window is a blob in the middle of
    the app. Both centres sit outside their own corner, so only the falloff
    reaches the screen."""
    (top_right, _, _), (bottom_left, _, _) = wash_geometry(1400, 900)
    assert top_right.x() > 1400 * 0.8 and top_right.y() < 0
    assert bottom_left.x() < 0 and bottom_left.y() > 900


def test_the_wash_scales_with_the_window():
    """Fractions, not fixed pixels -- a maximised window and a small one
    both get the glow in their own corner."""
    small = wash_geometry(800, 600)[0][0]
    large = wash_geometry(1600, 1200)[0][0]
    assert large.x() == pytest.approx(small.x() * 2)
    assert large.y() == pytest.approx(small.y() * 2)


def _pixel(widget, x, y):
    image = widget.grab().toImage()
    return QColor(image.pixel(x, y))


def _hex(colour):
    """QColor.name() is lower case; the palette's own hex is upper."""
    return colour.name().lower()


def test_the_ground_is_the_theme_and_the_glow_is_the_accent(qapp):
    """Both halves of "one continuous field": the flat fill is the
    theme's own background, and what tints it is the chosen accent --
    which is what the title bar has to match."""
    theme.apply(qapp, "green", "dark")
    shell = Shell()
    shell.resize(1200, 500)
    away = _pixel(shell, 40, 240)       # far from either wash
    glow = _pixel(shell, 1150, 30)      # under the top-right wash
    assert _hex(away) == theme.THEMES["dark"].bg.lower()
    assert glow.green() > away.green(), "the wash did not paint"
    assert glow.green() > glow.red(), "the wash is not in the green accent"


def test_a_different_accent_repaints_the_glow(qapp):
    theme.apply(qapp, "dark-red", "dark")
    shell = Shell()
    shell.resize(1200, 500)
    glow = _pixel(shell, 1150, 30)
    assert glow.red() > glow.green(), "the wash kept the previous accent"


def test_the_backdrop_leaves_the_ground_unpainted(qapp):
    """With Mica showing, the flat fill would paint over the desktop the
    backdrop exists to reveal.

    Checked in the LIGHT theme deliberately: dark's ground is #000000,
    which is also what an unpainted grab returns, so a dark check would
    pass whether or not the fill was skipped.
    """
    theme.apply(qapp, "green", "light")
    painted = Shell()
    painted.resize(1200, 500)
    assert _hex(_pixel(painted, 40, 240)) == theme.THEMES["light"].bg.lower()

    shell = Shell()
    shell.transparent = True
    shell.resize(1200, 500)
    assert _hex(_pixel(shell, 40, 240)) != theme.THEMES["light"].bg.lower()


# ---------------------------------------------------------------------------
# Closing while something is still rendering.
#
# The renders are on Kaggle and do not care whether this window is open.
# What closing loses is the app's VIEW of them: the stream that advances
# the frame counts, the chime when one finishes, and any collecting until
# it is opened again. So the choice is offered -- once, and only when
# there is something to lose.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("preference", ["ask", "background", "quit", "junk"])
def test_closing_with_nothing_rendering_always_just_quits(preference):
    """Whatever the preference says. A tray icon for an idle app is
    litter, and "keep running" with nothing to keep running for is a
    promise about nothing."""
    assert close_decision(preference, 0) == "quit"
    assert close_decision(preference, -1) == "quit"


def test_a_live_render_is_what_makes_it_ask():
    assert close_decision("ask", 1) == "ask"
    assert close_decision("ask", 4) == "ask"


def test_a_remembered_choice_is_honoured_without_asking_again():
    assert close_decision("background", 2) == "hide"
    assert close_decision("quit", 2) == "quit"


def test_an_unreadable_preference_falls_back_to_asking():
    """The safe one of the three: a corrupt value can only ever cost a
    dialog, never a silently abandoned render or a silently resident
    app."""
    assert close_decision("", 2) == "ask"
    assert close_decision("Background", 2) == "ask"


def test_the_question_names_the_scene_and_the_machines():
    """"A render is still going" is not worth interrupting somebody
    for. Which render, on how many accounts, is."""
    text = close_question(["waydown"], 2)
    assert "waydown" in text
    assert "2 accounts" in text


def test_the_question_counts_scenes_rather_than_listing_them_all():
    text = close_question(["waydown", "remember", "shaketomax"], 5)
    assert "3 scenes" in text
    assert "5 accounts" in text


def test_one_account_is_not_called_accounts():
    assert "1 account." in close_question(["waydown"], 1)


def test_the_question_never_claims_quitting_cancels_the_render():
    """The one thing it must not say. The render is on Kaggle; quitting
    stops this app watching it and nothing else -- and the whole app is
    built on not overstating what it controls."""
    text = close_question(["waydown"], 2).lower()
    assert "does not cancel" in text
    assert "cancels" not in text.replace("does not cancel", "")
    assert "stop the render" not in text


def test_without_a_system_tray_it_never_hides():
    """A window that hides into a notification area that does not exist
    is simply gone -- no icon to click, no window to find, and a process
    still holding the render state. Not even a remembered "keep running"
    may do that."""
    assert close_decision("background", 3, tray_available=False) == "quit"
    assert close_decision("ask", 3, tray_available=False) == "quit"
    # And with a tray, the same inputs behave as before.
    assert close_decision("background", 3, tray_available=True) == "hide"
