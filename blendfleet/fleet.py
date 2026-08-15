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

import hashlib
import json
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable

from blendfleet import crash_log, sharing
from blendfleet.accounts import Account
from blendfleet.assignment import assign_frames
from blendfleet.dataset_sync import sync_blend
from blendfleet.kaggle_client import (
    ACTIVE_STATES, PENDING_STATES, TERMINAL_STATES, KaggleError,
    RevokedTokenError, _mask, revoked_token_message)
# The SAME expression the live stream parses PROGRESS lines with, reused
# rather than re-written: _read_final_frame_count below reads exactly the
# lines log_stream reads, only out of a finished kernel's log instead of a
# live one, and a second copy of the pattern is a second thing to get out
# of step with what notebook_builder actually prints.
from blendfleet.log_stream import PROGRESS_RE
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


def _capped_stem(name: str) -> str:
    """slugify_stem(name), capped to MAX_STEM_LENGTH.

    Split out so slug_stem() (used for Kaggle dataset/kernel names) and
    FleetState.scene_key (used for the output folder) apply the exact
    same cap through the exact same code, and can never independently
    drift apart again -- Task 5's own defect was scene_key skipping this
    cap entirely, so a scene name over 30 slug characters got a
    scene_key that disagreed with the stem already baked into that same
    job's kernel_slug.
    """
    return slugify_stem(name)[:MAX_STEM_LENGTH].strip("-")


def slug_stem(blend: Path) -> str:
    """The validated slug stem for `blend`, or raise InvalidBlendNameError.

    Called at the very top of launch(), before any upload, so an unusable
    filename costs the user a dialog rather than a completed upload.
    """
    stem = _capped_stem(Path(blend).stem)
    if len(stem) < MIN_STEM_LENGTH:
        raise InvalidBlendNameError(
            f"the file name {Path(blend).name!r} cannot be turned into a "
            "Kaggle dataset name. Kaggle only accepts lowercase letters, "
            "digits and dashes, and after removing everything else there "
            f"were fewer than {MIN_STEM_LENGTH} characters left. Rename the "
            "file to something like 'big-buck-bunny.blend' and try again -- "
            "nothing has been uploaded.")
    return stem


