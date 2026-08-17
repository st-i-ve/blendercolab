"""Renders on Kaggle that this app lost track of.

A job forgotten, or the app killed between pushing a kernel and writing its
state file, leaves a session running with nothing on the dashboard to say
so. It keeps spending the account's thirty hours a week either way, and the
user finds out when the next render will not start.

The two rules that make this safe rather than reckless:

  - the action offered for a stray is CANCEL, so the pattern that decides
    "this is one of ours" is matched narrowly. A notebook the user wrote
    themselves must never appear in this list.
  - "nothing is running" and "we could not ask" are reported separately.
    A summary that said "0 found" after failing to reach an account would
    be a lie by arithmetic.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from blendfleet.accounts import Account, AccountStore
from blendfleet.fleet import (Fleet, FleetState, WorkerState,
                              is_blendfleet_kernel)
from blendfleet.kaggle_client import KernelStatus, Quota
from blendfleet.rpc.session import Session
from blendfleet.settings import Settings


# ---- whose notebook is it -------------------------------------------

@pytest.mark.parametrize("slug", [
    "user_0/blendfleet-worker-1a2b3c4d",
    "user_0/blendfleet-hwcheck-9f8e7d6c",
    "user_0/waydown-render-abcd1234",
    "user_0/a-render-00000000",
])
def test_our_own_kernels_are_recognised(slug):
    assert is_blendfleet_kernel(slug) is True


@pytest.mark.parametrize("slug", [
    "user_0/my-thesis-notebook",
    "user_0/render",                    # no job id
    "user_0/waydown-render-notahex1",   # not 8 hex
    "user_0/waydown-render-abcd12345",  # 9 hex, not 8
    "user_0/blendfleet-worker",         # no id
    "user_0/rendering-fluids",          # merely contains "render"
    "",
])
def test_somebody_elses_notebook_is_never_claimed(slug):
    """The action offered for a match is 'cancel this'. A loose pattern
    here would offer to kill the user's own work."""
    assert is_blendfleet_kernel(slug) is False


# ---- the search ------------------------------------------------------

class _Client:
    def __init__(self, refs=(), states=None, raises=None):
        self._refs = list(refs)
        self._states = dict(states or {})
        self._raises = raises
        self.status_calls = []
        self.cancelled = []

    def my_kernel_refs(self, pages=2, page_size=100):
        if self._raises is not None:
            raise self._raises
        return list(self._refs)

    def status(self, slug):
        self.status_calls.append(slug)
        return KernelStatus(state=self._states.get(slug, "running"))

    def machine_shape(self, slug):
        return None

    def cancel(self, slug):
        self.cancelled.append(slug)
        return True

    def quota(self):
        return Quota(0, 108000, "soon", "api")


def _fleet(tmp_path, clients, labels=("acct0",)):
    accounts = [Account(label=label, token=f"KGAT_{i:032x}",
                        username=f"user_{i}", verified=True)
                for i, label in enumerate(labels)]
    by_token = {a.token: clients[a.label] for a in accounts}
    return Fleet(accounts, lambda t: by_token[t], tmp_path / "w")


def test_a_running_session_with_no_tracked_job_is_found(tmp_path):
    client = _Client(refs=["user_0/waydown-render-abcd1234"])
    strays, errors = _fleet(tmp_path, {"acct0": client}).find_stray_sessions()

    assert errors == {}
    assert [(s.label, s.slug) for s in strays] == [
        ("acct0", "user_0/waydown-render-abcd1234")]


def test_a_session_belonging_to_a_tracked_job_is_not_a_stray(tmp_path):
    client = _Client(refs=["user_0/waydown-render-abcd1234"])
    fleet = _fleet(tmp_path, {"acct0": client})
    fleet.save_jobs([FleetState(
        job_id="abcd1234", blend_name="waydown.blend", start_frame=1,
        end_frame=2,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/waydown-render-abcd1234",
                             frames=[1, 2], state="running")])])

    strays, _ = fleet.find_stray_sessions()

    assert strays == [], "the render on the dashboard is not a stray"


def test_a_finished_session_is_not_reported(tmp_path):
    """A finished stray costs nothing, and listing it would turn a quota
    warning into an inventory of every notebook the app ever made."""
    client = _Client(refs=["user_0/waydown-render-abcd1234"],
                     states={"user_0/waydown-render-abcd1234": "complete"})

    strays, _ = _fleet(tmp_path, {"acct0": client}).find_stray_sessions()

    assert strays == []


def test_a_notebook_the_user_wrote_is_never_even_asked_about(tmp_path):
    """Not merely filtered out of the results -- not made the subject of a
    status call either, since that is a request against their account for
    no reason."""
    client = _Client(refs=["user_0/my-thesis-notebook"])

    strays, _ = _fleet(tmp_path, {"acct0": client}).find_stray_sessions()

    assert strays == []
    assert client.status_calls == []


