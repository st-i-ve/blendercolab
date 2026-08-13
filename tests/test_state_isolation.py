"""Guards the property tests/conftest.py's redirect_app_dirs fixture
exists to hold: nothing a test does may reach the user's real BlendFleet
config/state directory. See conftest.py's module docstring for the
corruption this replaces -- a suite run once overwrote the user's real
fleet.json with a synthetic job, which is how running Kaggle kernels
become uncancellable and uncollectable from the app while still billing
someone's GPU quota.

conftest.py's session-scoped guard_real_app_dir_untouched already catches
ANY test regressing this, suite-wide. This module additionally proves the
fix works for the specific call chain that broke: a real Fleet save.
"""
from __future__ import annotations

import blendfleet.platform_paths as pp
from blendfleet.fleet import STATE_FILE, Fleet, FleetState, WorkerState

# Captured at IMPORT time, before any test's monkeypatch.setattr on
# platform_paths.state_dir has run. This is a plain module-level name
# binding -- immune to the `state_dir` attribute later being reassigned on
# the platform_paths module by conftest.py's redirect_app_dirs fixture --
# so calling this always reaches the ORIGINAL, real function regardless of
# what any test's fixtures have patched. That immunity is the same
# mechanism, applied deliberately, that made the original bug possible:
# fleet.py's own `from blendfleet.platform_paths import state_dir` binding
# was immune to a patch aimed only at platform_paths.state_dir.
_REAL_STATE_DIR = pp.state_dir


def test_fleet_save_lands_under_tmp_not_the_real_state_dir(tmp_path):
    """The exact call chain that once corrupted the user's real
    fleet.json: Fleet.save_jobs() -> Fleet._state_path() ->
    state_dir()/STATE_FILE. Checks both halves of the fix at once -- the
    save lands under THIS test's own tmp_path, and the real file already
    on this machine is provably unchanged by it.
    """
    real_file = _REAL_STATE_DIR() / STATE_FILE
    before = real_file.read_bytes() if real_file.exists() else None

    job = FleetState(
        job_id="probe", blend_name="probe.blend", start_frame=1, end_frame=1,
        workers=[WorkerState(label="a", username="u",
                             kernel_slug="u/probe-a0", frames=[1])])
    fleet = Fleet([], lambda token: None, tmp_path / "w")
    fleet.save_jobs([job])

    saved_path = fleet._state_path()
    assert saved_path.exists(), "save_jobs() did not write anywhere at all"
    assert saved_path.is_relative_to(tmp_path), (
        f"Fleet saved to {saved_path}, which is not under this test's own "
        f"tmp_path ({tmp_path}) -- conftest.py's redirect_app_dirs fixture "
        "is not covering fleet.py's state_dir call site.")
    assert saved_path != real_file, (
        "the redirected save path and the real state file resolved to the "
        "same path -- redirect_app_dirs is not actually redirecting "
        "anything for this test.")

    after = real_file.read_bytes() if real_file.exists() else None
    assert after == before, (
        f"Fleet.save_jobs() changed the REAL state file at {real_file} -- "
        "exactly the damage this test exists to catch. Something bypassed "
        "conftest.py's redirect_app_dirs fixture for this call.")
