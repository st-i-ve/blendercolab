from blendfleet.fleet import FleetBusyError
from blendfleet.kaggle_client import KaggleError
from blendfleet.ui.messages import explain


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
