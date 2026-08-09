import json

import pytest

import blendfleet.platform_paths as pp
from blendfleet.settings import Settings
from blendfleet.ui.theme import ACCENTS, DEFAULT_ACCENT


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_load_with_no_file_returns_defaults():
    s = Settings.load()
    assert s.accent == DEFAULT_ACCENT
    assert s.fullscreen is False


def test_defaults_accent_is_a_real_accent():
    assert Settings().accent in ACCENTS


def test_save_then_load_round_trips():
    s = Settings(accent="blue", fullscreen=True)
    s.save()
    loaded = Settings.load()
    assert loaded.accent == "blue"
    assert loaded.fullscreen is True


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
