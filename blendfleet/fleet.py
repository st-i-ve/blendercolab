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
import re
import unicodedata
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable

from blendfleet import sharing
from blendfleet.accounts import Account
from blendfleet.assignment import assign_frames
from blendfleet.dataset_sync import sync_blend
from blendfleet.kaggle_client import ACTIVE_STATES
from blendfleet.notebook_builder import RenderSettings, build
from blendfleet.platform_paths import state_dir

STATE_FILE = "fleet.json"

# Kaggle slugs (dataset AND kernel) accept lowercase letters, digits and
# dashes -- nothing else. A .blend called "big buck bunny.blend" used to be
# lower()ed with "_"->"-" and nothing more, producing
# "user/big buck bunny-blend", which Kaggle rejects. Worse, the rejection
# arrived from dataset_create -- i.e. AFTER the whole .blend had been
# uploaded -- so the user waited out a full 60 MB upload to be told the
# name was wrong. Everything below runs before a single byte is sent.
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")

# Kaggle rejects very short slugs, and the stem is only part of what gets
# built from it ("<stem>-blend", "<stem>-render-<8 hex>"), so it is also
# capped well under Kaggle's ~50 character slug limit rather than letting a
# long filename fail at the same late, post-upload moment.
MIN_STEM_LENGTH = 3
MAX_STEM_LENGTH = 30


class InvalidBlendNameError(ValueError):
    """The .blend's filename cannot be turned into a usable Kaggle slug.

    Raised BEFORE the upload starts (see slug_stem), because the whole
    point is that the user finds out in a second rather than after a
    60 MB upload has run to completion and been rejected.
    """


def slugify_stem(name: str) -> str:
    """Reduce `name` to the [a-z0-9-] alphabet Kaggle slugs allow.

    Accents are folded to their ASCII base ("Ünïcödé" -> "unicode") rather
    than dropped outright, so an accented filename still produces a
    recognisable slug. Every remaining run of disallowed characters --
    spaces, punctuation, underscores, emoji, CJK -- collapses to a single
    dash, and leading/trailing dashes are stripped. May legitimately
    return "" (e.g. a name that is entirely CJK or punctuation); it is
    slug_stem's job to refuse that, not this function's.
    """
    folded = (unicodedata.normalize("NFKD", name)
              .encode("ascii", "ignore").decode("ascii"))
    return _SLUG_STRIP_RE.sub("-", folded.lower()).strip("-")


def slug_stem(blend: Path) -> str:
    """The validated slug stem for `blend`, or raise InvalidBlendNameError.

    Called at the very top of launch(), before any upload, so an unusable
    filename costs the user a dialog rather than a completed upload.
    """
    stem = slugify_stem(Path(blend).stem)[:MAX_STEM_LENGTH].strip("-")
    if len(stem) < MIN_STEM_LENGTH:
        raise InvalidBlendNameError(
            f"the file name {Path(blend).name!r} cannot be turned into a "
            "Kaggle dataset name. Kaggle only accepts lowercase letters, "
            "digits and dashes, and after removing everything else there "
            f"were fewer than {MIN_STEM_LENGTH} characters left. Rename the "
            "file to something like 'big-buck-bunny.blend' and try again -- "
            "nothing has been uploaded.")
    return stem


def _require_matching_dataset(client, username: str, slug: str,
                              filename: str, expected_size: int) -> None:
    """Raise StaleDatasetError unless `client`'s own view of `filename`
    inside dataset `slug` is exactly `expected_size` bytes.

    Two distinct failures get two distinct messages, deliberately -- see
    Task 5's brief: a MISSING file (the listing has no entry for this name
    at all) means access hasn't fully propagated or the file was never
    shared -- the fix is to re-share/retry. A SIZE MISMATCH means the
    dataset the account can see is real but is a different, stale upload --
    the fix is to re-upload the current .blend. Telling a user to
    "re-upload" when the real problem is a propagation delay (or vice
    versa) sends them chasing the wrong fix.

    Kaggle exposes no content hash on the installed SDK (see
    KaggleClient.dataset_file_size's docstring) -- this is a size check,
    and both messages say so plainly rather than implying a byte-for-byte
    comparison that was never actually performed.
    """
    remote_size = client.dataset_file_size(slug, filename)
    if remote_size is None:
        raise StaleDatasetError(
            f"{username} can reach dataset {slug!r}, but Kaggle's file "
            f"listing for it has no file named {filename!r} at all. "
            "Nothing has been started. This is not a stale copy -- the "
            "file simply is not there for this account yet, most likely "
            "because the READER grant has not finished propagating. "
            "Re-share the dataset with this account (or just retry once "
            "Kaggle has caught up) and launch again.")
    if remote_size != expected_size:
        raise StaleDatasetError(
            f"{username}'s copy of {filename!r} is {remote_size} bytes on "
            f"Kaggle, but the local file about to be rendered is "
            f"{expected_size} bytes. Nothing has been started. Kaggle "
            "exposes no content hash for dataset files, so this is a size "
            "check, not a byte-for-byte comparison -- but a different size "
            "means this account is looking at a STALE copy left over from "
            "an earlier upload, not the scene you are about to render. "
            "Re-upload the current .blend (just launch again -- the owner "
            "always re-uploads) so every account sees the same, current "
            "copy before retrying.")


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


