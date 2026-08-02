from pathlib import Path
import pytest
import blendfleet.platform_paths as pp
from blendfleet.accounts import Account
from blendfleet.kaggle_client import KernelStatus, Quota
from blendfleet.notebook_builder import RenderSettings
from blendfleet.fleet import Fleet, FleetBusyError, UnreachableAccountsError, WorkerState


class FakeDatasetApiClient:
    """Stands in for sdk.datasets.dataset_api_client -- Task 3 sharing.
    Constant, network-free responses: correctness of the sharing calls
    THEMSELVES (traps 1 and 2, preserving collaborators) is covered by
    tests/test_sharing.py; these fleet-level tests only care that
    Fleet.launch calls out to it at the right time with the right accounts.
    """

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


class FakeSdk:
    def __init__(self):
        self.datasets = type("D", (), {
            "dataset_api_client": FakeDatasetApiClient()})()


class FakeClient:
    def __init__(self, token, state="running", dataset_exists=True,
                 dataset_reachable=True):
        self.token = token
        self.state = state
        self.pushed = 0
        self.cancelled = []
        self.dataset_creates = 0
        self.dataset_versions = 0
        self._dataset_exists = dataset_exists
        self._dataset_reachable = dataset_reachable
        self.sdk = FakeSdk()
        self._sdk_factory = lambda tok: self.sdk

    def whoami(self): return "user_" + self.token[-1]
    def dataset_exists(self, slug): return self._dataset_exists
    def dataset_reachable(self, slug): return self._dataset_reachable
    def dataset_create(self, folder, on_progress=None): self.dataset_creates += 1
    def dataset_version(self, folder, message, on_progress=None): self.dataset_versions += 1
    def push_kernel(self, folder): self.pushed += 1
    def status(self, slug): return KernelStatus(state=self.state)
    def cancel(self, slug): self.cancelled.append(slug); return True
    def quota(self): return Quota(0, 21600, "2026-08-01", source="api")
    def fetch_output(self, slug, dest): return []


class RaisingCancelClient(FakeClient):
    """Cancel always raises, as if the network call or auth blew up."""
    def cancel(self, slug):
        raise RuntimeError("boom")


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


def test_launch_uploads_dataset_exactly_once_for_n_accounts(blend, tmp_path):
    """Task 3: dataset sharing is automatable, so N accounts must mean
    exactly ONE upload total -- never one copy per account."""
    clients = {}
    def factory(tok):
        clients[tok] = FakeClient(tok); return clients[tok]
    f = Fleet(accounts(3), factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)
    assert len(clients) == 3
    total_uploads = sum(c.dataset_creates + c.dataset_versions
                        for c in clients.values())
    assert total_uploads == 1


def test_launch_uses_the_owner_slug_for_every_worker(blend, tmp_path):
    """All kernels must reference the SAME (owner's) dataset -- that is the
    entire point of sharing instead of uploading N copies."""
    import json as _json

    accts = accounts(3)
    f = Fleet(accts, lambda t: FakeClient(t), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)

    owner_username = "user_" + accts[0].token[-1]
    for account in accts:
        meta = _json.loads(
            (tmp_path / "w" / f"kern_{account.label}" /
             "kernel-metadata.json").read_text())
        assert meta["dataset_sources"] == [f"{owner_username}/remember-blend"]


def test_launch_grants_every_friend_username_reader_in_one_call(blend, tmp_path):
    accts = accounts(3)
    clients = {}
    def factory(tok):
        clients[tok] = FakeClient(tok); return clients[tok]
    f = Fleet(accts, factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)

    owner_client = clients[accts[0].token]
    updated = owner_client.sdk.datasets.dataset_api_client.updated
    assert len(updated) == 1, "exactly one grant call, not one per friend"
    granted = {c.username for c in updated[0].settings.collaborators}
    assert granted == {"user_" + accts[1].token[-1], "user_" + accts[2].token[-1]}
    assert updated[0].settings.is_private is True
    assert len(updated[0].settings.licenses) == 1


def test_launch_skips_sharing_calls_for_a_single_account(blend, tmp_path):
    """No friends -- grant/verify must never fire, and there's nothing to
    check reachability for."""
    clients = {}
    def factory(tok):
        clients[tok] = FakeClient(tok); return clients[tok]
    f = Fleet(accounts(1), factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)
    only = next(iter(clients.values()))
    assert only.sdk.datasets.dataset_api_client.updated == []


