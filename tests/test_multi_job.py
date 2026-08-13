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
from blendfleet.fleet import (Fleet, FleetState, NoBlendInDatasetError,
                              StaleDatasetError, WorkerState)
from blendfleet.notebook_builder import RenderSettings


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


# ---------------------------------------------------------------------------
# Task 4 -- launch() takes the accounts it should use, and the busy check
# narrows from "any job is live" to "these particular accounts are busy".
# ---------------------------------------------------------------------------

def test_launching_a_second_scene_on_free_accounts_is_allowed(fleet,
                                                              monkeypatch):
    """The whole point: a0/a1 render one scene while a2/a3 render another."""
    fleet.save_jobs([job("alpha", ["a0", "a1"], "j1")])
    busy = fleet.busy_labels()
    assert busy == {"a0", "a1"}
    assert fleet.free_accounts() == [a for a in fleet.accounts
                                     if a.label in {"a2", "a3"}]


def test_launching_onto_an_account_that_is_already_rendering_is_refused(fleet):
    """Two kernels from one account on one job's frames would spend that
    account's quota twice for the same output."""
    from blendfleet.fleet import FleetBusyError
    fleet.save_jobs([job("alpha", ["a0", "a1"], "j1")])
    with pytest.raises(FleetBusyError) as excinfo:
        fleet.require_free([a for a in fleet.accounts if a.label == "a1"])
    message = str(excinfo.value)
    assert "a1" in message
    assert "alpha.blend" in message, "must name what it is already doing"


def test_a_finished_job_does_not_hold_its_accounts(fleet):
    finished = job("alpha", ["a0"], "j1")
    finished.workers[0].state = "complete"
    fleet.save_jobs([finished])
    assert fleet.busy_labels() == set()


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


# ---------------------------------------------------------------------------
# Task 5 -- poll() only ever refreshed load()'s newest job, so with two
# concurrent jobs the OLDER one's workers stayed "queued" forever and its
# accounts never came free (measured). cancel_all() had the identical bug
# for cancelling. poll_all()/cancel_job() are the fix.
# ---------------------------------------------------------------------------

def test_polling_updates_every_job(fleet, monkeypatch):
    class Client:
        def __init__(self, token): self.token = token
        def status(self, slug):
            from blendfleet.kaggle_client import KernelStatus
            return KernelStatus(state="complete")
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1"), job("beta", ["a1"], "j2")])
    jobs = fleet.poll_all()
    assert [w.state for j in jobs for w in j.workers] == ["complete", "complete"]


def test_polling_persists_every_jobs_refreshed_state(fleet):
    """Not just the return value -- a second load_jobs() must see the
    same refreshed states, exactly as poll() persists load()'s job."""
    class Client:
        def __init__(self, token): self.token = token
        def status(self, slug):
            from blendfleet.kaggle_client import KernelStatus
            return KernelStatus(state="complete")
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1"), job("beta", ["a1"], "j2")])
    fleet.poll_all()
    reloaded = fleet.load_jobs()
    assert [w.state for j in reloaded for w in j.workers] == ["complete", "complete"]


def test_poll_still_answers_with_the_newest_job_after_refreshing_both(fleet):
    """poll() becomes a thin wrapper over poll_all() -- every existing
    caller (bridge.py's timer, the Qt dashboard) keeps its "one job"
    answer, and it is the freshly-refreshed newest job, not a stale one."""
    class Client:
        def __init__(self, token): self.token = token
        def status(self, slug):
            from blendfleet.kaggle_client import KernelStatus
            return KernelStatus(state="complete")
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1"), job("beta", ["a1"], "j2")])
    st = fleet.poll()
    assert st.job_id == "j2"
    assert st.workers[0].state == "complete"


def test_cancelling_one_job_leaves_the_other_running(fleet):
    cancelled = []

    class Client:
        def __init__(self, token): self.token = token
        def cancel(self, slug):
            cancelled.append(slug)
            return True
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1"), job("beta", ["a1"], "j2")])
    fleet.cancel_job("j1")
    assert all("alpha" in s for s in cancelled), cancelled


