import pytest
from blendfleet.assignment import assign_frames, estimate


def test_splits_evenly():
    assert assign_frames(1, 6, 3) == [[1, 4], [2, 5], [3, 6]]


def test_handles_remainder():
    got = assign_frames(1, 7, 3)
    assert got == [[1, 4, 7], [2, 5], [3, 6]]
    assert sorted(f for w in got for f in w) == [1, 2, 3, 4, 5, 6, 7]


def test_every_frame_assigned_exactly_once():
    got = assign_frames(10, 250, 4)
    flat = [f for w in got for f in w]
    assert sorted(flat) == list(range(10, 251))
    assert len(flat) == len(set(flat))


def test_more_workers_than_frames_leaves_empty_lists():
    assert assign_frames(1, 2, 5) == [[1], [2], [], [], []]


def test_single_worker_gets_everything():
    assert assign_frames(1, 4, 1) == [[1, 2, 3, 4]]


def test_rejects_bad_input():
    with pytest.raises(ValueError):
        assign_frames(5, 1, 2)
    with pytest.raises(ValueError):
        assign_frames(1, 5, 0)


def test_estimate_divides_across_workers():
    # 250 frames at 57.1s measured on a Kaggle P100, 1920x1080/128spp
    assert estimate(250, 57.1, 1) == pytest.approx(3.965, abs=0.01)
    assert estimate(250, 57.1, 3) == pytest.approx(1.332, abs=0.01)
