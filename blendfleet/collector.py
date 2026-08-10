from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

FRAME_RE = re.compile(r"_(\d+)\.(png|jpg|jpeg)$", re.I)
ARCHIVE_SUFFIX = ".zip"


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


def _wipe(staging: Path) -> None:
    """Remove a staging dir, tolerating a file another process still holds."""
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)


def _frame_number(name: str) -> int | None:
    m = FRAME_RE.search(name)
    return int(m.group(1)) if m else None


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

    A corrupt archive (BadZipFile) is reported on `report.archive_errors`
    and treated as if no archive had been returned at all -- the loose
    files are still used, never a crash.
    """
    frame_paths: dict[int, Path] = {}
    archive = next((f for f in files if f.suffix.lower() == ARCHIVE_SUFFIX), None)
    if archive is not None:
        try:
            extract_dir = staging / "_extracted"
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract_dir)
            for p in sorted(extract_dir.rglob("*")):
                if not p.is_file():
                    continue
                frame = _frame_number(p.name)
                if frame is not None:
                    frame_paths[frame] = p
        except zipfile.BadZipFile as e:
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


def collect(fleet_state, accounts, client_factory: Callable,
            dest: Path, *, worker_label: str | None = None,
            on_progress: Callable[[str, object], None] | None = None
            ) -> CollectReport:
    """Pull worker output into one folder, renamed by real frame number.

    Missing frames are reported explicitly: a partial render must be
    visibly partial rather than quietly looking finished. Prefers each
    worker's single zip archive when Kaggle actually has one (Task 5: one
    download instead of hundreds), falling back to loose per-frame images
    when there is no archive, or when it will not open -- see
    _resolve_frame_sources.

    `worker_label`, if given, collects ONLY that one worker (Task 6: "can
    I just download fleet instance 1") -- `missing_frames` is then scoped
    to that worker's own assigned frames, not the whole fleet's range,
    since this worker was never responsible for anyone else's frames. An
    unknown label collects nothing (empty report) rather than raising.

    `on_progress`, if given, is called as `on_progress(label, progress)`
    for every DownloadProgress tick reported by whichever worker is
    currently downloading (see kaggle_client.KaggleClient.
    fetch_output_with_progress) -- used only when the client exposes that
    method; a client/test-double without it is still collected from, just
    without live progress for that worker.

    A worker's fetch failing (network, revoked token, ...) is recorded in
    `report.worker_errors` rather than raised, so it can never abort
    collecting the rest of the fleet -- see the class docstring on
    `CollectReport.worker_errors`.
    """
    dest.mkdir(parents=True, exist_ok=True)
    by_label = {a.label: a for a in accounts}
    stem = Path(fleet_state.blend_name).stem
    report = CollectReport()
    found: set[int] = set()

    workers = fleet_state.workers
    if worker_label is not None:
        workers = [w for w in workers if w.label == worker_label]

    for w in workers:
        acct = by_label.get(w.label)
        if acct is None:
            report.per_worker[w.label] = 0
            continue
        client = client_factory(acct.token)
        staging = dest / f".raw_{w.label}"
        # Staging must start empty. Left-over frames from an EARLIER job
        # collected into this same folder would be re-globbed into `found`
        # and silently subtracted from missing_frames -- exactly inverting
        # the guarantee this function makes. This check runs BEFORE the
        # per-worker try/except below (unlike a fetch failure) because it
        # is a precondition, not a download outcome: proceeding past an
        # unclearable staging dir risks corrupting every worker's report,
        # not just this one's.
        _wipe(staging)
        if staging.exists():
            raise RuntimeError(
                f"could not clear stale staging folder {staging}. Delete it "
                f"and collect again -- leaving it would make this report "
                f"claim frames were rendered when they were not.")
        try:
            if on_progress is not None and hasattr(client, "fetch_output_with_progress"):
                files = client.fetch_output_with_progress(
                    w.kernel_slug, staging,
                    on_progress=lambda p, label=w.label: on_progress(label, p))
            else:
                files = client.fetch_output(w.kernel_slug, staging)

            frame_paths = _resolve_frame_sources(files, staging, w.label, report)

            n = 0
            for frame in sorted(frame_paths):
                src = frame_paths[frame]
                is_new = frame not in found
                # Keep the source extension: the render format is a user
                # choice (PNG or JPEG) and a .jpg renamed to .png is a
                # corrupt file, not a converted one.
                suffix = src.suffix.lower()
                shutil.copy(src, dest / f"{stem}_{frame:04d}{suffix}")
                found.add(frame)
                if is_new:
                    n += 1
                    report.copied += 1
            report.per_worker[w.label] = n
        except Exception as e:
            # Task 6: one worker's fetch failing (dead kernel, revoked
            # token, network blip) must not abort collecting everyone
            # else -- reported here instead, and this worker's frames
            # simply stay out of `found`, which is exactly correct: they
            # were not actually collected.
            report.worker_errors[w.label] = str(e)
            report.per_worker[w.label] = 0
        finally:
            _wipe(staging)

    if worker_label is not None:
        expected = sorted({f for w in workers for f in w.frames})
    else:
        expected = range(fleet_state.start_frame, fleet_state.end_frame + 1)
    report.missing_frames = sorted(f for f in expected if f not in found)
    return report
