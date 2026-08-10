import re
from pathlib import Path
import pytest
import blendfleet.platform_paths as pp
from blendfleet.accounts import Account
from blendfleet.kaggle_client import KernelStatus, Quota
from blendfleet.notebook_builder import RenderSettings
from blendfleet.fleet import (Fleet, FleetBusyError, StaleDatasetError,
                              UnreachableAccountsError, WorkerState)


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


# Every `blend`-shaped fixture in this file writes exactly this many bytes
# (see the `blend` fixture below and the two ad hoc blends further down) --
# FakeClient's default dataset_file_size mirrors it so the many tests that
# don't care about Task 5's size-verification match with zero extra setup.
BLEND_SIZE = 100


class FakeClient:
    def __init__(self, token, state="running", dataset_exists=True,
                 dataset_reachable=True, remote_file_sizes=None):
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
        # filename -> size (or None for "missing"), as THIS account's
        # dataset_list_files would report it. Any filename not overridden
        # here defaults to BLEND_SIZE, i.e. "matches" -- a test simulating
        # a stale/missing remote copy for one account passes an explicit
        # override for that account's client only.
        self._remote_file_sizes = dict(remote_file_sizes or {})

    def whoami(self): return "user_" + self.token[-1]
    def dataset_exists(self, slug): return self._dataset_exists
    def dataset_reachable(self, slug): return self._dataset_reachable
    def dataset_file_size(self, slug, filename):
        if filename in self._remote_file_sizes:
            return self._remote_file_sizes[filename]
        return BLEND_SIZE
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


# ---------------------------------------------------------------------------
# Task 5: dataset_reachable() only proves an account can see A copy -- not
# that it's the RIGHT one. StaleDatasetError closes that gap. Covers both
# shared mode (a friend's view of the owner's dataset) and the per-account
# case (a lone account's own just-uploaded copy).
# ---------------------------------------------------------------------------

def test_launch_refuses_when_a_friends_dataset_size_differs_from_local(blend, tmp_path):
    """Shared mode: a friend's visible copy of the SAME dataset is a
    different size than the local .blend -- a stale copy from an earlier
    upload. Must refuse, name that account, and push ZERO kernels (not
    merely raise)."""
    accts = accounts(3)
    stale_token = accts[2].token
    clients = {}

    def factory(tok):
        sizes = {blend.name: 999} if tok == stale_token else None
        clients[tok] = FakeClient(tok, remote_file_sizes=sizes)
        return clients[tok]

    f = Fleet(accts, factory, tmp_path / "w")
    with pytest.raises(StaleDatasetError) as exc_info:
        f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)

    message = str(exc_info.value)
    assert "user_" + stale_token[-1] in message
    assert "999" in message and str(BLEND_SIZE) in message
    assert all(c.pushed == 0 for c in clients.values())
    assert f.load() is None


def test_launch_succeeds_when_every_accounts_dataset_size_matches(blend, tmp_path):
    accts = accounts(3)
    clients = {}

    def factory(tok):
        clients[tok] = FakeClient(tok, remote_file_sizes={blend.name: BLEND_SIZE})
        return clients[tok]

    f = Fleet(accts, factory, tmp_path / "w")
    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)
    assert len(st.workers) == 3
    assert all(c.pushed == 1 for c in clients.values())


def test_launch_refuses_with_different_advice_when_a_friends_copy_is_missing(blend, tmp_path):
    """A friend who can reach the dataset but whose listing has no file by
    this name at all is a DIFFERENT failure than a size mismatch, and needs
    different advice: re-share/retry, not re-upload."""
    accts = accounts(2)
    missing_token = accts[1].token
    clients = {}

    def factory(tok):
        sizes = {blend.name: None} if tok == missing_token else None
        clients[tok] = FakeClient(tok, remote_file_sizes=sizes)
        return clients[tok]

    f = Fleet(accts, factory, tmp_path / "w")
    with pytest.raises(StaleDatasetError) as exc_info:
        f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)

    message = str(exc_info.value)
    assert "user_" + missing_token[-1] in message
    assert "re-share" in message.lower()
    assert "re-upload" not in message.lower(), \
        "missing-file advice must not be worded like a size mismatch"
    assert all(c.pushed == 0 for c in clients.values())
    assert f.load() is None


