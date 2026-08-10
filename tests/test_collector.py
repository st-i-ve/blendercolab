import zipfile
from pathlib import Path
import pytest
from blendfleet.accounts import Account
from blendfleet.fleet import FleetState, WorkerState
from blendfleet.collector import collect


class FakeClient:
    def __init__(self, token, produce=(), archive=None):
        self.token = token
        self.produce = produce
        # `archive`, if given, is a dict {member_name: bytes} zipped into a
        # single "frames.zip" written alongside (or instead of) the loose
        # files in `produce` -- see the Task 5 tests below.
        self.archive = archive

    def fetch_output(self, slug, dest):
        dest.mkdir(parents=True, exist_ok=True)
        out = []
        for name in self.produce:
            p = dest / name
            p.write_bytes(b"PNG")
            out.append(p)
        if self.archive is not None:
            zpath = dest / "frames.zip"
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
                for name, content in self.archive.items():
                    zf.writestr(name, content)
            out.append(zpath)
        return out


class CorruptArchiveClient(FakeClient):
    """fetch_output returns loose files (the fallback) AND a zip file that
    is not actually a valid zip -- exactly what a truncated/interrupted
    archive write would look like."""

    def fetch_output(self, slug, dest):
        dest.mkdir(parents=True, exist_ok=True)
        out = []
        for name in self.produce:
            p = dest / name
            p.write_bytes(b"PNG")
            out.append(p)
        zpath = dest / "frames.zip"
        zpath.write_bytes(b"this is not a zip file at all")
        out.append(zpath)
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


def test_second_job_into_the_same_folder_via_archives_reports_its_own_missing_frames(
        tmp_path):
    """Same regression as above, but exercising the ARCHIVE path -- Task 5
    must not reintroduce it: job 2's collect() must not see job 1's
    archived frames leak in through stale staging."""
    out = tmp_path / "out"

    def factory1(tok):
        return FakeClient(tok, archive={"f_0001.png": b"PNG"} if tok.endswith("0"*32)
                          else {"f_0002.png": b"PNG"})

    job1 = FleetState(job_id="j1", blend_name="r.blend", start_frame=1, end_frame=2,
                      workers=[WorkerState("a0", "u0", "u0/k0", [1]),
                               WorkerState("a1", "u1", "u1/k1", [2])])
    r1 = collect(job1, accts(), factory1, out)
    assert r1.copied == 2 and r1.missing_frames == []
    assert not list(out.glob(".raw_*"))

    job2 = FleetState(job_id="j2", blend_name="r.blend", start_frame=1, end_frame=2,
                      workers=[WorkerState("a0", "u0", "u0/k2", [1]),
                               WorkerState("a1", "u1", "u1/k3", [2])])

    def factory2(tok):
        return FakeClient(tok, archive={})  # both kernels errored: empty archive

    r2 = collect(job2, accts(), factory2, out)
    assert r2.copied == 0
    assert r2.missing_frames == [1, 2]
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
    r = collect(state(), accts(), factory, out)
    assert not list(out.glob(".raw_*"))
    # Task 6: a fetch failure is REPORTED, not raised -- see the
    # one-worker's-failure-must-not-abort-the-others tests below.
    assert r.copied == 0
    assert r.worker_errors["a0"]
    assert r.worker_errors["a1"]


def test_unclearable_staging_fails_loudly_rather_than_under_reporting(tmp_path,
                                                                     monkeypatch):
    """If the stale staging folder cannot be removed (locked file), collect
    must raise -- not quietly count last job's frames as this job's."""
    import blendfleet.collector as collector_mod

    out = tmp_path / "out"
    stale = out / ".raw_a0"
    stale.mkdir(parents=True)
    (stale / "f_0001.png").write_bytes(b"PNG")
    monkeypatch.setattr(collector_mod.shutil, "rmtree",
                        lambda *a, **k: None)   # rmtree silently does nothing

    def factory(tok):
        return FakeClient(tok, [])

    with pytest.raises(RuntimeError, match="staging"):
        collect(state(), accts(), factory, out)


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


# --------------------------------------------------------------------------
# Task 5: archive-aware collection.
# --------------------------------------------------------------------------

def test_collects_from_archive_when_present_no_loose_files(tmp_path):
    def factory(tok):
        return FakeClient(tok, produce=(),
                          archive={"f_0001.png": b"AAA", "f_0003.png": b"BBB"}
                          if tok.endswith("0"*32)
                          else {"f_0002.png": b"CCC", "f_0004.png": b"DDD"})
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 4
    assert r.missing_frames == []
    out = tmp_path / "out"
    assert (out / "r_0001.png").read_bytes() == b"AAA"
    assert (out / "r_0003.png").read_bytes() == b"BBB"
    assert not r.archive_errors