def test_launch_refuses_when_a_friend_is_still_unreachable_after_grant(blend, tmp_path):
    """A grant that returns cleanly but doesn't actually take (propagation
    delay, silently dropped role, ...) must be caught before anything is
    started -- not surfaced later as an opaque kernel failure."""
    accts = accounts(3)
    unreachable_token = accts[2].token
    clients = {}

    def factory(tok):
        reachable = tok != unreachable_token
        clients[tok] = FakeClient(tok, dataset_reachable=reachable)
        return clients[tok]

    f = Fleet(accts, factory, tmp_path / "w")
    with pytest.raises(UnreachableAccountsError) as exc_info:
        f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)

    message = str(exc_info.value)
    assert "user_" + unreachable_token[-1] in message
    # nothing must have been started
    assert all(c.pushed == 0 for c in clients.values())
    assert f.load() is None


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


def test_cancel_all_survives_one_account_raising(blend, tmp_path):
    """A failing account (client_factory or .cancel() raising) must not
    strand the others -- an uncancelled kernel keeps burning GPU quota."""
    clients = {}
    accts = accounts(3)
    bad_token = accts[1].token

    def factory(tok):
        cls = RaisingCancelClient if tok == bad_token else FakeClient
        clients[tok] = cls(tok)
        return clients[tok]

    f = Fleet(accts, factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 6)

    f.cancel_all()  # must not raise

    for tok, client in clients.items():
        if tok == bad_token:
            continue
        assert len(client.cancelled) == 1


def test_state_persists(blend, tmp_path):
    f = Fleet(accounts(2), lambda t: FakeClient(t), tmp_path / "w")
    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 8)

    f2 = Fleet(accounts(2), lambda t: FakeClient(t), tmp_path / "w")
    loaded = f2.load()

    assert len(loaded.workers) == 2
    for original, restored in zip(st.workers, loaded.workers):
        assert isinstance(restored, WorkerState)
        assert restored.label == original.label
        assert restored.username == original.username
        assert restored.kernel_slug == original.kernel_slug
        assert restored.frames == original.frames
        assert restored.state == original.state


def test_poll_skips_removed_account_without_crashing(blend, tmp_path):
    accts = accounts(3)
    f = Fleet(accts, lambda t: FakeClient(t, "complete"), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 6)

    # Simulate the account for worker "a1" having been removed from the
    # fleet's account list between app sessions.
    remaining = [a for a in accts if a.label != "a1"]
    f2 = Fleet(remaining, lambda t: FakeClient(t, "complete"), tmp_path / "w")

    st = f2.poll()  # must not raise

    assert st is not None
    assert len(st.workers) == 3
    removed = next(w for w in st.workers if w.label == "a1")
    assert removed.state == "queued"  # untouched: no account to poll it with
    for w in st.workers:
        if w.label != "a1":
            assert w.state == "complete"


def test_launch_refuses_with_no_accounts(blend, tmp_path):
    f = Fleet([], lambda t: FakeClient(t), tmp_path / "w")
    with pytest.raises(ValueError, match="account"):
        f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)


# ---------------------------------------------------------------------------
# CRITICAL C1: a partial launch must never orphan already-started kernels.
# push_kernel ALWAYS starts a run, so every kernel pushed before the failure
# is already spending somebody else's GPU quota.
# ---------------------------------------------------------------------------

class ExplodingPushClient(FakeClient):
    """Stands in for an account whose token was revoked mid-launch."""
    def push_kernel(self, folder):
        raise RuntimeError("401 Unauthorized: token revoked")


def _factory_failing_on(bad_token, clients, state="running"):
    def factory(tok):
        cls = ExplodingPushClient if tok == bad_token else FakeClient
        clients[tok] = cls(tok, state)
        return clients[tok]
    return factory


def test_partial_launch_still_records_every_started_kernel(blend, tmp_path):
    """Account 3's token is revoked. Accounts 1 and 2 already have GPU
    kernels RUNNING -- they must be on disk, cancellable and collectable."""
    accts = accounts(3)
    clients = {}
    f = Fleet(accts, _factory_failing_on(accts[2].token, clients), tmp_path / "w")

    with pytest.raises(RuntimeError, match="revoked"):
        f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)

    st = f.load()
    assert st is not None, "state file never written: kernels are orphaned"
    assert [w.label for w in st.workers] == ["a0", "a1"]
    assert clients[accts[0].token].pushed == 1
    assert clients[accts[1].token].pushed == 1


def test_partial_launch_leaves_the_started_kernels_cancellable(blend, tmp_path):
    accts = accounts(3)
    clients = {}
    f = Fleet(accts, _factory_failing_on(accts[2].token, clients), tmp_path / "w")
    with pytest.raises(RuntimeError):
        f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)

    # The whole point of persisting incrementally: cancel_all() must reach
    # the two friends' accounts that are already burning quota.
    results = f.cancel_all()
    assert [r.label for r in results] == ["a0", "a1"]
    assert all(r.ok for r in results)
    assert len(clients[accts[0].token].cancelled) == 1
    assert len(clients[accts[1].token].cancelled) == 1