def _default_stale_message(username: str, filename: str, remote_size: int,
                           expected_size: int) -> str:
    """The size-mismatch wording for launch()'s own callers: a LOCAL
    .blend really is about to be re-uploaded, and "just launch again" is
    literally what fixes it (the owner always re-uploads). See
    launch_from_dataset's OWN mismatch wording (below, in that method)
    for why this specific message is wrong on that path -- there, both
    of those things are false, and saying so anyway sends the user
    chasing a fix that cannot work.
    """
    return (
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


def _owner_copy_mismatch_message(username: str, filename: str,
                                 expected_size: int, remote_size: int) -> str:
    """launch_from_dataset()'s own size-mismatch wording (Fix round 1,
    Important 2) -- passed as `_require_matching_dataset`'s
    `stale_message` hook.

    `_default_stale_message` (above) says "the local file about to be
    rendered" and "just launch again -- the owner always re-uploads" --
    both literally false on THIS path: there is no local file at all, and
    launching launch_from_dataset() again never uploads anything. This
    says what is actually true instead: two accounts disagree about what
    Kaggle holds for this scene right now, and what to do about that.
    """
    return (
        f"{username}'s copy of {filename!r} on Kaggle is {remote_size} "
        f"bytes, but the dataset owner's own copy is {expected_size} "
        "bytes. Nothing has been started. The accounts disagree about "
        "what is actually on Kaggle for this scene right now -- launching "
        "again will not fix this, since there is no local file to "
        "re-upload here. Re-upload the .blend for this scene from the "
        "Dashboard so every account is re-shared against the same, "
        "current copy, or delete this dataset from the library and add "
        "the scene again, then retry.")


def _require_matching_dataset(client, username: str, slug: str,
                              filename: str, expected_size: int, *,
                              stale_message: Callable[[int], str] | None = None
                              ) -> None:
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

    `stale_message`, when given, REPLACES the size-MISMATCH wording only
    (called with the remote size actually seen) -- the missing-file
    wording above is unaffected, since "the file simply is not there" is
    equally true regardless of what it is being compared against.
    launch_from_dataset() passes its own version (Fix round 1, Important
    2): the default wording below talks about "the local file about to be
    rendered" and says "just launch again -- the owner always re-uploads",
    both literally false when there is no local file at all, which is
    exactly launch_from_dataset()'s whole premise.
    """
    remote_size = client.dataset_file_size(slug, filename)
    if remote_size is None:
        # DatasetFileNotListedError, a StaleDatasetError subclass: every
        # existing catch still catches it, and the owner's post-upload
        # retry (which must wait this one out but must NOT wait out a
        # size mismatch) can tell the two apart.
        raise DatasetFileNotListedError(
            f"{username} can reach dataset {slug!r}, but Kaggle's file "
            f"listing for it has no file named {filename!r} at all. "
            "Nothing has been started. This is not a stale copy -- the "
            "file simply is not there for this account yet, most likely "
            "because the READER grant has not finished propagating. "
            "Re-share the dataset with this account (or just retry once "
            "Kaggle has caught up) and launch again.")
    if remote_size != expected_size:
        if stale_message is not None:
            raise StaleDatasetError(stale_message(remote_size))
        raise StaleDatasetError(
            _default_stale_message(username, filename, remote_size,
                                   expected_size))


# ---------------------------------------------------------------------------
# How long the OWNER waits for Kaggle to make its own fresh upload
# queryable. Measured, not guessed -- from
# %APPDATA%\BlendFleet\logs\blendfleet-20260815-142829.log:
#
#   14:30:37 uploading waydown.blend (498927212 bytes)   <- 499 MB
#   14:33:38 upload finished in 181045 ms                <- 3 minutes
#   14:33:40 the uploaded copy did NOT verify after 1059 ms
#            KaggleError: could not list the files in dataset ...: 403
#
# The upload itself was fine. ListDatasetFiles was asked 1.06 SECONDS
# after a half-gigabyte upload returned, Kaggle had not finished ingesting
# it, and three minutes of upload were thrown away at the final check with
# nothing shared. Pressing Upload again later "just worked" -- the only
# thing that had changed was elapsed time.
#
# So: wait, and scale the patience with the bytes, because ingest work
# does. A 5 MB scene and a 500 MB scene do not need the same window.
#   - floor 20 s: even the smallest scene gets ~20x the 1.06 s that failed
#     here, which costs nothing when the check normally passes first try.
#   - +120 s per GB: the 499 MB above gets ~80 s total, ~75x that measured
#     interval.
#   - cap 300 s: five minutes is already longer than the 181 s the upload
#     took. Past that, "Kaggle is still indexing" stops being the credible
#     explanation and the user deserves an answer instead of a spinner.
# The alternative to waiting is re-sending the file, which for this user
# cost 3 minutes and 499 MB -- every one of these numbers is cheap by
# comparison.
_VERIFY_WINDOW_MIN_S = 20.0
_VERIFY_WINDOW_PER_GB_S = 120.0
_VERIFY_WINDOW_MAX_S = 300.0
# Backoff is capped so the user gets a fresh "still waiting, Ns of Ms"
# line at least this often: a 60 s silence is indistinguishable from a
# hang, which is the complaint the stage vocabulary exists to answer.
_VERIFY_POLL_MAX_S = 15.0
_BYTES_PER_GB = 1 << 30


def verify_window_s(size_bytes: int) -> float:
    """Seconds to let Kaggle catch up after uploading `size_bytes`.

    See the constants above for where the numbers come from.
    """
    scaled = (_VERIFY_WINDOW_MIN_S
              + _VERIFY_WINDOW_PER_GB_S * (max(size_bytes, 0) / _BYTES_PER_GB))
    return min(scaled, _VERIFY_WINDOW_MAX_S)


def _verify_owner_upload(client, username: str, slug: str, filename: str,
                         expected_size: int, *,
                         note: Callable[..., None],
                         stage: Callable[[str, str], None],
                         sleep: Callable[[float], None] = time.sleep,
                         clock: Callable[[], float] = time.monotonic) -> None:
    """_require_matching_dataset for the owner's OWN just-finished upload,
    retried with backoff until Kaggle admits the file exists.

    Same verification, only patient: nothing here accepts an unverified
    upload. A launch must never start on a scene the accounts cannot
    actually see, so this still returns only when Kaggle itself confirms
    `filename` at `expected_size` -- it just stops treating "asked too
    soon" as "the upload failed".

    Two failures count as "not ready yet", because right after an upload
    they are the same state seen through two different calls:
      * KaggleError carrying a 403 from dataset_files -- Kaggle refusing
        to list a dataset it has not finished ingesting (this codebase
        already documents that a missing or invisible dataset answers 403,
        not 404 -- see dataset_reachable and _AMBIGUOUS_STATUSES).
      * DatasetFileNotListedError -- the listing exists but the file is
        not in it yet.

    Everything else fails immediately, and deliberately:
      * a SIZE MISMATCH (plain StaleDatasetError) means Kaggle's listing
        is complete and disagrees with the local file. That is a real
        stale copy; waiting cannot turn it into the right one.
      * RevokedTokenError means the credentials are dead. Waiting for a
        token that will never come back is the cruellest possible spinner.
    """
    started = clock()
    deadline = started + verify_window_s(expected_size)
    attempt = 0
    delay = 1.0
    while True:
        attempt += 1
        try:
            _require_matching_dataset(client, username, slug, filename,
                                      expected_size)
        except RevokedTokenError:
            raise                       # dead token: never a timing problem
        except (KaggleError, DatasetFileNotListedError) as e:
            waited = clock() - started
            # Logged per attempt, with elapsed time, because the ONLY
            # reason this bug was findable was that the log carried the
            # 1059 ms. The next occurrence should say how long Kaggle
            # actually took, so these numbers can be re-tuned from
            # evidence rather than argued about.
            note(f"owner {username}: Kaggle has not made the upload "
                 f"queryable yet -- attempt {attempt} failed "
                 f"{waited * 1000:.0f} ms after the upload finished. "
                 f"{type(e).__name__}: {e}")
            remaining = deadline - clock()
            if remaining <= 0:
                note(f"owner {username}: gave up waiting for Kaggle after "
                     f"{waited * 1000:.0f} ms and {attempt} attempt(s). The "
                     f"upload itself succeeded; Kaggle never made "
                     f"{filename} queryable inside the window.",
                     critical=True)
                raise UploadNotVisibleError(
                    f"{filename} finished uploading to Kaggle successfully, "
                    f"but Kaggle has not yet made the dataset {slug!r} "
                    f"queryable for {username}, its own owner -- so the "
                    f"upload could not be confirmed and nothing was shared "
                    f"or started. Kaggle indexes a new dataset some time "
                    f"after the bytes land, and the bigger the scene the "
                    f"longer that takes; this waited "
                    f"{(clock() - started):.0f}s ({attempt} attempts) and "
                    f"it still was not ready. Nothing is lost and nothing "
                    f"is wrong with the file: wait a minute and press "
                    f"Upload again. That retry will NOT re-send the scene "
                    f"-- the 'already on Kaggle' check at the start finds "
                    f"the copy that just landed and skips straight to "
                    f"verifying it.") from e
            nap = min(delay, remaining)
            # An honest new stage, not silence: the byte counter is
            # finished and frozen at 100% by now, so without this the UI
            # looks hung for the whole window.
            stage("waiting-for-kaggle",
                  f"{username}: {waited:.0f}s of up to "
                  f"{verify_window_s(expected_size):.0f}s")
            sleep(nap)
            delay = min(delay * 2, _VERIFY_POLL_MAX_S)
        else:
            if attempt > 1:
                note(f"owner {username}: Kaggle made the upload queryable "
                     f"after {(clock() - started) * 1000:.0f} ms and "
                     f"{attempt} attempt(s)")
            return


def _find_blend_file(client, slug: str) -> tuple[str, int]:
    """The (filename, size_bytes) of the .blend inside dataset `slug`, as
    `client` -- which MUST be the dataset's own OWNER's client -- actually
    lists it, or raise NoBlendInDatasetError.

    Only the owner's client is used here, for two reasons at once: the
    owner is the one account certain to be able to list every file (a
    friend's grant can be partial or lapsed -- see launch_from_dataset's
    own re-verification), and the owner's listing is also what this app
    already treats as ground truth for "the current copy"
    (_require_matching_dataset) -- so the size handed back here is
    deliberately the same number every OTHER account's copy gets checked
    against, not a size read from a local file that, for a scene rendered
    straight from Kaggle, does not exist.

    Every dataset this app itself uploads holds exactly one file, so
    picking among more than one is not a case this app's own upload path
    can produce -- only a hand-edited dataset on kaggle.com. Sorted by
    name (Fix round 1, Minor) so THAT pick is at least reproducible run to
    run, rather than whatever order Kaggle's own listing happened to
    return -- not a claim that a sorted pick is somehow "the right one".
    """
    blends = sorted((name, size) for name, size in client.dataset_files(slug)
                    if name.lower().endswith(".blend"))
    if not blends:
        raise NoBlendInDatasetError(
            f"dataset {slug!r} has no .blend file in it. Nothing has been "
            "started -- no kernel has been pushed. Either this dataset is "
            "not actually a rendered scene, or its .blend has been removed "
            "or renamed on kaggle.com. Pick a different scene from the "
            "library, or re-upload the .blend to this dataset and try "
            "again.")
    return blends[0]


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


def _tokenless(text: str, accounts: list) -> str:
    """`text` with every configured account's API token reduced to the same
    unusable fragment `_mask` puts in error messages.

    Everything the sharing path writes to the diagnostic log goes through
    here. That log quotes Kaggle's own failures verbatim -- which is the
    point of it -- and a Kaggle error is free to echo back whatever it was
    sent, including a token in a URL or a header dump. A diagnostic file
    the user is asked to send on is the last place a live credential may
    end up: the cost of this whole change must never be "now rotate every
    account's token".
    """
    for account in accounts:
        token = getattr(account, "token", "") or ""
        # Short values are not credentials and could match half the log.
        if len(token) > 12 and token in text:
            text = text.replace(token, _mask(token))
    return text


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
    # When frames_done was last written (epoch seconds), 0.0 meaning "never
    # recorded" -- which is also what a state file written before this
    # existed loads as. Kept BESIDE the count because a count read back off
    # disk after a restart is a CACHED reading, not a live one: the stream
    # that produced it is gone, and the render has kept going on Kaggle in
    # the meantime. The UI is required to say how old a cached number is
    # (the same rule the hardware snapshot already follows), and it cannot
    # do that unless the number carries its timestamp.
    frames_done_at: float = 0.0
    # Has this worker's OWN kernel log already been read for its final
    # frame count (Fleet._read_final_frame_count)? Set the first time the
    # worker is seen in a terminal state, whether or not the read produced
    # anything, so the fetch happens exactly ONCE per worker: it is a real
    # network call, and a finished job stays tracked -- and polled every 30
    # seconds -- until the user forgets it.
    final_count_checked: bool = False
    # True only when that read actually produced a count. frames_done is
    # then the render's own last word, not the last thing a live stream
    # happened to save before the window closed. False after a checked read
    # means the count is NOT KNOWN -- the UI must say so rather than show
    # the stale number as if it were current (see
    # bridge._frames_done_source).
    final_count_known: bool = False
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

        Used for the output folder (see collector.collect) and as the
        job's identity in the UI. Derived from blend_name rather than
        stored, so it cannot drift from the scene actually being
        rendered.

        Goes through _capped_stem -- the same length cap slug_stem()
        applies -- rather than raw slugify_stem, so this NEVER disagrees
        with the stem already baked into this same job's kernel_slug
        (see _capped_stem's docstring for the bug this fixes). Unlike
        slug_stem(), never raises: this only ever describes an
        ALREADY-launched job, and a getter that blows up on it would be
        strictly worse than falling back to "scene".

        Two differently-named .blend files can still collapse to the
        same scene_key ("shot 1.blend" and "shot-1.blend" both slugify
        to "shot-1") -- that collision is accepted, not fixed away here,
        because it is harmless where it matters: collect() names every
        copied FRAME from the raw, un-slugified stem, never from
        scene_key, so two colliding scenes only ever end up sharing a
        folder, never overwriting each other's files inside it.

        That reasoning has exactly one hole, and it is NOT fixed here:
        two stems that differ ONLY by case ("Kitchen.blend" vs
        "kitchen.blend") are already identical strings by the time this
        property lowercases them, but their raw, case-PRESERVED stems are
        what collect() actually names files from -- and on a case-
        insensitive filesystem (Windows, default macOS)
        "Kitchen_0001.png" and "kitchen_0001.png" are the same path, so
        the "never overwriting" guarantee above would break for exactly
        that pair. This property has no way to see that coming (it only
        ever looks at one blend_name at a time); collect() itself detects
        and disambiguates that one case at write time, by inspecting what
        is already on disk -- see its own comment, right where the
        collision would otherwise happen.
        """
        return _capped_stem(Path(self.blend_name).stem) or "scene"


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


class DatasetFileNotListedError(StaleDatasetError):
    """The account can reach the dataset, but its file listing has no file
    by that name at all -- the MISSING half of StaleDatasetError.

    A subclass, not a separate error, so that every existing caller
    catching StaleDatasetError keeps catching this unchanged; the only
    reason it exists is that the owner's post-upload verification has to
    tell the two halves apart. "The listing has no file called
    scene.blend yet", seconds after that exact file finished uploading,
    is Kaggle still ingesting -- worth waiting out. A SIZE MISMATCH is
    the opposite: the listing is complete and disagrees, which no amount
    of waiting repairs (see _verify_owner_upload).
    """


class UploadNotVisibleError(RuntimeError):
    """The .blend uploaded fine, but Kaggle never made the dataset
    queryable within the wait window (see _verify_owner_upload).

    Deliberately NOT a KaggleError or a StaleDatasetError: both of those
    carry advice ("the dataset was deleted or renamed", "this account's
    grant has lapsed", "re-upload the current .blend") that is actively
    wrong here. The owner has just uploaded the file itself, seconds
    ago, with its own token; nothing is deleted, nothing has lapsed and
    nothing is stale. The only true statement is that Kaggle's own index
    has not caught up yet, so this type carries its own wording.
    """


class NoBlendInDatasetError(RuntimeError):
    """A dataset that was about to be rendered has no .blend file in it at
    all.

    scenes.py's Scene.blend_name is a NAME-BASED GUESS -- its own docstring
    says so explicitly and warns that "Task 10 is what actually confirms a
    .blend exists, by listing the dataset's real files". This is that
    confirmation: launch_from_dataset() lists the dataset for real, before
    a single kernel is pushed, rather than trusting that guess with
    somebody's GPU quota. Raised for a dataset that merely happens to end
    in "-blend" (renamed by hand on kaggle.com, or never actually holding a
    scene) as much as for one that never had a .blend to begin with -- from
    here, the two look identical.
    """


class UnreadableJobChanged(RuntimeError):
    """forget_unreadable() was asked to drop an entry that is no longer
    the one the caller was shown (Fix round 2).

    Raised instead of silently forgetting whatever now happens to sit at
    `index`: save_jobs() writes parsed jobs first and unreadable entries
    LAST (see its own docstring), so a DIFFERENT job going unreadable
    between the moment a payload named this index and the moment the
    user clicks "forget" can shift every later unreadable entry's
    position by one. Forgetting by position alone, with no check, would
    then silently destroy the WRONG record -- along with the one kernel
    slug the user actually needed to go stop by hand at kaggle.com, while
    leaving the one they meant to dismiss still on screen.
    """


def fingerprint_unreadable_entry(entry: object) -> str:
    """A short, stable identifier for one raw, possibly-unparseable job
    entry -- see UnreadableJobChanged's own docstring for what this
    guards against.

    Hashes the entry's own canonical bytes rather than trusting job_id:
    a whole-file JSON failure entry (see load_jobs()) is a bare string
    with no job_id to read at all, and a per-entry failure's job_id is
    only 32 bits of uuid4 -- neither is safe as "this is genuinely the
    same broken record", but the bytes of the entry itself always are.
    Truncated to 16 hex characters: this is a same-process, same-session
    identity check against accidental drift, not a cryptographic
    guarantee, and the full 64 would tell a caller nothing more useful.
    """
    canonical = (entry if isinstance(entry, str)
                else json.dumps(entry, sort_keys=True, default=str))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


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
        # label -> why, for every worker the last poll_all() could not get
        # a status for. poll_all() is deliberately tolerant per worker (see
        # its own docstring): one blip must not abort the rest. But that
        # tolerance leaves the worker's state exactly as it was, and a
        # CALLER cannot otherwise tell "Kaggle says this is still running"
        # apart from "Kaggle was never asked" -- which is the difference
        # between a fact and a stale file. Reported here so a caller that
        # cares (the startup check, which decides whether to reconnect a
        # log stream) can say the view may be out of date instead of
        # presenting it as current. Reset on every poll_all().
        self.unreachable_workers: dict[str, str] = {}

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
            # Preserved, not dropped -- exactly like the per-entry failure
            # path below preserves a single bad job on self.unreadable_jobs.
            # A file mangled badly enough that even the OUTER JSON fails to
            # parse might still be the only surviving record of a running
            # kernel's slug; the next save_jobs() call (poll_all(), _save())
            # must carry this raw text through rather than silently
            # replacing it with an empty job list (Task 5 fix round 1,
            # IMPORTANT 2).
            self.unreadable_jobs = [raw]
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

        load() answers with the LAST job in the list, and active_workers,
        poll and forget_job()'s default all go through load() -- cancel_worker
        and fetch_failure_log used to as well (must-fix 1's other half; both
        now search load_jobs() so a non-newest job's worker is not silently
        invisible to them). Re-appending a re-saved job (rather than
        replacing it where it already sits) would silently move it to the
        end and repoint every remaining load()-based caller at the wrong
        job the moment more than one job is tracked at once.
        """
        jobs = self.load_jobs()
        for i, j in enumerate(jobs):
            if j.job_id == st.job_id:
                jobs[i] = st
                break
        else:
            jobs.append(st)
        self.save_jobs(jobs)

    def record_progress(self, progress: dict[str, int]) -> list[str]:
        """Write the live stream's frame counts through to disk.

        The ONLY thing that ever knew how many frames a worker had
        finished was the SSE log stream, held in memory. Kaggle's
        kernel-status API does not report a frame count at all -- poll_all()
        can learn "running", never "9 of 15" -- so closing the app threw
        away every number the stream had reported, and reopening it showed
        a job that was demonstrably still rendering with no progress at
        all. Persisting the count here is what lets a restarted app say
        something true before any stream has reconnected.

        `progress` is label -> frames done, and each label is applied to
        the NEWEST tracked job containing it -- the same "a label points at
        its most recent job" rule the dashboard payload itself uses, so the
        two cannot disagree about which worker a live reading belongs to.

        Never moves a count BACKWARDS. A resumed stream replays the whole
        log from the top, and a reconnect mid-replay can briefly report
        fewer frames than the last complete replay did; a bar that jumps
        back to 2/15 reads as a render that restarted, which is a lie about
        somebody's quota. Nothing else about the worker is touched: state
        and message belong to poll_all(), which talks to Kaggle.

        Returns the labels actually written, so a caller can tell an
        unchanged no-op (the common case -- nothing is saved at all then)
        from a real update.
        """
        jobs = self.load_jobs()
        if not jobs:
            return []
        # Oldest first, so a label present in more than one job ends up
        # bound to its most recent one -- see the docstring.
        newest: dict[str, WorkerState] = {}
        for st in jobs:
            for w in st.workers:
                newest[w.label] = w
        written: list[str] = []
        now = time.time()
        for label, done in progress.items():
            w = newest.get(label)
            if w is None or done <= w.frames_done:
                continue
            w.frames_done = done
            w.frames_done_at = now
            written.append(label)
        if not written:
            return []           # nothing moved: do not rewrite the file
        self.save_jobs(jobs)
        return written

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

    def forget_unreadable(self, index: int,
                          fingerprint: str | None = None) -> object | None:
        """Drop ONE entry from self.unreadable_jobs by its position,
        without touching anything on Kaggle -- forget_job()'s counterpart
        for a job record broken badly enough that it was never turned into
        a FleetState at all (Fix round 1, Important 3).

        forget_job() can only pop a PARSED job by position; a record on
        self.unreadable_jobs never becomes one, so it had no way to be
        forgotten and, since save_jobs() deliberately re-writes every
        unreadable entry verbatim on every write (see that method's own
        docstring -- the whole point is that a load-then-save round trip
        must never erase it), it sat on the payload forever with no way to
        acknowledge it once the user had actually gone and dealt with it
        by hand at kaggle.com.

        By POSITION, matching forget_job()'s own reasoning: an unreadable
        entry may have no job_id at all (a whole-file JSON failure has no
        fields to match on), and even when one is present it is only 32
        bits of uuid4, so equality could drop two for the price of one.

        `fingerprint`, when given, must match
        fingerprint_unreadable_entry() of whatever is CURRENTLY at
        `index` (Fix round 2) -- see UnreadableJobChanged's own docstring
        for the race this closes: a caller's copy of the list can go
        stale between reading a payload and clicking "forget" if some
        OTHER job goes unreadable in between, shifting positions.
        Optional so a caller with no payload to check against (an
        internal one, or a future one) can still forget by bare position,
        matching forget_job()'s own unchecked contract.

        Returns the dropped raw entry (whatever shape it had -- a dict, or
        the literal raw text for a whole-file failure), or None if `index`
        no longer exists: already forgotten, or the file changed since
        whatever payload named this index was read. This is NOT a cancel:
        whatever this entry might have been tracking (if anything) keeps
        running and keeps spending quota; all this does is stop the
        warning from being able to point at it any more.
        """
        # Refreshes self.unreadable_jobs as a side effect (see its own
        # docstring) -- read fresh, exactly like forget_job() reads a
        # fresh `jobs` before popping from it.
        jobs = self.load_jobs()
        if not (0 <= index < len(self.unreadable_jobs)):
            return None
        target = self.unreadable_jobs[index]
        if (fingerprint is not None
                and fingerprint_unreadable_entry(target) != fingerprint):
            raise UnreadableJobChanged(
                "The list of unreadable job records changed since this "
                "one was shown -- a different record may now be at this "
                "position, so nothing has been forgotten. Refresh and "
                "look again before forgetting one.")
        self.unreadable_jobs.pop(index)
        self.save_jobs(jobs)
        return target

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
        """Accounts with a worker still holding a job in some tracked job.

        PENDING_STATES, not ACTIVE_STATES (must-fix 3): a kernel that has
        been pushed but whose Kaggle session does not exist yet reports
        "not_started", which is NOT the same thing as finished -- treating
        it as "not active, therefore free" let a just-pushed render's
        account be handed to a second launch before the first had even
        started. A completed job (TERMINAL_STATES) holds nobody, so the
        fleet is reusable the moment its frames are done rather than when
        the user gets round to collecting.
        """
        return {w.label for j in self.load_jobs() for w in j.workers
                if w.state in PENDING_STATES}

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

        PENDING_STATES, not ACTIVE_STATES (must-fix 3, same reasoning as
        busy_labels()): a kernel just pushed for an OLDER job can sit at
        "not_started" for a poll or two before Kaggle's status API can see
        it, and that account must stay held for exactly that window, or a
        second launch racing the first one's own startup double-books it.
        """
        busy = {}
        for j in self.load_jobs():
            for w in j.workers:
                if w.state in PENDING_STATES:
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
                        required: list[Account] | None = None,
                        sleep: Callable[[float], None] = time.sleep,
                        clock: Callable[[], float] = time.monotonic) -> str:
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

        `sleep`/`clock` exist only so the owner's post-upload wait (see
        _verify_owner_upload) can be driven by a fake clock in tests --
        a real 20-300 second window has no business inside a test suite.
        Production callers never pass them.

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

        # A sharing failure was reported from the field -- "the upload
        # worked but the other accounts never got the file" -- and left NO
        # trace anywhere: the four steps below (grant, then reachable, then
        # the file at the right size, per account) all collapse into one
        # toast that disappears, and nothing about them reached the
        # diagnostic log. So this narrates itself from here on, per
        # account, with elapsed times: a Kaggle propagation delay and a
        # permission error are the same sentence to the user, and only the
        # timing and the step number tell them apart afterwards.
        def note(message: str, *, critical: bool = False) -> None:
            crash_log.record(
                _tokenless(f"share {dataset_slug}: {message}", self.accounts),
                critical=critical)

        def since(started: float) -> str:
            return f"{(time.monotonic() - started) * 1000:.0f} ms"

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
        except Exception as e:      # noqa: BLE001
            # Never a reason to fail: not being able to check just means
            # uploading, which is what would have happened anyway. It does
            # mean re-sending a whole .blend the user may already have on
            # Kaggle, though, so the reason is worth a line -- "why did it
            # upload 60 MB again?" is otherwise unanswerable.
            #
            # But say WHICH of the two it is. Kaggle answers a dataset that
            # does not exist (or is invisible to this account) with 403,
            # not 404 -- see dataset_reachable's docstring and
            # _AMBIGUOUS_STATUSES in kaggle_client.py -- so on a FIRST
            # upload this branch is the normal, expected path, and the old
            # single wording ("could not check ... so it will be uploaded
            # again") read like a fault every single time. The 2026-08-15
            # log shows exactly that at 14:30:37, three minutes before the
            # real failure, sending the reader hunting the wrong line.
            already_there = False
            not_there_yet = (isinstance(e, KaggleError)
                             and not isinstance(e, RevokedTokenError)
                             and "403" in str(e))
            if not_there_yet:
                reason = (f"{blend.name} is not on Kaggle under this slug "
                          f"yet (Kaggle answers 403, not 404, for a dataset "
                          f"that does not exist for this account), so it is "
                          f"being uploaded now. Normal for a first upload; "
                          f"nothing is wrong. {type(e).__name__}: {e}")
            else:
                reason = (f"could not check whether {blend.name} is already "
                          f"on Kaggle, so it will be uploaded again -- this "
                          f"is NOT the ordinary first-upload answer and is "
                          f"worth reading. {type(e).__name__}: {e}")
            crash_log.record(_tokenless(
                f"share {dataset_slug}: {reason}", self.accounts))

        if already_there:
            stage("already-uploaded", dataset_slug)
            note(f"owner {owner_username}: {blend.name} is already on Kaggle "
                 f"at {expected_size} bytes -- no upload needed")
        else:
            # One upload, shared by every account -- dataset sharing is
            # automatable, so N accounts does not mean N uploads.
            stage("uploading", dataset_slug)
            upload_started = time.monotonic()
            note(f"owner {owner_username}: uploading {blend.name} "
                 f"({expected_size} bytes)")
            sync_blend(owner_client, blend, dataset_slug,
                       self.work_dir / "ds_owner", on_progress=on_progress)
            # Timestamped on its own line so the log proves the ordering:
            # nothing below this point can have started before the upload
            # returned.
            note(f"owner {owner_username}: upload finished in "
                 f"{since(upload_started)}")

        # Confirm the upload that just happened actually landed as the
        # right content -- the remote-side counterpart of dataset_sync.py's
        # own local staging-size check. dataset_reachable()/dataset_exists()
        # only prove the dataset is THERE; they say nothing about whether
        # it's the file just uploaded versus a stale one from an earlier
        # job with the same slug.
        #
        # Retried, not asked once: the 2026-08-15 log has this check
        # failing 1059 ms after a 498,927,212-byte upload returned, with
        # Kaggle 403ing ListDatasetFiles because it was still ingesting.
        # That discarded three minutes and 499 MB of successful upload at
        # the very last step. See _verify_owner_upload for the window and
        # for which failures are "not ready yet" versus genuinely wrong.
        stage("verifying", owner_username)
        owner_check = time.monotonic()
        try:
            _verify_owner_upload(owner_client, owner_username, dataset_slug,
                                 blend.name, expected_size,
                                 note=note, stage=stage,
                                 sleep=sleep, clock=clock)
        except Exception as e:      # noqa: BLE001 -- logged, then re-raised
            note(f"owner {owner_username}: the uploaded copy did NOT verify "
                 f"after {since(owner_check)} -- nothing was shared with "
                 f"anyone. {type(e).__name__}: {e}", critical=True)
            raise
        note(f"owner {owner_username}: verified {blend.name} at "
             f"{expected_size} bytes in {since(owner_check)}")

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
                note(f"{account.label}: step 1/4 name -- has no resolved "
                     "Kaggle client, so it is not in this grant at all "
                     "(optional for this call; recorded as unshared)",
                     critical=True)
        resolvable = [a for a in friends if a.label in usernames]

        if resolvable:
            stage("sharing", ", ".join(usernames[a.label] for a in resolvable))
            who = ", ".join(f"{a.label} ({usernames[a.label]})"
                            for a in resolvable)
            note(f"step 1/4 name -- upload verified, now granting READER to "
                 f"{len(resolvable)} account(s): {who}")
            sdk = owner_client._sdk_factory(owner_client.token)
            # Wrong-username and bulk-grant-rejection failures are NOT
            # split by required/optional below -- both remain fully strict
            # for every resolvable friend, same as before this method took
            # a `required` list. Splitting THOSE too is possible but is
            # not what launch()'s coupling bug needed: this only has to
            # stop a REVOKED/UNREACHABLE account outside the subset from
            # failing a launch that never asked to render on it.
            grant_started = time.monotonic()
            try:
                self._require_real_usernames(resolvable, usernames, clients)
                current = sharing.get_settings(sdk, owner_username,
                                               dataset_name)
            except Exception as e:      # noqa: BLE001 -- logged, re-raised
                note(f"step 2/4 grant -- never attempted: the checks before "
                     f"it failed after {since(grant_started)}, so NO account "
                     f"was granted anything. {type(e).__name__}: {e}",
                     critical=True)
                raise
            try:
                sharing.grant_readers(
                    sdk, owner_username, dataset_name,
                    [usernames[a.label] for a in resolvable], current)
            except Exception as e:
                # ONE write covers every friend, so this failure genuinely
                # cannot be pinned on any one of them -- saying otherwise
                # in the log would send the next reader hunting the wrong
                # account.
                note(f"step 2/4 grant -- FAILED after {since(grant_started)} "
                     f"for all {len(resolvable)} account(s) at once ({who}). "
                     "grant_readers is a single bulk write, so this is NOT "
                     "attributable to any one account. "
                     f"{type(e).__name__}: {e}", critical=True)
                # The pre-check above cannot catch every bad name: an
                # account that owns nothing has no handle for Kaggle to
                # report, so it is allowed through deliberately and Kaggle
                # is the one that finds out. When it does, its answer --
                # 'The following collaborator usernames don\'t exist:
                # "james"' -- names the handle but not WHICH account of
                # yours carries it, and arrives wrapped as an unexplained
                # failure. Translate it here, where both are known.
                raise _explain_bad_collaborators(e, resolvable, usernames) from e
            # "Accepted", not "granted and visible": Kaggle takes the write
            # and propagates it afterwards, which is precisely why steps
            # 3 and 4 exist and why this line carries its own duration.
            note(f"step 2/4 grant -- bulk grant_readers accepted in "
                 f"{since(grant_started)} for all {len(resolvable)} "
                 f"account(s): {who}")

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
                username = usernames[account.label]
                need = ("required" if account.label in required_labels
                        else "optional")
                reach_started = time.monotonic()
                try:
                    reachable = clients[account.label].dataset_reachable(
                        dataset_slug)
                except Exception as e:  # noqa: BLE001 -- logged, re-raised
                    note(f"{account.label} ({username}): step 3/4 reachable "
                         f"-- the check itself failed after "
                         f"{since(reach_started)} ({need}). "
                         f"{type(e).__name__}: {e}", critical=True)
                    raise
                if reachable:
                    still_checkable.append(account)
                    note(f"{account.label} ({username}): step 3/4 reachable "
                         f"-- yes, in {since(reach_started)} ({need})")
                elif account.label in required_labels:
                    unreachable_required.append(username)
                    note(f"{account.label} ({username}): step 3/4 reachable "
                         f"-- NO, after {since(reach_started)}. The grant was "
                         "accepted but Kaggle does not show this dataset to "
                         "this account. Required for this call, so the whole "
                         "upload fails here.", critical=True)
                else:
                    self.unshared_accounts[account.label] = (
                        "granted READER access, but Kaggle has not made "
                        "the dataset reachable for this account yet")
                    note(f"{account.label} ({username}): step 3/4 reachable "
                         f"-- NO, after {since(reach_started)}. Optional for "
                         "this call, so it is recorded as unshared and the "
                         "upload continues.", critical=True)
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
                username = usernames[account.label]
                need = ("required" if account.label in required_labels
                        else "optional")
                file_started = time.monotonic()
                stage("verifying-access", username)
                try:
                    _require_matching_dataset(
                        clients[account.label], username,
                        dataset_slug, blend.name, expected_size)
                except StaleDatasetError as e:
                    note(f"{account.label} ({username}): step 4/4 file -- "
                         f"can see the dataset but NOT {blend.name} at "
                         f"{expected_size} bytes, after {since(file_started)} "
                         f"({need}). StaleDatasetError: {e}", critical=True)
                    if account.label in required_labels:
                        raise
                    self.unshared_accounts[account.label] = str(e)
                except RevokedTokenError as e:
                    # Caught BEFORE KaggleError below, which it subclasses.
                    # A revoked token is a definite, actionable fact, not a
                    # timing problem -- recording it with the propagation
                    # wording would tell the user to wait for something
                    # that will never happen. Its own message already says
                    # what to do.
                    note(f"{account.label} ({username}): step 4/4 file -- "
                         f"this account's token is REVOKED (after "
                         f"{since(file_started)}, {need}). Not a propagation "
                         f"delay: waiting will not fix it. {e}", critical=True)
                    if account.label in required_labels:
                        raise
                    self.unshared_accounts[account.label] = str(e)
                except KaggleError as e:
                    # A 403 from ListDatasetFiles here is the SAME
                    # propagation delay the reachability check above
                    # already tolerates, arriving one call later: Kaggle
                    # has accepted the READER grant but has not yet made
                    # the file listing visible to this account. Only
                    # StaleDatasetError was caught before, so that 403
                    # escaped and failed the whole upload -- which is
                    # exactly why pressing Upload a second time "worked",
                    # the grant having propagated in between. Same
                    # required/optional split as every other check here.
                    note(f"{account.label} ({username}): step 4/4 file -- "
                         f"Kaggle refused this account's file listing after "
                         f"{since(file_started)} ({need}), which is the same "
                         "grant propagation delay one call later. "
                         f"{type(e).__name__}: {e}", critical=True)
                    if account.label in required_labels:
                        raise
                    self.unshared_accounts[account.label] = (
                        "granted READER access, but Kaggle has not made the "
                        f"dataset's file listing visible yet ({e})")
                else:
                    note(f"{account.label} ({username}): step 4/4 file -- "
                         f"can see {blend.name} at {expected_size} bytes, "
                         f"confirmed in {since(file_started)} ({need}). "
                         "Sharing complete for this account.")
            # One closing line, so "who ended up with the scene" is a single
            # lookup rather than a reconstruction from the lines above.
            shared = [a.label for a in resolvable
                      if a.label not in self.unshared_accounts]
            missed = sorted(self.unshared_accounts)
            note(f"sharing finished: {len(shared)} of {len(friends)} other "
                 f"account(s) can see {blend.name}"
                 + (f"; NOT shared with {', '.join(missed)}" if missed
                    else " -- everyone"),
                 critical=bool(missed))
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
        # require_free(), scoped to the accounts THIS warm start actually
        # wants -- not active_workers() (must-fix 2). active_workers() only
        # ever asks Kaggle about load()'s single newest job: with an OLDER
        # job still rendering and a newer one already finished, that guard
        # passed and pushed a second warm kernel onto an account mid-
        # render, double-billing it -- and because _state_payload maps
        # each label to its NEWEST job while the dashboard groups sections
        # by job id, the still-running older job's whole section (Cancel
        # and Collect included) then vanished from the page. require_free()
        # is the same guard every other launch path already uses, and it
        # is scoped to `wanted` so accounts outside this warm start are
        # never blocked by a job they have nothing to do with.
        wanted = [a for a in self.accounts if a.label in labels]
        self.require_free(wanted)

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

    def launch_from_dataset(self, dataset_slug: str, settings: RenderSettings,
                            start_frame: int, end_frame: int, *,
                            accounts: list[Account] | None = None) -> FleetState:
        """Render a scene that already lives on Kaggle -- no local .blend
        at all. This is the point of the scene library: a scene uploaded
        five days ago (or by a session of this app that has since closed)
        renders again without re-sending a single byte of a possibly
        60 MB file.

        launch() needs a local .blend for exactly three things, and each is
        replaced here by something Kaggle itself can answer instead:
          - slug_stem(blend), for the kernel slug -> the dataset slug's own
            stem. Already Kaggle-legal: it went through slug_stem() at
            upload time (see dataset_slug_for), so it needs no re-slugifying.
          - blend.name, for which file to verify -> the .blend actually
            found by LISTING the dataset's real files (_find_blend_file),
            never guessed from the dataset's name the way scenes.py's
            Scene.blend_name is (see that module's own docstring on why it
            calls itself a guess).
          - blend.stat().st_size, for the expected size -> the OWNER's own
            reported size for that file. This is a deliberate change of
            what the check MEANS, not an incidental one: it stops being
            "does Kaggle match my local file" (there is no local file to
            match) and becomes "does every account see the same copy the
            owner sees" -- which is the property that actually matters for
            a fleet render, and arguably what the check was always really
            enforcing.

        Sharing is RE-VERIFIED here for every account in `accounts`, never
        assumed from whatever sharing happened at the original upload:
        accounts can be added to the fleet after a scene was last rendered,
        and a READER grant can lapse. sharing.grant_readers() is idempotent
        (a username already on the collaborator list is left alone), so
        re-granting on every call is harmless, and dataset_reachable()/
        _require_matching_dataset() are both live Kaggle calls made fresh
        here, never a cached "this worked once".
        """
        accounts = self.accounts if accounts is None else accounts
        if not accounts:
            raise ValueError("add at least one account before launching")

        # Reset, not accumulated: a stale entry from a PREVIOUS
        # prepare_dataset()/launch() call must never be misread as
        # reflecting THIS one. Unlike those, this method never shares
        # with accounts outside `accounts` at all (see this method's own
        # docstring -- sharing here is deliberately scoped to the render
        # subset), so there is nothing of ITS OWN to record here either;
        # this exists purely so a caller reading unshared_accounts after
        # this call does not see a PREVIOUS call's leftovers and mistake
        # them for current.
        self.unshared_accounts = {}

        # A local check, same as require_free() everywhere else in this
        # module -- done before any network call so a busy fleet fails
        # fast without first paying for a whoami() or a dataset listing.
        self.require_free(accounts)

        clients, usernames = self._resolve_clients(accounts)

        # The dataset's real Kaggle owner is NOT always self.accounts[0]:
        # this library is explicitly cross-account (Scene.owner exists,
        # and list_scenes() lists every configured account's own
        # datasets, precisely because a scene can belong to any of them),
        # so a scene rendered here can be owned by any configured
        # account. Only that account's own token can grant or re-verify
        # sharing on it -- Kaggle reserves ADMIN-only actions to the
        # literal owner, never to a READER grant -- so it is found here
        # by matching Kaggle USERNAME against every configured account,
        # not by assuming fleet position.
        dataset_owner = dataset_slug.split("/", 1)[0]
        owner_label = next((label for label, username in usernames.items()
                            if username == dataset_owner), None)
        if owner_label is None:
            # Not among the accounts already resolved for THIS launch's
            # render subset -- look tolerantly at every other configured
            # account too: a dead account elsewhere in the fleet, that
            # this render never asked to use, must not block rendering a
            # scene it does not even own.
            others = [a for a in self.accounts if a.label not in usernames]
            other_clients, other_usernames = self._resolve_clients_tolerant(
                others)
            owner_label = next(
                (label for label, username in other_usernames.items()
                 if username == dataset_owner), None)
            if owner_label is not None:
                clients = {**clients, owner_label: other_clients[owner_label]}
                usernames = {**usernames,
                            owner_label: other_usernames[owner_label]}

        if owner_label is None:
            raise ValueError(
                f"dataset {dataset_slug!r} is owned by {dataset_owner!r} on "
                "Kaggle, but no configured account in this fleet has that "
                "username. Only the account that owns a dataset can grant "
                "or re-verify sharing on it. Nothing has been started. Add "
                "that account under Manage accounts…, or confirm its "
                "stored username matches what Kaggle reports (Instances -> "
                "Set username), then try again.")

        owner_client, owner_username = clients[owner_label], usernames[owner_label]

        # Confirm a .blend genuinely exists BEFORE anything else -- see
        # NoBlendInDatasetError. No point granting access to, or pushing a
        # kernel against, a dataset that was never actually a scene.
        blend_name, expected_size = _find_blend_file(owner_client, dataset_slug)

        job_id = uuid.uuid4().hex[:8]
        buckets = assign_frames(start_frame, end_frame, len(accounts))

        dataset_name = dataset_slug.split("/", 1)[-1]
        stem = (dataset_name[: -len("-blend")] if dataset_name.endswith("-blend")
                else dataset_name)
        # Defensive, not load-bearing: every dataset this app itself
        # uploads already has a capped stem baked into its slug (slug_stem
        # ran at upload time), so this only matters for a dataset that got
        # onto Kaggle some other way -- never raises here, matching
        # scene_key's own "describes an ALREADY-existing thing" philosophy
        # rather than slug_stem's "refuse before an upload" one.
        stem = _capped_stem(stem) or "scene"

        friends = [a for a in accounts if a.label != owner_label]
        if friends:
            self._require_real_usernames(friends, usernames, clients)
            sdk = owner_client._sdk_factory(owner_client.token)
            current = sharing.get_settings(sdk, owner_username, dataset_name)
            try:
                sharing.grant_readers(sdk, owner_username, dataset_name,
                                      [usernames[a.label] for a in friends],
                                      current)
            except Exception as e:
                raise _explain_bad_collaborators(e, friends, usernames) from e

            # Proves access actually landed, not just that the grant call
            # returned cleanly -- see UnreachableAccountsError. Every
            # account in THIS launch is required here (there is no
            # optional/best-effort split the way prepare_dataset's
            # `required` has -- everyone in `accounts` is about to have a
            # kernel pushed).
            unreachable = [usernames[a.label] for a in friends
                          if not clients[a.label].dataset_reachable(dataset_slug)]
            if unreachable:
                raise UnreachableAccountsError(
                    "granted READER access but the dataset is still not "
                    f"reachable for: {', '.join(unreachable)}. Nothing has "
                    "been started -- retry once Kaggle's grant has "
                    "propagated.")

        # Every account in THIS launch, including the owner -- see this
        # method's own docstring for why this check now means "matches the
        # owner's copy" rather than "matches a local file". `stale_message`
        # replaces _require_matching_dataset's default wording (Fix round
        # 1, Important 2): that default talks about "the local file about
        # to be rendered" and says launching again re-uploads it -- both
        # false here, where there is no local file at all.
        for account in accounts:
            username = usernames[account.label]
            _require_matching_dataset(
                clients[account.label], username, dataset_slug, blend_name,
                expected_size,
                stale_message=lambda remote_size, u=username:
                _owner_copy_mismatch_message(u, blend_name, expected_size,
                                             remote_size))

        st = FleetState(job_id=job_id, blend_name=blend_name,
                        start_frame=start_frame, end_frame=end_frame,
                        workers=[], started_at=time.time())

        # Same incremental-persist contract as launch(): push_kernel ALWAYS
        # starts a run, so whatever has been pushed already spends quota
        # and must be on disk, cancellable and collectable, even if a later
        # account in this same loop fails.
        try:
            for account, frames in zip(accounts, buckets):
                client = clients[account.label]
                username = usernames[account.label]
                kernel_slug = f"{username}/{stem}-render-{job_id}"

                kern_dir = self.work_dir / f"kern_{account.label}"
                build(frames, settings, dataset_slug, kern_dir, kernel_slug)
                client.push_kernel(kern_dir)

                st.workers.append(WorkerState(
                    label=account.label, username=username,
                    kernel_slug=kernel_slug, frames=frames,
                    started_at=time.time()))
                self._save(st)
        finally:
            if st.workers:
                self._save(st)
        return st

    def poll_all(self) -> list[FleetState]:
        """Refresh every tracked job's worker states, not just the newest.

        poll() used to call load() -- the single newest job -- so with two
        concurrent jobs the OLDER one's workers were never refreshed at
        all: they sat "queued" forever, Kaggle's real state never reached
        the state file, and busy_labels()/require_free() (which read
        w.state straight off disk) never saw them finish, so those
        accounts could never come free for a new launch (measured with
        two live jobs).

        Nothing is saved when there is nothing to refresh (Task 5 fix
        round 1, IMPORTANT 2) -- mirrors poll()'s own original guard,
        which returned early without writing when load() answered None.
        load_jobs() also answers `[]` for a file it could NOT parse (see
        its own docstring / self.unreadable_jobs); writing back here
        regardless would silently replace that unparseable-but-maybe-
        still-readable text with a flat `{"jobs": []}`, destroying the one
        thing a user would need to go cancel a kernel by hand at
        kaggle.com. Guarding on an empty snapshot rather than on
        self.unreadable_jobs specifically means this holds even if some
        future load_jobs() failure mode forgets to populate
        self.unreadable_jobs the way this one now does.

        Re-reads and merges each polled job back by job_id, exactly like
        _save()'s own in-place contract, rather than overwriting the whole
        file with this call's own (now possibly stale) snapshot (Task 5
        fix round 1, IMPORTANT 3) -- dashboard's 30s poll timer and
        _LaunchWorker run concurrently with no mutual exclusion, and a job
        launched WHILE a poll is in flight must survive being polled at
        the exact moment it is created, or its kernels end up running,
        uncancellable and uncollectable. Returns the MERGED result (Task 5
        fix round 2), not the pre-merge snapshot -- poll()'s own "newest
        job" answer must reflect what was actually just written, not a
        tick-stale view from before a concurrent launch was folded in.

        A REVOKED token is deliberately NOT swallowed by the tolerant
        `except Exception` below (Task 5 fix round 2, NEW IMPORTANT): a
        network blip clears itself on the next poll, but a dead token
        never does, and leaving the worker's state exactly as it was
        (this method's usual tolerance, fine for a transient failure)
        would permanently wedge that worker at "queued" -- busy_labels()/
        require_free() read w.state straight off disk, so the account
        could never come free for a new launch again, EVER, and the app
        would never say a word about why. That is the exact failure this
        docstring's first paragraph describes, re-entered a third time,
        except unlike a network blip no retry ever clears it. Handled
        the same way _resolve_clients()/_resolve_clients_tolerant() (see
        their own docstrings) already mark a dead account -- caught
        FIRST, ahead of the generic tolerant branch, precisely because
        active_workers()'s "unreachable counts as not active" pattern
        does not apply here: THAT method fails safe (an unreachable
        worker frees its account); this bare-except would fail unsafe
        (an unreachable worker LOCKS its account) if a revoked token were
        left inside it.
        """
        # Cleared before anything is asked, so this never carries an answer
        # from a previous call into this one: a worker that failed last
        # time and succeeded now must not still read as unreachable.
        self.unreachable_workers = {}
        jobs = self.load_jobs()
        if not jobs:
            return jobs
        by_label = {a.label: a for a in self.accounts}
        for st in jobs:
            for w in st.workers:
                acct = by_label.get(w.label)
                if acct is None:
                    # No token, so this worker cannot be asked about at all
                    # -- reported for the same reason as the tolerant
                    # branch below: its state on disk is whatever it was
                    # when the account still existed, and a caller must be
                    # able to tell that apart from a fresh reading.
                    self.unreachable_workers[w.label] = (
                        "no account with this label is configured any more, "
                        "so there is no token to ask Kaggle with")
                    continue
                try:
                    s = self.client_factory(acct.token).status(w.kernel_slug)
                except RevokedTokenError:
                    # Never a transient failure and never something a
                    # retry fixes -- so, unlike the tolerant branch below,
                    # this worker's state is NOT left alone: "queued"
                    # (ACTIVE_STATES) would lock this account out of every
                    # future launch forever, in total silence. Moved OUT
                    # of ACTIVE_STATES instead, with a message the
                    # dashboard can surface, exactly mirroring how
                    # _resolve_clients() marks the same account dead on
                    # the launch side.
                    #
                    # was_active is read BEFORE w.state is overwritten
                    # below (must-fix 5): a worker that was queued/running
                    # the moment its token died has a kernel that may
                    # still be executing on Kaggle RIGHT NOW, with nobody
                    # able to cancel or collect it through this app any
                    # more -- the orphaned-kernel case in its purest form.
                    # revoked_token_message() alone only talks about the
                    # future ("nothing can run ... until it is replaced"),
                    # which reads as a claim about the CURRENT kernel too
                    # unless this appends the actual state of that kernel,
                    # names it, and points at the one place left to check
                    # or stop it. A worker that was already finished needs
                    # no such warning -- there is no live kernel to lose
                    # track of.
                    was_active = w.state in ACTIVE_STATES
                    acct.verified = False
                    acct.revoked = True
                    w.state = "error"
                    message = revoked_token_message(w.label, acct.token)
                    if was_active:
                        message += (
                            f" The kernel {w.kernel_slug!r} was still "
                            "running when this happened and may STILL be "
                            "running right now -- with this token dead, "
                            "BlendFleet can no longer cancel or collect "
                            "it. Check https://www.kaggle.com/code/"
                            f"{w.kernel_slug} and stop it there by hand "
                            "if it is still active.")
                    w.message = message
                    if not w.finished_at:
                        w.finished_at = time.time()
                    continue
                except Exception as e:      # noqa: BLE001 -- see below
                    # Recorded before being swallowed: leaving the state
                    # untouched is right, but it makes a worker nobody
                    # could ask about look exactly like one Kaggle
                    # confirmed is still running, and the startup check
                    # has to be able to tell those apart before it decides
                    # whether to reconnect a log stream. See
                    # self.unreachable_workers.
                    self.unreachable_workers[w.label] = f"{type(e).__name__}: {e}"
                    # One worker's status check failing for any OTHER
                    # reason (network blip, rate limit) must not abort
                    # refreshing every OTHER worker in every OTHER job --
                    # that is this method's OWN bug (see its first
                    # docstring paragraph above) re-entered through the
                    # other door (Task 5 fix round 1, IMPORTANT 1). Left
                    # exactly as it was; not fatal, and not overwritten
                    # with a guess -- safe here specifically because a
                    # transient failure is expected to clear itself on
                    # the very next poll, unlike RevokedTokenError above.
                    continue
                w.state, w.message = s.state, s.message
                # Terminal is stamped for TERMINAL_STATES explicitly
                # (must-fix 3), never merely for "not in ACTIVE_STATES":
                # a kernel that has been pushed but whose Kaggle session
                # does not exist YET answers "not_started" (and one whose
                # session exists but has not run its first cell answers
                # "new_script") -- neither is active, but neither is
                # finished either. Reading "not active" as "finished" used
                # to free the account for a second launch the moment a
                # poll landed in that window (busy_labels()/require_free()
                # -- see PENDING_STATES) and, because finished_at was
                # never cleared, permanently stamped a render that had not
                # even started as "finished in 0:04" the very first time
                # it came back "running". Stamped once (guarded by `not
                # w.finished_at`) rather than recomputed from "now" at
                # display time, or a finished job would keep ageing every
                # time the dashboard repainted; and cleared if the worker
                # is ever observed back in ACTIVE_STATES, so a stale stamp
                # from an earlier, mistaken poll cannot outlive the render
                # actually starting.
                if w.state in ACTIVE_STATES:
                    w.finished_at = 0.0
                    # Back from the dead (a stamp left by an earlier,
                    # mistaken poll): whatever was read out of a "final"
                    # log then describes a render that is still going, so
                    # the read is allowed to happen again when it really
                    # does stop.
                    w.final_count_checked = False
                    w.final_count_known = False
                elif w.state in TERMINAL_STATES and not w.finished_at:
                    w.finished_at = time.time()
                if w.state in TERMINAL_STATES and not w.final_count_checked:
                    self._read_final_frame_count(acct, w)
        current = self.load_jobs()
        updated_by_id = {st.job_id: st for st in jobs}
        merged = []
        for j in current:
            updated = updated_by_id.get(j.job_id)
            if updated is None:
                merged.append(j)
                continue
            # frames_done is the one field on a worker this method learns
            # only for a STOPPED kernel (_read_final_frame_count) -- while a
            # render is going, Kaggle's status API reports no frame count at
            # all and only the live log stream does (see record_progress).
            # `updated` was built from a snapshot read BEFORE the network
            # round trips above, so a frame the stream persisted while this
            # poll was in flight is in `j` and not in `updated`; writing
            # `updated` out verbatim would silently roll the user's progress
            # bar backwards every 30 seconds. Carried across per worker, and
            # only ever forwards -- EXCEPT past a count read from the
            # worker's own finished log, which is the render's final word
            # and outranks anything a mid-render stream managed to save.
            by_label = {w.label: w for w in j.workers}
            for w in updated.workers:
                was = by_label.get(w.label)
                if (was is not None and was.frames_done > w.frames_done
                        and not w.final_count_known):
                    w.frames_done = was.frames_done
                    w.frames_done_at = was.frames_done_at
            merged.append(updated)
        self.save_jobs(merged)
        return merged

    def _read_final_frame_count(self, account, worker) -> None:
        """Read a STOPPED worker's true frame count out of its own log.

        frames_done is only ever advanced by the live SSE stream
        (record_progress), and Kaggle's kernel-status API reports no frame
        count at all -- so a render that finished while the app was closed
        kept for ever whatever the stream last managed to persist. A worker
        that had in fact rendered both its frames read "FRAMES 1 / 2 saved
        1h ago", and the card went on to say "1 of 2 frames are waiting on
        Kaggle" about a render that was completely done.

        A COMPLETED kernel's log, unlike a running one's, IS fetchable:
        `kernels logs`/`kernels output` return nothing only while a session
        is still live (see log_stream's module docstring and
        KaggleClient.fetch_log_tail, which exists for exactly this reason).
        The notebook's last `PROGRESS frame=... done=N/M` line is the
        render's own final word on how many frames it wrote, and it is
        parsed here with log_stream.PROGRESS_RE -- the same expression the
        live stream uses, not a second copy of it.

        Fired ONCE per worker, guarded by final_count_checked, which is set
        before anything is fetched and stays set whether or not the read
        produced a number. This is a real network call and a finished job
        keeps being polled every 30 seconds for as long as it is tracked;
        retrying would turn one honest "not known" into a permanent
        background download nobody asked for.

        Never invents a number. A log that cannot be fetched, or that
        carries no PROGRESS line, leaves final_count_known False and
        frames_done exactly as it was -- and the payload then reports the
        count as NOT KNOWN rather than dressing the stale figure up as
        current (bridge._frames_done_source). "Collect frames…" stays the
        authoritative list of what actually exists on Kaggle.
        """
        worker.final_count_checked = True
        try:
            client = self.client_factory(account.token)
            text = client.fetch_log_tail(
                worker.kernel_slug,
                self.work_dir / f"finallog_{worker.label}")
        except Exception as e:      # noqa: BLE001
            # Deliberately NOT recorded on self.unreachable_workers: the
            # STATUS call for this worker succeeded, so Kaggle WAS reached
            # and the startup check must keep counting it as answered
            # (see _finish_startup_check's UNCHECKED bucket -- putting it
            # there would announce a finished render as one nobody could
            # ask about). The only thing missing is the frame count, and
            # that is reported as not known, in its own right.
            crash_log.record(
                f"could not read the final frame count for {worker.label} "
                f"({worker.kernel_slug}) out of its kernel log, so that "
                "card shows the count as not known rather than as a stale "
                f"number. {type(e).__name__}: {e}")
            return
        matches = PROGRESS_RE.findall(text or "")
        if not matches:
            # A real possibility, not a bug: the tail may be all Blender
            # output, the render may have died before its first frame, or
            # BR_PROGRESS output may have been trimmed away. Saying "0"
            # here would be an invented reading.
            return
        # The LAST match, never the first: the tail holds every PROGRESS
        # line the render emitted and only the final one carries the
        # final count. Group 2 is `done` (group 1 is the frame number,
        # group 3 the total) -- see log_stream.PROGRESS_RE.
        worker.frames_done = int(matches[-1][1])
        worker.frames_done_at = time.time()
        worker.final_count_known = True

    def poll(self) -> FleetState | None:
        """Kept for every existing caller (bridge.py's timer, the Qt
        dashboard): refreshes EVERY tracked job via poll_all(), same as
        before this cost nothing extra when there was only ever one, and
        answers with the newest -- exactly load()'s own definition of
        "the" job -- so nothing downstream has to change.
        """
        jobs = self.poll_all()
        return jobs[-1] if jobs else None

    def _cancel_workers(self, workers: list[WorkerState]) -> list[CancelResult]:
        """Cancel exactly `workers`, reporting the outcome for each.

        Shared by cancel_job() and cancel_all() so the two can never
        answer a cancel request differently. Failures are RETURNED, never
        swallowed: "I clicked cancel and nothing happened" must not look
        identical to success when the difference is hours of somebody
        else's GPU quota.
        """
        by_label = {a.label: a for a in self.accounts}
        results: list[CancelResult] = []
        for w in workers:
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

    def cancel_job(self, job_id: str) -> list[CancelResult]:
        """Cancel every worker in every tracked job named `job_id`, leaving
        every OTHER job's kernels running untouched.

        cancel_all() used to be the only cancel there was, and it only
        ever read load() -- the single newest job -- so with two jobs
        live, cancelling looked like it worked but silently left an
        older job's kernels running (and its accounts un-freed). This is
        the per-job counterpart: a user who cancels the scene they are
        looking at must never stop a different scene they never asked
        about. An unknown job_id cancels nothing (empty list), same as
        "no job at all" -- there is genuinely nothing to do.

        Matches EVERY job with this id, not just the first (Task 5 fix
        round 1, Minor): job_id is only 32 bits of uuid4 and save_jobs()
        does not itself forbid a duplicate (see
        forget_job()'s own docstring on this same class of bug). Unlike
        forget_job() -- which deliberately touches only ONE duplicate, so
        it cannot drop two jobs for the price of one -- a cancel that
        stopped only the first of two duplicates would leave the second
        one's kernel running and billing, which is the one outcome a
        cancel action must never produce.
        """
        matches = [j for j in self.load_jobs() if j.job_id == job_id]
        return [result for st in matches
                for result in self._cancel_workers(st.workers)]

    def cancel_all(self) -> list[CancelResult]:
        """Cancel every ACTIVE worker in every tracked job.

        Deliberately every job, not just load()'s newest: cancel_all() is
        the fleet-wide "stop everything" action, and a second job left
        running because it was not the most recent one would keep
        spending its accounts' quota while the user believes nothing is
        rendering any more.

        Filtered to ACTIVE_STATES (Task 5 fix round 1, IMPORTANT 5): once
        this started reading every tracked job instead of just the
        newest, it also started re-"cancelling" every job that had
        already finished days ago and was simply never forgotten -- each
        one answering False (nothing to cancel) and landing in the
        dashboard's "N account(s) could not be cancelled and may still be
        running" warning, degrading that warning into routine noise (plus
        a wasted HTTP call) for kernels that were never running in the
        first place.
        """
        return [result for st in self.load_jobs()
                for result in self._cancel_workers(
                    [w for w in st.workers if w.state in ACTIVE_STATES])]

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

        Searches load_jobs() -- every tracked job -- not load()'s single
        newest one (must-fix 1): the Instances-page Stop button and
        cancelInstance() both reach this method by label, and with two
        scenes live at once the account being stopped is just as likely
        to belong to an OLDER job as to the newest. Reading only load()
        found nothing for any but the newest job's workers and reported
        "already stopped" for a kernel this call never even looked at --
        while it kept running and billing.
        """
        worker = next(
            (w for j in self.load_jobs() for w in j.workers
             if w.label == label and w.state in ACTIVE_STATES), None)
        if worker is None:
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

        Searches load_jobs() -- every tracked job, newest first -- not
        load()'s single newest one (must-fix 1, same defect as
        cancel_worker() above): an errored worker belonging to an OLDER
        job returned "" here, silently, as if it had never failed at all.
        """
        worker = next(
            (w for j in reversed(self.load_jobs()) for w in j.workers
             if w.label == label and w.state == "error"), None)
        if worker is None:
            return ""
        acct = next((a for a in self.accounts if a.label == label), None)
        if acct is None:
            raise RuntimeError(
                f"no account with label {label!r} is configured any more, "
                "so there is no token to fetch its kernel log with")
        client = self.client_factory(acct.token)
        return client.fetch_log_tail(worker.kernel_slug,
                                     self.work_dir / f"log_{label}")
