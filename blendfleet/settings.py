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

from blendfleet.platform_paths import config_dir
from blendfleet.ui.theme import ACCENTS, DEFAULT_ACCENT

FILENAME = "settings.json"


@dataclass
class Settings:
    accent: str = DEFAULT_ACCENT
    fullscreen: bool = False

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
            fullscreen=bool(data.get("fullscreen", False)),
        )
