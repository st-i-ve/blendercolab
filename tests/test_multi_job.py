"""Two scenes rendering at once.

The state file was a single slot, and launch() refused to start while a
job was live -- deliberately, because overwriting it would leave running
kernels uncancellable and uncollectable, spending other people's quota.
Concurrency therefore is not "allow a second write"; it is a list of jobs
that all remain individually tracked, cancellable and collectable.
"""
import json

import pytest

import blendfleet.fleet as fleet_mod
from blendfleet.accounts import Account
from blendfleet.fleet import Fleet, FleetState, WorkerState


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_mod, "state_dir", lambda: tmp_path)
    accounts = [Account(label=f"a{i}", token=f"KGAT_{i:032x}",
                        username=f"user{i}", verified=True)
                for i in range(4)]
    return Fleet(accounts, lambda t: object(), tmp_path / "w")


def job(name, labels, job_id="j1"):
    return FleetState(
        job_id=job_id, blend_name=f"{name}.blend", start_frame=1,
        end_frame=len(labels),
        workers=[WorkerState(label=l, username=f"user_{l}",
                             kernel_slug=f"user_{l}/{name}-render-{job_id}",
                             frames=[i + 1])
                 for i, l in enumerate(labels)])


def test_two_jobs_are_both_kept(fleet):
    fleet.save_jobs([job("alpha", ["a0", "a1"], "j1"),
                     job("beta", ["a2", "a3"], "j2")])
    got = fleet.load_jobs()
    assert [j.blend_name for j in got] == ["alpha.blend", "beta.blend"]
    assert [w.label for w in got[1].workers] == ["a2", "a3"]


def test_a_single_job_state_file_from_an_older_build_still_loads(fleet,
                                                                 tmp_path):
    """An in-flight render must survive the upgrade. The old format was
    one FleetState object at the top level; dropping it would orphan
    kernels that are running right now."""
    old = {"job_id": "old1", "blend_name": "remember.blend",
           "start_frame": 1, "end_frame": 5,
           "workers": [{"label": "a0", "username": "user0",
                        "kernel_slug": "user0/remember-render-old1",
                        "frames": [1, 2], "state": "running",
                        "frames_done": 1, "message": ""}]}
    (tmp_path / "fleet.json").write_text(json.dumps(old), encoding="utf-8")
    jobs = fleet.load_jobs()
    assert len(jobs) == 1
    assert jobs[0].job_id == "old1"
    assert jobs[0].workers[0].kernel_slug == "user0/remember-render-old1"


def test_load_still_answers_with_the_most_recent_job(fleet):
    """Every existing caller uses load(). It keeps working, and answers
    with the newest job rather than silently picking an arbitrary one."""
    fleet.save_jobs([job("alpha", ["a0"], "j1"), job("beta", ["a1"], "j2")])
    assert fleet.load().blend_name == "beta.blend"


def test_no_state_file_is_no_jobs(fleet):
    assert fleet.load_jobs() == []
    assert fleet.load() is None


def test_an_empty_state_file_is_no_jobs(fleet, tmp_path):
    """A half-written save leaves this behind, and json.loads answers it
    with an error that surfaced as an unrelated upload failure."""
    (tmp_path / "fleet.json").write_text("", encoding="utf-8")
    assert fleet.load_jobs() == []


def test_a_scene_key_is_the_slug_stem(fleet):
    assert job("alpha", ["a0"]).scene_key == "alpha"