def test_a_slug_inside_an_unreadable_job_still_counts_as_tracked(tmp_path):
    """Those raw entries are preserved precisely because they name real
    sessions. Treating them as untracked would offer to cancel a render
    that is deliberately running."""
    client = _Client(refs=["user_0/waydown-render-abcd1234"])
    fleet = _fleet(tmp_path, {"acct0": client})
    # A real unparseable entry, written to the real file: `unreadable_jobs`
    # is POPULATED by load_jobs, so setting the attribute by hand is
    # overwritten the moment anything reads the file. The extra key is what
    # makes FleetState(**parsed) raise, exactly as a file from a newer
    # version would.
    from blendfleet.fleet import STATE_FILE
    from blendfleet.platform_paths import state_dir

    path = state_dir() / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": [{
        "job_id": "abcd1234",
        "blend_name": "waydown.blend",
        "start_frame": 1,
        "end_frame": 2,
        "from_a_later_version": True,
        "workers": [{"label": "acct0", "username": "user_0",
                     "kernel_slug": "user_0/waydown-render-abcd1234",
                     "frames": [1, 2], "state": "running"}],
    }]}), encoding="utf-8")

    strays, _ = fleet.find_stray_sessions()

    assert fleet.unreadable_jobs, "the entry was supposed to be unparseable"
    assert strays == [], (
        "a session named inside an unreadable job is still tracked -- those "
        "entries are preserved precisely because they name real kernels")


def test_one_unreachable_account_does_not_hide_another_burning_quota(tmp_path):
    """The rule refreshQuota and cancel_all already keep."""
    broken = _Client(raises=RuntimeError("token revoked"))
    working = _Client(refs=["user_1/waydown-render-abcd1234"])
    fleet = _fleet(tmp_path, {"acct0": broken, "acct1": working},
                   labels=("acct0", "acct1"))

    strays, errors = fleet.find_stray_sessions()

    assert [s.label for s in strays] == ["acct1"]
    assert "acct0" in errors and "revoked" in errors["acct0"]


def test_the_number_of_status_calls_is_capped(tmp_path):
    """The listing is cheap; confirming each candidate is not."""
    refs = [f"user_0/waydown-render-{i:08x}" for i in range(40)]
    client = _Client(refs=refs)

    _fleet(tmp_path, {"acct0": client}).find_stray_sessions(
        limit_per_account=5)

    assert len(client.status_calls) == 5


# ---- cancelling one --------------------------------------------------

def test_cancelling_a_stray_stops_it(tmp_path):
    client = _Client(refs=[])
    result = _fleet(tmp_path, {"acct0": client}).cancel_stray_session(
        "acct0", "user_0/waydown-render-abcd1234")

    assert result.ok is True
    assert client.cancelled == ["user_0/waydown-render-abcd1234"]


def test_a_slug_that_is_not_ours_is_refused_rather_than_cancelled(tmp_path):
    """The page passes back what it was shown, and "cancel this notebook"
    is not an instruction to take on trust."""
    client = _Client(refs=[])
    result = _fleet(tmp_path, {"acct0": client}).cancel_stray_session(
        "acct0", "user_0/my-thesis-notebook")

    assert result.ok is False
    assert "not a notebook BlendFleet created" in result.error
    assert client.cancelled == []


def test_an_unknown_account_is_refused(tmp_path):
    result = _fleet(tmp_path, {"acct0": _Client()}).cancel_stray_session(
        "nobody", "user_0/waydown-render-abcd1234")

    assert result.ok is False
    assert "no account called nobody" in result.error


# ---- what the page is told -------------------------------------------

def _session(tmp_path, client):
    store = AccountStore([Account(label="acct0", token="KGAT_" + "0" * 32,
                                  username="user_0", verified=True)])
    root = tmp_path / "session"
    root.mkdir()
    return Session(store, lambda accounts: Fleet(
        accounts, lambda t: client, root / "w"),
        lambda t: "someone", Settings())


def test_the_page_is_told_what_was_found_and_what_could_not_be_checked(
        tmp_path):
    client = _Client(refs=["user_0/waydown-render-abcd1234"])
    session = _session(tmp_path, client)
    seen = []
    notes = []
    try:
        session.straySessionsChanged.connect(lambda j: seen.append(json.loads(j)))
        session.notification.connect(lambda m, tone: notes.append((m, tone)))
        session.findStraySessions()
        session._workers["strays"].wait(10_000)

        assert seen, "the page was never told"
        assert seen[0]["strays"] == [
            {"label": "acct0", "slug": "user_0/waydown-render-abcd1234",
             "state": "running"}]
        assert seen[0]["errors"] == {}
        assert any("spending quota" in m for m, _ in notes)
    finally:
        session.stop()


def test_finding_nothing_says_so_out_loud(tmp_path):
    """A scan that finds nothing and says nothing is indistinguishable
    from a button that does nothing."""
    session = _session(tmp_path, _Client(refs=[]))
    notes = []
    try:
        session.notification.connect(lambda m, tone: notes.append((m, tone)))
        session.findStraySessions()
        session._workers["strays"].wait(10_000)

        assert any("No stray sessions" in m for m, _ in notes)
    finally:
        session.stop()