def test_cancel_job_reports_per_worker_results_for_just_that_job(fleet):
    class Client:
        def __init__(self, token): self.token = token
        def cancel(self, slug):
            return True
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1"), job("beta", ["a1"], "j2")])
    results = fleet.cancel_job("j1")
    assert [r.label for r in results] == ["a0"]
    assert all(r.ok for r in results)


def test_cancel_job_for_an_unknown_id_cancels_nothing(fleet):
    cancelled = []

    class Client:
        def __init__(self, token): self.token = token
        def cancel(self, slug):
            cancelled.append(slug)
            return True
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1")])
    assert fleet.cancel_job("no-such-job") == []
    assert cancelled == []


def test_cancel_all_stops_every_tracked_job_not_just_the_newest(fleet):
    """cancel_all() used to read load() -- the single newest job -- so a
    second, older job's kernels kept running (and its accounts stayed
    busy) even after the user hit "cancel everything"."""
    cancelled = []

    class Client:
        def __init__(self, token): self.token = token
        def cancel(self, slug):
            cancelled.append(slug)
            return True
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1"), job("beta", ["a1"], "j2")])
    results = fleet.cancel_all()
    assert sorted(r.label for r in results) == ["a0", "a1"]
    assert any("alpha" in s for s in cancelled)
    assert any("beta" in s for s in cancelled)


# ---------------------------------------------------------------------------
# Task 5 -- scene_key must apply slug_stem's own length cap (else it can
# disagree with the stem already baked into this same job's kernel_slug),
# and a residual collision between two differently-named scenes that
# happen to slugify to the same string is accepted as HARMLESS rather than
# fixed away -- see FleetState.scene_key's own docstring for why.
# ---------------------------------------------------------------------------

def test_scene_key_is_capped_exactly_like_the_kernel_slugs_own_stem(fleet):
    from blendfleet.fleet import slug_stem, MAX_STEM_LENGTH
    from pathlib import Path

    long_name = "x" * 40   # well over MAX_STEM_LENGTH once slugified
    st = job(long_name, ["a0"])
    assert st.scene_key == slug_stem(Path(f"{long_name}.blend"))
    assert len(st.scene_key) <= MAX_STEM_LENGTH


def test_colliding_scene_names_share_a_key_but_never_a_filename(fleet):
    """"shot 1.blend" and "shot-1.blend" both slugify to "shot-1" -- a
    genuine collision in the folder scene_key produces. It is harmless,
    not fixed away, because collect() names every copied FRAME from the
    raw, un-slugified stem (see collector.collect), never from scene_key
    -- so two colliding scenes only ever end up sharing a folder, never
    overwriting each other's files inside it."""
    a = FleetState(job_id="j1", blend_name="shot 1.blend",
                  start_frame=1, end_frame=1)
    b = FleetState(job_id="j2", blend_name="shot-1.blend",
                  start_frame=1, end_frame=1)
    assert a.scene_key == b.scene_key == "shot-1"


# ---------------------------------------------------------------------------
# Task 5 fix round 1 -- code review on the first landing of Task 5 found
# poll_all()/cancel_all()/cancel_job() re-introducing (or newly creating,
# for poll_all's save) the very bugs Task 5 itself exists to fix.
# ---------------------------------------------------------------------------

def test_one_jobs_broken_status_check_does_not_abort_polling_the_others(fleet):
    """IMPORTANT 1: poll_all's own bug (an unrefreshed worker sits
    "queued" forever and never frees its account) re-entered through the
    other door -- one job's status() call raising (dead account, network
    blip) used to abort refreshing every OTHER job too."""
    class Client:
        def __init__(self, token): self.token = token
        def status(self, slug):
            if "alpha" in slug:
                raise RuntimeError("network blip")
            from blendfleet.kaggle_client import KernelStatus
            return KernelStatus(state="complete")
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1"), job("beta", ["a1"], "j2")])

    jobs = fleet.poll_all()

    alpha, beta = jobs
    assert alpha.workers[0].state == "queued", "left alone, not crashed"
    assert beta.workers[0].state == "complete", "must still be refreshed"
    # Persisted, not just returned in memory.
    reloaded = fleet.load_jobs()
    assert reloaded[1].workers[0].state == "complete"


