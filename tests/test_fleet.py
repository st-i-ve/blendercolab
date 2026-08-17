import json
import re
from pathlib import Path
import pytest
import blendfleet.fleet as fleet_mod
import blendfleet.platform_paths as pp
from blendfleet.accounts import Account
from blendfleet.kaggle_client import (KaggleError, KernelStatus, Quota,
                                      RevokedTokenError)
from blendfleet.notebook_builder import RenderSettings
from blendfleet.fleet import (POLL_FANOUT, Fleet, FleetBusyError, FleetState,
                              StaleDatasetError, UnreachableAccountsError,
                              WorkerState, WrongUsernameError)


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


# Datasets live on Kaggle, not inside one client. Shared across the fakes
# so an owner's upload is visible to the friends it is shared with -- which
# is the whole point of the sharing path under test. Keyed slug -> the SIZE
# actually staged, so re-uploading an edited scene of the same name is
# distinguishable from not uploading at all.
_UPLOADED_SLUGS: dict = {}


class FakeClient:
    def __init__(self, token, state="running", dataset_exists=True,
                 dataset_reachable=True, remote_file_sizes=None):
        self.token = token
        self.state = state
        self.pushed = 0
        self.push_timeouts = []
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
        # None until something has actually been uploaded, and shared
        # across clients once it has. A dataset on Kaggle is ONE dataset:
        # the owner uploads it and every friend then sees the same file.
        # The fake used to report it present from the very first call,
        # which hid the "is it already up there?" check entirely -- every
        # test looked like a scene that was always already uploaded.
        if slug in _UPLOADED_SLUGS:
            return _UPLOADED_SLUGS[slug]
        return None
    def dataset_create(self, folder, on_progress=None):
        self.dataset_creates += 1
        self._record(folder)

    def dataset_version(self, folder, message, on_progress=None):
        self.dataset_versions += 1
        self._record(folder)

    def _record(self, folder):
        slug = self._slug_being_written(folder)
        staged = [f for f in Path(folder).iterdir()
                  if f.name != "dataset-metadata.json"]
        _UPLOADED_SLUGS[slug] = staged[0].stat().st_size if staged else 0

    @staticmethod
    def _slug_being_written(folder):
        """The slug dataset_sync staged into `folder`'s metadata."""
        import json as _json
        meta = Path(folder) / "dataset-metadata.json"
        if meta.exists():
            return _json.loads(meta.read_text(encoding="utf-8")).get("id", "")
        return ""
    def push_kernel(self, folder, timeout_seconds=0):
        self.pushed += 1
        # Recorded so a test can assert the session cap actually
        # reached the push (see tests/test_session_knobs.py).
        self.push_timeouts.append(timeout_seconds)
    def status(self, slug): return KernelStatus(state=self.state)
    def cancel(self, slug): self.cancelled.append(slug); return True
    def quota(self): return Quota(0, 21600, "2026-08-01", source="api")
    def fetch_output(self, slug, dest): return []


class RaisingCancelClient(FakeClient):
    """Cancel always raises, as if the network call or auth blew up."""
    def cancel(self, slug):
        raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def _fresh_kaggle(monkeypatch):
    """No dataset survives into the next test."""
    _UPLOADED_SLUGS.clear()
    yield
    _UPLOADED_SLUGS.clear()


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


def test_launch_with_an_explicitly_empty_selection_is_refused_not_widened(
        blend, tmp_path):
    """IMPORTANT 2: accounts=None means "the whole fleet"; accounts=[] means
    the caller asked for nobody (every per-instance checkbox unticked) --
    it must never be silently widened back out to the whole fleet, or
    unticking everyone and hitting Render would start a render on every
    account anyway."""
    accts = accounts(4)
    f = Fleet(accts, lambda t: FakeClient(t), tmp_path / "w")
    with pytest.raises(ValueError, match="account"):
        f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4, accounts=[])
    assert f.load() is None, "nothing was started, so no state may be written"


# ---------------------------------------------------------------------------
# CRITICAL C1: a partial launch must never orphan already-started kernels.
# push_kernel ALWAYS starts a run, so every kernel pushed before the failure
# is already spending somebody else's GPU quota.
# ---------------------------------------------------------------------------

class ExplodingPushClient(FakeClient):
    """Stands in for an account whose token was revoked mid-launch."""
    def push_kernel(self, folder, timeout_seconds=0):
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
    """A second launch on the SAME accounts would push a second kernel onto
    an account that already has one live, spending its quota twice for the
    same output (see Fleet.require_free)."""
    f = Fleet(accounts(2), lambda t: FakeClient(t, "running"), tmp_path / "w")
    first = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)

    with pytest.raises(FleetBusyError, match="already busy"):
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
    """require_free() (see Fleet) reads persisted worker state, not a live
    Kaggle call -- poll() is what refreshes that state from "queued" to a
    terminal one, so it is run here exactly as the app's own 30s timer
    would before a second launch is attempted."""
    f = Fleet(accounts(2), lambda t: FakeClient(t, "complete"), tmp_path / "w")
    first = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    f.poll()
    second = f.launch(blend, RenderSettings(1920, 1080, 128), 5, 8)
    assert second.job_id != first.job_id
    assert f.load().job_id == second.job_id


def test_unreachable_account_does_not_wedge_launch(blend, tmp_path):
    """A revoked token means status() raises, so poll() can never move this
    account's worker out of "queued" -- require_free() (persisted state
    only, no live call) would otherwise refuse every future launch on this
    account forever. forget_job() is the documented way out (see the
    deadlock escape hatch, below)."""
    class StatusRaises(FakeClient):
        def status(self, slug):
            raise RuntimeError("401 Unauthorized")

    accts = accounts(1)
    f = Fleet(accts, lambda t: StatusRaises(t), tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    f.forget_job()
    f.launch(blend, RenderSettings(1920, 1080, 128), 5, 8)  # must not raise


# ---------------------------------------------------------------------------
# Task 4 review, fix round 1 -- IMPORTANT 3: nothing exercised launch()'s
# own accounts= threading (as opposed to busy_labels/require_free/
# free_accounts, which the earlier Task 4 tests already cover). This is
# exactly what would have caught IMPORTANT 1 below.
# ---------------------------------------------------------------------------

def test_launch_on_a_subset_pushes_only_that_subset(blend, tmp_path):
    """Regression for IMPORTANT 1: a fresh upload (dataset_slug=None, the
    realistic default -- Task 6 calls launch() exactly this way for any
    scene not already uploaded this session) used to KeyError inside
    prepare_dataset() for ANY proper subset that excluded self.accounts[0]
    or self.accounts[1:] members not in the subset, because
    prepare_dataset() shares with self.accounts[1:] regardless of which
    subset renders THIS job."""
    accts = accounts(4)
    f = Fleet(accts, lambda t: FakeClient(t), tmp_path / "w")
    subset = [accts[2], accts[3]]

    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 8,
                  accounts=subset)

    assert [w.label for w in st.workers] == ["a2", "a3"]
    assert sorted(fr for w in st.workers for fr in w.frames) == list(range(1, 9))


