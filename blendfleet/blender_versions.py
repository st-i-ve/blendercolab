"""Which Blender version renders, and whether a given one is usable.

RenderSettings has carried `blender_version` since the beginning and the
notebook builds its download URL from it, but nothing let a user choose
and nothing checked the string. An unusable version is not discovered
until the kernel runs, where the notebook's wget 404s and the whole
session is wasted finding out -- so the check happens here, before a
kernel is pushed.

KNOWN_VERSIONS is what the UI offers. It is deliberately NOT a whitelist:
Blender releases far more often than this app does, and refusing an
unlisted-but-valid version would mean waiting for a BlendFleet release to
use a new Blender. Anything shaped like a release is allowed through,
with the list serving as the menu rather than the gate.
"""
from __future__ import annotations

import re

# Newest first -- the UI shows them in this order, and the first entry is
# what a new install renders with.
KNOWN_VERSIONS: tuple[str, ...] = (
    "5.2.0",
    "5.1.1",
    "5.0.2",
    "4.5.3",
    "4.2.9",     # LTS
)

DEFAULT_VERSION = KNOWN_VERSIONS[0]

# major.minor.patch, digits only. Anything else -- "latest", "5.2",
# "v5.2.0", a release candidate suffix -- has no matching tarball at
# download.blender.org, and is also the shape an injected string would
# take, since this value reaches a shell command inside the notebook.
# [0-9] rather than \d: bare \d matches any Unicode decimal digit (e.g.
# Arabic-Indic "٥"), which would sail through as "digits" and then have
# no matching file at download.blender.org -- the exact 404-discovered-
# mid-session failure this module exists to prevent.
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def validate_version(version: str) -> str:
    """Return `version` stripped, or raise ValueError explaining the shape.

    Called before a kernel is pushed. The alternative is discovering the
    problem from a 404 inside a running session, which costs a Kaggle
    session's startup and reads like a network fault rather than a typo.
    """
    cleaned = (version or "").strip()
    if not _VERSION_RE.match(cleaned):
        raise ValueError(
            f"{version!r} is not a Blender version BlendFleet can download. "
            "It needs to be major.minor.patch, digits only -- for example "
            f"{DEFAULT_VERSION} or 4.2.0. Blender's own downloads are named "
            "that way, so anything else has no matching file to fetch. "
            "Nothing has been started.")
    return cleaned


def download_url(version: str) -> str:
    """The official tarball URL for `version`.

    Mirrors Blender's own layout: the release directory carries only
    major.minor ("Blender5.2"), the file carries the full version.
    """
    valid = validate_version(version)
    series = ".".join(valid.split(".")[:2])
    return (f"https://download.blender.org/release/Blender{series}/"
            f"blender-{valid}-linux-x64.tar.xz")