def test_falls_back_to_loose_frames_when_archive_absent(tmp_path):
    """Unchanged pre-Task-5 behaviour: no archive at all -> loose files
    used exactly as before."""
    def factory(tok):
        return FakeClient(tok, ["f_0001.png"] if tok.endswith("0"*32)
                          else ["f_0002.png"], archive=None)
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 2
    assert not r.archive_errors


def test_falls_back_to_loose_frames_when_archive_is_corrupt(tmp_path):
    """A truncated/interrupted archive must be REPORTED, never crash the
    collect, and the worker's loose files (the fallback the brief demands)
    are still used."""
    def factory(tok):
        return CorruptArchiveClient(
            tok, ["f_0001.png", "f_0003.png"] if tok.endswith("0"*32)
            else ["f_0002.png", "f_0004.png"])

    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 4
    assert r.missing_frames == []
    assert r.archive_errors["a0"]
    assert r.archive_errors["a1"]


def test_archive_lagging_one_frame_behind_loose_files_still_finds_it(tmp_path):
    """The realistic partial-write race: the archive is appended to AFTER
    a frame's PNG is already on disk, so a session killed at exactly the
    wrong instant leaves the archive one frame behind the loose folder.
    missing_frames must not regress because of that -- the loose file
    must still be found and counted."""
    def factory(tok):
        if tok.endswith("0"*32):
            return FakeClient(tok, produce=["f_0003.png"],  # not yet archived
                              archive={"f_0001.png": b"AAA"})
        return FakeClient(tok, produce=(), archive={"f_0002.png": b"CCC",
                                                     "f_0004.png": b"DDD"})
    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 4
    assert r.missing_frames == []
    out = tmp_path / "out"
    assert (out / "r_0001.png").read_bytes() == b"AAA"
    assert (out / "r_0003.png").exists()   # recovered from the loose file


def test_archive_corruption_that_raises_something_other_than_badzipfile_still_falls_back(
        tmp_path, monkeypatch):
    """Review finding: a truncated/interrupted archive write is not
    guaranteed to always surface as zipfile.BadZipFile -- e.g. a disk-full
    or permission error during extractall raises a plain OSError. That
    must fall back to loose frames exactly like BadZipFile does, never
    escape and mark the whole worker as failed (which would throw away
    perfectly good loose files sitting right next to the archive)."""
    import blendfleet.collector as collector_mod

    def boom_extractall(self, path=None, members=None, pwd=None):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(collector_mod.zipfile.ZipFile, "extractall", boom_extractall)

    def factory(tok):
        return FakeClient(tok, ["f_0001.png", "f_0003.png"] if tok.endswith("0"*32)
                          else ["f_0002.png", "f_0004.png"],
                          archive={"f_0001.png": b"AAA"} if tok.endswith("0"*32)
                          else {"f_0002.png": b"CCC"})

    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.copied == 4
    assert r.missing_frames == []
    assert r.archive_errors["a0"]
    assert r.archive_errors["a1"]
    assert not r.worker_errors


def test_archive_zip_slip_entries_are_rejected_not_extracted_outside_staging(tmp_path):
    """A malicious or corrupted archive entry name must never be allowed
    to write outside the staging directory (zip-slip)."""
    def factory(tok):
        class SlipClient(FakeClient):
            def fetch_output(self, slug, dest):
                dest.mkdir(parents=True, exist_ok=True)
                zpath = dest / "frames.zip"
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
                    zf.writestr("f_0001.png", b"AAA")
                    zf.writestr("../../evil_0002.png", b"EVIL")
                return [zpath]
        return SlipClient(tok)

    st = FleetState(job_id="j1", blend_name="r.blend", start_frame=1, end_frame=1,
                    workers=[WorkerState("a0", "u0", "u0/k0", [1])])
    out = tmp_path / "out"
    r = collect(st, [Account("a0", "KGAT_" + "0"*32)], factory, out)

    assert r.copied == 1
    assert (out / "r_0001.png").read_bytes() == b"AAA"
    assert list(tmp_path.rglob("evil_0002.png")) == [], (
        "the escaping entry must never be extracted anywhere on disk")


