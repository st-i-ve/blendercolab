"""Fleet orchestration: split frames across accounts and drive each one.

Coordination is entirely client-side. Each account gets a DISJOINT stride of
frames (via assignment.assign_frames). Task 3 changed how the .blend gets to
Kaggle: dataset sharing turned out to be automatable
(ApiUpdateDatasetMetadataRequest.settings.collaborators, see
blendfleet/sharing.py), so the FIRST account (accounts[0], "the owner")
uploads the .blend exactly once, every other account's username is granted
READER on that one dataset, and every worker's kernel references the
owner's dataset slug -- N accounts no longer means N uploads. Accounts
never talk to each other directly; this module just fans work out, grants
access up front, and polls each account independently.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable

from blendfleet import sharing
from blendfleet.accounts import Account
from blendfleet.assignment import assign_frames
from blendfleet.dataset_sync import sync_blend
from blendfleet.notebook_builder import RenderSettings, build
from blendfleet.platform_paths import state_dir

STATE_FILE = "fleet.json"


@dataclass
class WorkerState:
    label: str
    username: str
    kernel_slug: str
    frames: list[int]
    state: str = "queued"
    frames_done: int = 0
    message: str = ""


@dataclass
class FleetState:
    job_id: str
    blend_name: str
    start_frame: int
    end_frame: int
    workers: list[WorkerState] = field(default_factory=list)


@dataclass
class CancelResult:
    """Per-account outcome of cancel_all(). `ok=False` means a kernel may
    still be running on somebody else's account and burning their quota."""
    label: str
    kernel_slug: str
    ok: bool
    error: str = ""


class FleetBusyError(RuntimeError):
    """A job with live kernels is already running.

    State is a single slot on disk, so launching over the top of a live job
    would orphan its kernels: nothing left on disk to cancel or collect them
    with, while they keep spending other people's GPU quota.
    """


class UnreachableAccountsError(RuntimeError):
    """A friend was granted READER but still can't reach the dataset.

    Raised BEFORE any kernel is pushed -- nothing has been started or spent
    yet. Without this check, a friend whose grant didn't actually take
    (propagation delay, a role that got silently dropped, etc.) would only
    find out when their kernel fails at run time with an opaque "dataset not
    found", long after their GPU quota started ticking.
    """


