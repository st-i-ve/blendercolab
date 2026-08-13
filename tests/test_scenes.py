"""Which of an account's Kaggle datasets are offered as renderable scenes.

The scene library (Task 9's brief) is built on a naming convention this
app already applies to its own uploads (fleet.dataset_slug_for gives every
scene "<stem>-blend"), not on anything Kaggle itself knows about a
dataset's contents. Two things need to hold no matter how that convention
is implemented: the Blender runtime dataset -- the renderer, not a scene,
shared with the whole fleet -- must never come back as a Scene, and a
name-only match must never be presented as more certain than it is.
"""
from datetime import datetime

import pytest

from blendfleet.kaggle_client import DatasetInfo
from blendfleet.scenes import RUNTIME_DATASET_RE, Scene, scenes_from_datasets


def ds(ref: str, *, size_bytes: int = 1_000_000,
       updated: datetime | None = None) -> DatasetInfo:
    """A DatasetInfo shaped like KaggleClient.list_datasets() would return,
    exposing only the fields any given test actually varies."""
    owner = ref.split("/", 1)[0]
    return DatasetInfo(ref=ref, title=ref, total_bytes=size_bytes,
                       last_updated=updated or datetime(2026, 1, 1),
                       is_private=True, owner=owner)


def test_a_blend_dataset_is_a_scene():
    assert [s.name for s in scenes_from_datasets([ds("me/remember-blend")])] \
        == ["remember"]


def test_the_blender_runtime_is_never_a_scene():
    """It is the renderer, not a scene. Offering it for deletion would
    silently cost every account a 380 MB re-upload."""
    assert scenes_from_datasets([ds("me/blender-5-2-0-linux")]) == []


@pytest.mark.parametrize("version_dashes", ["4-2-9", "5-1-1", "5-2-0", "10-0-0"])
def test_the_runtime_pattern_matches_any_blender_version_not_just_known_ones(
        version_dashes):
    """blender_versions.KNOWN_VERSIONS is explicitly not a whitelist -- any
    major.minor.patch string is accepted -- so the exclusion must key off
    the SHAPE of the name, not an enumerated list that a new release could
    slip past."""
    assert RUNTIME_DATASET_RE.match(f"blender-{version_dashes}-linux")


def test_the_runtime_pattern_matches_what_fleet_actually_builds():
    """Cross-checks against Fleet.blender_dataset_name itself rather than
    only a hand-written duplicate of its format string, so a future change
    to that method cannot silently drift out of sync with this regex --
    the one guard in this module that must never miss (see
    RUNTIME_DATASET_RE's own comment)."""
    from blendfleet.blender_versions import KNOWN_VERSIONS
    from blendfleet.fleet import Fleet

    empty_fleet = Fleet([], lambda token: None, "/unused")
    for version in KNOWN_VERSIONS:
        real_name = empty_fleet.blender_dataset_name(version)
        assert RUNTIME_DATASET_RE.match(real_name)
        assert scenes_from_datasets([ds(f"me/{real_name}")]) == []


def test_an_unrelated_dataset_is_not_a_scene():
    assert scenes_from_datasets([ds("me/titanic")]) == []


def test_a_bare_blend_suffix_with_no_stem_is_not_a_scene():
    """"-blend" alone has nothing to render -- there is no stem left once
    the suffix is stripped, so this must not become a Scene with an empty
    name."""
    assert scenes_from_datasets([ds("me/-blend")]) == []


def test_scenes_are_newest_first():
    older = ds("me/old-scene-blend", updated=datetime(2026, 1, 1))
    newer = ds("me/new-scene-blend", updated=datetime(2026, 6, 1))

    got = scenes_from_datasets([older, newer])

    assert [s.name for s in got] == ["new-scene", "old-scene"]


def test_a_mixed_list_keeps_only_what_looks_like_a_scene():
    """The Files page's input is one account's WHOLE dataset list --
    scenes, the runtime dataset, and unrelated leftovers all mixed
    together -- so the filter has to do real work, not just pass through
    a list that was already scenes-only."""
    datasets = [
        ds("me/remember-blend"),
        ds("me/blender-5-2-0-linux"),
        ds("me/titanic"),
        ds("me/kitchen-blend"),
    ]

    got = scenes_from_datasets(datasets)

    assert sorted(s.name for s in got) == ["kitchen", "remember"]


def test_a_scene_carries_the_fields_the_files_page_needs():
    updated = datetime(2026, 8, 1, 9, 30)
    dataset = ds("stive/remember-blend", size_bytes=54_321_000, updated=updated)

    [scene] = scenes_from_datasets([dataset])

    assert scene == Scene(slug="stive/remember-blend", name="remember",
                          owner="stive", size_bytes=54_321_000,
                          updated=updated, blend_name="remember.blend")


def test_the_guessed_blend_name_is_a_guess_not_a_verified_fact():
    """blend_name is built by reversing the same convention Fleet uses to
    name the dataset in the first place -- it is not read from the
    dataset's real file listing, so it can be wrong (a dataset renamed by
    hand, or uploaded by something other than this app). Task 10 confirms
    it for real before any render starts; this module only guesses."""
    [scene] = scenes_from_datasets([ds("me/some-scene-blend")])
    assert scene.blend_name == "some-scene.blend"