def test_missing_and_mismatch_advice_differ(blend, tmp_path):
    """Directly pin that the two failure messages actually differ in their
    advice, not just in which account they name."""
    accts = accounts(2)

    def mismatch_factory(tok):
        sizes = {blend.name: 1} if tok == accts[1].token else None
        return FakeClient(tok, remote_file_sizes=sizes)

    def missing_factory(tok):
        sizes = {blend.name: None} if tok == accts[1].token else None
        return FakeClient(tok, remote_file_sizes=sizes)

    with pytest.raises(StaleDatasetError) as mismatch_exc:
        Fleet(accts, mismatch_factory, tmp_path / "w1").launch(
            blend, RenderSettings(1920, 1080, 128), 1, 9)
    with pytest.raises(StaleDatasetError) as missing_exc:
        Fleet(accts, missing_factory, tmp_path / "w2").launch(
            blend, RenderSettings(1920, 1080, 128), 1, 9)

    assert str(mismatch_exc.value) != str(missing_exc.value)
    assert "re-upload" in str(mismatch_exc.value).lower()
    assert "re-upload" not in str(missing_exc.value).lower()


def test_launch_refuses_when_the_owners_own_upload_lands_with_the_wrong_size(blend, tmp_path):
    """Per-account case: a single account, nobody to share with. Even here,
    the owner's own just-uploaded copy must be confirmed the right size
    before any kernel is pushed."""
    clients = {}

    def factory(tok):
        clients[tok] = FakeClient(tok, remote_file_sizes={blend.name: 1})
        return clients[tok]

    f = Fleet(accounts(1), factory, tmp_path / "w")
    with pytest.raises(StaleDatasetError) as exc_info:
        f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)

    message = str(exc_info.value)
    assert "user_" + accounts(1)[0].token[-1] in message
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


# ---------------------------------------------------------------------------
# Task 3: cancel_worker(label) -- the per-instance counterpart to
# cancel_all(). Must touch exactly one account's client and leave every
# other account's cancel() uncalled, not merely "not raise".
# ---------------------------------------------------------------------------

def test_cancel_worker_cancels_only_that_worker_and_leaves_others_running(
        blend, tmp_path):
    clients = {}
    def factory(tok):
        clients[tok] = FakeClient(tok); return clients[tok]
    accts = accounts(3)
    f = Fleet(accts, factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)
    f.poll()  # FakeClient.status() defaults to state="running"

    result = f.cancel_worker("a1")

    assert result is not None
    assert result.ok is True
    assert result.label == "a1"
    assert clients[accts[1].token].cancelled == [result.kernel_slug]
    # the whole point: the other two accounts' cancel() must never be
    # called at all, not merely "no exception was raised".
    assert clients[accts[0].token].cancelled == []
    assert clients[accts[2].token].cancelled == []