class StaleDatasetError(RuntimeError):
    """An account's visible copy of the .blend does not match the one about
    to be rendered.

    dataset_reachable() (see UnreachableAccountsError) proves an account can
    see A copy of the dataset -- not that it is the RIGHT one. A stale copy
    left over from an earlier upload passes that check just as well as a
    current one, and the render then quietly produces the wrong scene --
    discovered only from the output, hours and quota later.

    Raised BEFORE any kernel is pushed for ANY account: a stale copy on one
    account must never let the others start rendering against it while the
    user is still being told about the first one. Covers both the owner's
    own just-uploaded copy (checked right after sync_blend, before a single
    friend is even granted access) and every friend's shared view of it
    (checked right after dataset_reachable, before push_kernel).
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
               start_frame: int, end_frame: int,
               on_progress: Callable | None = None) -> FleetState:
        """Launch a render across every configured account.

        `on_progress`, if given, is threaded straight through to
        dataset_sync.sync_blend -> KaggleClient.dataset_create/version ->
        blendfleet.uploader.upload_file, and is called with UploadProgress
        ticks as the owner's .blend upload proceeds -- this is how a caller
        (the dashboard's upload view) shows real upload progress instead of
        the UI thread blocking silently for however long a 60+ MB PUT takes.
        """
        if not self.accounts:
            raise ValueError("add at least one account before launching")

        # Validate the name FIRST -- before the busy check, before the
        # upload, before anything that costs time or quota. An unusable
        # filename used to surface as a Kaggle 400 from dataset_create,
        # i.e. only after the entire .blend had finished uploading.
        stem = slug_stem(blend)
        dataset_name = f"{stem}-blend"

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
                   self.work_dir / "ds_owner", on_progress=on_progress)

        # Confirm the upload that just happened actually landed as the
        # right content -- the remote-side counterpart of dataset_sync.py's
        # own local staging-size check. dataset_reachable()/dataset_exists()
        # only prove the dataset is THERE; they say nothing about whether
        # it's the file that was just uploaded versus a stale one from an
        # earlier job with the same slug. Runs before a single friend is
        # granted access or a single kernel is pushed -- in the no-friends
        # ("per-account") case this is the ENTIRE verification, since there
        # is nobody else to share with. See StaleDatasetError.
        expected_size = blend.stat().st_size
        _require_matching_dataset(owner_client, owner_username, dataset_slug,
                                  blend.name, expected_size)

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

            # Reachable proves a friend can see A copy -- not that it is
            # the SAME copy just verified above for the owner. Each
            # friend's OWN client is asked, in case Kaggle's read-side
            # replication genuinely disagrees between accounts (the same
            # kind of propagation lag dataset_reachable already accounts
            # for). Still strictly before push_kernel: nothing started.
            for account in friends:
                _require_matching_dataset(
                    clients[account.label], usernames[account.label],
                    dataset_slug, blend.name, expected_size)

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

    def cancel_worker(self, label: str) -> CancelResult | None:
        """Cancel exactly the worker labelled `label`, leaving every other
        worker running untouched -- the per-instance counterpart to
        cancel_all() above. Kaggle's own unit of control is a whole
        session, not an individual GPU: there is no way to release one
        GPU and keep the other within a session, so stopping this one
        account's session is the actual, closest equivalent to "stop
        rendering on just this account" -- callers should word it that
        way, never as "release this GPU".

        Returns None -- NOT a failure -- when there is no job at all, no
        worker under this label, or that worker is not currently active
        (kaggle_client.ACTIVE_STATES: "queued"/"running"). A worker that
        has already finished (complete/error/cancelled) or never started
        has nothing running to stop, so nothing is attempted and nobody's
        quota is at risk either way -- cancelling it again is a no-op, not
        an error.

        A CancelResult -- reusing cancel_all()'s own per-account reporting
        rather than a second mechanism -- is returned only once an actual
        cancel request was made, so a FAILED cancel is reported exactly
        like a failed cancel_all() entry, never silently: the whole point
        of this method is stopping somebody's GPU quota from draining, and
        a cancel that quietly does nothing defeats that just as badly here
        as it would in cancel_all().
        """
        st = self.load()
        if st is None:
            return None
        worker = next((w for w in st.workers if w.label == label), None)
        if worker is None or worker.state not in ACTIVE_STATES:
            return None
        acct = next((a for a in self.accounts if a.label == label), None)
        if acct is None:
            return CancelResult(
                label, worker.kernel_slug, False,
                "no account with this label is configured any more, so "
                "there is no token to cancel it with")
        try:
            ok = self.client_factory(acct.token).cancel(worker.kernel_slug)
        except Exception as e:
            # Exactly cancel_all()'s own handling: this account's cancel
            # failing must be reported, not raised past the caller.
            return CancelResult(label, worker.kernel_slug, False, str(e))
        return CancelResult(
            label, worker.kernel_slug, bool(ok),
            "" if ok else "Kaggle rejected the cancel request")

    def fetch_failure_log(self, label: str) -> str:
        """The tail of `label`'s kernel log -- the actual cause of a
        failure when kernels_status's own failure_message (surfaced as
        WorkerState.message by poll()) came back empty. See
        kaggle_client.KaggleClient.fetch_log_tail for what this
        downloads and why.

        Deliberately restricted to a worker Kaggle has reported as
        "error": this is a real network call, and the whole reason it is
        a method of its own -- rather than something poll() does for
        every worker on every 30s tick -- is that it must fire only for a
        worker that has actually failed, once, not on the healthy-worker
        polling path. Returns "" (not an error) for no job, no such
        worker, or a worker that is not (or no longer) in "error": there
        is genuinely nothing to fetch.
        """
        st = self.load()
        if st is None:
            return ""
        worker = next((w for w in st.workers if w.label == label), None)
        if worker is None or worker.state != "error":
            return ""
        acct = next((a for a in self.accounts if a.label == label), None)
        if acct is None:
            raise RuntimeError(
                f"no account with label {label!r} is configured any more, "
                "so there is no token to fetch its kernel log with")
        client = self.client_factory(acct.token)
        return client.fetch_log_tail(worker.kernel_slug,
                                     self.work_dir / f"log_{label}")