def test_launch_on_a_subset_never_resolves_a_client_outside_it(blend, tmp_path):
    """The render-path resolution (used to build and push each worker's own
    kernel) must stay scoped to the requested subset -- resolving an
    account nobody asked for is a wasted network call at best and a
    spurious failure (a revoked token on an account not being used) at
    worst. An already-known dataset_slug is passed so the dataset step
    -- which, unlike this, is deliberately fleet-wide (see IMPORTANT 1) --
    is skipped entirely, isolating exactly the render-path scoping."""
    accts = accounts(4)
    resolved: list[str] = []

    def factory(tok):
        resolved.append(tok)
        return FakeClient(tok)

    f = Fleet(accts, factory, tmp_path / "w")
    subset = [accts[2], accts[3]]
    dataset_slug = "user_2/remember-blend"
    _UPLOADED_SLUGS[dataset_slug] = BLEND_SIZE

    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 8,
             dataset_slug=dataset_slug, accounts=subset)

    assert set(resolved) == {accts[2].token, accts[3].token}


# ---------------------------------------------------------------------------
# Task 4 review, fix round 2 -- NEW IMPORTANT: fix round 1's dataset-sharing
# resolution (see IMPORTANT 1 above) resolved the WHOLE fleet strictly, so a
# dead account ANYWHERE -- in or out of the launch subset -- failed every
# subset launch that needed a fresh upload. Split into a HARD requirement
# for accounts inside the launch and a BEST-EFFORT one for accounts outside
# it (Fleet.prepare_dataset's `required`, Fleet.unshared_accounts).
# ---------------------------------------------------------------------------

class RevokedWhoamiClient(FakeClient):
    """Stands in for an account whose Kaggle token has been revoked --
    whoami() is where _resolve_clients() notices that for real."""
    def whoami(self):
        raise RevokedTokenError(
            "this account's Kaggle token has been revoked. Go to "
            "kaggle.com -> Settings -> API -> Generate New Token.")


def test_launch_tolerates_a_revoked_account_outside_the_subset(blend, tmp_path):
    """The account excluded from the render (a1, a plain friend -- NOT the
    fleet's owner, so this isolates the actual coupling bug rather than the
    separate, unavoidable "the owner itself must always work" question)
    being revoked must not fail a launch that never asked to render on it.
    It must still be named somewhere so the user knows that account was
    not shared with and will need re-sharing/re-upload before it can be
    used."""
    accts = accounts(4)

    def factory(tok):
        if tok == accts[1].token:
            return RevokedWhoamiClient(tok)
        return FakeClient(tok)

    f = Fleet(accts, factory, tmp_path / "w")
    subset = [accts[2], accts[3]]

    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 8,
                  accounts=subset)  # must not raise

    assert [w.label for w in st.workers] == ["a2", "a3"]
    assert "a1" in f.unshared_accounts, (
        "the account this launch could not share with must be named, not "
        "silently dropped")


def test_launch_still_fails_when_a_required_account_cannot_see_the_scene(
        blend, tmp_path):
    """The other half of the same split: an account INSIDE this launch's
    subset must still block the launch if it cannot see the scene -- its
    kernel is about to be pushed, and it would burn its quota failing to
    find the .blend. Uses an unreachable-after-grant account (checked only
    inside prepare_dataset's grant/verify step, not the earlier
    resolve-before-anything-else guard) so this specifically pins the
    required/optional split itself: a wrong or over-broadened version of
    that split (e.g. "simplifying" it back to treating every account the
    same) would let this one through silently instead of raising."""
    accts = accounts(4)
    f = Fleet(accts, lambda t: FakeClient(
        t, dataset_reachable=(t != accts[2].token)), tmp_path / "w")
    subset = [accts[2], accts[3]]

    with pytest.raises(UnreachableAccountsError, match="user_2"):
        f.launch(blend, RenderSettings(1920, 1080, 128), 1, 8,
                 accounts=subset)

    assert f.load() is None, "nothing should have been started"


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
# Must-fix 1: cancel_worker() used to start from self.load() -- the
# single newest tracked job -- so a worker belonging to any OLDER job was
# invisible to it. The per-job Cancel button (cancelInstance -> this
# method) and the Instances-page Stop button both reach cancel_worker()
# by label; with two scenes rendering at once, the account being stopped
# is just as likely to be busy in an older job as in the newest one.
# ---------------------------------------------------------------------------

def test_cancel_worker_finds_the_worker_in_an_older_job_not_just_the_newest(
        tmp_path):
    """acct0's live kernel is in job-old; job-new (the only one load()
    would see) belongs to a different account entirely. Cancelling acct0
    must still find and stop it, not report "already stopped" while it
    keeps running and billing."""
    clients = {}
    def factory(tok):
        clients[tok] = FakeClient(tok, "running"); return clients[tok]
    accts = accounts(2)
    f = Fleet(accts, factory, tmp_path / "w")
    f.save_jobs([
        FleetState(job_id="job-old", blend_name="alpha.blend",
                  start_frame=1, end_frame=4,
                  workers=[WorkerState(label="a0", username="user_0",
                                       kernel_slug="user_0/alpha-render-old",
                                       frames=[1, 2, 3, 4], state="running")]),
        FleetState(job_id="job-new", blend_name="beta.blend",
                  start_frame=1, end_frame=4,
                  workers=[WorkerState(label="a1", username="user_1",
                                       kernel_slug="user_1/beta-render-new",
                                       frames=[1, 2, 3, 4], state="running")]),
    ])

    result = f.cancel_worker("a0")

    assert result is not None, (
        "acct0's worker lives in the OLDER job, invisible to load() -- "
        "this must still find and cancel it")
    assert result.ok is True
    assert clients[accts[0].token].cancelled == ["user_0/alpha-render-old"]
    # The other job's account must never even be reached by this call.
    assert accts[1].token not in clients


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


def test_fetch_failure_log_finds_an_errored_worker_in_an_older_job(
        tmp_path):
    """Must-fix 1, same defect as cancel_worker(): this used to read only
    load()'s single newest job, so an errored worker belonging to an
    OLDER job silently returned "" as if it had never failed."""
    clients = {}
    accts = accounts(2)

    def factory(tok):
        text = ("RuntimeError: CUDA out of memory" if tok == accts[0].token
                else "")
        clients[tok] = LogFetchingClient(tok, "running", log_text=text)
        return clients[tok]

    f = Fleet(accts, factory, tmp_path / "w")
    f.save_jobs([
        FleetState(job_id="job-old", blend_name="alpha.blend",
                  start_frame=1, end_frame=4,
                  workers=[WorkerState(label="a0", username="user_0",
                                       kernel_slug="user_0/alpha-render-old",
                                       frames=[1, 2, 3, 4], state="error")]),
        FleetState(job_id="job-new", blend_name="beta.blend",
                  start_frame=1, end_frame=4,
                  workers=[WorkerState(label="a1", username="user_1",
                                       kernel_slug="user_1/beta-render-new",
                                       frames=[1, 2, 3, 4], state="running")]),
    ])

    text = f.fetch_failure_log("a0")

    assert text == "RuntimeError: CUDA out of memory"


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


# ---------------------------------------------------------------------------
# The deadlock escape hatch. Kaggle has been observed to report a kernel as
# active while refusing the cancel request: cancel_all() then cannot clear
# it and launch() keeps refusing because a job is "still running", which
# leaves the app wedged with no way out but editing state by hand.
# ---------------------------------------------------------------------------

