"""Which of an account's Kaggle datasets are renderable scenes.

Task 8 (kaggle_client.KaggleClient.list_datasets) can already fetch every
dataset an account owns. That list mixes scenes with everything else an
account has lying around on Kaggle -- diagnostic leftovers, unrelated
public datasets a friend downloaded, and the Blender runtime tarball this
app itself uploads. The coming Files page needs to show only the first
kind, which is what this module decides.

Pure functions over DatasetInfo values, deliberately: no network calls, no
caching, nothing here that would need a live Kaggle account to test. The
one Kaggle round trip involved (list_datasets) already happened in Task 8;
everything downstream of it, including this module, is plain data
transformation and can be exercised with plain values.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from blendfleet.kaggle_client import DatasetInfo

# Mirrors Fleet.blender_dataset_name's own format string exactly
# (fleet.py: f"{BLENDER_DATASET_PREFIX}-{version.replace('.', '-')}-linux",
# BLENDER_DATASET_PREFIX == "blender") and blender_versions.validate_version's
# version shape (major.minor.patch, digits only -- never a whitelist, so a
# version not yet in blender_versions.KNOWN_VERSIONS must match too). Written
# as a pattern rather than a fixed list of known versions for exactly that
# reason: a generic-shaped pattern here.
#
# This is the one check in this module that must never miss. The runtime
# dataset is not a scene -- it is the renderer itself, ~380 MB, shared with
# every account in the fleet. Classifying it as a scene would let the Files
# page offer it for deletion, and deleting it would silently cost every
# account on the fleet a re-upload the next time anyone renders. It is
# checked BEFORE the "-blend" convention below, unconditionally, rather
# than trusted to fail that check on its own merely because "-linux" and
# "-blend" happen not to overlap today -- see test_scenes.py's dedicated
# tests, including one that builds real names from Fleet.blender_dataset_name
# itself so a future change to that format cannot drift out of sync
# unnoticed.
RUNTIME_DATASET_RE = re.compile(r"^blender-[0-9]+-[0-9]+-[0-9]+-linux$")

# The suffix Fleet.dataset_slug_for gives every scene dataset this app
# itself uploads: f"{slug_stem(blend)}-blend" (fleet.py). slug_stem/
# slugify_stem already guarantee the stem is lowercase [a-z0-9-] and at
# least MIN_STEM_LENGTH characters BEFORE upload -- this module trusts that
# guarantee rather than re-validating it, since re-deriving it here could
# only drift out of sync with the code that actually applies it.
_SCENE_SUFFIX = "-blend"


@dataclass(frozen=True)
class Scene:
    """One Kaggle dataset that LOOKS like a renderable scene.

    "Looks like" is the operative word. The "-blend" suffix this is
    filtered on is a naming CONVENTION this app applies to its own
    uploads, not proof of what a dataset actually contains -- nothing in
    this module opens the dataset or lists its files. A dataset that
    merely happens to end in "-blend" (renamed by hand on kaggle.com, or
    uploaded by something other than this app) passes this filter with no
    .blend inside it at all. Task 10 is what actually confirms a .blend
    exists, by listing the dataset's real files, immediately before using
    it -- this module must never be treated as having done that check.
    """
    slug: str            # "owner/name" -- what Fleet needs to render or delete
    name: str             # display name: the stem, with "-blend" stripped
    owner: str
    size_bytes: int
    updated: datetime
    # GUESSED filename of the .blend inside the dataset, built the same
    # way Fleet.dataset_slug_for derives the dataset name FROM a .blend --
    # run in reverse. Unverified: see this class's own docstring.
    blend_name: str


def scenes_from_datasets(datasets: list[DatasetInfo]) -> list[Scene]:
    """Filter `datasets` down to the ones that look like scenes.

    Newest first, so the Files page can show what changed most recently
    without re-sorting -- "updated" is the only ordering a bare dataset
    listing carries that means anything to a person deciding what to
    render again.
    """
    scenes = []
    for dataset in datasets:
        name = dataset.ref.split("/", 1)[-1]

        # See RUNTIME_DATASET_RE's own comment: checked first and
        # unconditionally, never left to fall through the -blend check
        # below merely because the two suffixes don't happen to collide.
        if RUNTIME_DATASET_RE.match(name):
            continue

        if not name.endswith(_SCENE_SUFFIX):
            continue
        stem = name[: -len(_SCENE_SUFFIX)]
        if not stem:
            continue    # a bare "-blend" dataset has no name to render

        scenes.append(Scene(
            slug=dataset.ref,
            name=stem,
            owner=dataset.owner,
            size_bytes=dataset.total_bytes,
            updated=dataset.last_updated,
            blend_name=f"{stem}.blend",
        ))

    scenes.sort(key=lambda scene: scene.updated, reverse=True)
    return scenes
