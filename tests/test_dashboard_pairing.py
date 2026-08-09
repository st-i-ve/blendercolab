"""Workers are paired to accounts by LABEL, never by list position.

Dashboard._start_progress_threads used to do
`zip(self.store.list(), st.workers)`. Those two lists only line up while
nothing changes between launching and the launch returning -- and the launch
runs on a background thread for as long as a 60 MB upload plus N kernel
pushes takes, which is exactly the window in which somebody removes an
account. One removal shifts every later pairing, so each worker's SSE log
stream is opened with the WRONG person's token: a privacy leak on a private
notebook's logs, and a stream that 403s anyway.

Label is the join key everywhere else in this app (fleet.poll,
fleet.cancel_all, collector.collect). These tests pin it here too.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import blendfleet.platform_paths as pp
import blendfleet.ui.dashboard as dashboard_mod
from blendfleet.accounts import Account, AccountStore
from blendfleet.fleet import Fleet, FleetState, WorkerState
from blendfleet.kaggle_client import KernelStatus, Quota
from blendfleet.ui.dashboard import Dashboard


@pytest.fixture(autouse=True)
def tmp_cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeClient:
    def __init__(self, token):
        self.token = token

    def quota(self):
        return Quota(0, 21600, "2026-08-01", source="api")

    def status(self, slug):
        return KernelStatus(state="running")


def token_for(i: int) -> str:
    return "KGAT_" + str(i) * 32


@pytest.fixture
def dash(qapp, tmp_path):
    store = AccountStore()
    for i in range(3):
        store.add(Account(label=f"acct{i}", token=token_for(i),
                          username=f"user_{i}", verified=True))

    def fleet_factory(accounts):
        return Fleet(accounts, lambda tok: FakeClient(tok), tmp_path / "w")

    dashboard = Dashboard(store, fleet_factory, verifier=lambda t: "someone")
    yield dashboard
    dashboard.close()          # joins the stream threads it started
    dashboard.deleteLater()


def worker(label: str, index: int) -> WorkerState:
    return WorkerState(label=label, username=f"user_{index}",
                       kernel_slug=f"user_{index}/k{index}", frames=[index])


def state(workers) -> FleetState:
    return FleetState(job_id="job", blend_name="x.blend", start_frame=1,
                      end_frame=3, workers=list(workers))


def test_streams_use_the_token_of_the_worker_s_own_account(dash, monkeypatch):
    """Workers deliberately in a different order from store.list(): a
    position-based pairing would hand acct2's kernel acct0's token."""
    seen = []

    def fake_stream(token, user_name, kernel_slug, on_progress,
                    stop_event=None, on_telemetry=None, on_hardware=None):
        seen.append((token, kernel_slug))

    monkeypatch.setattr(dashboard_mod, "stream_progress", fake_stream)

    dash._start_progress_threads(state([worker("acct2", 2), worker("acct0", 0)]))
    for thread in dash._stream_threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert sorted(seen) == sorted([(token_for(2), "k2"), (token_for(0), "k0")])


def test_a_worker_whose_account_was_removed_is_skipped_not_mispaired(dash, monkeypatch):
    """The exact scenario: an account is removed between launch and success.
    The remaining workers must still get their OWN tokens, and the orphaned
    worker must get no stream at all rather than somebody else's token."""
    seen = []

    def fake_stream(token, user_name, kernel_slug, on_progress,
                    stop_event=None, on_telemetry=None, on_hardware=None):
        seen.append((token, kernel_slug))

    monkeypatch.setattr(dashboard_mod, "stream_progress", fake_stream)

    dash.store.remove("acct1")
    dash._start_progress_threads(
        state([worker("acct0", 0), worker("acct1", 1), worker("acct2", 2)]))
    for thread in dash._stream_threads:
        thread.join(timeout=5)

    assert sorted(seen) == sorted([(token_for(0), "k0"), (token_for(2), "k2")])
    assert token_for(1) not in [t for t, _ in seen]
    assert "k1" not in [s for _, s in seen], \
        "the orphaned worker must not be streamed with another account's token"


def test_stream_threads_are_kept_so_close_can_join_them(dash, monkeypatch):
    """closeEvent can only wait for threads it knows about -- 'daemon=True'
    is not a substitute for joining."""
    started = []

    def fake_stream(token, user_name, kernel_slug, on_progress,
                    stop_event=None, on_telemetry=None, on_hardware=None):
        started.append(kernel_slug)

    monkeypatch.setattr(dashboard_mod, "stream_progress", fake_stream)

    dash._start_progress_threads(state([worker("acct0", 0), worker("acct1", 1)]))
    assert len(dash._stream_threads) == 2

    dash.close()
    assert all(not t.is_alive() for t in dash._stream_threads), \
        "closeEvent must not return while an SSE thread is still running"
