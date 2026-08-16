"""User-adjustable app settings, persisted the same way AccountStore is:
one JSON file under platform_paths.config_dir(), a load()/save() pair.

Unlike an account, a Settings file can be hand-edited by a curious user or
carried forward from a future version that added an accent this build has
never heard of. __post_init__ is where that gets neutralised: an unknown
accent name falls back to the default rather than raising, so a bad or
forward-dated config can never brick the app on startup with no UI left to
fix it from.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from blendfleet.blender_versions import DEFAULT_VERSION, validate_version
from blendfleet.platform_paths import config_dir
# The NAMES only, from a module with no Qt in it -- see blendfleet/design.
# Importing ui.theme here would make PySide6 a dependency of anything that
# reads settings, including the headless sidecar that exists to avoid it.
from blendfleet.design import (ACCENT_NAMES as ACCENTS, DEFAULT_ACCENT,
                               DEFAULT_FONT, DEFAULT_THEME,
                               FONT_FAMILIES as FONTS,
                               THEME_NAMES as THEMES)

FILENAME = "settings.json"


DEFAULT_MIN_GPUS = 1

# What closing the window may do, and the default. Named here rather than
# in the UI so Settings can validate itself without importing a widget.
CLOSE_ACTIONS = ("ask", "background", "quit")
DEFAULT_CLOSE_ACTION = "ask"


@dataclass
class Settings:
    accent: str = DEFAULT_ACCENT
    # "light" or "dark". Guarded the same way accent is -- see
    # __post_init__ -- so a hand-edited or forward-dated config falls back
    # rather than bricking startup.
    theme: str = DEFAULT_THEME
    # The reference design offers translucency as a user setting rather
    # than always-on, and so do we -- with an extra reason to: the backdrop
    # tints from the desktop wallpaper, so text sitting on the shell can
    # land on anything. Off by default; only the Windows 11 Mica path can
    # honour it at all (see ui/mica.py).
    translucent: bool = False
    # The reference design ships sound effects ON. Kept that way rather
    # than defaulting to silence: this app runs long jobs that people walk
    # away from, so "the render finished" is exactly the kind of thing a
    # chime is for. One toggle in Settings turns it off.
    sound: bool = True
    fullscreen: bool = False
    # Minimum GPUs a launched kernel must report before the generated
    # notebook's PREFLIGHT gate (notebook_builder.py) lets a render
    # proceed -- see notebook_builder.RenderSettings.min_gpus, which this
    # is wired into at the one production call site (ui/dashboard.py's
    # _launch). Kaggle accepts an invalid machine_shape with no error and
    # silently falls back to a single P100; this gate is what stops a long
    # render from proceeding on far less hardware than requested when that
    # happens. Defaults to 1 rather than 0 (no gate): a render app has no
    # legitimate use for a CPU-only allocation, so requiring at least one
    # GPU -- and failing fast instead of rendering at unusable speed -- is
    # the right default, not something the user has to discover and turn
    # on themselves.
    min_gpus: int = DEFAULT_MIN_GPUS
    # Which Blender renders. Remembered because choosing it every launch
    # would be worse than not offering the choice. See
    # blendfleet/blender_versions.py for why an unlisted version is still
    # allowed.
    blender_version: str = DEFAULT_VERSION
    # Whether the dashboard offers the thumbnail view of a job's frames at
    # all. ON by default -- it costs nothing until it is opened -- but it
    # is a preference rather than a permanent fixture because the pictures
    # are full-size frames pulled from Kaggle one at a time, and somebody
    # on a metered or slow connection is entitled to take the option off
    # the screen entirely rather than be careful around it.
    frame_thumbnails: bool = True
    # Which typeface the whole app is set in -- window chrome and page
    # alike. Guarded exactly the way accent is, and for the same reason:
    # a name this build has never heard of falls back rather than
    # bricking startup.
    font: str = DEFAULT_FONT
    # What the window's close button does while a render is still going:
    #   "ask"        -- the default: put the choice, once, with the scene
    #                   and the account count in it
    #   "background" -- hide to the notification area and keep following
    #   "quit"       -- close and stop, the way it always did
    # Closing with NOTHING rendering always quits, whatever this says --
    # there is nothing to keep running for, and a tray icon for an idle
    # app is litter.
    close_action: str = "ask"

    def __post_init__(self) -> None:
        # Total, not just "wrong value": a hand-edited or forward-dated
        # config can hand this an unhashable value (a list or dict from
        # `"accent": ["a"]` / `{"accent": {"x": 1}}`), and `x not in dict`
        # raises TypeError for those rather than returning False. Checking
        # isinstance first means every construction path -- not just
        # load()'s except clause -- is safe against any JSON-representable
        # value, not only the ones that happen to be hashable.
        if not isinstance(self.accent, str) or self.accent not in ACCENTS:
            self.accent = DEFAULT_ACCENT
        # Same total guard as accent above, for the same reasons: any
        # JSON-representable value, not just the hashable ones.
        if not isinstance(self.theme, str) or self.theme not in THEMES:
            self.theme = DEFAULT_THEME
        if not isinstance(self.translucent, bool):
            self.translucent = False
        if not isinstance(self.sound, bool):
            self.sound = True
        # Same total guard as sound above.
        if not isinstance(self.frame_thumbnails, bool):
            self.frame_thumbnails = True
        # Same total guard as accent above: any JSON-representable value,
        # not only the hashable ones.
        if not isinstance(self.font, str) or self.font not in FONTS:
            self.font = DEFAULT_FONT
        # Same total guard again. "ask" is the safe fallback of the three:
        # a corrupt value can only ever cost a dialog, never a silently
        # abandoned render or a silently resident app.
        if (not isinstance(self.close_action, str)
                or self.close_action not in CLOSE_ACTIONS):
            self.close_action = DEFAULT_CLOSE_ACTION
        # Same principle as accent above: a hand-edited or forward-dated
        # config must never brick the app. bool is technically an int
        # subclass in Python, so it is excluded explicitly rather than
        # accepted as 0/1.
        if (not isinstance(self.min_gpus, int) or isinstance(self.min_gpus, bool)
                or self.min_gpus < 0):
            self.min_gpus = DEFAULT_MIN_GPUS
        # Same principle as accent above: a hand-edited or forward-dated
        # config, or a malformed value sent through setPreference from the
        # page, must never brick the app. Not a whitelist check -- see
        # blender_versions.py for why an unlisted-but-valid version is
        # allowed -- only a shape check, and normalised (stripped) at the
        # same time so this is the one place blender_version can diverge
        # from what validate_version would accept downstream.
        if not isinstance(self.blender_version, str):
            self.blender_version = DEFAULT_VERSION
        else:
            try:
                self.blender_version = validate_version(self.blender_version)
            except ValueError:
                self.blender_version = DEFAULT_VERSION

    def _path(self) -> Path:
        return config_dir() / FILENAME

    def save(self) -> None:
        self._path().write_text(
            json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "Settings":
        p = config_dir() / FILENAME
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable file -- same principle as an unknown
            # accent: a bad file on disk must not brick the app, it must
            # just fall back to defaults.
            return cls()
        return cls(
            accent=data.get("accent", DEFAULT_ACCENT),
            theme=data.get("theme", DEFAULT_THEME),
            translucent=data.get("translucent", False),
            sound=data.get("sound", True),
            fullscreen=bool(data.get("fullscreen", False)),
            min_gpus=data.get("min_gpus", DEFAULT_MIN_GPUS),
            blender_version=data.get("blender_version", DEFAULT_VERSION),
            frame_thumbnails=data.get("frame_thumbnails", True),
            font=data.get("font", DEFAULT_FONT),
            close_action=data.get("close_action", DEFAULT_CLOSE_ACTION),
        )
