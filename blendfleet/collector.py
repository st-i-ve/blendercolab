from __future__ import annotations

import re
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from blendfleet.fleet import slugify_stem
from blendfleet.notebook_builder import ARCHIVE_SUFFIX

FRAME_RE = re.compile(r"_(\d+)\.(png|jpg|jpeg)$", re.I)

# How many accounts download at once. Four because that is where the
# measurement flattened, not because it is a round number -- the figures
# are in the long comment inside collect().
COLLECT_FANOUT = 4


@dataclass
class CollectReport:
    copied: int = 0
    missing_frames: list[int] = field(default_factory=list)
    per_worker: dict[str, int] = field(default_factory=dict)
    # Task 5: a worker's archive existed but zipfile could not open/read it
    # (a truncated write, most likely). Never fatal -- the worker's loose
    # frames are used instead -- but must be visible, not silently eaten.
    archive_errors: dict[str, str] = field(default_factory=dict)
    # Task 6: a worker's whole fetch failed (network, revoked token, dead
    # kernel...). Recorded instead of raised so one worker's failure never
    # aborts collecting the rest of the fleet -- that worker's frames
    # simply stay unaccounted for and fall out in missing_frames exactly
    # as if nothing had been rendered yet.
    worker_errors: dict[str, str] = field(default_factory=dict)
    # The single zip this collect actually wrote, or None when it wrote
    # nothing. Deliberately None -- not a path to an empty zip -- when no
    # frame was collected at all: an empty <scene>.zip sitting in the
    # user's folder looks exactly like a delivered render until they open
    # it, and "absent" is the honest reading of "nothing came back", not
    # "zero frames, here is your file". Every caller must say which of the
    # two happened rather than claiming a destination it never wrote to.
    archive_path: Path | None = None
    # Non-empty ONLY when the name this collect wanted (`<scene>.zip`, or
    # `<scene>-<account>.zip` for a single-instance download) was already
    # taken, in which case it holds that wanted file name and
    # `archive_path` points at the numbered sibling actually written. The
    # UI needs both to explain the odd name; see _unique_archive_path for
    # why the existing file is never overwritten.
    wanted_name: str = ""


def _wipe(staging: Path) -> None:
    """Remove a staging dir, tolerating a file another process still holds."""
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)


def _frame_number(name: str) -> int | None:
    m = FRAME_RE.search(name)
    return int(m.group(1)) if m else None


def _safe_zip_members(zf: zipfile.ZipFile, extract_dir: Path) -> list[str]:
    """Entry names from `zf` that are safe to extract under `extract_dir`.

    Defence against zip-slip: an entry name containing '../' segments, an
    absolute path, or a backslash could otherwise make `extractall` write
    outside `extract_dir` entirely. This app's own notebook only ever
    writes flat, basename-only entries (`arcname=os.path.basename(f)`), so
    an entry that fails this check is itself evidence the archive is
    corrupt or tampered with -- it is skipped exactly like a frame this
    worker never rendered, never trusted onto disk.
    """
    resolved_root = extract_dir.resolve()
    safe = []
    for name in zf.namelist():
        if name.endswith("/"):
            continue  # directory entry -- nothing to extract
        if "\\" in name:
            continue
        candidate = (extract_dir / name).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            continue
        safe.append(name)
    return safe


