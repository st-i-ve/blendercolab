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


def test_second_job_into_the_same_folder_reports_its_own_missing_frames(tmp_path):
    """IMPORTANT 3: stale .raw_<label> staging from job 1 must not be
    re-globbed into job 2's `found` set. That silently UNDER-reports
    missing_frames -- the exact inverse of this function's promise."""
    out = tmp_path / "out"

    job1 = FleetState(job_id="j1", blend_name="r.blend", start_frame=1,
                      end_frame=4,
                      workers=[WorkerState("a0", "u0", "u0/k0", [1, 3]),
                               WorkerState("a1", "u1", "u1/k1", [2, 4])])

    def factory1(tok):
        return FakeClient(tok, ["f_0001.png", "f_0003.png"] if tok.endswith("0"*32)
                          else ["f_0002.png", "f_0004.png"])

    r1 = collect(job1, accts(), factory1, out)
    assert r1.copied == 4 and r1.missing_frames == []
    assert not list(out.glob(".raw_*")), "staging left behind"

    # Job 2 renders the SAME frame range but every worker comes back empty
    # (e.g. both kernels errored). Every frame must be reported missing.
    job2 = FleetState(job_id="j2", blend_name="r.blend", start_frame=1,
                      end_frame=4,
                      workers=[WorkerState("a0", "u0", "u0/k2", [1, 3]),
                               WorkerState("a1", "u1", "u1/k3", [2, 4])])

    def factory2(tok):
        return FakeClient(tok, [])

    r2 = collect(job2, accts(), factory2, out)
    assert r2.copied == 0
    assert r2.missing_frames == [1, 2, 3, 4]
    assert not list(out.glob(".raw_*"))


def test_staging_is_cleaned_up_after_a_successful_collect(tmp_path):
    def factory(tok):
        return FakeClient(tok, ["f_0001.png"] if tok.endswith("0"*32)
                          else ["f_0002.png"])
    out = tmp_path / "out"
    collect(state(), accts(), factory, out)
    assert sorted(p.name for p in out.iterdir()) == ["r_0001.png", "r_0002.png"]


def test_staging_is_cleaned_up_even_when_a_fetch_raises(tmp_path):
    class Boom(FakeClient):
        def fetch_output(self, slug, dest):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "f_0001.png").write_bytes(b"PNG")
            raise RuntimeError("network died mid-download")

    def factory(tok):
        return Boom(tok)

    out = tmp_path / "out"
    with pytest.raises(RuntimeError):
        collect(state(), accts(), factory, out)
    assert not list(out.glob(".raw_*"))


def test_collects_jpeg_frames_keeping_the_extension(tmp_path):
    """IMPORTANT 1: JPEG is a real option in the dashboard. Renaming a .jpg
    to .png would produce a corrupt file, not a converted one."""
    def factory(tok):
        return FakeClient(tok, ["f_0001.jpg", "f_0003.jpg"] if tok.endswith("0"*32)
                          else ["f_0002.jpg", "f_0004.jpg"])
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 4
    assert r.missing_frames == []
    assert sorted(p.name for p in (tmp_path / "out").glob("*.jpg")) == [
        "r_0001.jpg", "r_0002.jpg", "r_0003.jpg", "r_0004.jpg"]


def test_all_frames_missing(tmp_path):
    """All workers return nothing - all frames should be in missing_frames."""
    def factory(tok):
        return FakeClient(tok, [])
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 0
    assert r.per_worker == {"a0": 0, "a1": 0}
    assert r.missing_frames == [1, 2, 3, 4]
