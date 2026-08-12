"""The batched render runner, executed for real against a fake Blender.

test_notebook_builder.py checks the generated notebook says the right
things. This file RUNS the runner cell -- it is ordinary Python, needing
neither Blender nor Kaggle -- so the behaviour that matters is tested
rather than pattern-matched: which frames get archived and when, what
PROGRESS says, and what happens when the process dies mid-batch.

That last one is the whole risk of batching. A crash takes every
remaining frame with it, and no amount of reading the generated source
proves the fallback recovers them.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from blendfleet.notebook_builder import RenderSettings, build


@pytest.fixture
def settings():
    return RenderSettings(64, 36, 1, "PNG")


def runner_source(tmp_path, settings) -> str:
    """The runner cell, lifted out of a freshly generated notebook."""
    nb = json.loads((build([1, 2, 3], settings, "me/x", tmp_path, "me/r"))
                    .read_text(encoding="utf-8"))
    cells = ["".join(c["source"]) for c in nb["cells"]]
    return next(c for c in cells if "def render_frames" in c)


class FakeBlender:
    """Stands in for one Blender process.

    `script` is what this run prints, line by line. A line of the form
    "!write f_0001.png" writes a file instead of printing -- that is how a
    real render's output appears on disk partway through, which is what
    the incremental archiving depends on.
    """

    PIPE = -1        # the runner passes subprocess.PIPE/STDOUT through
    STDOUT = -2

    def __init__(self, out_dir: Path, runs: list[tuple[list[str], int]]):
        self.out_dir = out_dir
        self.runs = list(runs)
        self.calls: list[list[str]] = []

    def Popen(self, argv, env=None, text=None, stdout=None, stderr=None,
              bufsize=None):
        self.calls.append(env["BR_FRAMES"].split(","))
        lines, rc = self.runs.pop(0) if self.runs else ([], 0)
        return _FakeProc(self.out_dir, lines, rc)


class _FakeProc:
    def __init__(self, out_dir: Path, lines: list[str], rc: int):
        self.out_dir = out_dir
        self.returncode = rc
        self.stdout = self._emit(lines)

    def _emit(self, lines):
        for line in lines:
            if line.startswith("!write "):
                (self.out_dir / line.split(" ", 1)[1]).write_bytes(b"png")
                continue
            yield line + "\n"

    def wait(self):
        return self.returncode


def load_runner(tmp_path, settings, fake, printer):
    """The runner cell, live, with Blender and print swapped out.

    The fake is injected AFTER exec, never before: the cell's own
    `import subprocess` runs at exec time and would rebind it straight
    back to the real one, quietly spawning /fake/blender for real.
    """
    ns: dict = {"BBIN": "/fake/blender"}
    exec(runner_source(tmp_path, settings), ns)
    ns["subprocess"] = fake
    ns["print"] = printer
    return ns


def run_runner(tmp_path, settings, runs):
    """exec the runner cell with a fake subprocess, and render [1,2,3]."""
    out = tmp_path / "frames"
    out.mkdir(exist_ok=True)
    archive = tmp_path / "out.zip"
    fake = FakeBlender(out, runs)
    printed: list[str] = []
    ns = load_runner(tmp_path, settings, fake,
                     lambda *a, **k: printed.append(
                         " ".join(str(x) for x in a)))
    done, failed = ns["render_frames"](
        [1, 2, 3], "scene.blend", {}, str(out) + "/f_", str(archive))
    return done, failed, printed, fake, archive


def progress_lines(printed: list[str]) -> list[str]:
    return [p for p in printed if p.startswith("PROGRESS frame=")]


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------

def test_one_process_renders_every_frame(tmp_path, settings):
    done, failed, printed, fake, _ = run_runner(tmp_path, settings, [([
        "[setup] backend=OPTIX gpus=2 devices=['Tesla T4', 'Tesla T4']",
        "!write f_0001.png",
        "PROGRESS frame=1 ok=True secs=12.0 done=1/3",
        "!write f_0002.png",
        "PROGRESS frame=2 ok=True secs=10.0 done=2/3",
        "!write f_0003.png",
        "PROGRESS frame=3 ok=True secs=10.0 done=3/3",
    ], 0)])
    assert (done, failed) == ([1, 2, 3], [])
    assert len(fake.calls) == 1, "the whole point: one process, not three"
    assert fake.calls[0] == ["1", "2", "3"]


def test_the_setup_line_is_echoed(tmp_path, settings):
    _, _, printed, _, _ = run_runner(tmp_path, settings, [([
        "[setup] backend=OPTIX gpus=2 devices=['Tesla T4', 'Tesla T4']",
        "!write f_0001.png",
        "PROGRESS frame=1 ok=True secs=1.0 done=1/3",
        "!write f_0002.png",
        "PROGRESS frame=2 ok=True secs=1.0 done=2/3",
        "!write f_0003.png",
        "PROGRESS frame=3 ok=True secs=1.0 done=3/3",
    ], 0)])
    assert any("backend=OPTIX gpus=2" in p for p in printed), \
        "which backend and how many GPUs must reach the Kaggle log"


def test_frames_are_archived_as_they_land_not_at_the_end(tmp_path, settings):
    # Archived DURING the run: the zip must already contain frame 1 by the
    # time frame 2 is reported, or a kernel killed mid-render loses them.
    seen_at = {}
    out = tmp_path / "frames"
    out.mkdir()
    archive = tmp_path / "out.zip"

    fake = FakeBlender(out, [([
        "!write f_0001.png", "PROGRESS frame=1 ok=True secs=1.0 done=1/3",
        "!write f_0002.png", "PROGRESS frame=2 ok=True secs=1.0 done=2/3",
        "!write f_0003.png", "PROGRESS frame=3 ok=True secs=1.0 done=3/3",
    ], 0)])

    def watch(*args, **kwargs):
        text = " ".join(str(a) for a in args)
        if text.startswith("PROGRESS frame=") and archive.exists():
            with zipfile.ZipFile(archive) as zf:
                seen_at[text.split()[1]] = sorted(zf.namelist())

    ns = load_runner(tmp_path, settings, fake, watch)
    ns["render_frames"]([1, 2, 3], "scene.blend", {}, str(out) + "/f_",
                        str(archive))

    assert seen_at["frame=1"] == ["f_0001.png"]
    assert seen_at["frame=2"] == ["f_0001.png", "f_0002.png"]
    assert seen_at["frame=3"] == ["f_0001.png", "f_0002.png", "f_0003.png"]


def test_a_frame_is_never_archived_twice(tmp_path, settings):
    _, _, _, _, archive = run_runner(tmp_path, settings, [([
        "!write f_0001.png", "PROGRESS frame=1 ok=True secs=1.0 done=1/3",
        "PROGRESS frame=2 ok=True secs=1.0 done=2/3",
        "PROGRESS frame=3 ok=True secs=1.0 done=3/3",
    ], 0)])
    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == ["f_0001.png"]


def test_a_failed_frame_contributes_nothing_to_the_archive(tmp_path, settings):
    done, failed, _, _, archive = run_runner(tmp_path, settings, [([
        "!write f_0001.png", "PROGRESS frame=1 ok=True secs=1.0 done=1/3",
        "[frame 2] FAILED",
        "PROGRESS frame=2 ok=False secs=0.4 done=1/3",
        "!write f_0003.png", "PROGRESS frame=3 ok=True secs=1.0 done=2/3",
    ], 0)])
    assert (done, failed) == ([1, 3], [2])
    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == ["f_0001.png", "f_0003.png"]


# --------------------------------------------------------------------------
# The risk batching introduces: a dead process takes the rest with it
# --------------------------------------------------------------------------

def test_frames_lost_to_a_crash_are_retried_one_at_a_time(tmp_path, settings):
    # Batch renders frame 1, then dies (a segfault, which no try/except
    # inside Blender can catch). Frames 2 and 3 were never reported, so
    # they must be re-run -- one process each, so a second crash cannot
    # take the survivor with it.
    done, failed, _, fake, archive = run_runner(tmp_path, settings, [
        ([
            "!write f_0001.png",
            "PROGRESS frame=1 ok=True secs=12.0 done=1/3",
            "Segmentation fault (core dumped)",
        ], -11),
        (["!write f_0002.png",
          "PROGRESS frame=2 ok=True secs=30.0 done=1/1"], 0),
        (["!write f_0003.png",
          "PROGRESS frame=3 ok=True secs=30.0 done=1/1"], 0),
    ])
    assert (done, failed) == ([1, 2, 3], [])
    assert [c for c in fake.calls] == [["1", "2", "3"], ["2"], ["3"]]
    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == ["f_0001.png", "f_0002.png", "f_0003.png"]


def test_a_frame_that_kills_even_its_own_process_is_reported_failed(
        tmp_path, settings):
    # The retry crashes too, and never reports its frame. It must end up
    # in `failed` rather than vanishing -- a frame silently absent from
    # both lists is how a render "succeeds" with a missing frame.
    done, failed, printed, _, _ = run_runner(tmp_path, settings, [
        (["PROGRESS frame=1 ok=True secs=1.0 done=1/3"], -11),
        (["boom"], -11),
        (["PROGRESS frame=3 ok=True secs=1.0 done=1/1"], 0),
    ])
    assert done == [1, 3]
    assert failed == [2]
    assert any("frame=2 ok=False" in p for p in progress_lines(printed))


def test_progress_totals_stay_fleet_correct_through_the_fallback(
        tmp_path, settings):
    # Each fallback child believes it is rendering "1/1". If those were
    # echoed, the app's progress bar would jump to 100% on the first
    # recovered frame. Every PROGRESS line must report /3.
    _, _, printed, _, _ = run_runner(tmp_path, settings, [
        (["PROGRESS frame=1 ok=True secs=1.0 done=1/3"], -11),
        (["PROGRESS frame=2 ok=True secs=1.0 done=1/1"], 0),
        (["PROGRESS frame=3 ok=True secs=1.0 done=1/1"], 0),
    ])
    lines = progress_lines(printed)
    assert [l.split("done=")[1] for l in lines] == ["1/3", "2/3", "3/3"]


def test_the_app_can_still_parse_every_progress_line(tmp_path, settings):
    # The format is a contract with log_stream.PROGRESS_RE -- the runner
    # rewrites these lines, so the regex is asserted against the real
    # output rather than assumed.
    from blendfleet.log_stream import PROGRESS_RE

    _, _, printed, _, _ = run_runner(tmp_path, settings, [
        (["PROGRESS frame=1 ok=True secs=1.0 done=1/3"], -11),
        (["PROGRESS frame=2 ok=True secs=1.0 done=1/1"], 0),
        (["PROGRESS frame=3 ok=False secs=1.0 done=0/1"], 0),
    ])
    parsed = [PROGRESS_RE.search(l) for l in progress_lines(printed)]
    assert all(parsed), "every emitted PROGRESS line must match the app's regex"
    assert [(m.group(1), m.group(2), m.group(3)) for m in parsed] == [
        ("1", "1", "3"), ("2", "2", "3"), ("3", "2", "3")]


def test_archiving_failure_never_kills_the_render(tmp_path, settings):
    # The archive is an optimisation; the loose files are the real output.
    # A zip that cannot be written must cost a warning, not the frames.
    out = tmp_path / "frames"
    out.mkdir()
    blocked = tmp_path / "blocked"
    blocked.mkdir()          # a directory where the zip should go
    fake = FakeBlender(out, [([
        "!write f_0001.png", "PROGRESS frame=1 ok=True secs=1.0 done=1/3",
        "!write f_0002.png", "PROGRESS frame=2 ok=True secs=1.0 done=2/3",
        "!write f_0003.png", "PROGRESS frame=3 ok=True secs=1.0 done=3/3",
    ], 0)])
    printed: list[str] = []
    ns = load_runner(tmp_path, settings, fake,
                     lambda *a, **k: printed.append(
                         " ".join(str(x) for x in a)))
    done, failed = ns["render_frames"]([1, 2, 3], "scene.blend", {},
                                       str(out) + "/f_", str(blocked))
    assert (done, failed) == ([1, 2, 3], [])
    assert any("ARCHIVE skipped" in p for p in printed)
    assert sorted(p.name for p in out.iterdir()) == [
        "f_0001.png", "f_0002.png", "f_0003.png"]