def test_an_unparseable_state_file_is_preserved_through_a_save_jobs_round_trip(
        fleet, tmp_path):
    """IMPORTANT 2: the json.loads failure path used to return `[]`
    without recording anything on self.unreadable_jobs, so ANY caller
    that loads then saves (poll_all, _save, forget_job) would silently
    replace a file this mangled -- which might still be the only
    surviving record of a running kernel's slug -- with `{"jobs": []}`."""
    garbage = "{ this is not json but user0/render-old1 is still readable"
    (tmp_path / "fleet.json").write_text(garbage, encoding="utf-8")

    jobs = fleet.load_jobs()
    assert jobs == []
    fleet.save_jobs(jobs)   # exactly what poll_all()/_save() do internally

    on_disk = (tmp_path / "fleet.json").read_text(encoding="utf-8")
    assert "render-old1" in on_disk, (
        f"the only surviving record of that kernel's slug was erased: {on_disk!r}")


def test_polling_an_unparseable_state_file_does_not_erase_it(fleet, tmp_path):
    """IMPORTANT 2, poll_all()'s own angle: it saved unconditionally, so
    the very next unattended 30-second timer tick after the state file
    became unparseable would overwrite it with an empty job list."""
    garbage = "{not valid json, but kernel_slug user0/render-old1 is in here"
    (tmp_path / "fleet.json").write_text(garbage, encoding="utf-8")

    jobs = fleet.poll_all()

    assert jobs == []
    on_disk = (tmp_path / "fleet.json").read_text(encoding="utf-8")
    assert on_disk == garbage, (
        f"poll_all() must not write anything when it loaded nothing: {on_disk!r}")


def test_a_job_launched_while_a_poll_is_in_flight_is_not_erased(fleet):
    """IMPORTANT 3: dashboard's 30s poll timer and _LaunchWorker run
    concurrently with no mutual exclusion. poll_all() used to overwrite
    the WHOLE file with its own pre-poll snapshot, so a job launched
    while an older job's poll was still in flight vanished from the
    state file -- its kernels left running, uncancellable and
    uncollectable."""
    fleet.save_jobs([job("alpha", ["a0"], "j1")])

    class Client:
        def __init__(self, token): self.token = token
        def status(self, slug):
            # Simulates a launch landing on disk WHILE this poll is still
            # running -- BEFORE poll_all() has written back its own
            # result.
            fleet.save_jobs(fleet.load_jobs() + [job("beta", ["a1"], "j2")])
            from blendfleet.kaggle_client import KernelStatus
            return KernelStatus(state="complete")
    fleet.client_factory = Client

    fleet.poll_all()

    remaining = fleet.load_jobs()
    assert [j.job_id for j in remaining] == ["j1", "j2"], (
        "the job launched mid-poll must survive, and the polled job's "
        "own refreshed state must still be persisted")
    assert remaining[0].workers[0].state == "complete"


def test_cancel_all_skips_workers_that_already_finished(fleet):
    """IMPORTANT 5: once cancel_all() started reading every tracked job
    instead of just the newest, it also started re-"cancelling" jobs
    that finished days ago and were simply never forgotten -- each one
    answering False and degrading the dashboard's "could not be
    cancelled" warning into routine noise, plus a wasted HTTP call."""
    cancelled = []

    class Client:
        def __init__(self, token): self.token = token
        def cancel(self, slug):
            cancelled.append(slug)
            return True
    fleet.client_factory = Client

    old_job = job("alpha", ["a0"], "j1")
    old_job.workers[0].state = "complete"   # finished days ago
    fleet.save_jobs([old_job, job("beta", ["a1"], "j2")])

    results = fleet.cancel_all()

    assert [r.label for r in results] == ["a1"], (
        "a finished job's workers must never be cancelled again")
    assert all("alpha" not in s for s in cancelled), cancelled
    assert any("beta" in s for s in cancelled), cancelled


