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

import base64
import io
import json
import os
import time
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


# --------------------------------------------------------------------------
# Live frame previews, run for real.
#
# The render script itself cannot be exec'd here (it imports bpy on its
# first line), but the preview emitter is ordinary Python over an ordinary
# image file -- so it is lifted out and RUN, and its output is fed through
# the app's own parser. That round trip is the whole contract: what
# Blender prints must be what log_stream reassembles, byte for byte.
# --------------------------------------------------------------------------

def extract_function(source: str, name: str) -> str:
    """One top-level `def name(...)` block, lifted out of a larger script.

    Reads to the first following line that is neither blank nor indented,
    which is what ends a top-level def in the generated source.
    """
    lines = source.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.startswith(f"def {name}("))
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line[0].isspace():
            break
        end += 1
    return "\n".join(lines[start:end])


class _FakeRenderSettings:
    """Just enough of bpy's scene.render for the two functions under test:
    the extension the current file format writes."""

    def __init__(self, ext=".png"):
        self.file_extension = ext


class _FakeScene:
    def __init__(self, ext=".png"):
        self.render = _FakeRenderSettings(ext)


def load_thumbnailer(printer, scene=None, **overrides):
    """_rendered_path + _emit_thumb from SETUP_SCRIPT, ready to call.

    Every name they close over is supplied here rather than by running
    the surrounding script, so this exercises the generated code and
    nothing else.
    """
    from blendfleet import notebook_builder as nbmod

    ns = {
        "os": os, "io": io, "time": time, "base64": base64,
        "s": scene or _FakeScene(),
        "THUMBS_ON": True,
        "THUMB_W": nbmod.THUMB_WIDTH_PX,
        "THUMB_Q": nbmod.THUMB_QUALITY,
        "THUMB_CHUNK": nbmod.THUMB_CHUNK_CHARS,
        "THUMB_MAX": nbmod.THUMB_MAX_BYTES,
        "_thumb_timed": [False],
        "print": printer,
    }
    try:
        from PIL import Image as _Img
    except Exception:                                # pragma: no cover
        _Img = None
    ns["_Img"] = _Img
    ns.update(overrides)
    for name in ("_rendered_path", "_emit_thumb"):
        exec(extract_function(nbmod.SETUP_SCRIPT, name), ns)
    return ns


def a_rendered_png(path, size=(1920, 1080)):
    """A 1920x1080 PNG standing in for a finished frame.

    Smooth gradients with a few solid shapes, not noise: the byte-cost
    assertion below is about what a RENDER costs to preview, and a
    pixel-level checkerboard is the one image JPEG is worst at -- it would
    make the budget look four times worse than any real frame does.
    """
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    im = Image.new("RGB", size)
    d = ImageDraw.Draw(im)
    for y in range(size[1]):
        t = y / size[1]
        d.line([(0, y), (size[0], y)],
               fill=(int(30 + 120 * t), int(40 + 90 * t), int(70 + 140 * t)))
    for i in range(8):
        x = 120 * i + 60
        d.ellipse([x, 200 + 40 * i, x + 260, 460 + 40 * i],
                  fill=(40 + 20 * i, 200 - 15 * i, 90 + 10 * i))
    im.save(path)
    return path


def test_a_finished_frame_is_emitted_as_chunked_base64(tmp_path):
    """The chunk size is forced small here so the SPLIT itself is tested.

    A preview of a plain scene can fit on one line at the real 3000-char
    chunk size, and a one-line "chunked" format proves nothing about what
    happens to a busy frame -- which is the case the whole design exists
    for, since nothing documents how long a line Kaggle's log capture
    carries intact.
    """
    a_rendered_png(tmp_path / "f_0007.png")
    printed = []
    ns = load_thumbnailer(lambda *a, **k: printed.append(
        " ".join(str(x) for x in a)), THUMB_CHUNK=400)
    ns["_emit_thumb"](7, str(tmp_path / "f_0007"))

    thumbs = [p for p in printed if p.startswith("THUMB frame=")]
    assert len(thumbs) > 1, "the payload was never split"
    assert all("part=" in t and "bytes=" in t for t in thumbs)
    parts = [t.split("part=")[1].split()[0] for t in thumbs]
    assert parts == [f"{i}/{len(thumbs)}" for i in range(1, len(thumbs) + 1)]
    # Nothing on a line may exceed the chunk size, or the split has not
    # actually bounded anything.
    assert all(len(t.split(" ")[-1]) <= 400 for t in thumbs)


