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
import time
import unicodedata
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable

from blendfleet import sharing
from blendfleet.accounts import Account
from blendfleet.assignment import assign_frames
from blendfleet.dataset_sync import sync_blend
from blendfleet.kaggle_client import ACTIVE_STATES, RevokedTokenError
from blendfleet.notebook_builder import RenderSettings, build, build_probe
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


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file and replace, so an interrupted save can
    never leave a truncated one behind.

    A half-written state or accounts file reads back as "Expecting value:
    line 1 column 1 (char 0)" from somewhere unrelated -- os.replace is
    atomic on both Windows and POSIX, so the file on disk is only ever the
    old contents or the new.
    """
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


_BAD_COLLABORATOR_RE = re.compile(
    r"collaborator usernames don't exist:\s*(.+)", re.IGNORECASE)


def _explain_bad_collaborators(error: Exception, friends: list,
                               usernames: dict) -> Exception:
    """Turn Kaggle's collaborator rejection into something actionable.

    Kaggle names the handle it did not recognise but has no idea which of
    YOUR accounts carries it, so the message is matched back to the account
    label the user actually sees in the fleet. Anything that is not this
    specific rejection is returned unchanged -- guessing at unrelated
    failures would be worse than passing them through.
    """
    match = _BAD_COLLABORATOR_RE.search(str(error))
    if not match:
        return error
    named = {name.strip().strip('"\'')
             for name in match.group(1).replace(",", " ").split()}
    culprits = [f"{a.label} (set to {usernames[a.label]!r})"
                for a in friends if usernames.get(a.label) in named]
    who = ", ".join(culprits) if culprits else ", ".join(sorted(named))
    return WrongUsernameError(
        f"Kaggle does not recognise the username on {who}. The scene "
        "uploaded fine -- only sharing failed, so nothing is rendering and "
        "no quota has been spent. Fix it under Instances -> Set username "
        "(the name in that account's profile URL, kaggle.com/<username>), "
        "or remove the account if it was a placeholder.")


@dataclass
class WorkerState:
    label: str
    username: str
    kernel_slug: str
    frames: list[int]
    state: str = "queued"
    frames_done: int = 0
    message: str = ""
    # Epoch seconds. Both default to 0.0, meaning "not recorded" -- which
    # is also what a state file written before these existed will load as,
    # so an in-flight job survives the upgrade instead of reporting a
    # 56-year render. Set at push, and once the kernel reaches a terminal
    # state; NEVER derived from "now" at display time, or a finished job
    # would keep ageing every time the dashboard repainted.
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass
class FleetState:
    job_id: str
    blend_name: str
    start_frame: int
    end_frame: int
    workers: list[WorkerState] = field(default_factory=list)
    # When the fleet-wide job began, for "finished in 5:20". 0.0 means a
    # job started before this was recorded.
    started_at: float = 0.0

    @property
    def scene_key(self) -> str:
        """This job's scene, as a filesystem- and slug-safe stem.

        Used for the output folder and as the job's identity in the UI.
        Derived from blend_name rather than stored, so it cannot drift
        from the scene actually being rendered.
        """
        return slugify_stem(Path(self.blend_name).stem) or "scene"


@dataclass
class CancelResult:
    """Per-account outcome of cancel_all(). `ok=False` means a kernel may
    still be running on somebody else's account and burning their quota."""
    label: str
    kernel_slug: str
    ok: bool
    error: str = ""


class FleetBusyError(RuntimeError):
    """One of the accounts a launch actually asked for is already
    rendering something, in some tracked job -- not just the most recent
    one. See Fleet.require_free().

    Scoped to the REQUESTED accounts, not to "any job is live anywhere":
    two kernels from the same account rendering the same job's frames
    would spend that account's quota twice for the same output, which is
    the only thing this guard exists to prevent. A different account
    being busy with an unrelated scene must not refuse this launch, or
    two scenes could never render at once.
    """