def test_cancel_job_with_a_duplicate_job_id_cancels_both(fleet):
    """Minor: unlike forget_job() (which deliberately touches only the
    FIRST duplicate, so it cannot drop two jobs for the price of one),
    cancel_job() must stop EVERY job under a duplicate id -- leaving a
    second one's kernel running and billing is the one outcome a cancel
    action must never produce."""
    cancelled = []

    class Client:
        def __init__(self, token): self.token = token
        def cancel(self, slug):
            cancelled.append(slug)
            return True
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "dup"), job("beta", ["a1"], "dup")])

    results = fleet.cancel_job("dup")

    assert len(results) == 2, results
    assert any("alpha" in s for s in cancelled), cancelled
    assert any("beta" in s for s in cancelled), cancelled


# ---------------------------------------------------------------------------
# Task 5 fix round 2 -- fix round 1's own IMPORTANT 1 fix (a bare
# `except Exception: continue` around status()) swallowed a revoked
# token exactly like a network blip. A network blip clears itself on the
# next poll; a revoked token never does, so leaving the worker's state
# untouched (fine for a blip) permanently wedges that worker at "queued"
# -- busy_labels()/require_free() read w.state straight off disk, so the
# account can never come free for a new launch again, in total silence.
# ---------------------------------------------------------------------------

def test_a_revoked_token_during_poll_does_not_abort_polling_other_jobs(fleet):
    from blendfleet.kaggle_client import KernelStatus, RevokedTokenError

    class Client:
        def __init__(self, token): self.token = token
        def status(self, slug):
            if "alpha" in slug:
                raise RevokedTokenError("dead token")
            return KernelStatus(state="complete")
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1"), job("beta", ["a1"], "j2")])

    jobs = fleet.poll_all()

    beta = jobs[1]
    assert beta.workers[0].state == "complete", (
        "a revoked token on one job must not abort refreshing another")


def test_a_revoked_token_during_poll_marks_the_account_revoked(fleet):
    from blendfleet.kaggle_client import RevokedTokenError

    class Client:
        def __init__(self, token): self.token = token
        def status(self, slug):
            raise RevokedTokenError("dead token")
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1")])

    fleet.poll_all()

    acct0 = next(a for a in fleet.accounts if a.label == "a0")
    assert acct0.revoked is True, (
        "a token poll_all() itself discovers is revoked must be marked, "
        "exactly like _resolve_clients() already marks one on the launch "
        "side -- this is the only OTHER place that ever learns it")
    assert acct0.verified is False


def test_a_revoked_token_during_poll_does_not_leave_the_account_looking_busy_forever(
        fleet):
    """The failure poll_all()'s own docstring says it exists to fix
    ("they sat queued forever ... those accounts could never come free"),
    re-entered a third time -- except unlike a network blip, no retry
    ever clears a revoked token, so this one is permanent unless the
    worker's state is actually moved off "queued"."""
    from blendfleet.kaggle_client import RevokedTokenError

    class Client:
        def __init__(self, token): self.token = token
        def status(self, slug):
            raise RevokedTokenError("dead token")
    fleet.client_factory = Client
    fleet.save_jobs([job("alpha", ["a0"], "j1")])

    fleet.poll_all()

    assert "a0" not in fleet.busy_labels(), (
        "a revoked account must never look permanently busy -- every "
        "future launch on it would be refused with a false "
        "'already rendering' error, forever, in total silence")