def test_archive_and_matching_loose_files_do_not_double_count(tmp_path):
    def factory(tok):
        return FakeClient(tok, produce=["f_0001.png"],
                          archive={"f_0001.png": b"AAA"})
    st = FleetState(job_id="j1", blend_name="r.blend", start_frame=1, end_frame=1,
                    workers=[WorkerState("a0", "u0", "u0/k0", [1])])
    r = collect(st, [Account("a0", "KGAT_" + "0"*32)], factory, tmp_path / "out")
    assert r.copied == 1
    assert r.per_worker == {"a0": 1}


# --------------------------------------------------------------------------
# Task 6: per-instance collect, and one worker's failure must not abort
# the others.
# --------------------------------------------------------------------------

def test_worker_label_targets_only_that_one_worker(tmp_path):
    def factory(tok):
        return FakeClient(tok, ["f_0001.png", "f_0003.png"] if tok.endswith("0"*32)
                          else ["f_0002.png", "f_0004.png"])
    r = collect(state(), accts(), factory, tmp_path / "out", worker_label="a0")
    assert r.copied == 2
    assert r.per_worker == {"a0": 2}
    assert "a1" not in r.per_worker
    out = tmp_path / "out"
    assert sorted(p.name for p in out.glob("*.png")) == ["r_0001.png", "r_0003.png"]


def test_worker_label_missing_frames_is_scoped_to_that_workers_own_frames(tmp_path):
    """Collecting just a0 must report ONLY a0's own frames as missing --
    not b's, which a0 was never responsible for."""
    def factory(tok):
        return FakeClient(tok, [])  # nobody produced anything
    r = collect(state(), accts(), factory, tmp_path / "out", worker_label="a0")
    assert r.missing_frames == [1, 3]   # a0's own frames, not [1, 2, 3, 4]


def test_worker_label_for_an_unknown_label_collects_nothing(tmp_path):
    def factory(tok):
        return FakeClient(tok, ["f_0001.png"])
    r = collect(state(), accts(), factory, tmp_path / "out", worker_label="nope")
    assert r.copied == 0
    assert r.per_worker == {}
    assert r.missing_frames == []


def test_one_workers_failure_does_not_abort_collecting_the_others(tmp_path):
    class Boom(FakeClient):
        def fetch_output(self, slug, dest):
            raise RuntimeError("network died mid-download")

    def factory(tok):
        return Boom(tok) if tok.endswith("0"*32) else \
            FakeClient(tok, ["f_0002.png", "f_0004.png"])

    r = collect(state(), accts(), factory, tmp_path / "out")
    assert r.per_worker["a1"] == 2
    assert r.per_worker["a0"] == 0
    assert "network died mid-download" in r.worker_errors["a0"]
    assert "a1" not in r.worker_errors
    assert r.missing_frames == [1, 3]   # a0's frames never came in
    out = tmp_path / "out"
    assert sorted(p.name for p in out.glob("*.png")) == ["r_0002.png", "r_0004.png"]
    assert not list(out.glob(".raw_*"))


def test_progress_callback_receives_the_workers_label_and_download_progress(tmp_path):
    from blendfleet.downloader import DownloadProgress

    class ProgressClient(FakeClient):
        def fetch_output_with_progress(self, slug, dest, on_progress=None):
            if on_progress is not None:
                on_progress(DownloadProgress(downloaded=3, total=3, rate_bps=1.0))
            return self.fetch_output(slug, dest)

    def factory(tok):
        return ProgressClient(tok, ["f_0001.png", "f_0003.png"]
                              if tok.endswith("0"*32)
                              else ["f_0002.png", "f_0004.png"])

    seen: list[tuple[str, object]] = []
    r = collect(state(), accts(), factory, tmp_path / "out",
               on_progress=lambda label, p: seen.append((label, p)))

    assert r.copied == 4
    labels_seen = {label for label, _ in seen}
    assert labels_seen == {"a0", "a1"}
    for _, p in seen:
        assert isinstance(p, DownloadProgress)


def test_progress_requested_but_client_lacks_progress_support_still_collects(tmp_path):
    """A client (or test double) with no fetch_output_with_progress must
    not crash collect() when on_progress is passed -- it just collects
    without live progress for that worker rather than blowing up."""
    def factory(tok):
        return FakeClient(tok, ["f_0001.png"] if tok.endswith("0"*32)
                          else ["f_0002.png"])
    r = collect(state(), accts(), factory, tmp_path / "out",
               on_progress=lambda label, p: None)
    assert r.copied == 2
