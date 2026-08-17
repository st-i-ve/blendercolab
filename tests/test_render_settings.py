"""Blender's own settings, and the rule that makes exposing them safe.

Every one of these is ABSENT by default, and absent means the .blend keeps
what the artist saved. A farm that reset somebody's bounce limit or their
denoiser because a web form had a default in it would be editing their
render without telling them -- so "not asked for" has to survive the whole
way down: page -> options payload -> RenderSettings -> the notebook's
environment -> Blender.

The property names are Blender's own, read out of a local 5.1 install's UI
scripts (scripts/startup/bl_ui/properties_output.py and
scripts/addons_core/cycles/ui.py) rather than remembered:
resolution_percentage, film_transparent, image_settings.color_depth,
cycles.time_limit, cycles.use_adaptive_sampling, cycles.adaptive_threshold,
cycles.use_denoising, cycles.denoiser, cycles.max_bounces.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from blendfleet.notebook_builder import RenderSettings, build


def _cells(tmp_path, settings):
    path = build([1], settings, "me/scene-blend", tmp_path,
                 "me/scene-render-abcd1234")
    return "\n".join("".join(cell["source"])
                     for cell in json.loads(path.read_text())["cells"])


def _literal(source, name):
    for line in source.splitlines():
        if line.startswith(f"{name} = "):
            return line.split(" = ", 1)[1].strip()
    raise AssertionError(f"{name} is not in the generated notebook")


# ---- absent by default -----------------------------------------------

@pytest.mark.parametrize("name", [
    "RES_PCT", "TIME_LIMIT", "ADAPTIVE", "NOISE_THRESHOLD", "DENOISE",
    "DENOISER", "MAX_BOUNCES", "FILM_TRANSPARENT", "COLOR_DEPTH",
])
def test_a_plain_render_asks_for_none_of_them(tmp_path, name):
    """The whole safety property in one test: build a notebook the way the
    app does today and every optional setting is the empty string, which
    the setup cell reads as "leave the scene alone"."""
    source = _cells(tmp_path, RenderSettings(1920, 1080, 128))

    assert _literal(source, name) == "''", (
        f"{name} would override the .blend on a render that never asked")


def test_the_notebook_treats_empty_as_leave_it_alone():
    """_tune is what enforces it, so it has to return early on an empty
    value.

    Asserted against SETUP_SCRIPT rather than against a built notebook:
    the notebook EMBEDS that script as a Python string literal (it writes
    it to a file before running Blender), so inside the .ipynb its newlines
    are backslash-n and a match on real source would silently never fire.
    """
    from blendfleet.notebook_builder import SETUP_SCRIPT

    assert "def _tune(" in SETUP_SCRIPT
    # The guard AND its early return, as one string: asserting them
    # separately let a long docstring sit between them and still pass.
    assert 'if raw is None or raw == "":\n        return\n' in SETUP_SCRIPT


# ---- what a chosen value does ----------------------------------------

def test_each_setting_reaches_the_notebook_when_chosen(tmp_path):
    source = _cells(tmp_path, RenderSettings(
        1920, 1080, 128,
        resolution_percentage=50,
        time_limit_seconds=90,
        adaptive_sampling=True,
        noise_threshold=0.02,
        denoise=False,
        denoiser="OPTIX",
        max_bounces=6,
        film_transparent=True,
        color_depth="16"))

    assert _literal(source, "RES_PCT") == "'50'"
    assert _literal(source, "TIME_LIMIT") == "'90'"
    assert _literal(source, "ADAPTIVE") == "'1'"
    assert _literal(source, "NOISE_THRESHOLD") == "'0.02'"
    # A switch turned OFF is a decision and must travel as one.
    assert _literal(source, "DENOISE") == "'0'"
    assert _literal(source, "DENOISER") == "'OPTIX'"
    assert _literal(source, "MAX_BOUNCES") == "'6'"
    assert _literal(source, "FILM_TRANSPARENT") == "'1'"
    assert _literal(source, "COLOR_DEPTH") == "'16'"


def test_off_and_untouched_are_different_things(tmp_path):
    """The distinction the whole design rests on: False reaches Blender,
    None does not."""
    off = _cells(tmp_path, RenderSettings(1920, 1080, 128, denoise=False))
    assert _literal(off, "DENOISE") == "'0'"

    untouched = _cells(tmp_path, RenderSettings(1920, 1080, 128))
    assert _literal(untouched, "DENOISE") == "''"


def test_the_notebook_uses_blenders_own_property_names(tmp_path):
    """Read from a real Blender install rather than remembered. A wrong
    name would not raise -- _tune logs and moves on -- so nothing at
    runtime would tell us it never applied."""
    source = _cells(tmp_path, RenderSettings(1920, 1080, 128))

    for prop in ('"resolution_percentage"', '"time_limit"',
                 '"use_adaptive_sampling"', '"adaptive_threshold"',
                 '"use_denoising"', '"denoiser"', '"max_bounces"',
                 '"film_transparent"', '"color_depth"', '"color_mode"'):
        assert prop in source, f"{prop} is not set anywhere in the notebook"


def test_transparency_takes_the_alpha_channel_with_it(tmp_path):
    """A transparent film written as 8-bit RGB throws the alpha away, so
    the colour mode has to follow the film -- otherwise the setting looks
    applied and the output has no transparency in it."""
    source = _cells(tmp_path, RenderSettings(1920, 1080, 128,
                                             film_transparent=True))

    assert '"RGBA" if _transparent else "RGB"' in source


def test_full_size_is_still_the_default(tmp_path):
    """The app has always rendered at 100% regardless of what the .blend
    was saved at -- a scene left at 50% for viewport work must not render
    at half size on the farm. The new percentage is an override of THAT,
    not of the scene."""
    source = _cells(tmp_path, RenderSettings(1920, 1080, 128))

    assert "s.render.resolution_percentage = 100" in source


# ---- from the page's payload -----------------------------------------

def _session(tmp_path):
    from blendfleet.accounts import Account, AccountStore
    from blendfleet.fleet import Fleet
    from blendfleet.rpc.session import Session
    from blendfleet.settings import Settings

    store = AccountStore([Account(label="acct0", token="KGAT_" + "0" * 32,
                                  username="user_0", verified=True)])
    root = tmp_path / "s"
    root.mkdir(exist_ok=True)
    return Session(store, lambda accounts: Fleet(
        accounts, lambda t: None, root / "w"), lambda t: "someone", Settings())


def test_an_options_payload_without_them_changes_nothing(tmp_path):
    """An older page -- or the Qt build mid-upgrade -- sends the six fields
    it always sent. Every optional setting must come out as its sentinel."""
    from blendfleet.notebook_builder import RenderSettings as RS

    session = _session(tmp_path)
    try:
        # The same construction launch() performs, driven directly so the
        # test is about the mapping and not about Kaggle.
        options = {"startFrame": 1, "endFrame": 2, "resX": 1920,
                   "resY": 1080, "samples": 128, "format": "PNG"}
        built = RS(
            int(options.get("resX", 1920)), int(options.get("resY", 1080)),
            int(options.get("samples", 128)),
            resolution_percentage=int(options.get("resPct") or 0),
            time_limit_seconds=float(options.get("timeLimit") or 0),
            noise_threshold=float(options.get("noiseThreshold") or 0),
            max_bounces=int(options.get("maxBounces") or 0),
            denoiser=str(options.get("denoiser") or ""),
            color_depth=str(options.get("colorDepth") or ""))

        assert built.resolution_percentage == 0
        assert built.time_limit_seconds == 0
        assert built.noise_threshold == 0
        assert built.max_bounces == 0
        assert built.denoiser == ""
        assert built.color_depth == ""
        assert built.adaptive_sampling is None
        assert built.denoise is None
        assert built.film_transparent is None
    finally:
        session.stop()


@pytest.mark.parametrize("sent, expected", [
    (None, None),
    (True, True),
    (False, False),
])
def test_a_switch_the_page_did_not_send_stays_unasked(sent, expected):
    from blendfleet.rpc.session import _tri

    assert _tri(sent) is expected
