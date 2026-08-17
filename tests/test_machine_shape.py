"""What machine Kaggle actually gave a render, for free.

The only way this app knew what hardware an account got was to start a
session and read nvidia-smi out of the log -- about a minute of the thirty
hours a week an account has. A kernel's metadata carries `machine_shape`,
"the machine shape that was used in the last session", for the price of a
metadata request and no quota at all.

The rules it has to keep are the app's existing ones, not new ones:

  - it describes ONE session, so it is attached to the worker (whose kernel
    slug is unique per job) and needs no age -- unlike the cached hardware
    snapshot, which is a reading carried across runs and must carry one
  - it is asked ONCE per worker, exactly like the final frame count, since
    a finished job keeps being polled every 30 seconds until forgotten
  - an accelerator name this app does not recognise is shown as Kaggle
    said it, never blanked
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from blendfleet.accounts import Account, AccountStore
from blendfleet.fleet import Fleet, FleetState, WorkerState
from blendfleet.kaggle_client import KaggleClient, describe_machine
from blendfleet.rpc.session import Session
from blendfleet.settings import Settings


# ---- Kaggle's vocabulary ---------------------------------------------

@pytest.mark.parametrize("shape, expected", [
    # The live vocabulary, confirmed against a real fleet on 2026-08-17 and
    # matching both notebook_builder.MACHINE_SHAPE and
    # docs/machine-shape-findings.md: capitalised. The first version of
    # describe_machine only matched lowercase, so every card would have
    # printed the raw "NvidiaTeslaT4".
    ("NvidiaTeslaT4", "Tesla T4"),
    ("NvidiaTeslaP100", "Tesla P100"),
    # Lowercase and count-suffixed forms still read, because the vocabulary
    # is Kaggle's to change and this should not care about its shift key.
    ("nvidiaTeslaT4x2", "2x Tesla T4"),
    ("nvidiaL4x4", "4x L4"),
])
def test_a_machine_name_is_read_as_words(shape, expected):
    assert describe_machine(shape) == expected


def test_the_shape_the_app_itself_requests_reads_as_words():
    """The one name that is guaranteed to turn up in production, since it
    is the one every render asks for."""
    from blendfleet.notebook_builder import MACHINE_SHAPE

    assert describe_machine(MACHINE_SHAPE) == "Tesla T4"


def test_an_unknown_accelerator_is_shown_as_kaggle_said_it():
    """Kaggle adds hardware without asking. The choice for a name this
    app has never seen is between showing it and showing nothing, and a
    raw string is ugly and true."""
    assert describe_machine("tpu1vmV38") == "tpu1vmV38"
    assert describe_machine("something-new-entirely") == "something-new-entirely"


def test_nothing_is_described_as_nothing():
    assert describe_machine(None) is None
    assert describe_machine("") is None


# ---- the client call -------------------------------------------------

class _FakeSdk:
    """Just the shape KaggleClient.machine_shape reaches through."""

    def __init__(self, shape="nvidiaTeslaT4x2", raises=None):
        self._shape = shape
        self._raises = raises
        self.asked = []

        outer = self

        class _Metadata:
            machine_shape = shape

        class _Response:
            metadata = _Metadata()

        class _ApiClient:
            def get_kernel(self, request):
                outer.asked.append((request.user_name, request.kernel_slug))
                if outer._raises is not None:
                    raise outer._raises
                return _Response()

        class _Kernels:
            kernels_api_client = _ApiClient()

        self.kernels = _Kernels()


def _client(sdk):
    return KaggleClient("KGAT_" + "0" * 32, label="acct0",
                        sdk_factory=lambda token: sdk)


def test_the_machine_shape_comes_back_for_a_slug():
    sdk = _FakeSdk()
    assert _client(sdk).machine_shape("user_0/waydown-render-job") \
        == "nvidiaTeslaT4x2"
    assert sdk.asked == [("user_0", "waydown-render-job")], (
        "the slug has to be split into owner and kernel for this endpoint")


def test_a_metadata_failure_is_not_allowed_to_break_a_poll():
    """This runs inside the 30-second poll. A render's status must not go
    unreported because the name of its GPU could not be looked up."""
    sdk = _FakeSdk(raises=RuntimeError("503 from Kaggle"))
    assert _client(sdk).machine_shape("user_0/k") is None


def test_a_slug_that_is_not_a_slug_asks_nothing():
    sdk = _FakeSdk()
    assert _client(sdk).machine_shape("no-owner-here") is None
    assert sdk.asked == []


# ---- once per worker, and no more ------------------------------------

class _CountingClient:
    """A client that reports a running kernel and counts metadata calls."""

    def __init__(self, token, label=None, state="running"):
        self.token = token
        self._state = state
        self.machine_calls = 0

    def status(self, slug):
        from blendfleet.kaggle_client import KernelStatus
        return KernelStatus(state=self._state)

    def machine_shape(self, slug):
        self.machine_calls += 1
        return "nvidiaTeslaP100"

    def quota(self):
        from blendfleet.kaggle_client import Quota
        return Quota(0, 108000, "soon", "api")


def _fleet(tmp_path, client):
    store = AccountStore([Account(label="acct0", token="KGAT_" + "0" * 32,
                                  username="user_0", verified=True)])
    fleet = Fleet(store.list(), lambda t: client, tmp_path / "w")
    fleet.save_jobs([FleetState(
        job_id="job", blend_name="waydown.blend", start_frame=1, end_frame=2,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/waydown-render-job",
                             frames=[1, 2], state="running")])])
    return fleet


def test_the_machine_is_asked_for_once_and_then_remembered(tmp_path):
    """The same discipline as the final frame count: a real network call,
    on a job that keeps being polled every 30 seconds for as long as it is
    tracked."""
    client = _CountingClient("t")
    fleet = _fleet(tmp_path, client)

    fleet.poll()
    assert client.machine_calls == 1
    saved = fleet.load_jobs()[0].workers[0]
    assert saved.machine_shape == "nvidiaTeslaP100"
    assert saved.machine_checked is True

    fleet.poll()
    fleet.poll()
    assert client.machine_calls == 1, (
        "asked again on a later poll -- one kernel slug is one session, so "
        "there is nothing new to learn")


def test_a_queued_kernel_is_not_asked_what_machine_it_got(tmp_path):
    """A queued kernel has no session yet. Its metadata either says
    nothing or still describes an earlier run, and either would be
    reported as the machine THIS render got."""
    client = _CountingClient("t", state="queued")
    fleet = _fleet(tmp_path, client)

    fleet.poll()

    assert client.machine_calls == 0
    assert fleet.load_jobs()[0].workers[0].machine_shape is None


def test_a_state_file_written_before_this_existed_still_loads():
    """Fleet.load_jobs builds workers as `WorkerState(**w)`, so a file
    written by a version that had no machine fields hands over a dict
    without those keys. The defaults are what make that "not known"
    instead of a TypeError that would file the whole job as unreadable."""
    older_entry = {"label": "acct0", "username": "user_0",
                   "kernel_slug": "user_0/k", "frames": [1, 2],
                   "state": "complete", "frames_done": 2}

    worker = WorkerState(**older_entry)

    assert worker.machine_shape is None
    assert worker.machine_checked is False, (
        "an old file must not read as 'already asked', or the machine would "
        "never be looked up for a job that predates this feature")


# ---- what the page is told -------------------------------------------

def test_the_payload_carries_the_machine_raw_and_readable(tmp_path):
    """Both forms: the raw name because it is what Kaggle said, the
    readable one so the page never has to know Kaggle's vocabulary."""
    client = _CountingClient("t")
    store = AccountStore([Account(label="acct0", token="KGAT_" + "0" * 32,
                                  username="user_0", verified=True)])
    root = tmp_path / "session"
    root.mkdir()
    session = Session(store, lambda accounts: Fleet(
        accounts, lambda t: client, root / "w"),
        lambda t: "someone", Settings())
    try:
        Fleet(store.list(), lambda t: client, root / "w").save_jobs([FleetState(
            job_id="job", blend_name="waydown.blend", start_frame=1,
            end_frame=2,
            workers=[WorkerState(label="acct0", username="user_0",
                                 kernel_slug="user_0/k", frames=[1, 2],
                                 state="running",
                                 machine_shape="nvidiaTeslaT4x2",
                                 machine_checked=True)])])
        payload = json.loads(session.state())
        worker = payload["instances"][0]["worker"]
        assert worker["machineShape"] == "nvidiaTeslaT4x2"
        assert worker["machine"] == "2x Tesla T4"
    finally:
        session.stop()


def test_a_worker_with_no_machine_reported_says_nothing_rather_than_guessing(
        tmp_path):
    client = _CountingClient("t")
    store = AccountStore([Account(label="acct0", token="KGAT_" + "0" * 32,
                                  username="user_0", verified=True)])
    root = tmp_path / "session"
    root.mkdir()
    session = Session(store, lambda accounts: Fleet(
        accounts, lambda t: client, root / "w"),
        lambda t: "someone", Settings())
    try:
        Fleet(store.list(), lambda t: client, root / "w").save_jobs([FleetState(
            job_id="job", blend_name="waydown.blend", start_frame=1,
            end_frame=2,
            workers=[WorkerState(label="acct0", username="user_0",
                                 kernel_slug="user_0/k", frames=[1, 2],
                                 state="running")])])
        worker = json.loads(session.state())["instances"][0]["worker"]
        assert worker["machineShape"] == ""
        assert worker["machine"] == ""
    finally:
        session.stop()
