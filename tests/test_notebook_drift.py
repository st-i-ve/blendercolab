"""Whether the notebook on Kaggle still reports what this app reads.

The app learns a render's progress by parsing the notebook's own stdout --
`PROGRESS frame=... ok=... secs=...`, the hardware banner, TELEMETRY. That
is a contract between two programs, and the copy on Kaggle can change
without this one knowing: someone opens it in the browser and edits it, or
an older BlendFleet pushed it and a newer one is reading it. The render then
appears to run and report nothing, and before this the app could not say
which of those had happened.

Two rules hold everything here together:

  - "could not read the notebook" is never a verdict about the notebook. An
    unanswered request returns "", the same as a healthy one, because
    accusing someone's notebook on the strength of a failed HTTP call would
    be worse than saying nothing.
  - it is only asked when the frame count could NOT be read. A render that
    reported its frames needs no explanation, and asking anyway would be a
    request per finished worker to answer a question nobody asked.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

from blendfleet.fleet import Fleet, FleetState, WorkerState
from blendfleet.kaggle_client import KernelStatus, Quota
from blendfleet.notebook_builder import (NOTEBOOK_CONTRACT, NOTEBOOK_MARKER,
                                         RenderSettings, build,
                                         notebook_drift)


# ---- the marker ------------------------------------------------------

def test_every_generated_cell_carries_the_marker(tmp_path):
    """Marked in `_code`, the one place every cell passes through, so a
    template added later cannot forget it."""
    path = build([1, 2], RenderSettings(1920, 1080, 128),
                 "owner/scene-blend", tmp_path,
                 "user_0/waydown-render-abcd1234")
    notebook = json.loads(path.read_text(encoding="utf-8"))

    assert notebook["cells"], "a notebook with no cells is not a notebook"
    for cell in notebook["cells"]:
        assert any(NOTEBOOK_MARKER in line for line in cell["source"]), (
            "a generated cell with no marker cannot be told apart from an "
            "edited one")


def test_a_notebook_this_app_built_reads_as_healthy(tmp_path):
    path = build([1, 2], RenderSettings(1920, 1080, 128),
                 "owner/scene-blend", tmp_path,
                 "user_0/waydown-render-abcd1234")
    source = "\n".join("".join(cell["source"])
                       for cell in json.loads(path.read_text())["cells"])

    assert notebook_drift(source) == ""


# ---- what drift looks like -------------------------------------------

def test_a_notebook_without_the_marker_is_reported():
    assert "looks edited" in notebook_drift(
        "print('hello, I am somebody else\\'s notebook')")


def test_a_notebook_from_another_version_names_both_contracts():
    """The number matters: "yours is 2, this app reads 1" is actionable,
    "something is wrong" is not."""
    other = NOTEBOOK_CONTRACT + 7
    message = notebook_drift(
        f"# BLENDFLEET-NOTEBOOK contract={other}\nPROGRESS frame=1 ok=1")

    assert f"contract {other}" in message
    assert f"reads {NOTEBOOK_CONTRACT}" in message


def test_a_marked_notebook_missing_its_progress_lines_is_reported():
    assert "progress lines" in notebook_drift(
        f"# {NOTEBOOK_MARKER}\nprint('I render nothing and say nothing')")


def test_an_unreadable_source_is_not_an_accusation():
    """"" from KaggleClient.kernel_source means "could not read it". If
    that became "this notebook was edited", a rate limit would libel
    somebody's notebook."""
    assert notebook_drift("") == ""


def test_an_edit_the_app_can_still_read_is_not_reported():
    """Warning about changes that do not matter teaches the user to ignore
    the warning that does."""
    source = (f"# {NOTEBOOK_MARKER}\n"
              "# somebody added a comment here\n"
              "SAMPLES = 256   # and changed a setting\n"
              "print(f'PROGRESS frame={frame} ok=1 secs=1.0')\n")

    assert notebook_drift(source) == ""


# ---- when it is asked ------------------------------------------------

class _Client:
    """A finished worker whose log cannot be read, which is the one
    situation the source is fetched in."""

    def __init__(self, source="", log_readable=False):
        self._source = source
        self._log_readable = log_readable
        self.source_calls = 0

    def status(self, slug):
        return KernelStatus(state="complete")

    def machine_shape(self, slug):
        return None

    def kernel_source(self, slug):
        self.source_calls += 1
        return self._source

    def fetch_log_tail(self, slug, dest, max_lines=200):
        # Unreadable unless a test says otherwise: this is what makes
        # _final_frame_count return None.
        return "PROGRESS frame=1 ok=1 secs=1.0 done=2/2" if self._log_readable else ""

    def quota(self):
        return Quota(0, 108000, "soon", "api")


def _fleet(tmp_path, client):
    from blendfleet.accounts import Account
    accounts = [Account(label="acct0", token="KGAT_" + "0" * 32,
                        username="user_0", verified=True)]
    fleet = Fleet(accounts, lambda t: client, tmp_path / "w")
    fleet.save_jobs([FleetState(
        job_id="job", blend_name="waydown.blend", start_frame=1, end_frame=2,
        workers=[WorkerState(label="acct0", username="user_0",
                             kernel_slug="user_0/waydown-render-abcd1234",
                             frames=[1, 2], state="running")])])
    return fleet


def test_an_unreadable_count_is_what_triggers_the_question(tmp_path):
    client = _Client(source="print('not a blendfleet notebook')")
    fleet = _fleet(tmp_path, client)

    fleet.poll()

    assert client.source_calls == 1
    worker = fleet.load_jobs()[0].workers[0]
    assert "looks edited" in worker.notebook_drift


def test_a_readable_count_asks_nothing(tmp_path):
    """A render that reported its frames needs no explanation."""
    client = _Client(log_readable=True)
    fleet = _fleet(tmp_path, client)

    fleet.poll()

    assert client.source_calls == 0
    assert fleet.load_jobs()[0].workers[0].notebook_drift == ""


def test_the_question_is_asked_once_and_the_answer_kept(tmp_path):
    """It rides along with the final-count read, which is one-shot per
    worker -- and a later poll finding nothing must not erase what the
    first one learned."""
    client = _Client(source="print('not a blendfleet notebook')")
    fleet = _fleet(tmp_path, client)

    fleet.poll()
    fleet.poll()
    fleet.poll()

    assert client.source_calls == 1
    assert fleet.load_jobs()[0].workers[0].notebook_drift != ""


def test_a_worker_from_an_older_state_file_has_no_verdict_on_it():
    older = {"label": "acct0", "username": "user_0",
             "kernel_slug": "user_0/k", "frames": [1, 2], "state": "complete"}

    assert WorkerState(**older).notebook_drift == ""
