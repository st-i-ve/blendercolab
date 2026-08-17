"""Kaggle as a scene library: send many, keep them there, render later.

Two features, one premise -- measured on a real fleet on 2026-08-17: 1.82
GiB of scenes spread over five accounts, two of which held nothing at all,
and datasets from eighteen days earlier still untouched. So Kaggle is a
usable cache for scenes, and what the app was missing was a way to put
several there at once and a way to see what is already there.

WHAT NEITHER OF THESE CLAIMS. There is no storage quota in the payload,
because the API exposes none: `total_bytes` per dataset is measured, a
limit would be invented. And "scene" means "named like one" -- nothing here
opens a dataset, exactly as scenes.Scene's own docstring says of itself.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from blendfleet.accounts import Account, AccountStore
from blendfleet.fleet import Fleet
from blendfleet.kaggle_client import DatasetInfo
from blendfleet.rpc.session import Session
from blendfleet.scenes import dataset_kind
from blendfleet.settings import Settings


# ---- telling one dataset from another --------------------------------

@pytest.mark.parametrize("ref, kind", [
    ("me/waydown-blend", "scene"),
    ("me/blender-5-2-0-linux", "runtime"),
    ("me/bf-diag-big", "other"),
    ("me/blendfleet-smoketest", "other"),
    # A bare suffix names no scene, which is the same exclusion
    # scenes_from_datasets makes -- "-blend" with nothing in front of it
    # cannot be rendered, so calling it a scene would offer a dead button.
    ("me/-blend", "other"),
])
def test_a_dataset_is_named_for_what_it_is(ref, kind):
    assert dataset_kind(ref) == kind


def test_the_runtime_is_distinguished_because_deleting_it_costs_something():
    """Not cosmetic labelling: deleting the Blender runtime means the next
    render on that account re-uploads 367 MiB, so the page has to be able
    to say so before somebody presses the button."""
    assert dataset_kind("me/blender-5-2-0-linux") == "runtime"
    assert dataset_kind("me/blender-4-1-0-linux") == "runtime"


# ---- what the storage view reports -----------------------------------

class _StorageClient:
    def __init__(self, token, datasets=None, raises=None):
        self.token = token
        self._datasets = datasets or []
        self._raises = raises

    def list_datasets(self):
        if self._raises is not None:
            raise self._raises
        return list(self._datasets)


def _dataset(ref, mib, days_old=1):
    return DatasetInfo(
        ref=ref, title=ref.split("/")[-1], total_bytes=int(mib * 2 ** 20),
        last_updated=datetime.now() - timedelta(days=days_old),
        is_private=True, owner=ref.split("/")[0])


def _session(tmp_path, clients, labels=("acct0",)):
    accounts = [Account(label=label, token=f"KGAT_{i:032x}",
                        username=f"user_{i}", verified=True)
                for i, label in enumerate(labels)]
    store = AccountStore(accounts)
    by_token = {a.token: clients[a.label] for a in accounts}
    root = tmp_path / "s"
    root.mkdir(exist_ok=True)
    return Session(store, lambda accs: Fleet(
        accs, lambda t: by_token[t], root / "w"), lambda t: "someone",
        Settings())


def _wait_for(session, key, emitter_name):
    """Run one action to completion and return what it emitted."""
    seen = []
    getattr(session, emitter_name).connect(lambda j: seen.append(json.loads(j)))
    getattr(session, key)()
    worker = session._workers.get(key.lower()) or session._workers.get("storage")
    if worker is not None:
        worker.wait(10_000)
    return seen


def test_each_account_is_reported_with_what_it_holds(tmp_path):
    client = _StorageClient("t", [
        _dataset("user_0/waydown-blend", 402),
        _dataset("user_0/blender-5-2-0-linux", 367),
        _dataset("user_0/bf-diag-big", 60, days_old=15),
    ])
    session = _session(tmp_path, {"acct0": client})
    try:
        seen = _wait_for(session, "storage", "storageChanged")

        assert seen, "the page was never told"
        payload = seen[-1]
        account = payload["accounts"][0]
        assert account["label"] == "acct0"
        # Biggest first: the reason somebody opens this view is to find
        # what is taking up room.
        assert [d["slug"] for d in account["datasets"]] == [
            "user_0/waydown-blend", "user_0/blender-5-2-0-linux",
            "user_0/bf-diag-big"]
        assert [d["kind"] for d in account["datasets"]] == [
            "scene", "runtime", "other"]
        assert account["usedBytes"] == int((402 + 367 + 60) * 2 ** 20)
        assert payload["usedBytes"] == account["usedBytes"]
    finally:
        session.stop()


def test_every_dataset_carries_its_age(tmp_path):
    """The same rule the hardware line follows: a listing without ages
    cannot answer "is this still needed", which is the only question this
    view exists for."""
    client = _StorageClient("t", [_dataset("user_0/old-blend", 5, days_old=15)])
    session = _session(tmp_path, {"acct0": client})
    try:
        payload = _wait_for(session, "storage", "storageChanged")[-1]
        dataset = payload["accounts"][0]["datasets"][0]

        assert dataset["ageSeconds"] > 14 * 24 * 3600
        assert dataset["updated"], "the timestamp itself is missing"
    finally:
        session.stop()


def test_no_quota_figure_is_invented(tmp_path):
    """The Kaggle API exposes no storage limit. A "x of y GB" here would be
    a number this app made up, which is the one thing it must not do."""
    client = _StorageClient("t", [_dataset("user_0/waydown-blend", 402)])
    session = _session(tmp_path, {"acct0": client})
    try:
        payload = _wait_for(session, "storage", "storageChanged")[-1]

        assert "usedBytes" in payload
        for invented in ("quotaBytes", "limitBytes", "totalBytes", "freeBytes",
                         "percentUsed"):
            assert invented not in payload, (
                f"{invented} is not something Kaggle tells us")
    finally:
        session.stop()


def test_one_unreadable_account_does_not_hide_the_others(tmp_path):
    broken = _StorageClient("t", raises=RuntimeError("token revoked"))
    working = _StorageClient("t", [_dataset("user_1/waydown-blend", 10)])
    session = _session(tmp_path, {"acct0": broken, "acct1": working},
                       labels=("acct0", "acct1"))
    try:
        payload = _wait_for(session, "storage", "storageChanged")[-1]

        assert [a["label"] for a in payload["accounts"]] == ["acct1"]
        assert "acct0" in payload["errors"]
    finally:
        session.stop()


# ---- staging several files -------------------------------------------

def _blend(tmp_path, name, size=64):
    path = tmp_path / name
    path.write_bytes(b"BLENDER" + b"\0" * size)
    return path


def test_several_files_are_staged_with_their_sizes(tmp_path):
    session = _session(tmp_path, {"acct0": _StorageClient("t")})
    try:
        answer = json.loads(session.setBlends(json.dumps([
            str(_blend(tmp_path, "waydown.blend")),
            str(_blend(tmp_path, "supra.blend")),
        ])))

        assert answer["queued"] == 2
        assert answer["refused"] == []
        assert [f["name"] for f in session._upload_queue] == [
            "waydown.blend", "supra.blend"]
        assert all(f["state"] == "queued" for f in session._upload_queue)
    finally:
        session.stop()


def test_a_blend1_backup_is_refused_by_name_not_silently_dropped(tmp_path):
    """Blender writes a .blend1 beside every save, so a multi-select makes
    sweeping one up easy. Refusing it silently would leave "I chose nine"
    disagreeing with "eight staged" and no way to see which."""
    session = _session(tmp_path, {"acct0": _StorageClient("t")})
    try:
        answer = json.loads(session.setBlends(json.dumps([
            str(_blend(tmp_path, "waydown.blend")),
            str(_blend(tmp_path, "waydown.blend1")),
        ])))

        assert answer["queued"] == 1
        assert answer["refused"] == [{"name": "waydown.blend1",
                                      "why": "not a .blend file"}]
    finally:
        session.stop()


def test_a_file_that_has_gone_is_refused_before_the_upload_starts(tmp_path):
    """Better to say so now than six minutes into a batch."""
    session = _session(tmp_path, {"acct0": _StorageClient("t")})
    try:
        answer = json.loads(session.setBlends(json.dumps([
            str(tmp_path / "deleted.blend")])))

        assert answer["queued"] == 0
        assert answer["refused"][0]["why"] == "no longer there"
    finally:
        session.stop()


def test_a_second_pick_replaces_the_first(tmp_path):
    """The chooser's answer IS the queue. Appending would upload files the
    user did not just choose."""
    session = _session(tmp_path, {"acct0": _StorageClient("t")})
    try:
        session.setBlends(json.dumps([str(_blend(tmp_path, "one.blend"))]))
        session.setBlends(json.dumps([str(_blend(tmp_path, "two.blend"))]))

        assert [f["name"] for f in session._upload_queue] == ["two.blend"]
    finally:
        session.stop()


# ---- uploading them --------------------------------------------------

class _UploadFleet:
    """A Fleet that records which account owns each upload, and can fail
    exactly one file."""

    def __init__(self, accounts, fail_holder=None):
        self.accounts = accounts
        self.uploaded = []
        self.unshared_accounts = {}
        self._holder = fail_holder if fail_holder is not None else {}

    def prepare_dataset(self, blend, required=None, on_progress=None,
                        on_stage=None, **kwargs):
        if on_stage is not None:
            on_stage("preparing", blend.name)
        if blend.name == self._holder.get("fail"):
            raise RuntimeError("Kaggle refused the upload")
        owner = self.accounts[0]
        self.uploaded.append((owner.label, blend.name))
        self._holder.setdefault("uploaded", []).append(
            (owner.label, blend.name))
        return f"{owner.username}/{blend.stem}-blend"


def _upload_session(tmp_path, labels=("acct0", "acct1"), fail=None):
    """A Session whose Fleet is faked, plus the dict controlling it.

    `made["fail"]` is read at each prepare_dataset, not captured once:
    uploadScenes builds a fresh Fleet per run (as the real one does), so a
    failure baked into the closure could never be "fixed" between attempts
    -- which is exactly what a retry test has to do.
    """
    accounts = [Account(label=label, token=f"KGAT_{i:032x}",
                        username=f"user_{i}", verified=True)
                for i, label in enumerate(labels)]
    made = {"fail": fail}

    def factory(accs):
        fleet = _UploadFleet(list(accs), fail_holder=made)
        made["fleet"] = fleet
        return fleet

    session = Session(AccountStore(accounts), factory, lambda t: "someone",
                      Settings())
    return session, made


def _run_upload(session, label=""):
    session.uploadScenes(label)
    worker = session._workers.get("bulk-upload")
    if worker is not None:
        worker.wait(10_000)


def test_every_staged_scene_is_uploaded_in_order(tmp_path):
    session, made = _upload_session(tmp_path)
    try:
        session.setBlends(json.dumps([
            str(_blend(tmp_path, "waydown.blend")),
            str(_blend(tmp_path, "supra.blend")),
        ]))
        _run_upload(session)

        assert [name for _, name in made["uploaded"]] == [
            "waydown.blend", "supra.blend"], "order was not preserved"
        assert all(f["state"] == "done" for f in session._upload_queue)
        assert session._upload_queue[0]["slug"] == "user_0/waydown-blend"
    finally:
        session.stop()


def test_the_chosen_account_is_the_one_that_owns_them(tmp_path):
    """Fleet.prepare_dataset makes accounts[0] the owner, so choosing an
    account is choosing the order. Two of the measured fleet's five
    accounts held nothing -- picking one is the whole point."""
    session, made = _upload_session(tmp_path)
    try:
        session.setBlends(json.dumps([str(_blend(tmp_path, "waydown.blend"))]))
        _run_upload(session, label="acct1")

        assert made["fleet"].accounts[0].label == "acct1"
        assert made["uploaded"] == [("acct1", "waydown.blend")]
    finally:
        session.stop()


def test_one_bad_file_does_not_stop_the_rest(tmp_path):
    """Eight files where the fifth fails must still upload the other
    seven -- and must say WHICH failed."""
    session, made = _upload_session(tmp_path, fail="broken.blend")
    notes = []
    try:
        session.notification.connect(lambda m, tone: notes.append(m))
        session.setBlends(json.dumps([
            str(_blend(tmp_path, "first.blend")),
            str(_blend(tmp_path, "broken.blend")),
            str(_blend(tmp_path, "third.blend")),
        ]))
        _run_upload(session)

        states = {f["name"]: f["state"] for f in session._upload_queue}
        assert states == {"first.blend": "done", "broken.blend": "failed",
                          "third.blend": "done"}
        assert [name for _, name in made["uploaded"]] == [
            "first.blend", "third.blend"]
        assert any("broken.blend" in m for m in notes), (
            "the failure has to name the file")
        assert session._upload_queue[1]["error"], "no reason was recorded"
    finally:
        session.stop()


def test_a_failed_file_stays_queued_for_a_retry(tmp_path):
    """It is still in the queue and still failed, so pressing Upload again
    retries exactly it -- and does not re-upload what already landed."""
    session, made = _upload_session(tmp_path, fail="broken.blend")
    try:
        session.setBlends(json.dumps([
            str(_blend(tmp_path, "good.blend")),
            str(_blend(tmp_path, "broken.blend")),
        ]))
        _run_upload(session)
        made["fail"] = None       # whatever it was, it is fixed now
        _run_upload(session)

        assert all(f["state"] == "done" for f in session._upload_queue)
        # good.blend uploaded once, not twice.
        assert [name for _, name in made["uploaded"]] == [
            "good.blend", "broken.blend"]
    finally:
        session.stop()


def test_the_page_hears_the_queue_change_at_every_step(tmp_path):
    """One event per transition, carrying the whole queue: the page keeps
    no copy of the truth, so it needs the whole of it each time."""
    session, _ = _upload_session(tmp_path)
    seen = []
    try:
        session.uploadQueueChanged.connect(
            lambda j: seen.append(json.loads(j)))
        session.setBlends(json.dumps([str(_blend(tmp_path, "waydown.blend"))]))
        _run_upload(session)

        assert len(seen) >= 3, "staged, uploading and done are three states"
        assert seen[0]["files"][0]["state"] == "queued"
        assert any(s["current"] == "waydown.blend" for s in seen), (
            "nothing ever named the file being uploaded")
        assert seen[-1]["files"][0]["state"] == "done"
        assert seen[-1]["done"] == 1 and seen[-1]["total"] == 1
        # The absolute path never travels: the page has no use for it and a
        # log or a screenshot of this payload would carry the user's
        # directory layout for no reason.
        assert "path" not in seen[-1]["files"][0]
    finally:
        session.stop()


def test_uploading_nothing_says_so_rather_than_starting(tmp_path):
    session, made = _upload_session(tmp_path)
    notes = []
    try:
        session.notification.connect(lambda m, tone: notes.append((m, tone)))
        _run_upload(session)

        assert any("Choose some .blend files" in m for m, _ in notes)
        assert "fleet" not in made, "a Fleet was built for an empty queue"
    finally:
        session.stop()
