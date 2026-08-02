"""Pure frame-splitting maths. No I/O, no Kaggle, no UI."""
from __future__ import annotations


def assign_frames(start: int, end: int, n_workers: int) -> list[list[int]]:
    """Split [start, end] across n_workers by stride.

    Worker i renders frames start+i, start+i+n, ... A worker that dies leaves
    gaps spread evenly through the animation rather than one missing block,
    so partial output still previews as a whole.
    """
    if end < start:
        raise ValueError(f"end frame {end} is before start frame {start}")
    if n_workers < 1:
        raise ValueError("need at least one worker")
    frames = list(range(start, end + 1))
    return [frames[i::n_workers] for i in range(n_workers)]


def estimate(n_frames: int, seconds_per_frame: float, n_workers: int) -> float:
    """Wall-clock hours, assuming workers run concurrently and evenly."""
    if n_workers < 1:
        raise ValueError("need at least one worker")
    per_worker = -(-n_frames // n_workers)      # ceiling division
    return per_worker * seconds_per_frame / 3600.0