def test_forget_job_clears_the_state_so_a_new_render_can_start(tmp_path):
    fleet = Fleet([Account(label="a", token="KGAT_" + "0" * 32,
                           username="user_a")],
                  lambda t: object(), tmp_path / "w")
    fleet._save(FleetState(job_id="j", blend_name="s.blend", start_frame=1,
                           end_frame=4,
                           workers=[WorkerState(label="a", username="user_a",
                                                kernel_slug="user_a/k",
                                                frames=[1, 2], state="running")]))
    assert fleet.load() is not None

    forgotten = fleet.forget_job()

    assert [w.label for w in forgotten] == ["a"]
    assert fleet.load() is None, "the job is still tracked, so launch stays blocked"


def test_forget_job_does_not_cancel_anything(tmp_path):
    """It must not touch Kaggle at all. A button that quietly abandoned a
    running session while sounding like a cancel would be the worst lie
    this app could tell -- the caller is what has to say so."""
    calls = []

    class LoudClient:
        def __init__(self, token):
            self.token = token

        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError(f"forget_job called client.{name}")

    fleet = Fleet([Account(label="a", token="KGAT_" + "0" * 32,
                           username="user_a")],
                  LoudClient, tmp_path / "w")
    fleet._save(FleetState(job_id="j", blend_name="s.blend", start_frame=1,
                           end_frame=2,
                           workers=[WorkerState(label="a", username="user_a",
                                                kernel_slug="user_a/k",
                                                frames=[1], state="running")]))
    fleet.forget_job()
    assert calls == []


def test_forget_job_with_no_job_is_a_no_op(tmp_path):
    fleet = Fleet([Account(label="a", token="KGAT_" + "0" * 32,
                           username="user_a")],
                  lambda t: object(), tmp_path / "w")
    assert fleet.forget_job() == []


# ---------------------------------------------------------------------------
# A username typed by hand can be a label, an email or a typo. Kaggle
# answers that with 'The following collaborator usernames don't exist:
# "james"' -- AFTER the whole .blend has been uploaded, and worded as if
# the app had invented the name.
# ---------------------------------------------------------------------------

class _WhoamiClient:
    def __init__(self, token, says):
        self.token = token
        self._says = says

    def whoami(self):
        if self._says is None:
            raise KaggleError("this account has no notebooks or datasets")
        return self._says


def test_a_username_kaggle_disagrees_with_is_refused_before_sharing(tmp_path):
    accounts = [Account(label="owner", token="KGAT_" + "0" * 32,
                        username="realowner"),
                Account(label="james", token="KGAT_" + "1" * 32,
                        username="james")]
    says = {"KGAT_" + "0" * 32: "realowner",
            "KGAT_" + "1" * 32: "stepheneechikoi"}
    fleet = Fleet(accounts, lambda t: _WhoamiClient(t, says[t]), tmp_path / "w")

    with pytest.raises(WrongUsernameError) as excinfo:
        fleet._require_real_usernames(
            accounts[1:], {"owner": "realowner", "james": "james"},
            {"owner": _WhoamiClient(accounts[0].token, "realowner"),
             "james": _WhoamiClient(accounts[1].token, "stepheneechikoi")})

    message = str(excinfo.value)
    assert "james" in message and "stepheneechikoi" in message
    assert "Set username" in message
    assert "no render has started" in message


def test_an_unverifiable_username_is_allowed_through(tmp_path):
    """An account that owns nothing has no handle for Kaggle to report --
    which is the very case manual entry exists for. Refusing it here would
    block the fix. Kaggle stays the final word."""
    accounts = [Account(label="owner", token="KGAT_" + "0" * 32,
                        username="realowner"),
                Account(label="friend", token="KGAT_" + "1" * 32,
                        username="typed-by-hand")]
    fleet = Fleet(accounts, lambda t: _WhoamiClient(t, None), tmp_path / "w")

    fleet._require_real_usernames(
        accounts[1:], {"owner": "realowner", "friend": "typed-by-hand"},
        {"owner": _WhoamiClient(accounts[0].token, None),
         "friend": _WhoamiClient(accounts[1].token, None)})


def test_a_matching_username_passes(tmp_path):
    accounts = [Account(label="owner", token="KGAT_" + "0" * 32,
                        username="realowner"),
                Account(label="friend", token="KGAT_" + "1" * 32,
                        username="realfriend")]
    fleet = Fleet(accounts, lambda t: _WhoamiClient(t, "realfriend"),
                  tmp_path / "w")
    fleet._require_real_usernames(
        accounts[1:], {"owner": "realowner", "friend": "realfriend"},
        {"owner": _WhoamiClient(accounts[0].token, "realowner"),
         "friend": _WhoamiClient(accounts[1].token, "realfriend")})


# ---------------------------------------------------------------------------
# "Expecting value: line 1 column 1 (char 0)" -- json.loads' answer to an
# empty file, surfacing from wherever the next load happens to be rather
# than from the save that truncated it.
# ---------------------------------------------------------------------------

def test_an_empty_state_file_reads_as_no_job(tmp_path):
    fleet = Fleet([Account(label="a", token="KGAT_" + "0" * 32,
                           username="user_a")],
                  lambda t: object(), tmp_path / "w")
    fleet._state_path().parent.mkdir(parents=True, exist_ok=True)
    fleet._state_path().write_text("", encoding="utf-8")
    assert fleet.load() is None


def test_a_corrupt_state_file_reads_as_no_job(tmp_path):
    """No tracked job is both the truthful reading and the recoverable
    one: the alternative is an app that cannot start, cancel or collect
    anything until a file is edited by hand."""
    fleet = Fleet([Account(label="a", token="KGAT_" + "0" * 32,
                           username="user_a")],
                  lambda t: object(), tmp_path / "w")
    fleet._state_path().parent.mkdir(parents=True, exist_ok=True)
    fleet._state_path().write_text("{not json", encoding="utf-8")
    assert fleet.load() is None


def test_saving_state_is_atomic(tmp_path):
    """No .tmp left behind, and the file is complete JSON at every moment
    a reader could see it."""
    fleet = Fleet([Account(label="a", token="KGAT_" + "0" * 32,
                           username="user_a")],
                  lambda t: object(), tmp_path / "w")
    fleet._save(FleetState(job_id="j", blend_name="s.blend", start_frame=1,
                           end_frame=2, workers=[]))
    path = fleet._state_path()
    # Task 3: the on-disk shape became {"jobs": [...]} so more than one job
    # can be tracked at once -- job_id now lives inside that list.
    assert json.loads(path.read_text(encoding="utf-8"))["jobs"][0]["job_id"] == "j"
    assert not list(path.parent.glob("*.tmp")), "a temporary file was left"


def test_kaggles_collaborator_rejection_names_the_account_not_just_the_handle(
        tmp_path):
    """Kaggle says the handle is unknown but has no idea which of YOUR
    accounts carries it -- and the pre-check cannot catch this case,
    because an account that owns nothing has no handle to verify."""
    friends = [Account(label="james", token="KGAT_" + "1" * 32,
                       username="james")]
    original = RuntimeError(
        'granting james reader access failed: The following collaborator '
        'usernames don\'t exist: "james"')

    explained = fleet_mod._explain_bad_collaborators(
        original, friends, {"james": "james"})

    assert isinstance(explained, WrongUsernameError)
    message = str(explained)
    assert "james" in message
    assert "Set username" in message
    assert "no quota has been spent" in message


