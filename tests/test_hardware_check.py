"""Checking what hardware an account actually gets, before committing.

Kaggle's allocation is a lottery, not a setting. The same account, asking
for the same machine_shape minutes apart, got 2x Tesla T4 once and no GPU
at all the next time (docs/machine-shape-findings.md; seen again on
2026-08-12, when two sessions were cancelled mid-run and a third sat
queued). A render is the expensive way to discover that.

So: a probe kernel that reports its hardware and stops. About a minute of
quota, no scene upload, no Blender download.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from blendfleet.accounts import Account
from blendfleet.fleet import Fleet
from blendfleet.notebook_builder import (HARDWARE_REPORT, MACHINE_SHAPE,
                                         RenderSettings, build, build_probe)


def cells(path: Path) -> list[str]:
    nb = json.loads(Path(path).read_text(encoding="utf-8"))
    return ["".join(c["source"]) for c in nb["cells"]]


def metadata(out_dir: Path) -> dict:
    return json.loads((out_dir / "kernel-metadata.json")
                      .read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The probe notebook
# --------------------------------------------------------------------------

def test_the_probe_is_one_cell_and_valid_python(tmp_path):
    src = cells(build_probe(tmp_path, "me/bf-hwcheck-1"))
    assert len(src) == 1, "a probe that does more than ask has more to break"
    ast.parse(src[0])


def test_the_probe_reports_hardware_in_the_format_the_app_parses(tmp_path):
    """The strings are a contract with log_stream.parse_preflight.

    Shared verbatim with the render path rather than re-written, because a
    drifting copy fails SILENTLY -- the app would just report no hardware.
    """
    from blendfleet.log_stream import parse_preflight

    src = cells(build_probe(tmp_path, "me/bf-hwcheck-1"))[0]
    assert HARDWARE_REPORT.strip() in src
    # And the format really is the one the parser accepts.
    line = ('data: {"stream_name":"stdout","data":"PREFLIGHT gpus=2 '
            'gpu_names=Tesla T4|Tesla T4 cpu=4 ram=31.3"}')
    assert parse_preflight(line) is not None


def test_the_render_path_still_reports_hardware_the_same_way(tmp_path):
    # The extraction must not have taken the line out of a real render.
    joined = "\n".join(cells(build([1], RenderSettings(64, 36, 1), "me/x",
                                   tmp_path, "me/r")))
    assert "PREFLIGHT gpus=" in joined
    assert "cores | RAM" in joined


def test_the_probe_attaches_nothing(tmp_path):
    """No dataset, so it cannot fail for a reason unrelated to hardware.

    The render path's first cell walks /kaggle/input for a .blend and
    ASSERTS it found one -- a probe reusing that cell with no dataset
    attached would trip on the assert every time.
    """
    build_probe(tmp_path, "me/bf-hwcheck-1")
    meta = metadata(tmp_path)
    assert meta["dataset_sources"] == []
    src = cells(tmp_path / "render.ipynb")[0]
    assert "assert BLEND" not in src
    # No Blender install either -- that is a ~370 MB download or a dataset
    # mount, and neither tells you anything about the hardware. Asserted
    # against the code that would do it, not the word "blender", which
    # legitimately appears in the shared comment and in the DNS test host.
    for install in ("wget", "tar -xf", "BBIN", "os.walk"):
        assert install not in src, f"a probe must not {install}"


def test_the_probe_asks_for_exactly_what_a_render_asks_for(tmp_path):
    """Otherwise it answers a question nobody asked.

    machine_shape is what decides whether a session gets 2x T4 or a single
    P100, so a probe requesting anything else would report hardware the
    real render was never going to get.
    """
    build_probe(tmp_path, "me/bf-hwcheck-1")
    probe = metadata(tmp_path)
    render_dir = tmp_path / "r"
    build([1], RenderSettings(64, 36, 1), "me/x", render_dir, "me/r")
    render = metadata(render_dir)
    assert probe["machine_shape"] == render["machine_shape"] == MACHINE_SHAPE
    assert probe["enable_gpu"] is render["enable_gpu"] is True


def test_the_probe_checks_dns_too(tmp_path):
    """The two failures travel together.

    A session that gets no GPU is usually the same session that gets no
    outbound network -- measured 2026-08-11 as "Temporary failure in name
    resolution", which is what killed every early render in the Blender
    download. One probe should answer both, because the answers imply
    different fixes (retry vs attach the Blender dataset).
    """
    src = cells(build_probe(tmp_path, "me/bf-hwcheck-1"))[0]
    assert "getaddrinfo" in src
    assert "PROBE_DNS" in src


def test_the_probe_is_private(tmp_path):
    build_probe(tmp_path, "me/bf-hwcheck-1")
    assert metadata(tmp_path)["is_private"] is True


# --------------------------------------------------------------------------
# Fleet.check_hardware
# --------------------------------------------------------------------------

class FakeClient:
    def __init__(self, username="someone"):
        self.username = username
        self.pushed: list[Path] = []

    def whoami(self):
        return self.username

    def push_kernel(self, work_dir):
        self.pushed.append(Path(work_dir))


def fleet_with(accounts, client, tmp_path):
    return Fleet(accounts, lambda tok: client, tmp_path / "work")


def test_check_hardware_pushes_a_probe_for_that_account(tmp_path):
    client = FakeClient("stive-handle")
    accounts = [Account(label="stive", token="KGAT_" + "a" * 32,
                        username="stive-handle", verified=True)]
    slug = fleet_with(accounts, client, tmp_path).check_hardware("stive")
    assert slug.startswith("stive-handle/blendfleet-hwcheck-")
    assert len(client.pushed) == 1
    src = cells(client.pushed[0] / "render.ipynb")
    assert "PREFLIGHT gpus=" in src[0]


def test_each_check_gets_its_own_slug(tmp_path):
    # Two accounts checking at once must not collide on a shared name.
    client = FakeClient()
    accounts = [Account(label="a", token="KGAT_" + "a" * 32,
                        username="one", verified=True)]
    f = fleet_with(accounts, client, tmp_path)
    assert f.check_hardware("a") != f.check_hardware("a")


def test_check_hardware_refuses_an_unknown_account(tmp_path):
    client = FakeClient()
    accounts = [Account(label="a", token="KGAT_" + "a" * 32, username="one")]
    with pytest.raises(ValueError, match="no account labelled"):
        fleet_with(accounts, client, tmp_path).check_hardware("nope")
    assert client.pushed == [], "nothing may be pushed for an unknown account"


def test_check_hardware_does_not_touch_the_job_state(tmp_path, monkeypatch):
    """A probe is not a job.

    fleet.json holds ONE job. Writing a probe there would overwrite the
    record of a running render, leaving its kernels uncancellable and
    uncollectable -- exactly what launch()'s single-slot guard exists to
    prevent.
    """
    import blendfleet.fleet as fleet_mod
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(fleet_mod, "state_dir", lambda: state)
    client = FakeClient()
    accounts = [Account(label="a", token="KGAT_" + "a" * 32, username="one")]
    fleet_with(accounts, client, tmp_path).check_hardware("a")
    assert not (state / "fleet.json").exists()


def test_check_hardware_looks_up_a_missing_username(tmp_path):
    # An account added by token alone still needs a handle to build a slug.
    client = FakeClient("discovered")
    accounts = [Account(label="a", token="KGAT_" + "a" * 32, username=None)]
    slug = fleet_with(accounts, client, tmp_path).check_hardware("a")
    assert slug.startswith("discovered/")
