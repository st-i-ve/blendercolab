"""Two scenes rendering at once.

The state file was a single slot, and launch() refused to start while a
job was live -- deliberately, because overwriting it would leave running
kernels uncancellable and uncollectable, spending other people's quota.
Concurrency therefore is not "allow a second write"; it is a list of jobs
that all remain individually tracked, cancellable and collectable.
"""
import json
from dataclasses import asdict

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


# ---------------------------------------------------------------------------
# Review round 1 -- IMPORTANT 1: _save() re-appended the matched job instead
# of replacing it in place, silently moving it to the end. load() answers
# with the LAST job, and active_workers/poll/cancel_all/cancel_worker/
# fetch_failure_log/forget_job() all go through load() -- so a reorder would
# repoint every one of them at the wrong job the moment two are tracked.
# ---------------------------------------------------------------------------

def test_resaving_a_job_does_not_move_it_to_the_end(fleet):
    j1 = job("alpha", ["a0"], "j1")
    j2 = job("beta", ["a1"], "j2")
    fleet.save_jobs([j1, j2])

    # Exactly what poll()/launch() do internally: load, mutate one job,
    # persist just that job back through _save().
    updated = job("alpha", ["a0"], "j1")
    updated.workers[0].state = "running"
    fleet._save(updated)

    got = fleet.load_jobs()
    assert [j.job_id for j in got] == ["j1", "j2"], (
        "resaving j1 moved it to the end -- load() (and everything built "
        "on it) would now silently point at j2 instead of j1")
    assert fleet.load().job_id == "j2"


# ---------------------------------------------------------------------------
# Review round 1 -- IMPORTANT 3: the per-entry skip in load_jobs() was
# destructive. _save()/poll() rebuild the whole file from load_jobs(), and
# poll() runs on an unattended 30s timer (bridge.py) -- so an unparseable
# entry was being permanently erased within seconds of the app starting,
# taking its kernel_slugs (the one thing needed to cancel it by hand at
# kaggle.com) down with it. This is the test that matters most.
# ---------------------------------------------------------------------------

def test_an_unreadable_job_entry_survives_a_save_cycle(fleet, tmp_path):
    good = job("alpha", ["a0"], "j1")
    bad_entry = {"job_id": "corrupt1", "blend_name": "corrupt.blend",
                "start_frame": 1}   # no end_frame: FleetState(**entry) raises
    (tmp_path / "fleet.json").write_text(
        json.dumps({"jobs": [asdict(good), bad_entry]}), encoding="utf-8")

    jobs = fleet.load_jobs()
    assert [j.job_id for j in jobs] == ["j1"]

    # A save cycle exactly like the one poll()/_save() perform on every
    # tick -- load, then write back.
    fleet._save(jobs[0])

    raw = json.loads((tmp_path / "fleet.json").read_text(encoding="utf-8"))
    assert bad_entry in raw["jobs"], (
        "the unreadable entry was erased by the save cycle -- its "
        "kernel_slug is now permanently unreachable")


def test_an_unreadable_job_entry_is_surfaced_not_swallowed(fleet, tmp_path):
    bad_entry = {"job_id": "corrupt1", "blend_name": "corrupt.blend",
                "start_frame": 1}
    (tmp_path / "fleet.json").write_text(
        json.dumps({"jobs": [bad_entry]}), encoding="utf-8")

    fleet.load_jobs()

    assert fleet.unreadable_jobs == [bad_entry]


# ---------------------------------------------------------------------------
# Review round 1 -- Minors.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_jobs", [None, 5, "oops", {"nested": "dict"}])
def test_a_non_list_jobs_value_degrades_to_no_jobs(fleet, tmp_path, bad_jobs):
    """{"jobs": null} / {"jobs": 5} / ... must degrade like every other
    malformed shape, not escape as an uncaught TypeError."""
    (tmp_path / "fleet.json").write_text(
        json.dumps({"jobs": bad_jobs}), encoding="utf-8")
    assert fleet.load_jobs() == []


def test_forget_job_with_a_duplicate_job_id_drops_only_one(fleet):
    """job_id is only 32 bits of uuid4 and save_jobs() does not itself
    forbid a duplicate -- filtering by equality would drop both jobs for
    the price of one."""
    fleet.save_jobs([job("alpha", ["a0"], "dup"), job("beta", ["a1"], "dup")])

    forgotten = fleet.forget_job("dup")

    remaining = fleet.load_jobs()
    assert len(remaining) == 1, "forgetting one duplicate must not drop both"
    assert [w.label for w in forgotten] == ["a0"]
    assert [j.blend_name for j in remaining] == ["beta.blend"]