def test_an_unrelated_sharing_failure_is_passed_through_unchanged(tmp_path):
    """Guessing at failures it does not recognise would be worse than
    letting them through with their own words."""
    original = RuntimeError("503 Service Unavailable")
    assert fleet_mod._explain_bad_collaborators(original, [], {}) is original


def test_a_scene_already_on_kaggle_is_not_uploaded_again(blend, tmp_path):
    """"Render across fleet" kept re-sending the whole scene.

    "We already uploaded this" was remembered only for the lifetime of one
    session, so restarting the app -- or pressing Render without pressing
    Upload first -- re-sent bytes that were already on Kaggle. The question
    is now asked of Kaggle, not of memory.
    """
    clients = {}

    def factory(tok):
        clients.setdefault(tok, FakeClient(tok))
        return clients[tok]

    accounts_ = accounts(2)
    first = Fleet(accounts_, factory, tmp_path / "w")
    first.prepare_dataset(blend)
    uploads_after_first = sum(c.dataset_creates + c.dataset_versions
                              for c in clients.values())
    assert uploads_after_first == 1

    # A brand-new Fleet: no memory of the first one whatsoever.
    second = Fleet(accounts_, factory, tmp_path / "w2")
    second.prepare_dataset(blend)

    total = sum(c.dataset_creates + c.dataset_versions
                for c in clients.values())
    assert total == 1, f"re-uploaded a scene already on Kaggle ({total} uploads)"


def test_a_changed_scene_of_the_same_name_IS_uploaded_again(blend, tmp_path):
    """The check is on SIZE as well as name. Skipping on name alone would
    render last week's scene and look like it worked."""
    clients = {}

    def factory(tok):
        clients.setdefault(tok, FakeClient(tok))
        return clients[tok]

    accounts_ = accounts(1)
    fleet = Fleet(accounts_, factory, tmp_path / "w")
    fleet.prepare_dataset(blend)

    blend.write_bytes(b"Y" * (BLEND_SIZE + 500))     # edited since
    fleet.prepare_dataset(blend)

    total = sum(c.dataset_creates + c.dataset_versions
                for c in clients.values())
    assert total == 2, "a changed scene was not re-uploaded"


def test_the_stages_reported_say_whether_it_uploaded_or_skipped(blend, tmp_path):
    """"Uploading, shared or stuck" was indistinguishable. Each wait now
    names itself, including the one where there is nothing to do."""
    seen = []
    fleet = Fleet(accounts(1), lambda t: FakeClient(t), tmp_path / "w")
    fleet.prepare_dataset(blend, on_stage=lambda k, d: seen.append(k))
    assert "checking" in seen and "uploading" in seen and "ready" in seen

    seen.clear()
    fleet.prepare_dataset(blend, on_stage=lambda k, d: seen.append(k))
    assert "already-uploaded" in seen
    assert "uploading" not in seen


# ---------------------------------------------------------------------------
# Must-fix 2: start_workers() used to guard with active_workers(), which
# reads self.load() -- the single newest tracked job -- and asks Kaggle
# LIVE. With an OLDER job still rendering and a NEWER job already
# finished, that guard passed and pushed a second warm kernel onto an
# account that was already mid-render, double-billing it. No test in
# this file called start_workers() at all before this.
# ---------------------------------------------------------------------------

def test_start_workers_refuses_an_account_busy_in_an_older_job(blend, tmp_path):
    """a0 is mid-render in job-old; job-new (the only one active_workers()
    -> load() would ever see) is a0's own job already complete, and a1's
    account is untouched by either. Starting a0 warm must be refused;
    starting a1 (never busy in any job) must still be allowed."""
    accts = accounts(2)

    def factory(tok):
        state = "running" if tok == accts[0].token else "complete"
        return FakeClient(tok, state)

    f = Fleet(accts, factory, tmp_path / "w")
    f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4,
            accounts=[accts[0]])
    f.poll()   # a0's worker becomes "running" -- an OLDER job now
    f.launch(blend, RenderSettings(1920, 1080, 128), 5, 8,
            accounts=[accts[1]])
    f.poll()   # a1's worker becomes "complete" -- the NEWEST job

    with pytest.raises(FleetBusyError, match="a0"):
        f.start_workers(["a0"], RenderSettings(1920, 1080, 128), "u/scene")

    st = f.start_workers(["a1"], RenderSettings(1920, 1080, 128), "u/scene")
    assert [w.label for w in st.workers] == ["a1"]


# ---------------------------------------------------------------------------
# A 403 from ListDatasetFiles is a PROPAGATION DELAY, not a failed upload.
#
# Reported from the field: the first Upload of a scene reported
#
#     Uploading the scene failed: could not list the files in dataset
#     'sudaouserwithani/stranger-blend': 403 Client Error: Forbidden
#
# and pressing Upload a second time succeeded with no other change. That is
# the signature of Kaggle accepting the READER grant but not yet exposing
# the dataset's FILE LISTING to the friend account -- the same delay the
# reachability check one call earlier already tolerates. Only
# StaleDatasetError was caught around the per-friend verification, so the
# KaggleError carrying that 403 escaped and failed the whole upload.
# ---------------------------------------------------------------------------

class ForbiddenListingClient(FakeClient):
    """A friend whose READER grant has landed but whose file listing Kaggle
    still answers 403 for -- exactly what KaggleClient.list_dataset_files
    turns into a KaggleError."""
    def dataset_file_size(self, slug, filename):
        raise KaggleError(
            f"could not list the files in dataset {slug!r}: 403 Client "
            "Error: Forbidden for url: https://api.kaggle.com/v1/"
            "datasets.DatasetApiService/ListDatasetFiles.")


def test_a_friends_403_listing_does_not_fail_an_upload_that_needs_only_the_owner(
        blend, tmp_path):
    """The reported bug. The bytes are on Kaggle and the owner has verified
    them; a friend whose listing has not caught up is not a failed
    upload."""
    accts = accounts(3)

    def factory(tok):
        if tok == accts[1].token:
            return ForbiddenListingClient(tok)
        return FakeClient(tok)

    f = Fleet(accts, factory, tmp_path / "w")

    slug = f.prepare_dataset(blend, required=[accts[0]])   # must not raise

    assert slug.endswith("-blend")
    assert "a1" in f.unshared_accounts, (
        "an upload that could not verify a friend's copy must name that "
        "friend, or a later render on it fails for no visible reason")
    assert "403" in f.unshared_accounts["a1"]


def test_a_403_still_blocks_an_account_that_is_about_to_render(blend, tmp_path):
    """The other half of the split, unchanged: an account inside the launch
    subset that cannot see the scene must still stop the launch, because
    its kernel is about to be pushed and would burn quota failing."""
    accts = accounts(3)

    def factory(tok):
        if tok == accts[1].token:
            return ForbiddenListingClient(tok)
        return FakeClient(tok)

    f = Fleet(accts, factory, tmp_path / "w")

    with pytest.raises(KaggleError, match="403"):
        f.prepare_dataset(blend, required=[accts[0], accts[1]])


