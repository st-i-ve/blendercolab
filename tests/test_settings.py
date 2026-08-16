import json

import pytest

import blendfleet.platform_paths as pp
from blendfleet.settings import DEFAULT_MIN_GPUS, Settings
from blendfleet.ui.theme import ACCENTS, DEFAULT_ACCENT, DEFAULT_FONT, FONTS


# No per-module config_dir redirect needed here: conftest.py's autouse
# redirect_app_dirs already patches both platform_paths.config_dir and
# settings.config_dir (the name Settings.load()/save() resolve through)
# to the same fake directory, so the `pp.config_dir()` calls sprinkled
# through this module's tests agree with what Settings itself reads/
# writes without this module repeating the redirect itself. The two tests
# below that still monkeypatch settings_mod.config_dir directly (rather
# than relying on that fixture) need something the fixture does not give
# them: an exact, literal directory they can also write to/read from by
# hand (`tmp_path / settings_mod.FILENAME`) -- the fixture's fake
# directory is a subpath of tmp_path, not tmp_path itself, so those two
# keep their own override.
def test_load_with_no_file_returns_defaults():
    s = Settings.load()
    assert s.accent == DEFAULT_ACCENT
    assert s.fullscreen is False
    assert s.min_gpus == DEFAULT_MIN_GPUS


def test_defaults_accent_is_a_real_accent():
    assert Settings().accent in ACCENTS


def test_closing_asks_by_default_and_remembers_what_it_is_told(tmp_path):
    """"Remember my choice" writes here, and the Settings page reads the
    same field -- which is what makes a remembered choice undoable."""
    assert Settings().close_action == "ask"
    for choice in ("background", "quit", "ask"):
        Settings(close_action=choice).save()
        assert Settings.load().close_action == choice


@pytest.mark.parametrize("bad", ["minimise", "", 1, None, [], {"a": 1}])
def test_malformed_close_action_falls_back_to_asking(bad):
    """The safe one of the three: a corrupt value costs a dialog, never a
    silently abandoned render or a silently resident app."""
    assert Settings(close_action=bad).close_action == "ask"


def test_a_settings_file_from_before_the_close_choice_still_loads(
        tmp_path, monkeypatch):
    import blendfleet.settings as settings_mod
    monkeypatch.setattr(settings_mod, "config_dir", lambda: tmp_path)
    (tmp_path / settings_mod.FILENAME).write_text(
        json.dumps({"accent": "blue"}), encoding="utf-8")
    assert Settings.load().close_action == "ask"


def test_the_chosen_face_is_remembered(tmp_path):
    assert Settings().font == DEFAULT_FONT
    Settings(font="oswald").save()
    assert Settings.load().font == "oswald"


@pytest.mark.parametrize("bad", ["papyrus", "", 7, None, [], {"a": 1}])
def test_malformed_font_falls_back_rather_than_raising(bad):
    assert Settings(font=bad).font == DEFAULT_FONT


def test_every_real_face_name_round_trips(tmp_path):
    for name in FONTS:
        Settings(font=name).save()
        assert Settings.load().font == name


def test_frame_thumbnails_is_on_by_default_and_round_trips(tmp_path):
    """It costs nothing until the view is opened, so the option is there
    out of the box -- but it IS an option, because opening it pulls
    full-size frames off Kaggle one at a time."""
    assert Settings().frame_thumbnails is True
    Settings(frame_thumbnails=False).save()
    assert Settings.load().frame_thumbnails is False


@pytest.mark.parametrize("bad", ["yes", 1, None, [], {"a": 1}])
def test_malformed_frame_thumbnails_falls_back_rather_than_raising(bad):
    """Same total guard as every other field: a hand-edited config, or a
    malformed value sent through setPreference from the page, must never
    brick the app."""
    assert Settings(frame_thumbnails=bad).frame_thumbnails is True


