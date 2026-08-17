"""Three things the Kaggle push has always accepted and this app never sent.

Found by auditing every endpoint in the SDK against what the app calls:

  - `session_timeout_seconds` -- a hung render otherwise spends the
    account's quota until KAGGLE stops it, which takes hours, and a session
    cannot be given a limit once it is running
  - `machine_shape` per render -- T4 (two cards, split across a frame) or
    P100 (one card, memory undivided) was a hardcoded constant
  - `docker_image` -- an exact base image, which is the only way to hold
    two renders a month apart on the same CUDA driver

The rule they share: an invalid `machine_shape` is accepted at push time
with NO error and silently gives a single P100 instead. So these are
validated in Python, whitelisted where a whitelist exists, and the page is
sent the list rather than keeping its own copy of it.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from blendfleet import design
from blendfleet.notebook_builder import MACHINE_SHAPE, RenderSettings, build
from blendfleet.settings import Settings


def _settings(**kwargs) -> Settings:
    s = Settings(**kwargs)
    s.__post_init__()
    return s


# ---- what Settings will accept ---------------------------------------

def test_the_machine_shape_is_a_whitelist_because_a_typo_is_silent():
    """Kaggle takes an invalid shape without complaint and hands back one
    P100. A shape check that let anything through would therefore not fail
    loudly -- it would quietly halve the hardware."""
    assert _settings(machine_shape="NvidiaTeslaP100").machine_shape \
        == "NvidiaTeslaP100"
    # A plausible typo, and the exact one docs/machine-shape-findings.md
    # records as silently downgrading:
    assert _settings(machine_shape="NvidiaTeslaT4x2").machine_shape \
        == design.DEFAULT_MACHINE_SHAPE
    assert _settings(machine_shape=["a list"]).machine_shape \
        == design.DEFAULT_MACHINE_SHAPE


def test_a_tpu_is_not_offered_at_all():
    """Cycles cannot render on a TPU, so a TPU session is a way to spend
    quota producing nothing."""
    assert "Tpu1VmV38" not in design.MACHINE_SHAPES
    assert _settings(machine_shape="Tpu1VmV38").machine_shape \
        == design.DEFAULT_MACHINE_SHAPE


@pytest.mark.parametrize("given, expected", [
    (0, 0),                                   # 0 = leave Kaggle's own limit
    (90, 90),
    (10_000, design.MAX_SESSION_TIMEOUT_MINUTES),   # clamped, not rejected
    (-5, 0),
    (True, 0),                                # bool is an int; never a time
    ("45", 0),
])
def test_the_timeout_is_clamped_rather_than_refused(given, expected):
    """A number too large is a request Kaggle ignores; a negative one is
    nonsense. Neither should stop the app opening."""
    assert _settings(session_timeout_minutes=given).session_timeout_minutes \
        == expected


def test_an_image_reference_is_shape_checked_not_whitelisted():
    """Digests are Kaggle's to mint and this app has no list of them. What
    it can say is that a value with spaces in it is not one."""
    digest = "gcr.io/kaggle-images/python@sha256:" + "a" * 64
    assert _settings(docker_image=f"  {digest}  ").docker_image == digest
    assert _settings(docker_image="not an image").docker_image == ""
    assert _settings(docker_image=None).docker_image == ""


def test_they_survive_a_round_trip_to_disk(tmp_path, monkeypatch):
    import blendfleet.settings as settings_mod

    monkeypatch.setattr(settings_mod, "config_dir", lambda: tmp_path)
    digest = "gcr.io/kaggle-images/python@sha256:" + "b" * 64
    _settings(machine_shape="NvidiaTeslaP100", session_timeout_minutes=45,
              docker_image=digest).save()

    reloaded = Settings.load()

    assert reloaded.machine_shape == "NvidiaTeslaP100"
    assert reloaded.session_timeout_minutes == 45
    assert reloaded.docker_image == digest


def test_a_settings_file_from_before_these_existed_gets_the_defaults(
        tmp_path, monkeypatch):
    import blendfleet.settings as settings_mod

    monkeypatch.setattr(settings_mod, "config_dir", lambda: tmp_path)
    (tmp_path / "settings.json").write_text(
        json.dumps({"accent": "green", "theme": "dark"}), encoding="utf-8")

    loaded = Settings.load()

    assert loaded.machine_shape == design.DEFAULT_MACHINE_SHAPE
    assert loaded.session_timeout_minutes == 0, (
        "an upgrade must not start capping sessions nobody asked to cap")
    assert loaded.docker_image == ""


# ---- what reaches the kernel metadata --------------------------------

def _metadata(tmp_path, settings):
    build([1], settings, "me/scene-blend", tmp_path, "me/scene-render-abcd1234")
    return json.loads((tmp_path / "kernel-metadata.json").read_text())


def test_the_chosen_machine_is_what_the_push_asks_for(tmp_path):
    meta = _metadata(tmp_path, RenderSettings(
        1920, 1080, 128, machine_shape="NvidiaTeslaP100"))
    assert meta["machine_shape"] == "NvidiaTeslaP100"


def test_the_default_is_still_the_shape_that_was_hardcoded(tmp_path):
    """Two T4s were measured to be the better default. Making the shape a
    choice must not quietly change what everybody already gets."""
    meta = _metadata(tmp_path, RenderSettings(1920, 1080, 128))
    assert meta["machine_shape"] == MACHINE_SHAPE == "NvidiaTeslaT4"


def test_a_named_image_is_sent_and_an_unnamed_one_is_absent(tmp_path):
    """Absent, not empty: kernels_push reads this key and would hand
    Kaggle an empty image reference, which is not the same request as
    "whatever is current"."""
    digest = "gcr.io/kaggle-images/python@sha256:" + "c" * 64
    named = _metadata(tmp_path, RenderSettings(1920, 1080, 128,
                                               docker_image=digest))
    assert named["docker_image"] == digest

    unnamed = _metadata(tmp_path, RenderSettings(1920, 1080, 128))
    assert "docker_image" not in unnamed
    assert unnamed["docker_image_pinning_type"] == "original", (
        "with no image named, the per-kernel pin is what still holds")


# ---- what reaches the push -------------------------------------------

class _PushRecorder:
    def __init__(self):
        self.pushes = []

    def kernels_push(self, folder, timeout=None, acc=None):
        self.pushes.append({"folder": folder, "timeout": timeout, "acc": acc})


def _client(api):
    from blendfleet.kaggle_client import KaggleClient
    return KaggleClient("KGAT_" + "0" * 32, api_factory=lambda token: api,
                        label="acct0")


def test_a_timeout_is_passed_as_kaggle_wants_it(tmp_path):
    """kernels_push takes `timeout` as a string of SECONDS. The setting is
    in minutes because that is what a person types."""
    api = _PushRecorder()
    _client(api).push_kernel(tmp_path, timeout_seconds=45 * 60)

    assert api.pushes[0]["timeout"] == "2700"


def test_no_timeout_means_the_argument_is_not_sent_at_all(tmp_path):
    """Rather than sent as "0", which kernels_push would cast to a
    zero-second session."""
    api = _PushRecorder()
    _client(api).push_kernel(tmp_path)

    assert api.pushes[0]["timeout"] is None


def test_the_render_path_carries_the_users_cap_to_the_push(tmp_path):
    """The whole point: a setting nobody wires through is a control that
    does nothing."""
    from blendfleet.accounts import Account
    from blendfleet.fleet import Fleet

    from test_fleet import FakeClient      # the complete fake, not a new one

    client = FakeClient("KGAT_" + "0" * 32)
    accounts = [Account(label="acct0", token="KGAT_" + "0" * 32,
                        username="user_0", verified=True)]
    fleet = Fleet(accounts, lambda t: client, tmp_path / "w")

    blend = tmp_path / "waydown.blend"
    blend.write_bytes(b"BLENDER")

    fleet.launch(blend, RenderSettings(1920, 1080, 128,
                                       session_timeout_seconds=3600), 1, 2)

    assert client.push_timeouts, "nothing was pushed"
    assert client.push_timeouts[0] == 3600


# ---- what the page is given ------------------------------------------

def test_the_page_is_sent_the_shapes_rather_than_keeping_its_own_list(
        tmp_path, monkeypatch):
    """The page must offer exactly what Python validates, or a button
    would save a value that is silently replaced by the default."""
    import blendfleet.settings as settings_mod
    from blendfleet.accounts import Account, AccountStore
    from blendfleet.fleet import Fleet
    from blendfleet.rpc.session import Session

    monkeypatch.setattr(settings_mod, "config_dir", lambda: tmp_path)
    store = AccountStore([Account(label="acct0", token="KGAT_" + "0" * 32,
                                  username="user_0", verified=True)])
    root = tmp_path / "s"
    root.mkdir()
    session = Session(store, lambda accounts: Fleet(
        accounts, lambda t: None, root / "w"), lambda t: "someone",
        _settings())
    try:
        payload = json.loads(session.preferences())

        assert payload["machineShapes"] == dict(design.MACHINE_SHAPES)
        assert payload["machineShape"] == design.DEFAULT_MACHINE_SHAPE
        assert payload["sessionTimeout"] == 0
        assert payload["sessionTimeoutMax"] == design.MAX_SESSION_TIMEOUT_MINUTES
        assert payload["dockerImage"] == ""
    finally:
        session.stop()


@pytest.mark.parametrize("key, field, value", [
    ("machineShape", "machine_shape", "NvidiaTeslaP100"),
    ("sessionTimeout", "session_timeout_minutes", 30),
    ("dockerImage", "docker_image", "gcr.io/kaggle-images/python@sha256:d"),
])
def test_the_page_can_actually_set_them(tmp_path, monkeypatch, key, field,
                                        value):
    import blendfleet.settings as settings_mod
    from blendfleet.accounts import Account, AccountStore
    from blendfleet.fleet import Fleet
    from blendfleet.rpc.session import Session

    monkeypatch.setattr(settings_mod, "config_dir", lambda: tmp_path)
    store = AccountStore([Account(label="acct0", token="KGAT_" + "0" * 32,
                                  username="user_0", verified=True)])
    root = tmp_path / "s"
    root.mkdir()
    session = Session(store, lambda accounts: Fleet(
        accounts, lambda t: None, root / "w"), lambda t: "someone",
        _settings())
    try:
        session.setPreference(key, json.dumps(value))

        assert getattr(session.settings, field) == value
        # And saved, not merely held: the sidecar and the Qt window read
        # the same file, and a render started from either has to see it.
        assert getattr(Settings.load(), field) == value
    finally:
        session.stop()