class WrongUsernameError(RuntimeError):
    """A stored Kaggle username is not what Kaggle says that account is.

    Raised BEFORE any sharing is attempted. Kaggle's own answer --
    'The following collaborator usernames don't exist: "james"' -- arrives
    only after the whole .blend has been uploaded, and reads as if the app
    had invented the name, so this catches it first and says which account
    and where to fix it.
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
        # Raw (unparsed) job entries the last load_jobs() call could not
        # reconstruct into a FleetState -- e.g. a hand-edited or
        # partially-upgraded file. Kept verbatim, and re-included by
        # save_jobs() on every write, precisely so that a load-then-save
        # round trip (which is what _save/poll/forget_job all do) cannot
        # silently ERASE the one thing a user would need to go cancel that
        # job's kernels by hand at kaggle.com. See load_jobs().
        self.unreadable_jobs: list = []
        # label -> why, for every account prepare_dataset() could not
        # share the most recent upload with. Only ever populated for
        # accounts OUTSIDE that call's `required` list -- sharing with
        # them is an optimisation, not something the launch needed, so
        # this is a report for the caller to surface, not an error.
        # Overwritten (not accumulated) on every prepare_dataset() call.
        self.unshared_accounts: dict[str, str] = {}

    def _state_path(self) -> Path:
        return state_dir() / STATE_FILE

    def save_jobs(self, jobs: list[FleetState]) -> None:
        """Persist every tracked job, oldest first.

        Also re-writes self.unreadable_jobs verbatim, alongside the parsed
        jobs -- a raw entry load_jobs() could not parse must never be
        dropped just because something else on the fleet triggered a save
        (poll() runs on an unattended 30s timer). Losing it here would
        permanently destroy the only record of that job's kernel_slugs,
        the one thing a user needs to go cancel them by hand at
        kaggle.com.

        Written atomically for the same reason as before: a half-written
        state file reads back as an unrelated error from wherever it is
        next parsed, and with two jobs it would now orphan twice as many
        running kernels.
        """
        entries = [asdict(j) for j in jobs] + list(self.unreadable_jobs)
        _atomic_write(self._state_path(),
                      json.dumps({"jobs": entries}, indent=2))

    def load_jobs(self) -> list[FleetState]:
        """Every tracked job that could be parsed. Empty when there is
        nothing running.

        Tolerates the pre-multi-job format -- one FleetState at the top
        level -- because an in-flight render must survive the upgrade;
        dropping it would orphan kernels that are running right now.

        An entry that fails to parse is skipped here (one bad entry must
        not hide every other job) but is never discarded: it is recorded
        verbatim on self.unreadable_jobs, which save_jobs() carries
        through on every subsequent write, so it survives round trips
        instead of being erased the next time anything saves.
        """
        self.unreadable_jobs = []
        p = self._state_path()
        if not p.exists():
            return []
        raw = p.read_text(encoding="utf-8").strip()
        if not raw:
            # An empty state file is what a half-written save leaves
            # behind, and json.loads answers it with "Expecting value:
            # line 1 column 1 (char 0)" -- which surfaced as an unrelated
            # upload failure. No tracked jobs is the truthful reading, and
            # it is also the recoverable one.
            return []
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return []
        raw_jobs = d.get("jobs") if isinstance(d, dict) and "jobs" in d else [d]
        if not isinstance(raw_jobs, list):
            # {"jobs": null} / {"jobs": 5} / etc: not a shape this format
            # has ever produced. Every other malformed shape above already
            # degrades to "no tracked jobs" rather than raising; this one
            # must too, not escape as an uncaught TypeError from the loop
            # below.
            return []
        jobs = []
        for entry in raw_jobs:
            try:
                parsed = dict(entry)
                parsed["workers"] = [WorkerState(**w)
                                     for w in parsed.get("workers", [])]
                jobs.append(FleetState(**parsed))
            except (TypeError, ValueError):
                # Preserved, not dropped -- see self.unreadable_jobs above.
                self.unreadable_jobs.append(entry)
        return jobs

    def load(self) -> FleetState | None:
        """The most recent job, or None.

        Kept because every existing caller uses it. "Most recent" rather
        than "the only one" -- with two jobs live, an arbitrary pick would
        be a silent bug.
        """
        jobs = self.load_jobs()
        return jobs[-1] if jobs else None

    def _save(self, st: FleetState) -> None:
        """Replace `st` among the tracked jobs, matched by job_id, IN
        PLACE -- appending only if no job with this id exists yet.

        load() answers with the LAST job in the list, and every one of
        active_workers/poll/cancel_all/cancel_worker/fetch_failure_log and
        forget_job()'s default all go through load(). Re-appending a
        re-saved job (rather than replacing it where it already sits)
        would silently move it to the end and repoint every one of those
        at the wrong job the moment more than one job is tracked at once.
        """
        jobs = self.load_jobs()
        for i, j in enumerate(jobs):
            if j.job_id == st.job_id:
                jobs[i] = st
                break
        else:
            jobs.append(st)
        self.save_jobs(jobs)

    def forget_job(self, job_id: str | None = None) -> list[WorkerState]:
        """Drop one tracked job WITHOUT stopping anything on Kaggle.

        The escape hatch for a genuine deadlock: Kaggle reports a kernel as
        still active but refuses the cancel request, so cancel_all() cannot
        clear it and launch() keeps refusing because a job is "still
        running". Without this the app is wedged with no way out but
        editing state by hand.

        `job_id` defaults to the most recent job, matching load()'s own
        "most recent" answer -- but any job_id may be named explicitly, so
        forgetting one stuck job does not force forgetting a different one
        that is fine.

        Drops exactly ONE job by position, not by filtering on job_id:
        job_id is only 32 bits of uuid4, and save_jobs() does not itself
        forbid a duplicate, so matching by equality could silently drop
        two jobs for the price of one.

        This is deliberately NOT a cancel and must never be worded as one.
        Whatever is running on Kaggle keeps running, and keeps spending
        quota; all that changes is that this app stops tracking it -- which
        also means it can no longer cancel or collect from those kernels.
        The caller is responsible for saying so plainly and pointing at
        kaggle.com, and the returned workers are exactly what it has
        stopped being able to reach.
        """
        jobs = self.load_jobs()
        if not jobs:
            return []
        if job_id is None:
            index = len(jobs) - 1
        else:
            index = next((i for i, j in enumerate(jobs)
                         if j.job_id == job_id), None)
        if index is None:
            return []
        target = jobs.pop(index)
        self.save_jobs(jobs)
        return list(target.workers)

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

    def busy_labels(self) -> set[str]:
        """Accounts with a worker still active in some tracked job.

        Active means ACTIVE_STATES (queued/running) -- a completed job
        holds nobody, so the fleet is reusable the moment its frames are
        done rather than when the user gets round to collecting.
        """
        return {w.label for j in self.load_jobs() for w in j.workers
                if w.state in ACTIVE_STATES}

    def free_accounts(self) -> list[Account]:
        """Configured accounts not currently rendering anything."""
        busy = self.busy_labels()
        return [a for a in self.accounts if a.label not in busy]

    def require_free(self, accounts: list[Account]) -> None:
        """Raise FleetBusyError if any of `accounts` is already rendering.

        Scoped to the accounts actually being asked for: refusing every
        launch while ANY job is live is what made two scenes impossible,
        but launching a second kernel on an account that is already
        rendering would spend its quota twice for the same output.
        """
        busy = {}
        for j in self.load_jobs():
            for w in j.workers:
                if w.state in ACTIVE_STATES:
                    busy[w.label] = j.blend_name
        clash = [(a.label, busy[a.label]) for a in accounts
                 if a.label in busy]
        if clash:
            detail = "; ".join(f"{label} is rendering {scene}"
                               for label, scene in clash)
            raise FleetBusyError(
                f"these accounts are already busy: {detail}. Nothing has "
                "been started. Wait for that render to finish, cancel it, "
                "choose different accounts for this scene, or -- if Kaggle "
                "is refusing the cancel, which does happen -- use 'Stop "
                "tracking job' so this app stops refusing on its account.")

    def _resolve_clients(
            self, accounts: list[Account] | None = None) -> tuple[dict, dict]:
        """A client and a Kaggle username for every account in `accounts`
        (every configured account when omitted).

        One network call per account (whoami), so callers that need both a
        dataset step and a push step resolve once and hand the result down
        rather than paying for it twice. Scoped to `accounts` rather than
        always resolving self.accounts, so a launch restricted to a subset
        does not pay for (or spuriously fail on) an account it never asked
        for.
        """
        accounts = accounts if accounts is not None else self.accounts
        clients: dict[str, object] = {}
        usernames: dict[str, str] = {}
        for account in accounts:
            client = self.client_factory(account.token)
            clients[account.label] = client
            try:
                usernames[account.label] = (account.username
                                            or client.whoami())
            except RevokedTokenError:
                # A dead token is not a transient failure and not something
                # the user can retry past, so the account is marked here --
                # this is the one code path every launch, dataset step and
                # quota refresh goes through, which makes it the only place
                # guaranteed to notice. The error still propagates: nothing
                # may proceed as if this account were usable.
                account.verified = False
                account.revoked = True
                raise
        return clients, usernames

    def _resolve_clients_tolerant(
            self, accounts: list[Account]) -> tuple[dict, dict]:
        """Best-effort counterpart to _resolve_clients(): an account whose
        client cannot even be built, or whose whoami() fails, is simply
        left out of the returned dicts rather than raising.

        Used only for accounts a caller does NOT strictly need -- see
        prepare_dataset's `required` -- where sharing with them is purely
        an optimisation (a later launch on that account needs no
        re-upload), so a dead account there must not block whatever this
        resolution was actually for.
        """
        clients: dict[str, object] = {}
        usernames: dict[str, str] = {}
        for account in accounts:
            try:
                client = self.client_factory(account.token)
                username = account.username or client.whoami()
            except RevokedTokenError:
                # Same bookkeeping as _resolve_clients() -- this account
                # really is dead and the rest of the app should know that
                # too -- just without re-raising past an optional account.
                account.verified = False
                account.revoked = True
                continue
            except Exception:
                continue
            clients[account.label] = client
            usernames[account.label] = username
        return clients, usernames

    def dataset_slug_for(self, blend: Path, owner_username: str) -> str:
        """Where `blend` lives on Kaggle once uploaded. Pure -- no network,
        so the UI can show the destination before anything is sent."""
        return f"{owner_username}/{slug_stem(blend)}-blend"

    BLENDER_DATASET_PREFIX = "blender"

    def blender_dataset_name(self, version: str) -> str:
        """`blender-5-2-0-linux` for 5.2.0 -- Kaggle slugs take no dots."""
        return f"{self.BLENDER_DATASET_PREFIX}-{version.replace('.', '-')}-linux"

    def ensure_blender_dataset(self, tarball: Path, version: str,
                               on_progress: Callable | None = None,
                               on_stage: Callable | None = None,
                               *, clients: dict | None = None,
                               usernames: dict | None = None) -> str:
        """Put Blender itself on Kaggle, once, and share it with everyone.

        A Kaggle session has NO outbound network unless the account is
        phone-verified -- enable_internet is accepted and silently ignored,
        and DNS does not even resolve (measured on a real session). So the
        notebook cannot download Blender, and every render this app started
        died in that cell without producing a frame.

        An attached dataset needs no network at all. It also starts in
        seconds rather than minutes, and works on an unverified account --
        which is what matters for a fleet of friends' accounts, since the
        alternative is asking every one of them to phone-verify.

        Uploaded once and reused: the tarball is ~300 MB and does not
        change between renders, so this checks Kaggle first and skips.
        """
        if clients is None or usernames is None:
            clients, usernames = self._resolve_clients()
        owner = self.accounts[0]
        owner_client = clients[owner.label]
        owner_username = usernames[owner.label]
        name = self.blender_dataset_name(version)
        slug = f"{owner_username}/{name}"

        def stage(key: str, detail: str = "") -> None:
            if on_stage is not None:
                on_stage(key, detail)

        expected = tarball.stat().st_size
        stage("blender-checking", slug)
        try:
            present = (owner_client.dataset_file_size(slug, tarball.name)
                       == expected)
        except Exception:
            present = False

        if not present:
            stage("blender-uploading", f"{expected / 1e6:.0f} MB, one time only")
            sync_blend(owner_client, tarball, slug,
                       self.work_dir / "ds_blender", on_progress=on_progress)
        else:
            stage("blender-ready", slug)

        friends = self.accounts[1:]
        if friends:
            stage("blender-sharing", "")
            sdk = owner_client._sdk_factory(owner_client.token)
            current = sharing.get_settings(sdk, owner_username, name)
            try:
                sharing.grant_readers(
                    sdk, owner_username, name,
                    [usernames[a.label] for a in friends], current)
            except Exception as e:
                raise _explain_bad_collaborators(e, friends, usernames) from e
        stage("blender-ready", slug)
        return slug

    def _require_real_usernames(self, friends: list, usernames: dict,
                                clients: dict) -> None:
        """Refuse to grant access to a name Kaggle will not recognise.

        A username here can have been typed by hand (Instances -> Set
        username), which means it can be a label, an email, a display name
        or a typo. Kaggle answers that with:

            The following collaborator usernames don't exist: "james"

        -- after the whole .blend has already been uploaded, and worded as
        if the app had done something inexplicable. Each friend's own
        client is asked to confirm its handle FIRST, so a wrong name costs
        a sentence instead of an upload.

        Only checked for accounts whose own client can answer. An account
        that owns nothing has no handle to read (see KaggleClient.whoami),
        and refusing it here would block the very case manual entry exists
        for -- so an unanswerable check passes, and Kaggle remains the
        final word.
        """
        wrong: list[str] = []
        for account in friends:
            claimed = usernames[account.label]
            try:
                # The client already resolved for this account, not a new
                # one: whoami is a network call, and building a second
                # client per friend doubles them for no benefit.
                actual = clients[account.label].whoami()
            except Exception:
                continue        # cannot verify; not the same as wrong
            if actual and actual != claimed:
                wrong.append(
                    f"{account.label} is set to {claimed!r} but Kaggle says "
                    f"that account is {actual!r}")
        if wrong:
            raise WrongUsernameError(
                "the Kaggle username stored for "
                + ("an account" if len(wrong) == 1 else "some accounts")
                + " does not match what Kaggle reports, so sharing the "
                "scene would fail: " + "; ".join(wrong)
                + ". Fix it under Instances -> Set username. Nothing has "
                "been shared and no render has started.")

    def prepare_dataset(self, blend: Path, on_progress: Callable | None = None,
                        *, clients: dict | None = None,
                        usernames: dict | None = None,
                        on_stage: Callable[[str, str], None] | None = None,
                        required: list[Account] | None = None) -> str:
        """Upload the .blend as a Kaggle dataset, share it, and verify it.

        Split out of launch() so the upload can be driven on its own: it is
        the slowest step by far, it is the one most likely to fail, and it
        does not need to be repeated for every render of the same scene.
        Doing it separately also means a failed upload is a failed upload,
        rather than something that takes a whole render attempt down with
        it.

        Costs no GPU quota: a dataset upload is not a session. Nothing here
        starts a kernel, so a caller may run this as often as it likes.

        `required` names the accounts this particular call cannot proceed
        without -- every configured account when omitted, which is the
        original, fully-strict behaviour every caller except launch() still
        gets (the standalone "Upload" action, existing tests, etc.).
        launch() instead passes its own render subset: sharing with a
        configured account OUTSIDE that subset is only ever an
        optimisation -- so THAT job's later render on it needs no
        re-upload -- and a revoked or unreachable account there must not
        fail a launch that never asked to render on it. An account INSIDE
        `required` gets the ORIGINAL, strict treatment, same as the owner
        below: its kernel is about to be pushed, and a scene it cannot see
        would burn its quota failing to find the .blend. Everything this
        call could not share with an optional account is recorded on
        self.unshared_accounts (label -> why) rather than raised.

        Returns the dataset slug every worker's notebook will reference.
        """
        if not self.accounts:
            raise ValueError("add at least one account before uploading")
        required_labels = ({a.label for a in required} if required is not None
                           else {a.label for a in self.accounts})
        # Reset, not accumulated: a stale entry from a PREVIOUS upload
        # must never be reported as something that just happened.
        self.unshared_accounts = {}

        def stage(key: str, detail: str = "") -> None:
            # Bytes alone cannot distinguish "uploading" from "granting
            # access to three friends" from "stuck": the byte counter stops
            # moving for all three. Naming the stage is what separates them.
            if on_stage is not None:
                on_stage(key, detail)
        # Validate the name FIRST -- before the upload, before anything
        # that costs time. An unusable filename used to surface as a Kaggle
        # 400 from dataset_create, i.e. only after the entire .blend had
        # finished uploading.
        stem = slug_stem(blend)
        dataset_name = f"{stem}-blend"

        if clients is None or usernames is None:
            clients, usernames = self._resolve_clients()
        owner = self.accounts[0]
        # The owner is never optional, in `required` or not: without it
        # there is no account left to perform the upload at all, so a dead
        # owner is a hard failure regardless of who is actually rendering.
        # Indexing straight in (not a tolerant .get) keeps that contract
        # explicit -- a missing owner here is the CALLER's bug (it must
        # resolve the owner strictly before calling this, exactly as
        # launch() does below), not something to paper over with a vague
        # KeyError.
        owner_client = clients[owner.label]
        owner_username = usernames[owner.label]
        dataset_slug = f"{owner_username}/{dataset_name}"

        # Is it already up there? Asked of KAGGLE, not of memory. "We
        # uploaded this" used to be a fact the app only knew for the
        # lifetime of one session, so restarting it -- or pressing Render
        # without pressing Upload first -- re-sent the whole scene even
        # though the identical bytes were already on Kaggle.
        expected_size = blend.stat().st_size
        stage("checking", dataset_slug)
        already_there = False
        try:
            already_there = (
                owner_client.dataset_file_size(dataset_slug, blend.name)
                == expected_size)
        except Exception:
            # Never a reason to fail: not being able to check just means
            # uploading, which is what would have happened anyway.
            already_there = False

        if already_there:
            stage("already-uploaded", dataset_slug)
        else:
            # One upload, shared by every account -- dataset sharing is
            # automatable, so N accounts does not mean N uploads.
            stage("uploading", dataset_slug)
            sync_blend(owner_client, blend, dataset_slug,
                       self.work_dir / "ds_owner", on_progress=on_progress)

        # Confirm the upload that just happened actually landed as the
        # right content -- the remote-side counterpart of dataset_sync.py's
        # own local staging-size check. dataset_reachable()/dataset_exists()
        # only prove the dataset is THERE; they say nothing about whether
        # it's the file just uploaded versus a stale one from an earlier
        # job with the same slug.
        stage("verifying", owner_username)
        _require_matching_dataset(owner_client, owner_username, dataset_slug,
                                  blend.name, expected_size)

        # "Sharing" is attempted for every configured account -- see the
        # docstring above for why that stays fleet-wide regardless of
        # `required` -- but a friend whose OWN client/whoami() already
        # failed (see launch()'s tolerant resolution) has no client to
        # share or verify with at all. A required one reaching here missing
        # is the caller's bug, exactly like a missing owner above; an
        # optional one is simply not shareable right now.
        friends = self.accounts[1:]
        for account in friends:
            if account.label not in usernames:
                if account.label in required_labels:
                    raise ValueError(
                        f"{account.label} is required for this launch but "
                        "has no resolved client -- resolve it before "
                        "calling prepare_dataset, or remove it from "
                        "`required`.")
                self.unshared_accounts[account.label] = (
                    "could not be reached to share the scene with")
        resolvable = [a for a in friends if a.label in usernames]

        if resolvable:
            stage("sharing", ", ".join(usernames[a.label] for a in resolvable))
            sdk = owner_client._sdk_factory(owner_client.token)
            # Wrong-username and bulk-grant-rejection failures are NOT
            # split by required/optional below -- both remain fully strict
            # for every resolvable friend, same as before this method took
            # a `required` list. Splitting THOSE too is possible but is
            # not what launch()'s coupling bug needed: this only has to
            # stop a REVOKED/UNREACHABLE account outside the subset from
            # failing a launch that never asked to render on it.
            self._require_real_usernames(resolvable, usernames, clients)
            current = sharing.get_settings(sdk, owner_username, dataset_name)
            try:
                sharing.grant_readers(
                    sdk, owner_username, dataset_name,
                    [usernames[a.label] for a in resolvable], current)
            except Exception as e:
                # The pre-check above cannot catch every bad name: an
                # account that owns nothing has no handle for Kaggle to
                # report, so it is allowed through deliberately and Kaggle
                # is the one that finds out. When it does, its answer --
                # 'The following collaborator usernames don\'t exist:
                # "james"' -- names the handle but not WHICH account of
                # yours carries it, and arrives wrapped as an unexplained
                # failure. Translate it here, where both are known.
                raise _explain_bad_collaborators(e, resolvable, usernames) from e

            # Verify access actually landed, not just that the write
            # returned cleanly -- see UnreachableAccountsError. Deliberately
            # dataset_reachable(), NOT dataset_exists(): the latter is built
            # on dataset_status(), measured live to 404 for a non-owner
            # account even with a genuine READER grant, so it would refuse
            # every shared launch here.
            #
            # Split by required/optional: an optional friend Kaggle hasn't
            # made the grant visible to yet is exactly the "best effort"
            # case this `required` parameter exists for -- recorded, not
            # raised, so a launch that never asked to render on them is not
            # held up by their propagation delay.
            unreachable_required = []
            still_checkable = []
            for account in resolvable:
                if clients[account.label].dataset_reachable(dataset_slug):
                    still_checkable.append(account)
                elif account.label in required_labels:
                    unreachable_required.append(usernames[account.label])
                else:
                    self.unshared_accounts[account.label] = (
                        "granted READER access, but Kaggle has not made "
                        "the dataset reachable for this account yet")
            if unreachable_required:
                raise UnreachableAccountsError(
                    "granted READER access but the dataset is still not "
                    f"reachable for: {', '.join(unreachable_required)}. "
                    "Nothing has been started -- retry once Kaggle's grant "
                    "has propagated.")

            # Reachable proves a friend can see A copy -- not that it is
            # the SAME copy just verified above for the owner. Each
            # friend's OWN client is asked, in case Kaggle's read-side
            # replication genuinely disagrees between accounts. Same
            # required/optional split as above: a STALE or missing copy on
            # an optional friend is recorded, not raised.
            for account in still_checkable:
                stage("verifying-access", usernames[account.label])
                try:
                    _require_matching_dataset(
                        clients[account.label], usernames[account.label],
                        dataset_slug, blend.name, expected_size)
                except StaleDatasetError as e:
                    if account.label in required_labels:
                        raise
                    self.unshared_accounts[account.label] = str(e)
        stage("ready", dataset_slug)
        return dataset_slug

    # ---- warm workers -------------------------------------------------
    CONTROL_DATASET_NAME = "blendfleet-control"

    def control_slug(self, owner_username: str) -> str:
        """The tiny dataset a warm worker polls for jobs.

        Owned by the parent account and shared with the others exactly like
        the scene, so a friend's worker can read it. It carries a job
        descriptor only -- frame lists and render settings -- never scene
        data, so sharing it discloses nothing beyond what a collaborator
        already knows.
        """
        return f"{owner_username}/{self.CONTROL_DATASET_NAME}"

    def publish_job(self, job: dict, *, clients: dict | None = None,
                    usernames: dict | None = None) -> str:
        """Put a job descriptor where the warm workers will find it.

        A new VERSION of the control dataset -- which is what the workers
        poll for. Returns the control slug.

        The job carries its own id. A worker records the last id it acted
        on, so republishing the same job (a retry, a duplicate click) does
        not make a machine render it twice.
        """
        if clients is None or usernames is None:
            clients, usernames = self._resolve_clients()
        owner = self.accounts[0]
        owner_client = clients[owner.label]
        owner_username = usernames[owner.label]
        slug = self.control_slug(owner_username)

        staging = self.work_dir / "ctl"
        staging.mkdir(parents=True, exist_ok=True)
        job_file = staging / "job.json"
        job_file.write_text(json.dumps(job, indent=2), encoding="utf-8")
        sync_blend(owner_client, job_file, slug, self.work_dir / "ds_ctl")

        friends = self.accounts[1:]
        if friends:
            sdk = owner_client._sdk_factory(owner_client.token)
            current = sharing.get_settings(sdk, owner_username,
                                           self.CONTROL_DATASET_NAME)
            sharing.grant_readers(sdk, owner_username,
                                  self.CONTROL_DATASET_NAME,
                                  [usernames[a.label] for a in friends],
                                  current)
        return slug

    def check_hardware(self, label: str) -> str:
        """Push a hardware probe on ONE account. Returns its kernel slug.

        Kaggle's allocation is a lottery, not a setting: the same account
        asking for the same machine_shape got 2x Tesla T4 one minute and
        no GPU at all the next (docs/machine-shape-findings.md, and again
        on 2026-08-12). A render is the expensive way to find that out --
        this is the cheap way, and it is deliberately a SEPARATE kernel
        rather than a flag on a render, so that finding out costs about a
        minute of quota and never a scene upload.

        Does NOT wait: a caller on a UI thread must not block on Kaggle,
        and the answer arrives through the same log stream a render's
        PREFLIGHT line does -- the probe prints the identical format (see
        notebook_builder.HARDWARE_REPORT).

        Deliberately does not touch the job state file. A probe is not a
        job: writing it there would overwrite the record of a running
        render, exactly the failure launch() guards against.
        """
        account = next((a for a in self.accounts if a.label == label), None)
        if account is None:
            raise ValueError(
                f"no account labelled {label!r} to check -- add it under "
                "Manage accounts… first")
        client = self.client_factory(account.token)
        username = account.username or client.whoami()
        # A fresh slug per check. Reusing one would make Kaggle cancel the
        # previous session on push, which is fine, but it also means two
        # accounts' probes could collide on a shared name.
        kernel_slug = f"{username}/blendfleet-hwcheck-{uuid.uuid4().hex[:8]}"
        work = self.work_dir / f"hwcheck-{label}"
        build_probe(work, kernel_slug)
        client.push_kernel(work)
        return kernel_slug

    def start_workers(self, labels: list[str], settings: RenderSettings,
                      dataset_slug: str,
                      blender_slug: str | None = None) -> FleetState:
        """Bring machines up WITHOUT giving them work yet.

        Each pushed kernel reports the hardware it actually got, sets up
        Blender, then waits. That ordering is the point: Kaggle decides
        what hardware a session gets, and until now the only way to find
        out was to commit to a render and read it from the logs. A warm
        worker lets the answer arrive first, so the decision to spend
        somebody's quota on a P100 instead of two T4s is one you make.

        Every started machine is spending quota from this moment -- warm is
        not free -- which is why the worker carries its own idle timeout
        (notebook_builder.IDLE_TIMEOUT_S) rather than trusting this app to
        still be running later.
        """
        if not labels:
            raise ValueError("name at least one account to start")
        busy = self.active_workers()
        if busy:
            raise FleetBusyError(
                "already running on: "
                + ", ".join(f"{w.label} ({w.kernel_slug})" for w in busy)
                + ". Stop those before starting more, or the state file "
                  "would lose track of them.")

        clients, usernames = self._resolve_clients()
        owner_username = usernames[self.accounts[0].label]
        control = self.control_slug(owner_username)
        # Publish an empty job FIRST: a worker that polls before anything
        # exists logs "unavailable" every tick, which reads like a fault
        # rather than like an idle machine.
        self.publish_job({"id": "idle", "workers": [], "frames": []},
                         clients=clients, usernames=usernames)

        job_id = uuid.uuid4().hex[:8]
        st = FleetState(job_id=job_id, blend_name="", start_frame=0,
                        end_frame=0, workers=[])
        wanted = [a for a in self.accounts if a.label in labels]
        try:
            for account in wanted:
                client = clients[account.label]
                username = usernames[account.label]
                kernel_slug = f"{username}/blendfleet-worker-{job_id}"
                kern_dir = self.work_dir / f"warm_{account.label}"
                build([], settings, dataset_slug, kern_dir, kernel_slug,
                      mode="worker", control_slug=control,
                      token=account.token, worker_label=account.label,
                      blender_slug=blender_slug)
                client.push_kernel(kern_dir)
                st.workers.append(WorkerState(
                    label=account.label, username=username,
                    kernel_slug=kernel_slug, frames=[], state="queued"))
                self._save(st)
        finally:
            # Same contract as launch(): a kernel that has been pushed is
            # already spending quota, so whatever got started is on disk
            # and cancellable even if a later push failed.
            self._save(st)
        return st

    def launch(self, blend: Path, settings: RenderSettings,
               start_frame: int, end_frame: int,
               on_progress: Callable | None = None,
               dataset_slug: str | None = None,
               blender_slug: str | None = None, *,
               accounts: list[Account] | None = None) -> FleetState:
        """Launch a render across `accounts` (every configured account
        when omitted, so every existing caller is unaffected).

        `on_progress`, if given, is threaded straight through to
        dataset_sync.sync_blend -> KaggleClient.dataset_create/version ->
        blendfleet.uploader.upload_file, and is called with UploadProgress
        ticks as the owner's .blend upload proceeds -- this is how a caller
        (the dashboard's upload view) shows real upload progress instead of
        the UI thread blocking silently for however long a 60+ MB PUT takes.
        """
        # `is None`, not `or`: accounts=None means "the whole fleet", but
        # accounts=[] means the caller explicitly asked for nobody (e.g.
        # every per-instance checkbox unticked) -- `or` would silently
        # widen that empty selection back out to every configured account
        # and start a render nobody asked for. An empty list instead falls
        # through to the guard just below, with a real error message.
        accounts = self.accounts if accounts is None else accounts
        if not accounts:
            raise ValueError("add at least one account before launching")

        # Validate the name FIRST -- before the busy check, before the
        # upload, before anything that costs time or quota. An unusable
        # filename used to surface as a Kaggle 400 from dataset_create,
        # i.e. only after the entire .blend had finished uploading.
        stem = slug_stem(blend)

        # Scoped to the accounts THIS launch actually wants, across every
        # tracked job (not just the most recent) -- two kernels from the
        # same account rendering the same job's frames would spend that
        # account's quota twice for the same output, but a different
        # account being busy with an unrelated scene must not block this
        # one, or two scenes could never render at once.
        self.require_free(accounts)

        job_id = uuid.uuid4().hex[:8]
        buckets = assign_frames(start_frame, end_frame, len(accounts))

        # Resolve a client + username for every account up front: needed
        # for the push loop below regardless, and for the dataset step
        # that has to happen before it. Scoped to `accounts`, not
        # self.accounts -- resolving an account that is not part of this
        # launch is a wasted network call at best and a spurious failure
        # (e.g. a revoked token on an account nobody asked for) at worst.
        clients, usernames = self._resolve_clients(accounts)

        # The dataset step. Skipped entirely when the caller has already
        # run prepare_dataset() and hands the slug back -- re-uploading a
        # scene that is already on Kaggle is the single most expensive
        # thing this app can do for no reason. It is still VERIFIED below
        # before a kernel is pushed: "the caller says it is there" is not
        # evidence, and a stale or half-replaced dataset would otherwise
        # render the wrong scene on somebody else's quota.
        if dataset_slug is None:
            # Render scope and sharing scope are deliberately NOT the same
            # thing. `accounts` is who renders THIS job; prepare_dataset()
            # shares the freshly-uploaded dataset with self.accounts[1:] --
            # every OTHER configured account, full stop -- because a friend
            # sitting this job out may well render a later job against the
            # very same dataset, and re-sharing per launch would mean
            # re-granting the same reader access over and over for no
            # reason. `required=accounts` tells prepare_dataset which of
            # those accounts this launch cannot proceed without (their
            # kernel is about to be pushed) versus which are purely an
            # optimisation for some possible future launch -- a revoked or
            # unreachable account in the second group must not fail a
            # launch that never asked to render on it (see
            # prepare_dataset's `required` and self.unshared_accounts).
            if {a.label for a in accounts} == {a.label for a in self.accounts}:
                dataset_clients, dataset_usernames = clients, usernames
            else:
                # The owner is never optional (see prepare_dataset) --
                # resolved STRICTLY here, merged with the subset's own
                # strict resolution above, so prepare_dataset never has to
                # guess whether a missing dict entry means "revoked" or
                # merely "not part of this launch". Every OTHER configured
                # account is resolved TOLERANTLY: a friend this launch
                # never asked to render on being unreachable is not this
                # launch's problem, and must not become its failure.
                dataset_clients, dataset_usernames = dict(clients), dict(usernames)
                owner = self.accounts[0]
                if owner.label not in dataset_usernames:
                    owner_clients, owner_usernames = self._resolve_clients(
                        [owner])
                    dataset_clients.update(owner_clients)
                    dataset_usernames.update(owner_usernames)
                others = [a for a in self.accounts
                         if a.label not in dataset_usernames]
                extra_clients, extra_usernames = (
                    self._resolve_clients_tolerant(others))
                dataset_clients.update(extra_clients)
                dataset_usernames.update(extra_usernames)
            dataset_slug = self.prepare_dataset(
                blend, on_progress, clients=dataset_clients,
                usernames=dataset_usernames, required=accounts)
        else:
            expected_size = blend.stat().st_size
            for account in accounts:
                _require_matching_dataset(
                    clients[account.label], usernames[account.label],
                    dataset_slug, blend.name, expected_size)

        st = FleetState(job_id=job_id, blend_name=blend.name,
                        start_frame=start_frame, end_frame=end_frame,
                        workers=[], started_at=time.time())

        # push_kernel ALWAYS starts a run, so a kernel that has been pushed is
        # already spending quota. Persist after EVERY push -- not once at the
        # end -- so a failure part-way through (revoked token on account 3)
        # still leaves accounts 1 and 2 on disk, cancellable and collectable.
        try:
            for account, frames in zip(accounts, buckets):
                client = clients[account.label]
                username = usernames[account.label]
                kernel_slug = f"{username}/{stem}-render-{job_id}"

                kern_dir = self.work_dir / f"kern_{account.label}"
                build(frames, settings, dataset_slug, kern_dir, kernel_slug,
                      blender_slug=blender_slug)
                client.push_kernel(kern_dir)

                st.workers.append(WorkerState(
                    label=account.label, username=username,
                    kernel_slug=kernel_slug, frames=frames,
                    started_at=time.time()))
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
            # Stamped once, when the worker stops being active. Not
            # recomputed from "now" at display time, or a finished job
            # would keep ageing every time the dashboard repainted; and
            # guarded by `not w.finished_at` so a later poll of an
            # already-finished worker cannot push its end time forward.
            if w.state not in ACTIVE_STATES and not w.finished_at:
                w.finished_at = time.time()
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