def test_a_settings_file_from_before_frame_thumbnails_still_loads(tmp_path,
                                                                  monkeypatch):
    """Every settings.json already on disk predates this field."""
    import blendfleet.settings as settings_mod
    monkeypatch.setattr(settings_mod, "config_dir", lambda: tmp_path)
    (tmp_path / settings_mod.FILENAME).write_text(
        json.dumps({"accent": "blue", "theme": "dark"}), encoding="utf-8")
    loaded = Settings.load()
    assert loaded.frame_thumbnails is True
    assert loaded.accent == "blue"


def test_defaults_min_gpus_requires_at_least_one_gpu():
    # A render app has no legitimate use for a CPU-only allocation --
    # min_gpus=0 (no gate at all) must never be the out-of-the-box
    # behaviour, only something a user opts into explicitly.
    assert Settings().min_gpus == 1


def test_save_then_load_round_trips():
    s = Settings(accent="blue", fullscreen=True, min_gpus=2)
    s.save()
    loaded = Settings.load()
    assert loaded.accent == "blue"
    assert loaded.fullscreen is True
    assert loaded.min_gpus == 2


def test_unknown_accent_falls_back_to_default_rather_than_raising():
    """A hand-edited config, or one written by a future version with an
    accent name this build has never heard of, must not brick the app on
    startup -- there is no UI to fix it from if load() raises."""
    s = Settings(accent="mystery-colour")
    assert s.accent == DEFAULT_ACCENT


def test_load_with_unknown_accent_in_file_falls_back(tmp_path):
    p = pp.config_dir() / "settings.json"
    p.write_text(json.dumps({"accent": "ultraviolet", "fullscreen": False}),
                 encoding="utf-8")
    s = Settings.load()
    assert s.accent == DEFAULT_ACCENT


def test_load_with_corrupt_json_falls_back_to_defaults():
    p = pp.config_dir() / "settings.json"
    p.write_text("{not valid json", encoding="utf-8")
    s = Settings.load()
    assert s.accent == DEFAULT_ACCENT
    assert s.fullscreen is False


def test_every_real_accent_name_round_trips():
    for name in ACCENTS:
        s = Settings(accent=name)
        assert s.accent == name
        s.save()
        assert Settings.load().accent == name


# ---------------- malformed accent values, direct construction ----------------
# `x not in ACCENTS` raises TypeError for unhashable x instead of returning
# False, so the fallback must check the type first. Covering the full space
# a hand-edited or forward-dated config could contain: a plain unknown
# string is already covered above; here every other JSON-representable
# shape must also fall back rather than raise.

@pytest.mark.parametrize("bad_accent", [
    None,
    42,        # wrong scalar type
    3.5,
    True,      # bool is technically an int but still not a valid accent
    [1, 2],    # unhashable
    {"x": 1},  # unhashable
])
def test_malformed_accent_falls_back_rather_than_raising(bad_accent):
    s = Settings(accent=bad_accent)
    assert s.accent == DEFAULT_ACCENT


# ---------------- malformed accent values, via load() ----------------

@pytest.mark.parametrize("bad_accent_json", [
    "null",
    "42",
    "[1, 2]",
    '{"x": 1}',
])
def test_load_with_malformed_accent_in_file_falls_back(bad_accent_json):
    p = pp.config_dir() / "settings.json"
    p.write_text(
        '{"accent": %s, "fullscreen": false}' % bad_accent_json,
        encoding="utf-8")
    s = Settings.load()
    assert s.accent == DEFAULT_ACCENT


def test_load_with_accent_key_entirely_missing_falls_back():
    p = pp.config_dir() / "settings.json"
    p.write_text(json.dumps({"fullscreen": True}), encoding="utf-8")
    s = Settings.load()
    assert s.accent == DEFAULT_ACCENT
    assert s.fullscreen is True


# ---------------- malformed min_gpus values -- same principle as accent ----

@pytest.mark.parametrize("bad_min_gpus", [
    None,
    3.5,
    True,       # bool is technically an int but not a real GPU count
    -1,         # negative has no meaning
    [1, 2],
    {"x": 1},
    "two",
])
def test_malformed_min_gpus_falls_back_rather_than_raising(bad_min_gpus):
    s = Settings(min_gpus=bad_min_gpus)
    assert s.min_gpus == DEFAULT_MIN_GPUS


