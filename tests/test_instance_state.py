import json

import pytest

import blendfleet.platform_paths as pp
from blendfleet.instance_state import (DEFAULT_STALE_AFTER_SECONDS,
                                       FILENAME, GpuSnapshot,
                                       InstanceSnapshot, InstanceStore)


@pytest.fixture(autouse=True)
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def make_snapshot(username="stive", gpus=None, observed_at=1000.0):
    return InstanceSnapshot(
        username=username,
        gpus=gpus if gpus is not None else [GpuSnapshot(index=0, mem_total=16280)],
        cpu_count=None, ram_total=None, observed_at=observed_at)


# ---------------- basic store behaviour ----------------

def test_get_for_unseen_account_returns_none():
    store = InstanceStore()
    assert store.get("nobody") is None


def test_record_then_get_round_trips_in_memory():
    store = InstanceStore()
    snap = make_snapshot()
    store.record("acct0", snap)
    assert store.get("acct0") == snap


def test_record_replaces_previous_snapshot_for_same_label():
    store = InstanceStore()
    store.record("acct0", make_snapshot(username="first"))
    store.record("acct0", make_snapshot(username="second"))
    assert store.get("acct0").username == "second"


# ---------------- disk round-trip ----------------

def test_save_then_load_round_trips():
    store = InstanceStore()
    store.record("acct0", make_snapshot(
        username="stive",
        gpus=[GpuSnapshot(index=0, mem_total=16280), GpuSnapshot(index=1, mem_total=16280)],
        observed_at=12345.0))
    store.save()

    loaded = InstanceStore.load()
    snap = loaded.get("acct0")
    assert snap is not None
    assert snap.username == "stive"
    assert snap.gpus == [GpuSnapshot(index=0, mem_total=16280),
                         GpuSnapshot(index=1, mem_total=16280)]
    assert snap.observed_at == 12345.0
    assert snap.cpu_count is None
    assert snap.ram_total is None


def test_load_with_no_file_returns_empty_store():
    store = InstanceStore.load()
    assert store.get("anyone") is None


def test_save_persists_under_state_dir_not_config_dir():
    """This is app-derived cache, rebuilt from observation, disposable the
    same way fleet.json is -- not user config like accounts.json/
    settings.json, so it belongs in state_dir()."""
    store = InstanceStore()
    store.record("acct0", make_snapshot())
    store.save()
    assert (pp.state_dir() / FILENAME).exists()
    assert not (pp.config_dir() / FILENAME).exists()


def test_multiple_accounts_round_trip_independently():
    store = InstanceStore()
    store.record("acct0", make_snapshot(username="stive"))
    store.record("acct1", make_snapshot(username="friend", observed_at=2000.0))
    store.save()

    loaded = InstanceStore.load()
    assert loaded.get("acct0").username == "stive"
    assert loaded.get("acct1").username == "friend"


# ---------------- staleness ----------------

def test_fresh_snapshot_is_not_stale():
    snap = make_snapshot(observed_at=1000.0)
    assert snap.is_stale(3600.0, now=1000.0 + 60.0) is False


def test_snapshot_older_than_threshold_is_reported_as_stale():
    """Kaggle's allocation genuinely varies run to run -- a P100 last time
    does not mean a P100 next time -- so age past the threshold must be
    detectable by the caller, not silently treated as current."""
    snap = make_snapshot(observed_at=1000.0)
    assert snap.is_stale(3600.0, now=1000.0 + 3601.0) is True


def test_snapshot_exactly_at_threshold_is_not_yet_stale():
    snap = make_snapshot(observed_at=1000.0)
    assert snap.is_stale(3600.0, now=1000.0 + 3600.0) is False


def test_is_stale_uses_a_sane_default_threshold():
    snap = make_snapshot(observed_at=1000.0)
    assert snap.is_stale(now=1000.0 + 1.0) is False
    assert snap.is_stale(now=1000.0 + DEFAULT_STALE_AFTER_SECONDS + 1.0) is True


def test_snapshot_missing_observed_at_reads_as_maximally_stale():
    """A file written before observed_at existed must not be treated as
    fresh -- absence of a timestamp is not evidence of recency."""
    snap = InstanceSnapshot(username="stive", gpus=[])
    assert snap.is_stale(1.0, now=2.0) is True


# ---------------- forward/backward compatibility ----------------

def test_load_accepts_file_written_by_older_version_missing_fields():
    """An older version's file might not have cpu_count/ram_total/gpus at
    all -- load() must default them rather than raising KeyError."""
    p = pp.state_dir() / FILENAME
    p.write_text(json.dumps({
        "acct0": {"username": "stive", "observed_at": 500.0}
    }), encoding="utf-8")

    store = InstanceStore.load()
    snap = store.get("acct0")
    assert snap is not None
    assert snap.username == "stive"
    assert snap.gpus == []
    assert snap.cpu_count is None
    assert snap.ram_total is None
    assert snap.observed_at == 500.0


def test_load_with_corrupt_json_falls_back_to_empty_store():
    p = pp.state_dir() / FILENAME
    p.write_text("{not valid json", encoding="utf-8")
    store = InstanceStore.load()
    assert store.get("acct0") is None


def test_load_with_one_malformed_entry_still_loads_the_rest():
    p = pp.state_dir() / FILENAME
    p.write_text(json.dumps({
        "acct0": "not-a-dict-at-all",
        "acct1": {"username": "friend", "observed_at": 42.0},
    }), encoding="utf-8")

    store = InstanceStore.load()
    assert store.get("acct0") is None
    assert store.get("acct1").username == "friend"


# ---------------- hardware banner fields (cpu_count/ram_total/GPU model) ----------------

def test_snapshot_can_carry_cpu_ram_and_gpu_model():
    """The fields the hardware-banner parser feeds -- previously always
    None, now populated when the banner arrived for this run."""
    snap = InstanceSnapshot(
        username="stive",
        gpus=[GpuSnapshot(index=0, mem_total=16280, model="Tesla P100-PCIE-16GB")],
        cpu_count=4, ram_total=31.3, observed_at=1000.0)
    assert snap.cpu_count == 4
    assert snap.ram_total == 31.3
    assert snap.gpus[0].model == "Tesla P100-PCIE-16GB"


def test_gpu_model_round_trips_through_save_and_load():
    store = InstanceStore()
    store.record("acct0", InstanceSnapshot(
        username="stive",
        gpus=[GpuSnapshot(index=0, mem_total=15360, model="Tesla T4"),
             GpuSnapshot(index=1, mem_total=15360, model="Tesla T4")],
        cpu_count=4, ram_total=31.3, observed_at=12345.0))
    store.save()

    loaded = InstanceStore.load()
    snap = loaded.get("acct0")
    assert snap.cpu_count == 4
    assert snap.ram_total == 31.3
    assert snap.gpus == [GpuSnapshot(index=0, mem_total=15360, model="Tesla T4"),
                         GpuSnapshot(index=1, mem_total=15360, model="Tesla T4")]


def test_load_accepts_gpu_entries_from_before_model_existed():
    """A file written before GpuSnapshot had a model field must not raise
    KeyError -- model defaults to None, same discipline as cpu_count/
    ram_total on the snapshot itself."""
    p = pp.state_dir() / FILENAME
    p.write_text(json.dumps({
        "acct0": {"username": "stive", "observed_at": 500.0,
                  "gpus": [{"index": 0, "mem_total": 16280}]}
    }), encoding="utf-8")

    store = InstanceStore.load()
    snap = store.get("acct0")
    assert snap.gpus == [GpuSnapshot(index=0, mem_total=16280, model=None)]