class RevokedListingClient(FakeClient):
    """RevokedTokenError SUBCLASSES KaggleError, so the new tolerant branch
    must not quietly relabel a dead token as a timing problem."""
    def dataset_file_size(self, slug, filename):
        raise RevokedTokenError(
            "this account's Kaggle token has been revoked. Go to "
            "kaggle.com -> Settings -> API -> Generate New Token.")


def test_a_revoked_token_is_never_reported_as_a_propagation_delay(blend,
                                                                  tmp_path):
    """Telling someone to wait for a grant that will never arrive is worse
    than telling them nothing."""
    accts = accounts(3)

    def factory(tok):
        if tok == accts[1].token:
            return RevokedListingClient(tok)
        return FakeClient(tok)

    f = Fleet(accts, factory, tmp_path / "w")
    f.prepare_dataset(blend, required=[accts[0]])

    reason = f.unshared_accounts["a1"]
    assert "revoked" in reason
    assert "has not made" not in reason, (
        "a revoked token was described as a propagation delay")


# ---------------------------------------------------------------------------
# The OWNER's own post-upload verification is a RACE, not a failure.
#
# From %APPDATA%\BlendFleet\logs\blendfleet-20260815-142829.log:
#
#   14:30:37 uploading waydown.blend (498927212 bytes)
#   14:33:38 upload finished in 181045 ms
#   14:33:40 the uploaded copy did NOT verify after 1059 ms -- nothing was
#            shared with anyone. KaggleError: could not list the files in
#            dataset 'sudaouserwithani/waydown-blend': 403
#
# That 403 is the OWNER failing to list its OWN dataset one second after a
# 499 MB upload: Kaggle had not finished ingesting it. Three minutes and
# half a gigabyte of successful upload were discarded at the last check,
# and pressing Upload again later "just worked".
# ---------------------------------------------------------------------------

class FakeClock:
    """A clock that moves only when something sleeps on it.

    The retry window is 20-300 real seconds; nothing in a test suite may
    wait that out, and nothing may busy-loop either, so the fake sleep is
    what advances the fake clock.
    """

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class IngestingOwnerClient(FakeClient):
    """The owner's own file listing 403s for its first `forbidden` calls.

    Call 1 is the pre-upload "is it already there?" check, which on a
    first upload legitimately 403s because the dataset does not exist yet
    (Kaggle answers 403, not 404 -- see kaggle_client). Every call after
    that is the post-upload verification.
    """

    def __init__(self, token, forbidden=2, **kw):
        super().__init__(token, **kw)
        self.forbidden = forbidden
        self.listing_calls = 0

    def dataset_file_size(self, slug, filename):
        self.listing_calls += 1
        if self.listing_calls <= self.forbidden:
            raise KaggleError(
                f"could not list the files in dataset {slug!r}: 403 Client "
                "Error: Forbidden for url: https://api.kaggle.com/v1/"
                "datasets.DatasetApiService/ListDatasetFiles.")
        return super().dataset_file_size(slug, filename)


def _uploads(clients):
    return sum(c.dataset_creates + c.dataset_versions for c in clients.values())


def test_a_403_that_clears_on_the_second_attempt_does_not_re_upload(blend,
                                                                    tmp_path):
    """The reported bug. One 403 immediately after the upload is Kaggle
    still ingesting; waiting a second is the whole fix, and the file must
    not be sent again to get it."""
    clock = FakeClock()
    clients = {}

    def factory(tok):
        clients[tok] = IngestingOwnerClient(tok, forbidden=2)
        return clients[tok]

    f = Fleet(accounts(1), factory, tmp_path / "w")
    slug = f.prepare_dataset(blend, sleep=clock.sleep, clock=clock)

    assert slug.endswith("-blend")
    assert _uploads(clients) == 1, "the scene was re-sent to survive a 403"
    assert clock.slept, "the owner's verification never waited at all"


def test_a_403_that_never_clears_fails_with_upload_succeeded_wording(blend,
                                                                     tmp_path):
    """When the window really does expire, the message must not blame a
    deleted dataset or a lapsed grant: the owner uploaded this file itself,
    seconds ago."""
    clock = FakeClock()
    clients = {}

    def factory(tok):
        clients[tok] = IngestingOwnerClient(tok, forbidden=10_000)
        return clients[tok]

    f = Fleet(accounts(1), factory, tmp_path / "w")
    with pytest.raises(fleet_mod.UploadNotVisibleError) as exc_info:
        f.prepare_dataset(blend, sleep=clock.sleep, clock=clock)

    message = str(exc_info.value)
    assert "uploading to Kaggle successfully" in message
    assert "not re-send" in message.lower() or "will not" in message.lower()
    assert "already on Kaggle" in message, (
        "the user must be told the retry skips the upload")
    assert "deleted or renamed" not in message and "lapsed" not in message, (
        "the owner's own fresh upload is not a deleted dataset or a lapsed "
        "grant")
    assert _uploads(clients) == 1, "gave up but still re-sent the scene"
    assert clock.now >= fleet_mod.verify_window_s(BLEND_SIZE), (
        "gave up before the window it promised to wait")


def test_a_size_mismatch_fails_immediately_and_is_never_retried(blend,
                                                                tmp_path):
    """A stale copy is not a timing problem. Kaggle's listing is complete
    and disagrees -- waiting cannot turn 999 bytes into the right file, so
    this must not spend the window discovering that."""
    clock = FakeClock()

    def factory(tok):
        return FakeClient(tok, remote_file_sizes={blend.name: 999})

    f = Fleet(accounts(1), factory, tmp_path / "w")
    with pytest.raises(StaleDatasetError):
        f.prepare_dataset(blend, sleep=clock.sleep, clock=clock)

    assert clock.slept == [], "a size mismatch was retried as if it were a race"


def test_the_wait_for_kaggle_is_reported_through_the_stage_callback(blend,
                                                                    tmp_path):
    """A frozen 100% progress bar for a minute is indistinguishable from a
    hang. The wait names itself, like every other stage does."""
    clock = FakeClock()
    seen = []

    def factory(tok):
        return IngestingOwnerClient(tok, forbidden=3)

    f = Fleet(accounts(1), factory, tmp_path / "w")
    f.prepare_dataset(blend, sleep=clock.sleep, clock=clock,
                      on_stage=lambda k, d: seen.append((k, d)))

    waits = [d for k, d in seen if k == "waiting-for-kaggle"]
    assert waits, "the app went silent while waiting for Kaggle"
    assert "s of up to" in waits[0], (
        "the wait must say how long it has waited and how long it will")
    assert [k for k, _ in seen][-1] == "ready"


def test_the_wait_window_scales_with_the_size_actually_uploaded(blend):
    """A 5 MB scene and a 500 MB scene do not need the same patience, and
    neither may wait forever."""
    small = fleet_mod.verify_window_s(5 << 20)
    big = fleet_mod.verify_window_s(499 * (1 << 20))     # the reported scene

    assert small >= 20, "less patience than the 1.06 s that already failed"
    assert big > small, "half a gigabyte got no more time than 5 MB"
    assert fleet_mod.verify_window_s(50 << 30) == fleet_mod._VERIFY_WINDOW_MAX_S