def test_failed_first_push_leaves_previous_job_state_intact(blend, tmp_path):
    """Nothing was started, so the previous (collectable) job must survive."""
    accts = accounts(2)
    f = Fleet(accts, lambda t: FakeClient(t, "complete"), tmp_path / "w")
    first = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)

    clients = {}
    f2 = Fleet(accts, _factory_failing_on(accts[0].token, clients, "complete"),
               tmp_path / "w")
    with pytest.raises(RuntimeError):
        f2.launch(blend, RenderSettings(1920, 1080, 128), 5, 8)

    st = f2.load()
    assert st.job_id == first.job_id
    assert len(st.workers) == 2


def test_launch_refuses_while_a_job_is_still_running(blend, tmp_path):
    """A second launch would overwrite the single-slot state file and orphan
    the first job's kernels."""
    f = Fleet(accounts(2), lambda t: FakeClient(t, "running"), tmp_path / "w")
    first = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)

    with pytest.raises(FleetBusyError, match="still running"):
        f.launch(blend, RenderSettings(1920, 1080, 128), 5, 8)

    # First job untouched: still cancellable and collectable.
    assert f.load().job_id == first.job_id


def test_launch_refusal_names_the_busy_accounts(blend, tmp_path):
    f = Fleet(accounts(2), lambda t: FakeClient(t, "queued"), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    with pytest.raises(FleetBusyError) as e:
        f.launch(blend, RenderSettings(1920, 1080, 128), 5, 8)
    assert "a0" in str(e.value) and "a1" in str(e.value)


def test_launch_allowed_once_the_previous_job_finished(blend, tmp_path):
    f = Fleet(accounts(2), lambda t: FakeClient(t, "complete"), tmp_path / "w")
    first = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    second = f.launch(blend, RenderSettings(1920, 1080, 128), 5, 8)
    assert second.job_id != first.job_id
    assert f.load().job_id == second.job_id


def test_unreachable_account_does_not_wedge_launch(blend, tmp_path):
    """A revoked token means status() raises. That must not block every
    future render forever -- there would be no way out from the UI."""
    class StatusRaises(FakeClient):
        def status(self, slug):
            raise RuntimeError("401 Unauthorized")

    accts = accounts(1)
    f = Fleet(accts, lambda t: StatusRaises(t), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    f.launch(blend, RenderSettings(1920, 1080, 128), 5, 8)  # must not raise


# ---------------------------------------------------------------------------
# IMPORTANT 2: a failed cancel must not look identical to a successful one.
# ---------------------------------------------------------------------------

class RefusingCancelClient(FakeClient):
    """cancel() returns False, exactly as the real client does on any error."""
    def cancel(self, slug):
        return False


def test_cancel_all_reports_success_per_account(blend, tmp_path):
    f = Fleet(accounts(2), lambda t: FakeClient(t), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    results = f.cancel_all()
    assert [(r.label, r.ok) for r in results] == [("a0", True), ("a1", True)]


def test_cancel_all_reports_the_account_that_raised(blend, tmp_path):
    accts = accounts(3)
    bad = accts[1].token

    def factory(tok):
        return (RaisingCancelClient if tok == bad else FakeClient)(tok)

    f = Fleet(accts, factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 6)

    results = f.cancel_all()
    failed = [r for r in results if not r.ok]
    assert [r.label for r in failed] == ["a1"]
    assert "boom" in failed[0].error
    assert failed[0].kernel_slug
    assert all(r.ok for r in results if r.label != "a1")


def test_cancel_all_reports_a_refused_cancel_as_failure(blend, tmp_path):
    """cancel() returning False means the kernel is probably still running."""
    f = Fleet(accounts(2), lambda t: RefusingCancelClient(t), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    results = f.cancel_all()
    assert all(not r.ok for r in results)
    assert all(r.error for r in results)


def test_cancel_all_reports_worker_with_no_account_as_failure(blend, tmp_path):
    """No token means no way to stop it -- the user has to be told."""
    accts = accounts(2)
    f = Fleet(accts, lambda t: FakeClient(t), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)

    f2 = Fleet([accts[0]], lambda t: FakeClient(t), tmp_path / "w")
    results = f2.cancel_all()
    assert [(r.label, r.ok) for r in results] == [("a0", True), ("a1", False)]
    assert "no account" in results[1].error


def test_cancel_all_with_no_job_returns_empty(tmp_path):
    f = Fleet(accounts(1), lambda t: FakeClient(t), tmp_path / "w")
    assert f.cancel_all() == []
