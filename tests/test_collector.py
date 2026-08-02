from pathlib import Path
import pytest
from blendfleet.accounts import Account
from blendfleet.fleet import FleetState, WorkerState
from blendfleet.collector import collect


class FakeClient:
    def __init__(self, token, produce=()):
        self.token = token
        self.produce = produce

    def fetch_output(self, slug, dest):
        dest.mkdir(parents=True, exist_ok=True)
        out = []
        for name in self.produce:
            p = dest / name
            p.write_bytes(b"PNG")
            out.append(p)
        return out


def state():
    return FleetState(job_id="j1", blend_name="r.blend", start_frame=1, end_frame=4,
                      workers=[WorkerState("a0", "u0", "u0/k0", [1, 3]),
                               WorkerState("a1", "u1", "u1/k1", [2, 4])])


def accts():
    return [Account("a0", "KGAT_" + "0"*32), Account("a1", "KGAT_" + "1"*32)]


def test_collects_from_all_workers(tmp_path):
    def factory(tok):
        return FakeClient(tok, ["f_0001.png", "f_0003.png"] if tok.endswith("0"*32)
                          else ["f_0002.png", "f_0004.png"])
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 4
    assert r.missing_frames == []
    assert sorted(p.name for p in (tmp_path / "out").glob("*.png")) == [
        "r_0001.png", "r_0002.png", "r_0003.png", "r_0004.png"]


def test_reports_missing_frames(tmp_path):
    def factory(tok):
        return FakeClient(tok, ["f_0001.png"] if tok.endswith("0"*32) else [])
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 1
    assert r.missing_frames == [2, 3, 4]


def test_per_worker_counts(tmp_path):
    def factory(tok):
        return FakeClient(tok, ["f_0001.png"] if tok.endswith("0"*32) else ["f_0002.png"])
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.per_worker == {"a0": 1, "a1": 1}


def test_duplicate_frame_counted_once(tmp_path):
    """Two workers returning the same frame should count as 1 copied, not 2."""
    def factory(tok):
        return FakeClient(tok, ["f_0002.png"] if tok.endswith("0"*32)
                          else ["f_0002.png"])
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 1
    assert sum(r.per_worker.values()) == 1
    assert len(list((tmp_path / "out").glob("*.png"))) == 1


def test_account_removed_mid_job(tmp_path):
    """Missing account should contribute 0 frames, no crash, frames appear in missing."""
    # Create state with two workers but only one account
    st = FleetState(job_id="j1", blend_name="r.blend", start_frame=1, end_frame=3,
                    workers=[WorkerState("a0", "u0", "u0/k0", [1, 2]),
                             WorkerState("a1", "u1", "u1/k1", [3])])
    accounts = [Account("a0", "KGAT_" + "0"*32)]  # a1 is missing

    def factory(tok):
        return FakeClient(tok, ["f_0001.png", "f_0002.png"] if tok.endswith("0"*32)
                          else ["f_0003.png"])
    r = collect(st, accounts, factory, tmp_path / "out")
    assert r.copied == 2
    assert r.per_worker == {"a0": 2, "a1": 0}
    assert r.missing_frames == [3]


def test_all_frames_missing(tmp_path):
    """All workers return nothing - all frames should be in missing_frames."""
    def factory(tok):
        return FakeClient(tok, [])
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 0
    assert r.per_worker == {"a0": 0, "a1": 0}
    assert r.missing_frames == [1, 2, 3, 4]