def _resolve_frame_sources(files: list[Path], staging: Path,
                           label: str, report: CollectReport) -> dict[int, Path]:
    """Map frame number -> the file to copy for it, from whatever
    `client.fetch_output[_with_progress]` returned for one worker.

    The archive (if present and openable) is preferred, but every frame
    number found in the LOOSE files is folded in too rather than
    discarded -- a session can be killed at the exact instant a frame's
    PNG has been written but before that frame has been appended to the
    archive (see notebook_builder.py: the archive is appended to right
    after each frame succeeds, not before), which would otherwise make
    the archive lag the loose folder by exactly one frame and silently
    under-report it as missing. `setdefault` below means the archive
    wins on any frame number both sources agree on; the loose files only
    ever fill a genuine gap.

    A corrupt archive is reported on `report.archive_errors` and treated
    as if no archive had been returned at all -- the loose files are
    still used, never a crash. Caught broadly (not just BadZipFile):
    a truncated/interrupted write (the exact failure mode Task 5's
    incremental append can leave behind if a session is killed mid-write)
    can just as easily surface as a plain OSError/EOFError from
    `extractall` depending on exactly where the corruption falls, and
    every one of those must fall back to loose frames identically --
    letting any of them escape uncaught would mark this worker as failed
    (report.worker_errors) instead, discarding perfectly good loose files
    sitting right next to the archive.

    Still goes through an EXTRACT (rather than copying entries straight
    from the worker's zip into the merged one) because the loose-file
    fallback above has to be able to win on a frame the archive is
    missing, and comparing the two sources by frame number is only
    possible once both are plain files on disk. Extraction is cheap here:
    the worker's archive is ZIP_STORED, so this is a byte copy, not a
    decompress.
    """
    frame_paths: dict[int, Path] = {}
    archive = next((f for f in files if f.suffix.lower() == ARCHIVE_SUFFIX), None)
    if archive is not None:
        try:
            extract_dir = staging / "_extracted"
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract_dir, members=_safe_zip_members(zf, extract_dir))
            for p in sorted(extract_dir.rglob("*")):
                if not p.is_file():
                    continue
                frame = _frame_number(p.name)
                if frame is not None:
                    frame_paths[frame] = p
        except Exception as e:
            report.archive_errors[label] = (
                f"{label}'s archive ({archive.name}) is corrupt ({e}) -- "
                "falling back to this worker's loose frames.")

    for p in files:
        if p.suffix.lower() == ARCHIVE_SUFFIX:
            continue
        frame = _frame_number(p.name)
        if frame is not None:
            frame_paths.setdefault(frame, p)
    return frame_paths


