import pytest

from blendfleet.fleet import FleetBusyError, NoBlendInDatasetError, StaleDatasetError
from blendfleet.kaggle_client import KaggleError
from blendfleet.ui.messages import explain, explain_kernel_failure


def test_self_explanatory_exception_is_prefixed_not_rewrapped():
    exc = KaggleError(
        "Dataset creation failed: the .blend file did not finish "
        "uploading to Kaggle, so the request was submitted with no file "
        "attached -- retry the render.")
    msg = explain("Starting the render", exc)
    assert msg.startswith("Starting the render failed:")
    assert "did not finish uploading" in msg
    assert "retry the render" in msg
    # no added generic boilerplate on top of an already-complete message
    assert "not one of the problems" not in msg


def test_fleet_busy_error_is_prefixed():
    exc = FleetBusyError("a render is still running on: you (you/k). "
                         "Cancel it before starting another job.")
    msg = explain("Starting the render", exc)
    assert "Starting the render failed:" in msg
    assert "still running" in msg


def test_unexpected_exception_gets_wrapped_with_next_step():
    msg = explain("Cancelling the render", AttributeError("'NoneType' has no attr 'x'"))
    assert msg.startswith("Cancelling the render failed:")
    assert "'NoneType'" in msg
    assert "try again" in msg


def test_never_shows_a_bare_empty_message():
    msg = explain("Collecting frames", RuntimeError(""))
    assert "Collecting frames failed:" in msg
    assert "RuntimeError" in msg


# ---------------------------------------------------------------------------
# Task 10, Fix round 1, Minor: NoBlendInDatasetError/StaleDatasetError were
# missing from _SELF_EXPLANATORY, so their own carefully written
# what/why/next-step messages got "not one of the problems BlendFleet knows
# how to explain" appended on top -- contradicting a message that already
# explains itself in full.
# ---------------------------------------------------------------------------

def test_no_blend_in_dataset_error_is_shown_verbatim_not_rewrapped():
    exc = NoBlendInDatasetError(
        "dataset 'user0/x-blend' has no .blend file in it. Nothing has "
        "been started -- no kernel has been pushed. Pick a different "
        "scene from the library.")
    msg = explain("Starting the render", exc)
    assert "Starting the render failed:" in msg
    assert "no .blend file in it" in msg
    assert "not one of the problems" not in msg


def test_stale_dataset_error_is_shown_verbatim_not_rewrapped():
    exc = StaleDatasetError(
        "user3's copy of 'remember.blend' on Kaggle is 999 bytes, but the "
        "dataset owner's own copy is 100 bytes. Nothing has been started.")
    msg = explain("Starting the render", exc)
    assert "Starting the render failed:" in msg
    assert "999 bytes" in msg
    assert "not one of the problems" not in msg


# ---------------------------------------------------------------------------
# Task 4: explain_kernel_failure -- plain-language causes, not a raw
# traceback or a bare status word.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected_cause", [
    ("CUDA out of memory: tried to allocate 2.00 GiB", "Ran out of memory"),
    ("RuntimeError: CUDA out of memory", "Ran out of memory"),
    ("Fatal Python error: Segmentation fault", "Blender crashed"),
    ("blender: core dumped", "Blender crashed"),
    ("FileNotFoundError: [Errno 2] No such file or directory: 'scene.blend'",
     "A file the render needed was missing"),
    ("the session timed out after 12 hours", "The session timed out"),
    ("you have exceeded your weekly GPU quota", "This account's GPU quota ran out"),
])
def test_explain_kernel_failure_recognises_common_causes(raw, expected_cause):
    cause, explanation = explain_kernel_failure(raw)
    assert cause == expected_cause
    assert explanation  # what/why/next-step, never blank
    assert len(explanation) > len(cause)


def test_explain_kernel_failure_is_case_insensitive():
    cause, _ = explain_kernel_failure("CUDA Out Of Memory: allocation failed")
    assert cause == "Ran out of memory"


def test_explain_kernel_failure_never_hides_an_unrecognised_raw_detail():
    """An unrecognised message must still show the raw text somewhere --
    the rule is "never a bare status", not "only ever the five recognised
    causes"."""
    cause, explanation = explain_kernel_failure("Weird custom error 0xDEADBEEF")
    assert "Weird custom error 0xDEADBEEF" in cause or \
        "Weird custom error 0xDEADBEEF" in explanation


def test_explain_kernel_failure_with_empty_text_says_so_honestly():
    cause, explanation = explain_kernel_failure("")
    assert cause == "Failed for an unknown reason"
    assert "no failure message" in explanation.lower()
    assert "no kernel log" in explanation.lower()


def test_explain_kernel_failure_never_returns_a_bare_status_word():
    for raw in ("error", "ERROR", ""):
        cause, explanation = explain_kernel_failure(raw)
        assert cause.lower() != "error"
        assert explanation


def test_a_scene_with_no_camera_is_named_as_such():
    """Measured on a real render: Blender exits in about a second per
    frame with "ERROR Cannot render, no camera". Reported as "Blender
    crashed" -- which it matches on "blender quit" -- it looks like a fault
    in the farm rather than a scene nothing could render."""
    cause, explanation = explain_kernel_failure(
        "00:02.617  reports  | ERROR Cannot render, no camera\n"
        "Blender 5.2.0 LTS\nBlender quit")
    assert cause == "The scene has no camera"
    assert "add a camera" in explanation
    assert "nothing to do with Kaggle" in explanation