class Fleet:
    def __init__(self, accounts: list[Account],
                 client_factory: Callable, work_dir: Path) -> None:
        self.accounts = accounts
        self.client_factory = client_factory
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def _state_path(self) -> Path:
        return state_dir() / STATE_FILE

    def _save(self, st: FleetState) -> None:
        self._state_path().write_text(json.dumps(asdict(st), indent=2),
                                      encoding="utf-8")

    def load(self) -> FleetState | None:
        p = self._state_path()
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        d["workers"] = [WorkerState(**w) for w in d["workers"]]
        return FleetState(**d)

    def active_workers(self) -> list[WorkerState]:
        """Workers whose kernel Kaggle currently reports as queued/running.

        A worker whose status cannot be fetched (account removed, token
        revoked, network down) counts as NOT active: an unreachable account
        must never wedge the app into a state where no new render can start.
        """
        st = self.load()
        if st is None:
            return []
        by_label = {a.label: a for a in self.accounts}
        live: list[WorkerState] = []
        for w in st.workers:
            acct = by_label.get(w.label)
            if acct is None:
                continue
            try:
                if self.client_factory(acct.token).status(w.kernel_slug).is_active:
                    live.append(w)
            except Exception:
                continue
        return live

    def launch(self, blend: Path, settings: RenderSettings,
               start_frame: int, end_frame: int) -> FleetState:
        if not self.accounts:
            raise ValueError("add at least one account before launching")

        # Single-slot state file: launching over a live job would overwrite
        # the only record of the running kernels, leaving them uncancellable
        # and uncollectable while they spend other people's GPU quota.
        busy = self.active_workers()
        if busy:
            raise FleetBusyError(
                "a render is still running on: "
                + ", ".join(f"{w.label} ({w.kernel_slug})" for w in busy)
                + ". Cancel it before starting another job, or its kernels "
                  "would keep running with no way to stop them from here.")

        job_id = uuid.uuid4().hex[:8]
        buckets = assign_frames(start_frame, end_frame, len(self.accounts))
        stem = blend.stem.lower().replace("_", "-")
        dataset_name = f"{stem}-blend"

        # Resolve a client + username for every account up front: needed
        # for the push loop below regardless, and for the grant/verify
        # step that has to happen before it.
        clients: dict[str, object] = {}
        usernames: dict[str, str] = {}
        for account in self.accounts:
            client = self.client_factory(account.token)
            clients[account.label] = client
            usernames[account.label] = account.username or client.whoami()

        owner = self.accounts[0]
        owner_client = clients[owner.label]
        owner_username = usernames[owner.label]
        dataset_slug = f"{owner_username}/{dataset_name}"

        # One upload, shared by every account (Task 3) -- dataset sharing is
        # automatable, so N accounts no longer means N uploads.
        sync_blend(owner_client, blend, dataset_slug,
                   self.work_dir / "ds_owner")

        friends = self.accounts[1:]
        friend_usernames = [usernames[a.label] for a in friends]
        if friend_usernames:
            sdk = owner_client._sdk_factory(owner_client.token)
            current = sharing.get_settings(sdk, owner_username, dataset_name)
            sharing.grant_readers(sdk, owner_username, dataset_name,
                                  friend_usernames, current)

            # Verify access actually landed, not just that the write
            # returned cleanly -- see UnreachableAccountsError. Deliberately
            # dataset_reachable(), NOT dataset_exists(): dataset_exists()
            # is built on dataset_status(), which was measured live to 404
            # for a non-owner account even with a genuine READER grant (see
            # task-3-report.md) -- it only reflects datasets an account
            # owns, so it would refuse every shared launch here.
            unreachable = [usernames[a.label] for a in friends
                          if not clients[a.label].dataset_reachable(dataset_slug)]
            if unreachable:
                raise UnreachableAccountsError(
                    "granted READER access but the dataset is still not "
                    f"reachable for: {', '.join(unreachable)}. Nothing has "
                    "been started -- retry once Kaggle's grant has "
                    "propagated.")

        st = FleetState(job_id=job_id, blend_name=blend.name,
                        start_frame=start_frame, end_frame=end_frame,
                        workers=[])

        # push_kernel ALWAYS starts a run, so a kernel that has been pushed is
        # already spending quota. Persist after EVERY push -- not once at the
        # end -- so a failure part-way through (revoked token on account 3)
        # still leaves accounts 1 and 2 on disk, cancellable and collectable.
        try:
            for account, frames in zip(self.accounts, buckets):
                client = clients[account.label]
                username = usernames[account.label]
                kernel_slug = f"{username}/{stem}-render-{job_id}"

                kern_dir = self.work_dir / f"kern_{account.label}"
                build(frames, settings, dataset_slug, kern_dir, kernel_slug)
                client.push_kernel(kern_dir)

                st.workers.append(WorkerState(
                    label=account.label, username=username,
                    kernel_slug=kernel_slug, frames=frames))
                self._save(st)
        finally:
            # Belt and braces: covers an exception raised between the append
            # and the save above. Skipped while no kernel has been pushed
            # yet -- nothing is running, so the previous job's state (which
            # the user may still want to collect) is left alone.
            if st.workers:
                self._save(st)
        return st

    def poll(self) -> FleetState | None:
        st = self.load()
        if st is None:
            return None
        by_label = {a.label: a for a in self.accounts}
        for w in st.workers:
            acct = by_label.get(w.label)
            if acct is None:
                continue
            s = self.client_factory(acct.token).status(w.kernel_slug)
            w.state, w.message = s.state, s.message
        self._save(st)
        return st

    def cancel_all(self) -> list[CancelResult]:
        """Cancel every worker, reporting the outcome for each.

        Failures are RETURNED, never swallowed: "I clicked cancel and nothing
        happened" must not look identical to success when the difference is
        hours of somebody else's GPU quota.
        """
        st = self.load()
        if st is None:
            return []
        by_label = {a.label: a for a in self.accounts}
        results: list[CancelResult] = []
        for w in st.workers:
            acct = by_label.get(w.label)
            if not acct:
                results.append(CancelResult(
                    w.label, w.kernel_slug, False,
                    "no account with this label is configured any more, so "
                    "there is no token to cancel it with"))
                continue
            try:
                ok = self.client_factory(acct.token).cancel(w.kernel_slug)
            except Exception as e:
                # One account's factory/cancel failing must not strand the
                # rest -- an uncancelled kernel keeps burning GPU quota for
                # hours, so every other worker still gets its shot.
                results.append(CancelResult(w.label, w.kernel_slug, False, str(e)))
                continue
            results.append(CancelResult(
                w.label, w.kernel_slug, bool(ok),
                "" if ok else "Kaggle rejected the cancel request"))
        return results