def test_a_revoked_owner_token_is_never_waited_out(blend, tmp_path):
    """Waiting five minutes for credentials that are dead is the cruellest
    possible spinner -- and it is a KaggleError subclass, so the tolerant
    branch has to exclude it deliberately."""
    clock = FakeClock()

    f = Fleet(accounts(1), lambda t: RevokedListingClient(t), tmp_path / "w")
    with pytest.raises(RevokedTokenError):
        f.prepare_dataset(blend, sleep=clock.sleep, clock=clock)

    assert clock.slept == []


# ---------------------------------------------------------------------------
# what the diagnostic log says about sharing
#
# A user reported "the upload worked but the other accounts never got the
# file", and %APPDATA%\BlendFleet\logs had NOTHING about it -- the four
# steps prepare_dataset performs (name, bulk grant, reachable, file at the
# right size) produced one indistinguishable outcome and reached no file at
# all. These tests are about that evidence existing, per account.
# ---------------------------------------------------------------------------

@pytest.fixture
def diagnostic_log(tmp_path):
    """A real crash_log for one test, then process-global state back.

    install() rebinds sys.excepthook, threading.excepthook, Qt's message
    handler and faulthandler's target fd -- left in place, a later test
    would be reporting into a tmp file this one already deleted (the same
    reasoning as tests/test_crash_log.py's own fixture).
    """
    import faulthandler
    import sys as _sys
    import threading as _threading

    from blendfleet import crash_log

    saved_excepthook = _sys.excepthook
    saved_thread_hook = _threading.excepthook
    saved_faulthandler = faulthandler.is_enabled()
    path = crash_log.install(tmp_path / "logs")
    yield path
    crash_log.shutdown()
    _sys.excepthook = saved_excepthook
    _threading.excepthook = saved_thread_hook
    try:
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(None)
    except ImportError:
        pass
    if saved_faulthandler:
        faulthandler.enable()
    else:
        faulthandler.disable()


def _log_text(path):
    return path.read_text(encoding="utf-8", errors="replace")


def test_every_sharing_step_is_recorded_for_every_account(blend, tmp_path,
                                                          diagnostic_log):
    """Which step an account reached, named, with its Kaggle username."""
    accts = accounts(3)
    f = Fleet(accts, lambda t: FakeClient(t), tmp_path / "w")

    slug = f.prepare_dataset(blend)
    body = _log_text(diagnostic_log)

    assert slug in body, "a sharing line that does not name the dataset is unusable"
    for label, username in (("a1", "user_1"), ("a2", "user_2")):
        assert f"{label} ({username})" in body
    assert "step 1/4" in body
    assert "step 2/4 grant -- bulk grant_readers accepted" in body
    for label in ("a1", "a2"):
        assert re.search(rf"{label} \(user_\d\): step 3/4 reachable -- yes", body)
        assert re.search(rf"{label} \(user_\d\): step 4/4 file -- can see", body)


def test_the_log_times_every_sharing_step(blend, tmp_path, diagnostic_log):
    """A propagation delay and a permission error are the same sentence to
    the user; only the elapsed time tells them apart afterwards."""
    f = Fleet(accounts(2), lambda t: FakeClient(t), tmp_path / "w")
    f.prepare_dataset(blend)

    body = _log_text(diagnostic_log)
    assert re.search(r"step 3/4 reachable -- yes, in \d+ ms", body)
    assert re.search(r"step 4/4 file -- can see .* in \d+ ms", body)


def test_the_log_proves_sharing_happened_after_the_upload(blend, tmp_path,
                                                          diagnostic_log):
    """The user asked whether sharing could wait until the upload is fully
    complete. It already does -- and now the file says so in order."""
    f = Fleet(accounts(2), lambda t: FakeClient(t), tmp_path / "w")
    f.prepare_dataset(blend)

    body = _log_text(diagnostic_log)
    assert (body.index("upload finished")
            < body.index("owner user_0: verified")
            < body.index("step 1/4")
            < body.index("step 2/4"))


class BulkGrantRejectingSdk(FakeSdk):
    def __init__(self):
        super().__init__()

        def update(request):
            class Resp:
                errors = ["Kaggle said no"]
            return Resp()
        self.datasets.dataset_api_client.update_dataset_metadata = update


def test_a_failed_bulk_grant_is_never_pinned_on_one_account(blend, tmp_path,
                                                            diagnostic_log):
    """grant_readers is ONE write for every friend at once, so its failure
    is not attributable to any single account -- the log has to say that
    rather than let the next reader hunt the wrong one."""
    accts = accounts(3)

    def factory(tok):
        client = FakeClient(tok)
        if tok == accts[0].token:
            client.sdk = BulkGrantRejectingSdk()
            client._sdk_factory = lambda _t: client.sdk
        return client

    f = Fleet(accts, factory, tmp_path / "w")
    with pytest.raises(Exception):
        f.prepare_dataset(blend)

    body = _log_text(diagnostic_log)
    assert "step 2/4 grant -- FAILED" in body
    assert "NOT attributable to any one account" in body
    assert "a1 (user_1)" in body and "a2 (user_2)" in body


def test_an_account_the_grant_never_reached_is_named_at_its_step(
        blend, tmp_path, diagnostic_log):
    """The reported failure: granted, but the friend still cannot see it."""
    accts = accounts(3)

    def factory(tok):
        return FakeClient(tok, dataset_reachable=(tok != accts[1].token))

    f = Fleet(accts, factory, tmp_path / "w")
    f.prepare_dataset(blend, required=[accts[0]])

    body = _log_text(diagnostic_log)
    assert re.search(r"a1 \(user_1\): step 3/4 reachable -- NO", body)
    assert "optional" in body
    assert "a2 (user_2): step 3/4 reachable -- yes" in body, (
        "one account failing must not stop the others being recorded")


class TokenLeakingClient(FakeClient):
    """A Kaggle failure that echoes back the credential it was sent.

    Not hypothetical: errors from the SDK quote URLs and request context,
    and this log is a file the user is asked to send on.
    """
    def dataset_file_size(self, slug, filename):
        raise KaggleError(f"403 Forbidden for token={self.token}")


def test_no_account_token_ever_reaches_the_diagnostic_log(blend, tmp_path,
                                                          diagnostic_log):
    accts = accounts(3)

    def factory(tok):
        if tok == accts[1].token:
            return TokenLeakingClient(tok)
        return FakeClient(tok)

    f = Fleet(accts, factory, tmp_path / "w")
    f.prepare_dataset(blend, required=[accts[0]])

    body = _log_text(diagnostic_log)
    assert "step 4/4 file" in body, "the failure itself must still be recorded"
    for account in accts:
        assert account.token not in body, (
            "a diagnostic log carrying a live token turns a support request "
            "into a credential rotation")
    assert accts[1].token[:9] + "…" in body


def test_the_closing_line_says_who_ended_up_with_the_scene(blend, tmp_path,
                                                           diagnostic_log):
    accts = accounts(3)

    def factory(tok):
        return FakeClient(tok, dataset_reachable=(tok != accts[2].token))

    f = Fleet(accts, factory, tmp_path / "w")
    f.prepare_dataset(blend, required=[accts[0]])

    body = _log_text(diagnostic_log)
    assert "sharing finished: 1 of 2 other account(s) can see" in body
    assert "NOT shared with a2" in body


# ---------------------------------------------------------------------------
# Progress that survives a restart.
#
# Kaggle's kernel-status API reports a state and a message, never a frame
# count, so poll_all() cannot learn one -- which meant frames_done was only
# ever written by the live log stream held in memory, and closing the app
# threw away every number it had reported. See Fleet.record_progress.
# ---------------------------------------------------------------------------

