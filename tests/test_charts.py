import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from blendfleet.fleet import WorkerState
from blendfleet.ui.charts import (Filmstrip, GpuPanel, Sparkline, frame_done,
                                  frame_owners, frame_stopped)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def workers(*, frames_done=(0, 0, 0)):
    return [
        WorkerState(label="you", username="you", kernel_slug="you/k",
                   frames=[1, 4, 7, 10], state="running",
                   frames_done=frames_done[0]),
        WorkerState(label="anna", username="anna", kernel_slug="anna/k",
                   frames=[2, 5, 8], state="running",
                   frames_done=frames_done[1]),
        WorkerState(label="theo", username="theo", kernel_slug="theo/k",
                   frames=[3, 6, 9], state="running",
                   frames_done=frames_done[2]),
    ]


# ---------------- frame_owners / frame_done / frame_stopped ----------------

def test_frame_owners_interleaves_by_stride():
    ws = workers()
    owners = frame_owners(1, 10, ws)
    # frame 1 -> worker 0 (you), frame 2 -> worker 1 (anna), frame 3 -> worker 2 (theo)
    assert owners[0] == 0 and owners[1] == 1 and owners[2] == 2
    assert owners[3] == 0   # frame 4 -> you again


def test_frame_owners_gap_for_unassigned_frame():
    ws = [WorkerState(label="you", username="you", kernel_slug="you/k",
                      frames=[1, 2], state="running", frames_done=0)]
    owners = frame_owners(1, 5, ws)
    assert owners == [0, 0, None, None, None]


def test_frame_done_reflects_first_n_of_stride_in_order():
    ws = workers(frames_done=(2, 1, 0))
    done = frame_done(1, 10, ws)
    # you: frames_done=2 -> frames 1,4 done (not 7, 10)
    assert done[0] is True and done[3] is True
    assert done[6] is False and done[9] is False
    # anna: frames_done=1 -> frame 2 done, frame 5/8 not
    assert done[1] is True and done[4] is False
    # theo: frames_done=0 -> nothing done
    assert done[2] is False


def test_frame_stopped_marks_remainder_of_errored_worker():
    ws = [
        WorkerState(label="you", username="you", kernel_slug="you/k",
                   frames=[1, 4, 7], state="error", frames_done=1),
        WorkerState(label="anna", username="anna", kernel_slug="anna/k",
                   frames=[2, 5, 8], state="running", frames_done=1),
    ]
    stopped = frame_stopped(1, 8, ws)
    # you rendered frame 1, then errored -- frames 4 and 7 read as stopped
    assert stopped[0] is False       # frame 1: already done
    assert stopped[3] is True        # frame 4
    assert stopped[6] is True        # frame 7
    # anna is still running -- nothing of hers is "stopped"
    assert stopped[1] is False
    assert stopped[4] is False


def test_gaps_and_interleave_across_250_frames_with_a_missing_worker():
    # 3 accounts, 250 frames, one account (theo) errors partway through --
    # exercises the exact scenario called out in the brief: 3 accounts,
    # 250 frames, including gaps.
    ws = [
        WorkerState(label="you", username="you", kernel_slug="you/k",
                   frames=list(range(1, 251, 3)), state="running",
                   frames_done=40),
        WorkerState(label="anna", username="anna", kernel_slug="anna/k",
                   frames=list(range(2, 251, 3)), state="running",
                   frames_done=35),
        WorkerState(label="theo", username="theo", kernel_slug="theo/k",
                   frames=list(range(3, 251, 3)), state="error",
                   frames_done=10),
    ]
    owners = frame_owners(1, 250, ws)
    done = frame_done(1, 250, ws)
    stopped = frame_stopped(1, 250, ws)
    assert len(owners) == len(done) == len(stopped) == 250
    assert None not in owners        # every frame in range is claimed
    assert any(stopped)              # theo's un-rendered remainder is a gap
    assert sum(done) == 40 + 35 + 10


# ---------------- widget smoke tests (offscreen) ----------------

def test_filmstrip_empty_state(qapp):
    fs = Filmstrip()
    fs.set_empty()
    fs.resize(400, 40)
    fs.repaint()
    assert fs.total_frames == 0
    assert fs.done_count == 0


def test_filmstrip_renders_with_three_accounts_and_gaps(qapp):
    fs = Filmstrip()
    ws = [
        WorkerState(label="you", username="you", kernel_slug="you/k",
                   frames=list(range(1, 251, 3)), state="running",
                   frames_done=40),
        WorkerState(label="anna", username="anna", kernel_slug="anna/k",
                   frames=list(range(2, 251, 3)), state="running",
                   frames_done=35),
        WorkerState(label="theo", username="theo", kernel_slug="theo/k",
                   frames=list(range(3, 251, 3)), state="error",
                   frames_done=10),
    ]
    fs.set_workers(1, 250, ws)
    fs.resize(500, 40)
    fs.repaint()   # must not raise
    assert fs.total_frames == 250
    assert fs.done_count == 85


def test_sparkline_paints_with_few_and_many_samples(qapp):
    spark = Sparkline()
    spark.resize(100, 28)
    spark.repaint()          # zero samples: must not raise
    spark.push(10.0)
    spark.repaint()          # one sample: must not raise (needs >= 2)
    for v in range(60):
        spark.push(float(v))
    spark.repaint()
    spark.clear()
    spark.repaint()


def test_gpu_panel_renders_whatever_arrives_single_gpu(qapp):
    panel = GpuPanel()
    panel.ingest("you", {"gpu": 0, "util": 87, "mem_used": 6144,
                        "mem_total": 15360, "temp": 71, "power": 58.0})
    assert panel.gpu_count == 1


def test_gpu_panel_renders_multiple_gpus_independently(qapp):
    panel = GpuPanel()
    panel.ingest("you", {"gpu": 0, "util": 87, "mem_used": 6144,
                        "mem_total": 15360, "temp": 71, "power": 58.0})
    panel.ingest("you", {"gpu": 1, "util": 12, "mem_used": 1024,
                        "mem_total": 15360, "temp": 45, "power": None})
    assert panel.gpu_count == 2
    assert panel._rows[("you", 0)].util_label.text() == " 87%"
    assert panel._rows[("you", 1)].util_label.text() == " 12%"


def test_gpu_panel_keeps_different_accounts_gpu_0_separate(qapp):
    # Two accounts each on their own Kaggle session both report "gpu=0" --
    # these are different physical devices and must never be merged.
    panel = GpuPanel()
    panel.ingest("you", {"gpu": 0, "util": 87, "mem_used": 6144,
                        "mem_total": 15360, "temp": 71, "power": 58.0})
    panel.ingest("anna", {"gpu": 0, "util": 30, "mem_used": 2048,
                         "mem_total": 15360, "temp": 50, "power": 20.0})
    assert panel.gpu_count == 2
    assert panel._rows[("you", 0)].util_label.text() == " 87%"
    assert panel._rows[("anna", 0)].util_label.text() == " 30%"


def test_gpu_panel_clear_resets_to_empty(qapp):
    panel = GpuPanel()
    panel.ingest("you", {"gpu": 0, "util": 50, "mem_used": 100,
                        "mem_total": 200, "temp": 60, "power": 10.0})
    panel.clear()
    assert panel.gpu_count == 0