def test_cancel_worker_on_an_already_finished_worker_is_a_no_op(blend, tmp_path):
    clients = {}
    def factory(tok):
        clients[tok] = FakeClient(tok, "complete"); return clients[tok]
    f = Fleet(accounts(2), factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    f.poll()

    result = f.cancel_worker("a0")

    assert result is None, "an already-finished worker must be a no-op, not an error"
    assert all(c.cancelled == [] for c in clients.values())


def test_cancel_worker_on_a_still_queued_worker_actually_cancels_it(blend, tmp_path):
    """queued is active (kaggle_client.ACTIVE_STATES), not finished -- a
    kernel that has not started rendering yet still deserves a real
    cancel, not a no-op."""
    clients = {}
    def factory(tok):
        clients[tok] = FakeClient(tok, "queued"); return clients[tok]
    f = Fleet(accounts(1), factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    f.poll()

    result = f.cancel_worker("a0")

    assert result is not None
    assert result.ok is True
    assert list(clients.values())[0].cancelled == [result.kernel_slug]


def test_cancel_worker_with_no_job_is_a_no_op(tmp_path):
    f = Fleet(accounts(1), lambda t: FakeClient(t), tmp_path / "w")
    assert f.cancel_worker("a0") is None


def test_cancel_worker_with_an_unknown_label_is_a_no_op(blend, tmp_path):
    f = Fleet(accounts(2), lambda t: FakeClient(t), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    assert f.cancel_worker("does-not-exist") is None


def test_cancel_worker_reports_a_raised_cancel_as_failure(blend, tmp_path):
    accts = accounts(2)
    bad = accts[0].token

    def factory(tok):
        return (RaisingCancelClient if tok == bad else FakeClient)(tok)

    f = Fleet(accts, factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    f.poll()

    result = f.cancel_worker("a0")

    assert result is not None
    assert result.ok is False
    assert "boom" in result.error


def test_cancel_worker_reports_a_refused_cancel_as_failure(blend, tmp_path):
    f = Fleet(accounts(1), lambda t: RefusingCancelClient(t), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    f.poll()

    result = f.cancel_worker("a0")

    assert result.ok is False
    assert result.error


def test_cancel_worker_reports_a_removed_account_as_failure(blend, tmp_path):
    accts = accounts(2)
    f = Fleet(accts, lambda t: FakeClient(t), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    f.poll()

    f2 = Fleet([accts[0]], lambda t: FakeClient(t), tmp_path / "w")
    result = f2.cancel_worker("a1")

    assert result is not None
    assert result.ok is False
    assert "no account" in result.error


# ---------------------------------------------------------------------------
# Task 4: fetch_failure_log(label) -- the tail of the kernel log, fetched
# only for a worker Kaggle has actually reported as "error". Never a
# network call for a healthy worker.
# ---------------------------------------------------------------------------

class LogFetchingClient(FakeClient):
    def __init__(self, *a, log_text="RuntimeError: CUDA out of memory", **kw):
        super().__init__(*a, **kw)
        self._log_text = log_text
        self.log_fetches = []

    def fetch_log_tail(self, slug, dest, max_lines=200):
        self.log_fetches.append(slug)
        return self._log_text


def test_fetch_failure_log_returns_the_tail_for_a_failed_worker(blend, tmp_path):
    f = Fleet(accounts(1), lambda t: LogFetchingClient(t, "error"), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    f.poll()

    text = f.fetch_failure_log("a0")

    assert text == "RuntimeError: CUDA out of memory"


def test_fetch_failure_log_never_calls_out_for_a_healthy_worker(blend, tmp_path):
    clients = {}
    def factory(tok):
        clients[tok] = LogFetchingClient(tok, "running"); return clients[tok]
    f = Fleet(accounts(1), factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    f.poll()

    text = f.fetch_failure_log("a0")

    assert text == ""
    assert all(c.log_fetches == [] for c in clients.values())


def test_fetch_failure_log_with_no_job_returns_empty(tmp_path):
    f = Fleet(accounts(1), lambda t: LogFetchingClient(t), tmp_path / "w")
    assert f.fetch_failure_log("a0") == ""


def test_fetch_failure_log_with_unknown_label_returns_empty(blend, tmp_path):
    f = Fleet(accounts(1), lambda t: LogFetchingClient(t, "error"), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    f.poll()
    assert f.fetch_failure_log("does-not-exist") == ""


# ------------------------------------------------- dataset slug scrubbing --
# `stem = blend.stem.lower().replace("_", "-")` was the ONLY transform, so a
# .blend with spaces in its name -- extremely common -- produced
# "user/big buck bunny-blend", which is not a valid Kaggle slug. And because
# dataset_create uploads first and validates second, the user waited out a
# full 60 MB upload before being told the name was wrong. The same stem also
# feeds kernel_slug, so both were broken by the same character.

import json as _json_slug

from blendfleet.fleet import InvalidBlendNameError, slug_stem, slugify_stem


@pytest.mark.parametrize("name, expected", [
    ("big buck bunny", "big-buck-bunny"),          # spaces: the common case
    ("Remember_The_Titans", "remember-the-titans"),  # underscores + case
    ("scene(final)[v2]!", "scene-final-v2"),       # punctuation
    ("my....scene", "my-scene"),                   # runs collapse to one dash
    ("--leading-and-trailing--", "leading-and-trailing"),
    ("Ünïcödé Scéne", "unicode-scene"),            # accents fold to ASCII
    ("shot 42", "shot-42"),                        # digits survive
])
def test_slugify_produces_a_valid_kaggle_slug(name, expected):
    got = slugify_stem(name)
    assert got == expected
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", got), \
        "slug must be lowercase alphanumerics separated by single dashes"


@pytest.mark.parametrize("name", ["日本語", "!!!", "   ", "___", "--", ""])
def test_slugify_can_legitimately_scrub_a_name_to_nothing(name):
    assert slugify_stem(name) == ""


@pytest.mark.parametrize("name", ["日本語.blend", "!!!.blend", "  .blend",
                                  "a.blend", "ab.blend"])
def test_slug_stem_refuses_a_name_that_scrubs_to_nothing_or_too_little(name):
    with pytest.raises(InvalidBlendNameError) as exc_info:
        slug_stem(Path(name))
    message = str(exc_info.value)
    assert name in message, "the message must name the offending file"
    assert "Rename the file" in message, "must say what to do next"
    assert "nothing has been uploaded" in message.lower()


def test_slug_stem_caps_a_very_long_name():
    """A 200-character filename would otherwise blow Kaggle's slug length
    limit -- and fail at the same late, post-upload moment."""
    stem = slug_stem(Path("a" * 200 + ".blend"))
    assert len(stem) <= 30
    assert not stem.endswith("-")


def test_launch_with_spaces_in_the_filename_builds_valid_slugs(tmp_path):
    """The end-to-end version of the bug: a .blend with spaces must produce
    usable dataset AND kernel slugs, not "user/big buck bunny-blend"."""
    blend = tmp_path / "Big Buck Bunny.blend"
    blend.write_bytes(b"X" * 100)
    accts = accounts(2)
    f = Fleet(accts, lambda t: FakeClient(t), tmp_path / "w")
    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)

    meta = _json_slug.loads(
        (tmp_path / "w" / f"kern_{accts[0].label}" /
         "kernel-metadata.json").read_text())
    dataset_slug = meta["dataset_sources"][0]
    assert dataset_slug == "user_0/big-buck-bunny-blend"
    for worker in st.workers:
        owner, name = worker.kernel_slug.split("/", 1)
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name), worker.kernel_slug
        assert name.startswith("big-buck-bunny-render-")


def test_an_unusable_filename_is_rejected_before_anything_is_uploaded(tmp_path):
    """The half of this that actually costs the user time: validation has to
    happen BEFORE dataset_create, not inside it."""
    blend = tmp_path / "日本語.blend"
    blend.write_bytes(b"X" * 100)
    clients = {}

    def factory(tok):
        clients[tok] = FakeClient(tok)
        return clients[tok]

    f = Fleet(accounts(3), factory, tmp_path / "w")
    with pytest.raises(InvalidBlendNameError):
        f.launch(blend, RenderSettings(1920, 1080, 128), 1, 9)

    uploads = sum(c.dataset_creates + c.dataset_versions
                  for c in clients.values())
    assert uploads == 0, "the user must not wait out an upload to be told the name is bad"
    assert all(c.pushed == 0 for c in clients.values())
    assert f.load() is None, "nothing was started, so no state may be written"
