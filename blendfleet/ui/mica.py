"""Windows 11 translucent window backdrop (Mica), and dark window chrome.

This is the feature Discord and VS Code use for their translucency -- the
DESKTOP showing through the window, composited by DWM. It is not the same
thing as the reference design's `backdrop-filter`, which blurs one panel
against the app's OWN content behind it; Qt has no equivalent for that and
this module does not pretend otherwise.

Everything here is best-effort and silent by design. It is called for its
visual effect only, so on Linux, on Windows 10, or on a Windows 11 build
older than 22621, every function no-ops and the app renders with an opaque
shell -- which is the same app, minus one flourish. A missing DWM attribute
must never be the reason a render farm fails to open.

The window must ALSO be genuinely see-through for any of this to show:
DWM composites behind the window, so an opaque QSS background hides the
backdrop completely. See Dashboard._apply_translucency for the other half.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import byref, c_int, sizeof

# DWMWINDOWATTRIBUTE values -- see the Win32 docs for dwmapi.h.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38

# DWM_SYSTEMBACKDROP_TYPE
BACKDROP_AUTO = 0
BACKDROP_NONE = 1
BACKDROP_MICA = 2         # the whole-window wallpaper tint
BACKDROP_ACRYLIC = 3      # heavier blur, meant for transient surfaces
BACKDROP_MICA_ALT = 4     # "tabbed" -- a stronger Mica

# DWM_WINDOW_CORNER_PREFERENCE
CORNER_DEFAULT = 0
CORNER_DONOTROUND = 1
CORNER_ROUND = 2

# DWMWA_SYSTEMBACKDROP_TYPE landed in Windows 11 22H2. Asking for it on an
# older build returns a failure code rather than crashing, but checking is
# cheaper and lets is_supported() answer honestly for the settings UI.
_MIN_BUILD_FOR_BACKDROP = 22621


def _build_number() -> int:
    """The Windows build number, or 0 anywhere that is not Windows."""
    if sys.platform != "win32":
        return 0
    version = sys.getwindowsversion()  # type: ignore[attr-defined]
    return getattr(version, "build", 0)


def is_supported() -> bool:
    """True when this machine can actually show a Mica backdrop."""
    return _build_number() >= _MIN_BUILD_FOR_BACKDROP


def _set_attribute(hwnd: int, attribute: int, value: int) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    try:
        result = dwm.DwmSetWindowAttribute(
            ctypes.wintypes.HWND(hwnd) if hasattr(ctypes, "wintypes")
            else ctypes.c_void_p(hwnd),
            c_int(attribute), byref(c_int(value)), sizeof(c_int))
    except Exception:
        return False
    return result == 0


def apply_backdrop(widget, *, enabled: bool, dark: bool,
                   kind: int = BACKDROP_MICA) -> bool:
    """Ask DWM for a translucent backdrop behind `widget`'s window.

    Returns True only if the backdrop was actually set, so a caller can
    tell "the user turned it on and it worked" from "the user turned it on
    and this machine cannot do it" -- the settings UI needs that difference
    to avoid promising an effect that will never appear.

    `dark` also drives DWMWA_USE_IMMERSIVE_DARK_MODE, which is what stops
    Windows drawing light-mode chrome (and a light resize border) around a
    dark window.
    """
    hwnd = int(widget.winId())
    # Set unconditionally: the immersive-dark-mode flag is about the window
    # frame, and is worth having whether or not the backdrop is available.
    _set_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
    _set_attribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, CORNER_ROUND)
    if not is_supported():
        return False
    return _set_attribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
                          kind if enabled else BACKDROP_NONE)
