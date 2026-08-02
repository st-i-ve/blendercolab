from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

FRAME_RE = re.compile(r"_(\d+)\.(png|jpg|jpeg)$", re.I)


@dataclass
class CollectReport:
    copied: int = 0
    missing_frames: list[int] = field(default_factory=list)
    per_worker: dict[str, int] = field(default_factory=dict)


def _wipe(staging: Path) -> None:
    """Remove a staging dir, tolerating a file another process still holds."""
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)


def collect(fleet_state, accounts, client_factory: Callable,
            dest: Path) -> CollectReport:
    """Pull every worker's output into one folder, renamed by real frame number.

    Missing frames are reported explicitly: a partial render must be visibly
    partial rather than quietly looking finished.
    """
    dest.mkdir(parents=True, exist_ok=True)
    by_label = {a.label: a for a in accounts}
    stem = Path(fleet_state.blend_name).stem
    report = CollectReport()
    found: set[int] = set()

    for w in fleet_state.workers:
        acct = by_label.get(w.label)
        if acct is None:
            report.per_worker[w.label] = 0
            continue
        client = client_factory(acct.token)
        staging = dest / f".raw_{w.label}"
        # Staging must start empty. Left-over frames from an EARLIER job
        # collected into this same folder would be re-globbed into `found`
        # and silently subtracted from missing_frames -- exactly inverting
        # the guarantee this function makes.
        _wipe(staging)
        if staging.exists():
            # rmtree is best-effort (a locked file on Windows). If the old
            # frames are still there, FAIL LOUDLY: silently under-reporting
            # missing frames is the one outcome this function exists to
            # prevent.
            raise RuntimeError(
                f"could not clear stale staging folder {staging}. Delete it "
                f"and collect again -- leaving it would make this report "
                f"claim frames were rendered when they were not.")
        try:
            files = client.fetch_output(w.kernel_slug, staging)

            n = 0
            for src in sorted(files):
                m = FRAME_RE.search(src.name)
                if not m:
                    continue
                frame = int(m.group(1))
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
        finally:
            _wipe(staging)

    expected = range(fleet_state.start_frame, fleet_state.end_frame + 1)
    report.missing_frames = sorted(f for f in expected if f not in found)
    return report