def _fetch_with_retry(client, w, staging: Path, on_progress, sleep,
                      attempts: int = 3) -> list[Path]:
    """One worker's output files, retrying a dropped connection.

    Every attempt starts from an empty staging folder: a truncated file
    left behind by a failed attempt would otherwise be indistinguishable
    from a complete one, and could be copied out as a "collected" frame.

    Task D: `fetch_output_with_progress` is used whenever the client has
    it, even when this caller asked for no progress at all -- a no-op
    callback is substituted rather than falling through to
    `fetch_output`. The two are not interchangeable transports.
    `fetch_output` is the kaggle package's own `kernels_output()`, which
    reads each response body in one `.content` shot: no progress AND, more
    importantly, no seam where a timeout can be bounded (commit beb0f0d
    bounded every other Kaggle call; this was the one hole left), so a
    stalled socket there hangs the download thread with nothing on screen
    moving. Choosing the transport on whether a caller happened to want a
    progress bar meant the download the user could not see was also the
    only one that could hang forever. `fetch_output` survives purely as
    the fallback for a client that genuinely lacks the newer method --
    which is every test double in tests/test_collector.py and
    tests/test_transient_failures.py.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if hasattr(client, "fetch_output_with_progress"):
                report_progress = on_progress or (lambda label, p: None)
                return client.fetch_output_with_progress(
                    w.kernel_slug, staging,
                    on_progress=lambda p, label=w.label: report_progress(label, p))
            return client.fetch_output(w.kernel_slug, staging)
        except Exception as e:
            last = e
            if attempt == attempts:
                break
            _wipe(staging)
            staging.mkdir(parents=True, exist_ok=True)
            sleep(min(2 ** (attempt - 1), 8))
    raise last


def _archive_base_name(fleet_state, worker_label: str | None) -> str:
    """The zip's file name, minus ".zip" and minus any de-duplication.

    Fleet-wide it is the scene, so the one file the user is handed says
    which render it holds without being opened.

    A PER-INSTANCE download ("can I just download fleet instance 2")
    additionally carries that account's label, because such a zip holds
    only that account's own stride of frames. Naming it `<scene>.zip`
    like the merged one would leave a SLICE of a render indistinguishable
    on disk from the whole of it -- and, under the never-overwrite rule
    in _unique_archive_path, it would also shove the eventual real merged
    archive out to `<scene>-2.zip` for no reason at all. The label is
    slugified through the same function the scene key uses so a nickname
    like "Stive's laptop / 2" cannot produce a path separator or an
    illegal Windows character; a label that slugifies to nothing at all
    (all emoji, say) falls back to a literal word rather than collapsing
    into the fleet-wide name.
    """
    base = fleet_state.scene_key
    if worker_label is not None:
        base = f"{base}-{slugify_stem(worker_label) or 'instance'}"
    return base


def _unique_archive_path(dest: Path, base: str) -> Path:
    """`dest/<base>.zip`, or `dest/<base>-2.zip`, `-3.zip`... if taken.

    Re-collecting the same scene into the same folder must NEVER replace
    the zip already sitting there. That file may be the only copy of an
    earlier, LONGER render -- collecting a half-finished job, then
    collecting again after two more accounts finish, is the flow this app
    actively tells people to use ("Collect again once those accounts
    finish"), and it is just as easy to do it the other way round after
    re-launching a shorter frame range. Silently overwriting would
    destroy already-rendered, already-paid-for output with no way back.
    Merging into the existing zip was the alternative and is worse: it
    would fold two different jobs' frames together behind one name with
    nothing on disk recording that it happened.

    The caller reports BOTH names to the user (CollectReport.wanted_name)
    so a "-2" is explained rather than mysterious.

    Compared case-INSENSITIVELY against what is really in the directory,
    not with Path.exists(): two scenes whose stems differ only by case
    ("Kitchen.blend" vs "kitchen.blend") slugify to the identical
    scene_key, and on Windows/macOS "Kitchen.zip" and "kitchen.zip" are
    the same file. Folding the case here makes every platform behave the
    way the most restrictive one has to.
    """
    taken = {p.name.casefold() for p in dest.iterdir()} if dest.is_dir() else set()
    name = f"{base}.zip"
    n = 1
    while name.casefold() in taken:
        n += 1
        name = f"{base}-{n}.zip"
    return dest / name


def collect(fleet_state, accounts, client_factory: Callable,
            dest: Path, *, worker_label: str | None = None,
            on_progress: Callable[[str, object], None] | None = None,
            sleep: Callable[[float], None] = time.sleep,
            fanout: int = COLLECT_FANOUT,
            ) -> CollectReport:
    """Merge every worker's rendered frames into ONE zip in `dest`.

    `dest` gets exactly one new file -- `<scene>.zip`, named from
    `fleet_state.scene_key` -- and nothing else: no loose frames, no
    per-scene subfolder. Inside it the frames keep the names that make
    them useful when the zip is opened (`<stem>_0001.png`, numbered by
    real frame number, in the format the render actually produced), so
    unzipping gives the same folder of frames this used to write directly
    and the user decides when and where to unpack it.

    The scene subfolder this used to create is gone because the zip's own
    NAME now does that job: two scenes rendering into the same chosen
    folder can no longer collide, since a name already taken is never
    overwritten (see _unique_archive_path).

    Missing frames are reported explicitly: a partial render must be
    visibly partial rather than quietly looking finished. Prefers each
    worker's single zip archive when Kaggle actually has one (Task 5: one
    download instead of hundreds), falling back to loose per-frame images
    when there is no archive, or when it will not open -- see
    _resolve_frame_sources. Either way those frames end up in the merged
    zip; the fallback is about where a frame is READ from, never about
    whether it is delivered.

    `worker_label`, if given, collects ONLY that one worker (Task 6: "can
    I just download fleet instance 1") -- `missing_frames` is then scoped
    to that worker's own assigned frames, not the whole fleet's range,
    since this worker was never responsible for anyone else's frames, and
    the zip is named for that account as well as the scene (see
    _archive_base_name). An unknown label collects nothing (empty report,
    no zip) rather than raising.

    `on_progress`, if given, is called as `on_progress(label, progress)`
    for every DownloadProgress tick reported by whichever worker is
    currently downloading (see kaggle_client.KaggleClient.
    fetch_output_with_progress). Task D: passing nothing no longer
    switches transports, it only silences the ticks -- see
    _fetch_with_retry for why that distinction mattered.

    A worker's fetch failing (network, revoked token, ...) is recorded in
    `report.worker_errors` rather than raised, so it can never abort
    collecting the rest of the fleet -- see the class docstring on
    `CollectReport.worker_errors`.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    by_label = {a.label: a for a in accounts}
    # A worker records BOTH its label (the user's own nickname for the
    # account, which they can edit at any time) and its username (the
    # account's actual Kaggle identity, which they cannot). Matching on
    # label alone means renaming an account while a job is running
    # orphans that job's frames -- they are on Kaggle, rendered and paid
    # for, and the app can no longer name a token to fetch them with.
    # Username is the stable key, so it is the fallback.
    by_username = {getattr(a, "username", None): a for a in accounts
                   if getattr(a, "username", None)}
    # The raw, case-preserved stem, which is what makes the entry names
    # inside the zip recognisable as this scene. It no longer needs the
    # job_id disambiguation the loose-frame layout required for stems
    # differing only by case: those two jobs now land in two separate
    # zip FILES (kitchen.zip and kitchen-2.zip), so their entries cannot
    # be the same path as each other any more.
    stem = Path(fleet_state.blend_name).stem
    report = CollectReport()
    found: set[int] = set()

    workers = fleet_state.workers
    if worker_label is not None:
        workers = [w for w in workers if w.label == worker_label]

    # Staging is named from the job id and the worker's POSITION, never
    # from its label: a label is a nickname the user types, so it can
    # hold a slash, a colon or a quote, none of which can appear in a
    # Windows directory name -- and this folder is created inside the
    # destination the user picked, so an unusable name here would fail
    # the whole download of an account whose only sin is being called
    # "Stive's laptop / 2". The position comes from the FULL worker list
    # so it stays the same whether this call is collecting the fleet or
    # just one instance, which is what keeps a fleet-wide collect and a
    # per-instance download of the same job from sharing a folder. It
    # used to live inside the per-scene subfolder, which is what kept two
    # different scenes collected into one chosen folder apart; with that
    # folder gone, the job id does that instead.
    #
    # Cleared for EVERY worker up front, before a single byte is
    # downloaded: left-over frames from an earlier job would be
    # re-globbed into `found` and silently subtracted from missing_frames
    # -- exactly inverting the guarantee this function makes -- and
    # failing on that after four of five workers had already downloaded
    # would both waste the download and throw away the frames it
    # produced, since a raise from here abandons the merged zip (see the
    # `finally` below). A precondition belongs before the work, not in
    # the middle of it.
    positions = {w.label: i for i, w in enumerate(fleet_state.workers)}
    stagings = {w.label: dest / f".raw_{fleet_state.job_id}_{positions[w.label]}"
                for w in workers}
    for label, staging in stagings.items():
        _wipe(staging)
        if staging.exists():
            raise RuntimeError(
                f"could not clear stale staging folder {staging}. Delete it "
                f"and collect again -- leaving it would make this report "
                f"claim frames were rendered when they were not.")

    # Built under a hidden, job-scoped temporary name and only renamed
    # into place once every worker has been through. A collect that dies
    # half way (or is killed) must never leave a file called
    # "<scene>.zip" holding half a render: on disk that is
    # indistinguishable from the finished article, which is the same
    # mistake as counting a truncated download as a collected frame.
    part = dest / f".{fleet_state.job_id}-collecting.zip.part"
    part.unlink(missing_ok=True)   # a previous interrupted collect's leftovers

    try:
        # ---- WHO CAN BE ASKED, settled before a byte moves ------------
        # Serial and in worker order on purpose: this is where the "no
        # configured account" verdict is recorded, and a report written from
        # one thread needs no lock around it.
        askable = []
        for w in workers:
            acct = by_label.get(w.label) or by_username.get(w.username)
            if acct is None:
                # This used to skip in silence. The worker's frames then
                # landed in missing_frames with nothing to explain them, which
                # on screen is indistinguishable from an account that rendered
                # nothing at all -- and that is exactly how a real 25-frame
                # render (2026-08-12) was read as "two accounts did not
                # render", when both had in fact finished every frame.
                report.per_worker[w.label] = 0
                report.worker_errors[w.label] = (
                    f"{w.username or w.label} rendered "
                    f"{len(w.frames)} frame(s), but no configured account "
                    f"matches it any more, so BlendFleet has no token to "
                    f"download them with. Nothing is lost -- the frames are "
                    f"still on Kaggle. Re-add that account under Manage "
                    f"accounts… (the Kaggle username is {w.username or 'unknown'}) "
                    f"and download again.")
                continue
            askable.append((w, acct))

        # ---- FETCH, ALL ACCOUNTS AT ONCE ------------------------------
        # MEASURED, not assumed. Against a real five-account job (2026-09-05,
        # waydown, 35 MB of output): 60.9s one account after another, 22.1s
        # with all five downloading together; repeated, 61.0s against 26.2s.
        # A single connection sits at ~0.58 MB/s however long it is given, so
        # the limit is per connection and not this machine's link.
        #
        # The aggregate flattens near 1.5 MB/s though -- five streams gave
        # 2.3-2.75x, nowhere near 5x -- so something above one connection (the
        # link, or a Kaggle-side cap) becomes the ceiling. Hence a default of
        # four: past that, more streams buy noise rather than throughput. On
        # the 536-frame render sitting on these accounts (~1.8 GB) this is the
        # difference between about 54 minutes and about 21.
        #
        # DOWNLOADS ONLY. The merge below stays serial for two reasons that
        # are not about speed: a ZipFile open for writing is one cursor, and
        # "the first worker to contribute a frame keeps it" has to stay
        # deterministic, which means walking the workers in their own order.
        fetched: dict[str, list[Path]] = {}

        def _fetch(pair):
            w, acct = pair
            # The client is built HERE, on the thread that will use it, not
            # in the loop above: each account gets its own kaggle client and
            # therefore its own connection, which is the whole point -- one
            # shared client would serialise these back into one stream.
            client = client_factory(acct.token)
            # Retried as a whole, on top of the per-file retry inside
            # downloader.fetch_files. Measured 2026-08-11: a finished
            # 15-minute render reported ZERO frames because one download was
            # truncated, and simply calling collect again recovered all 15.
            # The frames are already rendered and paid for by the time this
            # runs -- a transient socket error must not be what loses them.
            return w.label, _fetch_with_retry(client, w, stagings[w.label],
                                              on_progress, sleep)

        if askable:
            with ThreadPoolExecutor(
                    max_workers=max(1, min(fanout, len(askable))),
                    thread_name_prefix="blendfleet-collect") as pool:
                futures = {pool.submit(_fetch, pair): pair[0] for pair in askable}
                for future in as_completed(futures):
                    w = futures[future]
                    try:
                        label, files = future.result()
                    except Exception as e:      # noqa: BLE001 -- per worker
                        # Unchanged contract: one worker failing (dead kernel,
                        # revoked token, network blip) is recorded and every
                        # other worker still collects. Its frames simply stay
                        # out of the found set, which is exactly correct --
                        # they were not collected.
                        report.worker_errors[w.label] = str(e)
                        report.per_worker[w.label] = 0
                    else:
                        fetched[label] = files

        # ---- MERGE, ONE AT A TIME, IN WORKER ORDER --------------------
        # ZIP_STORED for exactly the reason notebook_builder.py uses it on
        # the worker side: PNG and JPEG are already compressed, so
        # deflating them burns CPU over the whole render for a percent or
        # two. This is a container, not a compressor.
        with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED) as merged:
            for w in workers:
                files = fetched.get(w.label)
                if files is None:
                    continue        # never asked, or its fetch failed
                staging = stagings[w.label]
                try:
                    frame_paths = _resolve_frame_sources(files, staging,
                                                         w.label, report)

                    n = 0
                    for frame in sorted(frame_paths):
                        if frame in found:
                            # An earlier worker already contributed this
                            # frame. Writing it again would put two entries
                            # under one name in the zip, and extractors
                            # disagree about which of them wins -- so the
                            # first writer keeps it, which also keeps
                            # copied counting frames rather than copies.
                            continue
                        src = frame_paths[frame]
                        # Keep the source extension: the render format is a
                        # user choice (PNG or JPEG) and a .jpg renamed to
                        # .png is a corrupt file, not a converted one.
                        suffix = src.suffix.lower()
                        merged.write(src, arcname=f"{stem}_{frame:04d}{suffix}")
                        found.add(frame)
                        n += 1
                        report.copied += 1
                    report.per_worker[w.label] = n
                except Exception as e:      # noqa: BLE001 -- per worker
                    report.worker_errors[w.label] = str(e)
                    report.per_worker[w.label] = 0

        if report.copied:
            base = _archive_base_name(fleet_state, worker_label)
            final = _unique_archive_path(dest, base)
            part.rename(final)
            report.archive_path = final
            if final.name != f"{base}.zip":
                report.wanted_name = f"{base}.zip"
    finally:
        # Every staging folder, including those of workers whose fetch
        # failed: the per-worker cleanup that used to do this went with
        # the single loop.
        for staging in stagings.values():
            _wipe(staging)
        # Nothing was collected (or something escaped): no zip. An empty
        # <scene>.zip would read as a delivered render right up until it
        # is opened -- absent is the honest answer, and CollectReport
        # already says how many frames are missing and why.
        part.unlink(missing_ok=True)

    if worker_label is not None:
        expected = sorted({f for w in workers for f in w.frames})
    else:
        expected = range(fleet_state.start_frame, fleet_state.end_frame + 1)
    report.missing_frames = sorted(f for f in expected if f not in found)
    return report
