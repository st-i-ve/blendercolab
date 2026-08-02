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
        files = client.fetch_output(w.kernel_slug, staging)

        n = 0
        for src in sorted(files):
            m = FRAME_RE.search(src.name)
            if not m:
                continue
            frame = int(m.group(1))
            shutil.copy(src, dest / f"{stem}_{frame:04d}.png")
            found.add(frame)
            n += 1
        report.per_worker[w.label] = n
        report.copied += n

    expected = range(fleet_state.start_frame, fleet_state.end_frame + 1)
    report.missing_frames = sorted(f for f in expected if f not in found)
    return report
