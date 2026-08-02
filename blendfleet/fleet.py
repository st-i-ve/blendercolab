"""Fleet orchestration: split frames across accounts and drive each one.

Coordination is entirely client-side. Each account gets a DISJOINT stride of
frames (via assignment.assign_frames) and its OWN dataset upload -- the
Kaggle API has no way to add dataset collaborators, so N accounts means N
uploads. Accounts never talk to each other; this module just fans work out
and polls each one independently.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable

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

    def launch(self, blend: Path, settings: RenderSettings,
               start_frame: int, end_frame: int) -> FleetState:
        if not self.accounts:
            raise ValueError("add at least one account before launching")

        job_id = uuid.uuid4().hex[:8]
        buckets = assign_frames(start_frame, end_frame, len(self.accounts))
        stem = blend.stem.lower().replace("_", "-")
        workers: list[WorkerState] = []

        for account, frames in zip(self.accounts, buckets):
            client = self.client_factory(account.token)
            username = account.username or client.whoami()

            dataset_slug = f"{username}/{stem}-blend"
            kernel_slug = f"{username}/{stem}-render-{job_id}"

            # Each account needs its OWN copy: the API cannot add dataset
            # collaborators, so N accounts means N uploads.
            sync_blend(client, blend, dataset_slug,
                       self.work_dir / f"ds_{account.label}")

            kern_dir = self.work_dir / f"kern_{account.label}"
            build(frames, settings, dataset_slug, kern_dir, kernel_slug)
            client.push_kernel(kern_dir)

            workers.append(WorkerState(label=account.label, username=username,
                                       kernel_slug=kernel_slug, frames=frames))

        st = FleetState(job_id=job_id, blend_name=blend.name,
                        start_frame=start_frame, end_frame=end_frame,
                        workers=workers)
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

    def cancel_all(self) -> None:
        st = self.load()
        if st is None:
            return
        by_label = {a.label: a for a in self.accounts}
        for w in st.workers:
            acct = by_label.get(w.label)
            if not acct:
                continue
            try:
                self.client_factory(acct.token).cancel(w.kernel_slug)
            except Exception:
                # One account's factory/cancel failing must not strand the
                # rest -- an uncancelled kernel keeps burning GPU quota for
                # hours, so every other worker still gets its shot.
                continue