def test_poll_returns_the_newest_job_even_when_one_was_launched_mid_poll(fleet):
    """Small fix (Task 5 fix round 2): poll_all() used to `return jobs`
    -- its own PRE-merge snapshot -- rather than `merged`, the result it
    actually wrote to disk. poll()'s "newest job" answer
    (`poll_all()[-1]`) was therefore a tick stale whenever a job was
    launched mid-poll: correct on disk, wrong in the very value poll()
    handed back to its caller (e.g. the dashboard's _last_state)."""
    fleet.save_jobs([job("alpha", ["a0"], "j1")])

    class Client:
        def __init__(self, token): self.token = token
        def status(self, slug):
            fleet.save_jobs(fleet.load_jobs() + [job("beta", ["a1"], "j2")])
            from blendfleet.kaggle_client import KernelStatus
            return KernelStatus(state="complete")
    fleet.client_factory = Client

    st = fleet.poll()

    assert st.job_id == "j2", (
        "poll() must answer with the job that is ACTUALLY newest on disk "
        "after this poll, not a pre-merge snapshot from before it")


# ---------------------------------------------------------------------------
# Task 10 -- launch_from_dataset(): render a scene that lives ONLY on
# Kaggle, no local .blend at all. The fleet fixture's accounts already
# carry a real `username` (user0..user3), so the fixture's own owner
# (a0/user0) is used as dataset_slug's owner throughout.
# ---------------------------------------------------------------------------

class _FakeDatasetApiClient:
    """Stands in for sdk.datasets.dataset_api_client -- Task 3 sharing.
    Correctness of the sharing calls themselves is covered by
    tests/test_sharing.py; these tests only care that launch_from_dataset
    re-grants and re-verifies before pushing anything."""

    def __init__(self):
        self.updated = []

    def get_dataset_metadata(self, request):
        class Info:
            title = ""
            licenses = []
            collaborators = []

        class Resp:
            info = Info()
        return Resp()

    def update_dataset_metadata(self, request):
        self.updated.append(request)

        class Resp:
            errors = []
        return Resp()


class _FakeSdk:
    def __init__(self):
        self.datasets = type("D", (), {
            "dataset_api_client": _FakeDatasetApiClient()})()


class FakeDatasetClient:
    """Stands in for KaggleClient for launch_from_dataset()'s tests. No
    upload path is exercised here at all -- the whole point of Task 10 --
    so this fake only needs the read/share/push surface that method
    actually calls: dataset_files (to find the .blend), dataset_file_size
    (the per-account match check), dataset_reachable, and push_kernel.
    """

    def __init__(self, token, files=None, reachable=True):
        self.token = token
        self.pushed: list = []
        self._files = (list(files) if files is not None
                       else [("remember.blend", 100)])
        self._reachable = reachable
        self.sdk = _FakeSdk()
        self._sdk_factory = lambda tok: self.sdk

    def whoami(self):
        return "user" + self.token[-1]

    def dataset_files(self, slug):
        return list(self._files)

    def dataset_file_size(self, slug, filename):
        for name, size in self._files:
            if name == filename:
                return size
        return None

    def dataset_reachable(self, slug):
        return self._reachable

    def push_kernel(self, folder):
        self.pushed.append(folder)


def test_rendering_an_uploaded_scene_needs_no_local_file(fleet):
    """The point of the library: a scene from five days ago renders
    without a 60 MB upload."""
    clients = {}

    def factory(tok):
        clients[tok] = FakeDatasetClient(tok)
        return clients[tok]
    fleet.client_factory = factory

    st = fleet.launch_from_dataset(
        "user0/remember-blend", RenderSettings(1920, 1080, 128), 1, 4)

    assert st.blend_name == "remember.blend", \
        "the real filename found on Kaggle, not a guess"
    assert len(st.workers) == 4
    assert all(c.pushed for c in clients.values())