def test_a_recorded_frame_count_survives_a_save_and_load(blend, tmp_path):
    f = Fleet(accounts(2), lambda t: FakeClient(t), tmp_path / "w")
    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 8)
    label = st.workers[0].label

    f.record_progress({label: 3})

    reloaded = Fleet(accounts(2), lambda t: FakeClient(t),
                     tmp_path / "w").load()
    by_label = {w.label: w for w in reloaded.workers}
    assert by_label[label].frames_done == 3
    assert by_label[label].frames_done_at > 0
    # Nobody else was touched: this only ever knows about the labels it
    # was handed.
    assert all(w.frames_done == 0 for w in reloaded.workers
               if w.label != label)


def test_a_count_that_did_not_move_writes_nothing(blend, tmp_path):
    f = Fleet(accounts(1), lambda t: FakeClient(t), tmp_path / "w")
    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 4)
    label = st.workers[0].label
    f.record_progress({label: 2})
    before = f._state_path().read_text(encoding="utf-8")

    assert f.record_progress({label: 2}) == []
    assert f._state_path().read_text(encoding="utf-8") == before


def test_a_replayed_log_never_rolls_the_count_backwards(blend, tmp_path):
    """A resumed stream replays the whole log from the top, and a
    reconnect mid-replay can briefly report fewer frames than the last
    complete pass did. A bar that jumps back reads as a render that
    restarted -- a lie about somebody's quota."""
    f = Fleet(accounts(1), lambda t: FakeClient(t), tmp_path / "w")
    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 6)
    label = st.workers[0].label
    f.record_progress({label: 5})

    assert f.record_progress({label: 2}) == []
    assert f.load().workers[0].frames_done == 5


def test_progress_lands_on_the_most_recent_job_a_label_is_in(tmp_path):
    """A label that rendered an older job and is now rendering a newer one
    points at the newer one -- the same rule the dashboard payload uses,
    so the two cannot disagree about which worker a reading belongs to."""
    f = Fleet(accounts(1), lambda t: FakeClient(t), tmp_path / "w")
    f.save_jobs([
        FleetState(job_id="old", blend_name="a.blend", start_frame=1,
                   end_frame=2,
                   workers=[WorkerState(label="a0", username="u0",
                                        kernel_slug="u0/a-render-1",
                                        frames=[1, 2], state="complete")]),
        FleetState(job_id="new", blend_name="b.blend", start_frame=1,
                   end_frame=2,
                   workers=[WorkerState(label="a0", username="u0",
                                        kernel_slug="u0/b-render-1",
                                        frames=[1, 2], state="running")]),
    ])

    f.record_progress({"a0": 1})

    jobs = f.load_jobs()
    assert jobs[0].workers[0].frames_done == 0
    assert jobs[1].workers[0].frames_done == 1


def test_record_progress_ignores_a_label_that_is_in_no_job(tmp_path):
    f = Fleet(accounts(1), lambda t: FakeClient(t), tmp_path / "w")
    assert f.record_progress({"nobody": 4}) == []


def test_a_poll_does_not_undo_a_frame_count_saved_while_it_was_in_flight(
        blend, tmp_path):
    """poll_all builds its answer from a snapshot read BEFORE the network
    round trips. Writing that snapshot back verbatim would silently roll
    the user's progress bar backwards every 30 seconds."""
    f = Fleet(accounts(1), lambda t: FakeClient(t, "running"),
              tmp_path / "w")
    st = f.launch(blend, RenderSettings(1920, 1080, 128), 1, 6)
    label = st.workers[0].label

    saved: list[int] = []

    class MidPollClient(FakeClient):
        """Stands in for the live stream landing a frame between
        poll_all's own load_jobs() and its write-back."""

        def status(self, slug):
            if not saved:
                saved.append(1)
                Fleet(accounts(1), lambda t: FakeClient(t),
                      tmp_path / "w").record_progress({label: 4})
            return super().status(slug)

    polled = Fleet(accounts(1), lambda t: MidPollClient(t, "running"),
                   tmp_path / "w").poll_all()

    assert polled[0].workers[0].frames_done == 4
    assert f.load().workers[0].frames_done == 4


# ---------------------------------------------------------------------------
# The count a FINISHED render actually reached.
#
# frames_done is only ever advanced by the live SSE stream, so a render that
# finished while the app was closed kept whatever the stream last managed to
# save: a worker that had rendered both its frames read "1 / 2 saved 1h ago",
# and the card said "1 of 2 frames are waiting on Kaggle" about a render that
# was completely done. Kaggle's status API reports no frame count, but a
# COMPLETED kernel's log is fetchable -- see Fleet._read_final_frame_count.
# ---------------------------------------------------------------------------

def _finished_job(label="a0", frames=(1, 2), frames_done=1):
    """One tracked job whose single worker Kaggle will report as complete,
    carrying the stale count a closed window left behind."""
    return FleetState(
        job_id="j1", blend_name="shot.blend", start_frame=frames[0],
        end_frame=frames[-1],
        workers=[WorkerState(label=label, username="u0",
                             kernel_slug="u0/shot-render-1",
                             frames=list(frames), state="running",
                             frames_done=frames_done,
                             frames_done_at=1.0)])


class _LogClient(FakeClient):
    """A client whose kernel is complete and whose log can be fetched.

    Counts the fetches, because the whole point of final_count_checked is
    that this network call happens exactly once per worker however many
    times a finished job is polled.
    """

    fetches: list[str] = []

    def __init__(self, token, log="PROGRESS frame=1 ok=1 secs=3 done=1/2\n"
                                  "PROGRESS frame=2 ok=1 secs=3 done=2/2\n"):
        super().__init__(token, state="complete")
        self._log = log

    def fetch_log_tail(self, slug, dest, max_lines=200):
        _LogClient.fetches.append(slug)
        return self._log


def test_a_finished_workers_true_frame_count_is_read_from_its_log(tmp_path):
    _LogClient.fetches = []
    f = Fleet(accounts(1), _LogClient, tmp_path / "w")
    f.save_jobs([_finished_job()])

    jobs = f.poll_all()

    w = jobs[0].workers[0]
    assert w.frames_done == 2, (
        "the stale 1 must be replaced by the render's own final count")
    assert w.final_count_known is True
    assert f.load().workers[0].frames_done == 2, "and it must be persisted"


def test_the_final_count_is_read_once_and_never_re_fetched(tmp_path):
    """It is a real network call, and a finished job keeps being polled
    every 30 seconds for as long as it is tracked."""
    _LogClient.fetches = []
    f = Fleet(accounts(1), _LogClient, tmp_path / "w")
    f.save_jobs([_finished_job()])

    f.poll_all()
    f.poll_all()
    f.poll_all()

    assert len(_LogClient.fetches) == 1, _LogClient.fetches