def test_min_gpus_zero_is_accepted_as_an_explicit_opt_out():
    # 0 disables the gate entirely -- a deliberate, valid choice, unlike
    # the malformed values above, so it must NOT be coerced back to 1.
    s = Settings(min_gpus=0)
    assert s.min_gpus == 0


def test_load_with_malformed_min_gpus_in_file_falls_back():
    p = pp.config_dir() / "settings.json"
    p.write_text(json.dumps({"min_gpus": "lots"}), encoding="utf-8")
    s = Settings.load()
    assert s.min_gpus == DEFAULT_MIN_GPUS


def test_load_with_min_gpus_key_missing_falls_back():
    p = pp.config_dir() / "settings.json"
    p.write_text(json.dumps({"accent": "blue"}), encoding="utf-8")
    s = Settings.load()
    assert s.min_gpus == DEFAULT_MIN_GPUS


# NOTE: Settings uses config_dir(), NOT state_dir(), and its loader reads
# every field explicitly with data.get(...) rather than **data -- so a new
# field needs a line in load() as well as on the dataclass. Both verified
# against blendfleet/settings.py:94-106 before writing this.
def test_the_blender_version_is_remembered(tmp_path, monkeypatch):
    """Choosing a version once and having it reset next launch would be
    worse than not offering the choice."""
    import blendfleet.settings as settings_mod
    monkeypatch.setattr(settings_mod, "config_dir", lambda: tmp_path)
    s = Settings()
    assert s.blender_version == "5.2.0"
    s.blender_version = "4.2.9"
    s.save()
    assert Settings.load().blender_version == "4.2.9"


def test_a_settings_file_from_before_this_field_still_loads(tmp_path,
                                                            monkeypatch):
    """A settings.json written by any earlier build must not lose the
    user's accent or theme just because a field was added."""
    import json
    import blendfleet.settings as settings_mod
    monkeypatch.setattr(settings_mod, "config_dir", lambda: tmp_path)
    (tmp_path / settings_mod.FILENAME).write_text(
        json.dumps({"accent": "blue"}), encoding="utf-8")
    loaded = Settings.load()
    assert loaded.blender_version == "5.2.0"
    assert loaded.accent == "blue", "the rest of the file must survive"


# ---------------- malformed blender_version values -- same principle as
# accent and min_gpus above: a hand-edited or forward-dated config, or a
# malformed value relayed from the page through setPreference, must fall
# back rather than raising out of __post_init__ with no UI left to fix it
# from (bridge.py's blenderVersions()/launch() slots call validate_version
# directly on this field, with nothing else standing between a bad value
# and an unhandled ValueError from inside a @Slot).

@pytest.mark.parametrize("bad_version", [
    None,
    42,
    3.5,
    True,
    [1, 2],
    {"x": 1},
    "latest",          # well-formed string, wrong shape
    "5.2",              # missing patch
    "v5.2.0",
])
def test_malformed_blender_version_falls_back_rather_than_raising(bad_version):
    s = Settings(blender_version=bad_version)
    assert s.blender_version == "5.2.0"


def test_load_with_malformed_blender_version_in_file_falls_back():
    p = pp.config_dir() / "settings.json"
    p.write_text(json.dumps({"blender_version": "latest"}), encoding="utf-8")
    s = Settings.load()
    assert s.blender_version == "5.2.0"


def test_an_unlisted_but_well_formed_blender_version_is_kept():
    """blender_version is a menu, not a gate (see blender_versions.py) --
    __post_init__ must not narrow it down to KNOWN_VERSIONS."""
    s = Settings(blender_version="3.6.14")
    assert s.blender_version == "3.6.14"


def test_a_blender_version_with_stray_whitespace_is_normalised():
    """validate_version's own return value is stripped -- __post_init__
    must keep that normalisation rather than storing the raw string, or
    notebook_builder's URL and the embedded BLENDER_VERSION could diverge
    (see notebook_builder.build's own note on this)."""
    s = Settings(blender_version=" 4.2.9 ")
    assert s.blender_version == "4.2.9"