def test_every_account_is_verified_against_the_owners_copy(fleet):
    """The size check changes meaning here -- from "does Kaggle match my
    local file" to "does every account see the same copy the owner sees",
    which is the property that actually matters for a fleet render."""
    stale_token = fleet.accounts[3].token
    clients = {}

    def factory(tok):
        files = ([("remember.blend", 999)] if tok == stale_token
                 else [("remember.blend", 100)])
        clients[tok] = FakeDatasetClient(tok, files=files)
        return clients[tok]
    fleet.client_factory = factory

    with pytest.raises(StaleDatasetError) as excinfo:
        fleet.launch_from_dataset(
            "user0/remember-blend", RenderSettings(1920, 1080, 128), 1, 4)

    message = str(excinfo.value)
    assert "user3" in message, "must name the account with the stale copy"
    assert "999" in message and "100" in message
    # Fix round 1, Important 2: the default _require_matching_dataset
    # wording talks about "the local file about to be rendered" and says
    # launching again re-uploads it -- both false here, where there is no
    # local file at all. The message must say what is ACTUALLY true.
    assert "the local file about to be rendered" not in message, \
        "that claim describes launch()'s world, not this one -- there is no local file here"
    assert "the owner always re-uploads" not in message, \
        "launching launch_from_dataset() again never uploads anything"
    assert "owner's own copy" in message
    assert "Dashboard" in message, "must say what to do about a genuine disagreement"
    assert all(c.pushed == [] for c in clients.values()), \
        "nothing may be started while one account's copy is stale"
    assert fleet.load() is None


def test_a_scene_owned_by_a_non_first_account_still_renders(fleet):
    """Fix round 1, Important 1: the library is explicitly cross-account
    (Scene.owner exists precisely because a scene can belong to any
    configured account, not just the first) -- a scene owned by accounts[2]
    must still render, using accounts[2]'s own token to grant/re-verify
    sharing, not self.accounts[0]'s."""
    clients = {}

    def factory(tok):
        clients[tok] = FakeDatasetClient(tok)
        return clients[tok]
    fleet.client_factory = factory

    st = fleet.launch_from_dataset(
        "user2/remember-blend", RenderSettings(1920, 1080, 128), 1, 4)

    assert len(st.workers) == 4
    assert all(c.pushed for c in clients.values())
    # The grant call must have gone out through accounts[2]'s own sdk --
    # the only client whose sdk is wired to _FakeDatasetApiClient here.
    owner_client = clients[fleet.accounts[2].token]
    assert len(owner_client.sdk.datasets.dataset_api_client.updated) == 1


def test_no_configured_account_owns_the_dataset_fails_closed(fleet):
    """The other half of the same fix: when truly NO configured account
    has the dataset's Kaggle username, this must refuse with an explained
    message, not silently guess or crash."""
    clients = {}

    def factory(tok):
        clients[tok] = FakeDatasetClient(tok)
        return clients[tok]
    fleet.client_factory = factory

    with pytest.raises(ValueError) as excinfo:
        fleet.launch_from_dataset(
            "somebody-else/remember-blend",
            RenderSettings(1920, 1080, 128), 1, 4)

    message = str(excinfo.value)
    assert "somebody-else" in message
    assert "no configured account" in message.lower()
    pushed = [p for c in clients.values() for p in c.pushed]
    assert pushed == [], "nothing may be started"


def test_more_than_one_blend_in_a_dataset_picks_deterministically(fleet):
    """Fix round 1, Minor: picking the FIRST file as Kaggle's own listing
    happened to return it was listing-order dependent. Sorted by name, so
    the (still arbitrary, for a dataset this app itself never produces)
    choice is at least reproducible."""
    clients = {}

    def factory(tok):
        clients[tok] = FakeDatasetClient(
            tok, files=[("z.blend", 10), ("a.blend", 20)])
        return clients[tok]
    fleet.client_factory = factory

    st = fleet.launch_from_dataset(
        "user0/two-blends-blend", RenderSettings(1920, 1080, 128), 1, 4)

    assert st.blend_name == "a.blend"


def test_a_dataset_with_no_blend_in_it_refuses_before_pushing_a_kernel(fleet):
    clients = {}

    def factory(tok):
        clients[tok] = FakeDatasetClient(tok, files=[("readme.txt", 12)])
        return clients[tok]
    fleet.client_factory = factory

    with pytest.raises(NoBlendInDatasetError) as excinfo:
        fleet.launch_from_dataset(
            "user0/not-a-scene", RenderSettings(1920, 1080, 128), 1, 4)

    assert "no .blend" in str(excinfo.value)
    pushed = [p for c in clients.values() for p in c.pushed]
    assert pushed == [], "nothing may be started"
    assert fleet.load() is None