def test_an_unfetchable_log_leaves_the_count_not_known_not_stale(tmp_path):
    """Never invent a reading, and never keep showing the stale one as if
    it were current: final_count_known stays False, which is what the
    payload turns into "not known"."""
    class Unfetchable(FakeClient):
        def __init__(self, token):
            super().__init__(token, state="complete")

        def fetch_log_tail(self, slug, dest, max_lines=200):
            raise RuntimeError("kaggle said no")

    f = Fleet(accounts(1), Unfetchable, tmp_path / "w")
    f.save_jobs([_finished_job()])

    w = f.poll_all()[0].workers[0]

    assert w.final_count_checked is True
    assert w.final_count_known is False
    assert w.state == "complete", (
        "a log that could not be read must not affect the kernel's state")


def test_a_log_with_no_progress_line_never_invents_a_count(tmp_path):
    _LogClient.fetches = []
    f = Fleet(accounts(1), lambda t: _LogClient(t, log="Blender quit\n"),
              tmp_path / "w")
    f.save_jobs([_finished_job()])

    w = f.poll_all()[0].workers[0]

    assert w.final_count_known is False
    assert w.frames_done == 1, (
        "the old value is left alone rather than zeroed -- but it is "
        "reported as not known, never as a count")


def test_reading_the_final_count_never_marks_the_worker_unreachable(tmp_path):
    """The STATUS call succeeded, so the startup check must keep counting
    this worker as answered -- otherwise a finished render is announced as
    one Kaggle could not be asked about."""
    class Unfetchable(FakeClient):
        def __init__(self, token):
            super().__init__(token, state="complete")

        def fetch_log_tail(self, slug, dest, max_lines=200):
            raise RuntimeError("kaggle said no")

    f = Fleet(accounts(1), Unfetchable, tmp_path / "w")
    f.save_jobs([_finished_job()])

    f.poll_all()

    assert f.unreachable_workers == {}


def test_a_worker_seen_running_again_may_have_its_final_count_re_read(tmp_path):
    """A "final" count read after a mistaken terminal poll describes a
    render that is still going, so the one-shot guard is released when
    Kaggle reports the kernel active again."""
    f = Fleet(accounts(1), lambda t: FakeClient(t, "running"), tmp_path / "w")
    job = _finished_job()
    job.workers[0].final_count_checked = True
    job.workers[0].final_count_known = True
    f.save_jobs([job])

    w = f.poll_all()[0].workers[0]

    assert w.final_count_checked is False and w.final_count_known is False


# ---------------------------------------------------------------------------
# The startup poll's wall clock.
#
# It asked Kaggle about every tracked worker one after another, so a
# reopened app spent about two minutes before it could say anything --
# 115887 ms on the connection indicator. Serial cost is the SUM of every
# account; the fan-out makes it the slowest one. See Fleet.POLL_FANOUT.
# ---------------------------------------------------------------------------

def test_the_poll_asks_the_accounts_concurrently(tmp_path):
    """Five accounts, each holding for a moment. Serial would take five
    times as long as one; concurrent takes about as long as one."""
    import threading

    barrier = threading.Barrier(5, timeout=10)

    class SlowClient(FakeClient):
        def __init__(self, token):
            super().__init__(token, state="running")

        def status(self, slug):
            # Only passes if all five are inside status() AT ONCE. A serial
            # poll deadlocks here and the timeout fails the test, which is
            # exactly the assertion.
            barrier.wait()
            return super().status(slug)

    f = Fleet(accounts(5), SlowClient, tmp_path / "w")
    f.save_jobs([FleetState(
        job_id="j1", blend_name="shot.blend", start_frame=1, end_frame=5,
        workers=[WorkerState(label=f"a{i}", username=f"u{i}",
                             kernel_slug=f"u{i}/shot-render-1",
                             frames=[i + 1], state="running")
                 for i in range(5)])])

    jobs = f.poll_all()

    assert [w.state for w in jobs[0].workers] == ["running"] * 5


def test_one_failing_account_does_not_block_or_break_the_others(tmp_path):
    """poll_all was already tolerant per worker; moving the calls into a
    pool must not quietly lose that."""
    class OneBadClient(FakeClient):
        def __init__(self, token):
            super().__init__(token, state="complete")

        def status(self, slug):
            if slug.startswith("u1/"):
                raise RuntimeError("kaggle timed out")
            return super().status(slug)

    f = Fleet(accounts(3), OneBadClient, tmp_path / "w")
    f.save_jobs([FleetState(
        job_id="j1", blend_name="shot.blend", start_frame=1, end_frame=3,
        workers=[WorkerState(label=f"a{i}", username=f"u{i}",
                             kernel_slug=f"u{i}/shot-render-1",
                             frames=[i + 1], state="running")
                 for i in range(3)])])

    jobs = f.poll_all()

    by_label = {w.label: w for w in jobs[0].workers}
    assert by_label["a0"].state == "complete"
    assert by_label["a2"].state == "complete"
    assert by_label["a1"].state == "running", (
        "an unreachable worker is left exactly as it was, never guessed at")
    assert "a1" in f.unreachable_workers
    assert "a0" not in f.unreachable_workers


def test_the_fan_out_is_bounded(tmp_path):
    """A fleet can be large, and fifty simultaneous TLS handshakes to
    kaggle.com is a self-inflicted rate limit."""
    import threading

    live = set()
    peak = []
    lock = threading.Lock()
    gate = threading.Event()

    class CountingClient(FakeClient):
        def __init__(self, token):
            super().__init__(token, state="running")

        def status(self, slug):
            with lock:
                live.add(threading.current_thread().name)
                peak.append(len(live))
            # Held only until the pool is demonstrably full, so the test
            # never depends on timing to observe the ceiling.
            if len(peak) >= POLL_FANOUT:
                gate.set()
            gate.wait(5)
            with lock:
                live.discard(threading.current_thread().name)
            return super().status(slug)

    n = POLL_FANOUT + 6
    f = Fleet(accounts(n), CountingClient, tmp_path / "w")
    f.save_jobs([FleetState(
        job_id="j1", blend_name="shot.blend", start_frame=1, end_frame=n,
        workers=[WorkerState(label=f"a{i}", username=f"u{i}",
                             kernel_slug=f"u{i}/shot-render-1",
                             frames=[i + 1], state="running")
                 for i in range(n)])])

    f.poll_all()

    assert max(peak) <= POLL_FANOUT, max(peak)


def test_one_account_gets_one_client_however_many_workers_it_has(tmp_path):
    """Building a client authenticates, and that handshake is serialised
    process-wide by kaggle_client._ENV_TOKEN_LOCK -- so a client per WORKER
    paid it once per worker and queued them all behind the same lock."""
    built = []

    def factory(token):
        built.append(token)
        return FakeClient(token, state="running")

    f = Fleet(accounts(2), factory, tmp_path / "w")
    f.save_jobs([
        FleetState(job_id="j1", blend_name="a.blend", start_frame=1,
                   end_frame=2,
                   workers=[WorkerState(label="a0", username="u0",
                                        kernel_slug="u0/a-render-1",
                                        frames=[1], state="running"),
                            WorkerState(label="a1", username="u1",
                                        kernel_slug="u1/a-render-1",
                                        frames=[2], state="running")]),
        FleetState(job_id="j2", blend_name="b.blend", start_frame=1,
                   end_frame=2,
                   workers=[WorkerState(label="a0", username="u0",
                                        kernel_slug="u0/b-render-1",
                                        frames=[1], state="running")]),
    ])

    f.poll_all()

    assert len(built) == 2, (
        f"three workers across two accounts must build two clients: {built}")
