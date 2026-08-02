from pathlib import Path
import pytest
import blendfleet.platform_paths as pp
from blendfleet.accounts import Account
from blendfleet.kaggle_client import KernelStatus, Quota
from blendfleet.notebook_builder import RenderSettings
from blendfleet.fleet import Fleet


class FakeClient:
    def __init__(self, token, state="running"):
        self.token = token
        self.state = state
        self.pushed = 0
        self.cancelled = []

    def whoami(self): return "user_" + self.token[-1]
    def _exec(self, argv): return "ok"
    def dataset_exists(self, slug): return False
    def dataset_create(self, folder): pass
    def dataset_version(self, folder, message): pass
    def push_kernel(self, folder): self.pushed += 1
    def status(self, slug): return KernelStatus(state=self.state)
    def cancel(self, slug): self.cancelled.append(slug); return True
    def quota(self): return Quota(0, 21600, "2026-08-01", source="api")
    def fetch_output(self, slug, dest): return []


@pytest.fixture(autouse=True)
def tmp_cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture
def blend(tmp_path):
    p = tmp_path / "remember.blend"
    p.write_bytes(b"X" * 100)
    return p


def accounts(n):
    return [Account(label=f"a{i}", token="KGAT_" + str(i) * 32) for i in range(n)]


def test_launch_pushes_one_kernel_per_account(blend, tmp_path):
    clients = {}
    def factory(tok):
        clients[tok] = FakeClient(tok); return clients[tok]
    f = Fleet(accounts(3), factory, tmp_path / "w")
    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)
    assert len(st.workers) == 3
    assert all(c.pushed == 1 for c in clients.values())


def test_frames_are_disjoint_and_complete(blend, tmp_path):
    f = Fleet(accounts(3), lambda t: FakeClient(t), tmp_path / "w")
    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 10)
    allf = [fr for w in st.workers for fr in w.frames]
    assert sorted(allf) == list(range(1, 11))
    assert len(allf) == len(set(allf))


def test_poll_aggregates_state(blend, tmp_path):
    f = Fleet(accounts(2), lambda t: FakeClient(t, "complete"), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    st = f.poll()
    assert all(w.state == "complete" for w in st.workers)


def test_cancel_all_hits_every_worker(blend, tmp_path):
    clients = {}
    def factory(tok):
        clients[tok] = FakeClient(tok); return clients[tok]
    f = Fleet(accounts(3), factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 6)
    f.cancel_all()
    assert all(len(c.cancelled) == 1 for c in clients.values())


def test_state_persists(blend, tmp_path):
    f = Fleet(accounts(2), lambda t: FakeClient(t), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 8)
    f2 = Fleet(accounts(2), lambda t: FakeClient(t), tmp_path / "w")
    assert len(f2.load().workers) == 2


def test_launch_refuses_with_no_accounts(blend, tmp_path):
    f = Fleet([], lambda t: FakeClient(t), tmp_path / "w")
    with pytest.raises(ValueError, match="account"):
        f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
