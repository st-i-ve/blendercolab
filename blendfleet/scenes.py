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
# Honest accounting of what actually protects the runtime dataset TODAY:
# every name this regex matches ends in "-linux", and the ordinary "-blend"
# suffix check below already excludes anything that doesn't end in
# "-blend" -- a string cannot end in both, so the suffix filter alone
# already excludes every runtime name that exists right now. Deleting
# this regex and its call site changes scenes_from_datasets' output for
# NO input in the current test suite; a reviewer confirmed exactly that.
#
# It is kept anyway, as defence-in-depth against a future rename: if
# Fleet.blender_dataset_name's format ever changes to something that
# could end in "-blend" (or the "-blend" convention itself changes),
# this check -- keyed to the runtime dataset's own naming code, not to
# the scene convention -- still excludes it while the suffix filter
# alone would not. test_the_runtime_pattern_matches_what_fleet_actually_builds
# pins that: it calls Fleet.blender_dataset_name() for real and asserts
# the result is excluded, so a rename that broke this guarantee would be
# caught even though today's suite cannot show the guard doing any work.
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
    # datetime, but None is a real possibility, not a defensive-programming
    # nicety: kaggle_client.list_datasets builds DatasetInfo.last_updated
    # with getattr(d, "last_updated", None), so a real SDK item lacking the
    # field flows all the way through as None. The annotation says so
    # rather than claiming a guarantee this module cannot make.
    updated: datetime | None
    # GUESSED filename of the .blend inside the dataset, built the same
    # way Fleet.dataset_slug_for derives the dataset name FROM a .blend --
    # run in reverse. Unverified: see this class's own docstring.
    blend_name: str


def dataset_kind(ref: str) -> str:
    """"scene", "runtime" or "other", from the name alone.

    The storage view needs to tell three things apart: a scene worth
    keeping, the Blender runtime this app uploads (deleting that costs the
    next render a re-upload, so it must be labelled and not merely listed),
    and everything else -- diagnostic leftovers, an old smoke test, a
    dataset from a version of this app that no longer exists.

    Name-based, and no more trustworthy than the filter below for the same
    reason: nothing here opens a dataset. "scene" means "named like one",
    which is exactly what Scene's own docstring says about itself.
    """
    name = str(ref).split("/", 1)[-1]
    if RUNTIME_DATASET_RE.match(name):
        return "runtime"
    # The same suffix scenes_from_datasets filters on, and the same
    # "a bare '-blend' has no name to render" exclusion: a dataset this
    # would call a scene must be one that dataset actually yields.
    if name.endswith(_SCENE_SUFFIX) and name[: -len(_SCENE_SUFFIX)]:
        return "scene"
    return "other"


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

        # See RUNTIME_DATASET_RE's own comment: this does not change
        # today's output (the suffix check below already excludes every
        # "-linux" name) -- it is defence-in-depth against the runtime
        # dataset's naming ever changing to something the suffix check
        # would miss.
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

    # A plain `scenes.sort(key=lambda s: s.updated, reverse=True)` raises
    # TypeError the moment ONE scene has updated=None (Python refuses to
    # compare None to a datetime) -- one dataset Kaggle returned with no
    # timestamp would then break the WHOLE account's listing, not just
    # that entry. Splitting dated from undated sorts the dated ones
    # exactly as before and appends the undated ones after, deliberately:
    # "we don't know when this changed" is not "newest", so an undated
    # scene must never be sorted as if it were -- it goes last, not first
    # by whatever a raw comparison would have decided.
    dated = sorted((s for s in scenes if s.updated is not None),
                   key=lambda scene: scene.updated, reverse=True)
    undated = [s for s in scenes if s.updated is None]
    return dated + undated