def test_what_blender_prints_is_what_the_app_reassembles(tmp_path):
    """The round trip, end to end: the generated emitter's own lines, read
    back by log_stream, must decode to a real JPEG of the frame."""
    import json as _json

    from blendfleet.log_stream import ThumbnailAssembler, parse_thumbnail_part

    a_rendered_png(tmp_path / "f_0003.png")
    printed = []
    # Forced across several lines, so the reassembly being tested is real
    # reassembly and not a single line handed straight back.
    ns = load_thumbnailer(lambda *a, **k: printed.append(
        " ".join(str(x) for x in a)), THUMB_CHUNK=400)
    ns["_emit_thumb"](3, str(tmp_path / "f_0003"))

    asm = ThumbnailAssembler()
    got = None
    for line in printed:
        if not line.startswith("THUMB frame="):
            continue
        # Wrapped exactly as Kaggle's SSE stream delivers a stdout line.
        sse = ('data: {"stream_name":"stdout","data":'
               + _json.dumps(line + "\n") + "}")
        part = parse_thumbnail_part(sse)
        assert part is not None, f"the app cannot parse its own line: {line}"
        got = asm.add(part) or got
    assert got is not None and got["frame"] == 3

    raw = base64.b64decode(got["jpeg_b64"])
    assert raw[:2] == b"\xff\xd8", "not a JPEG"
    Image = pytest.importorskip("PIL.Image")
    im = Image.open(io.BytesIO(raw))
    from blendfleet import notebook_builder as nbmod
    assert im.width == nbmod.THUMB_WIDTH_PX
    assert im.height == nbmod.THUMB_WIDTH_PX * 1080 // 1920


def test_a_preview_costs_a_few_kilobytes_not_a_few_megabytes(tmp_path):
    """The budget this whole design rests on. A full frame down the log
    pipe would be absurd; the number is asserted so a later change to
    width or quality cannot quietly blow it."""
    a_rendered_png(tmp_path / "f_0001.png")
    printed = []
    ns = load_thumbnailer(lambda *a, **k: printed.append(
        " ".join(str(x) for x in a)))
    ns["_emit_thumb"](1, str(tmp_path / "f_0001"))

    thumbs = [p for p in printed if p.startswith("THUMB frame=")]
    declared = int(thumbs[0].split("bytes=")[1].split()[0])
    # The hard ceiling the emitter enforces, and then the figure the
    # design is actually costed on: measured at about 5 kB for a
    # 1920x1080 frame at 320px/q60, so a 100-frame render spends well
    # under a megabyte of log text in total. 12 kB leaves room for a
    # busier frame without letting a careless change to width or quality
    # through unnoticed.
    assert declared < 32768, f"one preview spent {declared} bytes"
    assert declared < 12000, f"a 320px preview grew to {declared} bytes"
    # Every line agrees on the total -- that agreement is what makes a
    # clipped part detectable.
    assert all(f"bytes={declared} " in t for t in thumbs)


def test_no_pillow_costs_the_preview_and_never_the_render(tmp_path):
    """Guarded exactly as psutil is in the telemetry cell. Without Pillow
    there is simply nothing to send -- and nothing raised."""
    a_rendered_png(tmp_path / "f_0002.png")
    printed = []
    ns = load_thumbnailer(
        lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
        _Img=None)
    ns["_emit_thumb"](2, str(tmp_path / "f_0002"))
    assert not [p for p in printed if p.startswith("THUMB")]


def test_previews_switched_off_send_nothing(tmp_path):
    a_rendered_png(tmp_path / "f_0004.png")
    printed = []
    ns = load_thumbnailer(
        lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
        THUMBS_ON=False)
    ns["_emit_thumb"](4, str(tmp_path / "f_0004"))
    assert not [p for p in printed if p.startswith("THUMB")]


def test_a_missing_rendered_file_is_reported_not_raised(tmp_path):
    """write_still wrote somewhere unexpected. That costs the preview and
    says so; it must not take the render down with it."""
    printed = []
    ns = load_thumbnailer(lambda *a, **k: printed.append(
        " ".join(str(x) for x in a)))
    ns["_emit_thumb"](5, str(tmp_path / "nothing_here"))
    assert not [p for p in printed if p.startswith("THUMB")]
    assert any("preview skipped" in p for p in printed)


def test_an_unreadable_image_is_reported_not_raised(tmp_path):
    """Pillow raising on a half-written file must cost the preview only.
    The frame is on disk either way, and Collect is what fetches it."""
    (tmp_path / "f_0006.png").write_bytes(b"not an image at all")
    printed = []
    ns = load_thumbnailer(lambda *a, **k: printed.append(
        " ".join(str(x) for x in a)))
    ns["_emit_thumb"](6, str(tmp_path / "f_0006"))
    assert not [p for p in printed if p.startswith("THUMB")]
    assert any("preview failed, render unaffected" in p for p in printed)


def test_the_runner_forwards_preview_lines_verbatim(tmp_path, settings):
    """_run_blender filters Blender's stdout hard -- PROGRESS is
    re-emitted, most lines are dropped. A preview that never got past that
    filter would never leave the kernel."""
    out = tmp_path / "frames"
    out.mkdir()
    line = "THUMB frame=1 part=1/2 bytes=1536 QUJDRA=="
    fake = FakeBlender(out, [([
        "!write f_0001.png",
        line,
        "THUMB frame=1 part=2/2 bytes=1536 RUZHSA==",
        "PROGRESS frame=1 ok=True secs=1.0 done=1/3",
        "PROGRESS frame=2 ok=True secs=1.0 done=2/3",
        "PROGRESS frame=3 ok=True secs=1.0 done=3/3",
    ], 0)])
    printed = []
    ns = load_runner(tmp_path, settings, fake,
                     lambda *a, **k: printed.append(
                         " ".join(str(x) for x in a)))
    ns["render_frames"]([1, 2, 3], "scene.blend", {}, str(out) + "/f_", None)
    assert line in printed, "the preview never reached the Kaggle log"
    assert len([p for p in printed if p.startswith("THUMB frame=1")]) == 2
