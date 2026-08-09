import json
from pathlib import Path
import pytest
from blendfleet.dataset_sync import SyncError, sync_blend


class FakeClient:
    def __init__(self, exists=False):
        self.exists = exists
        self.calls = []

    def dataset_exists(self, slug: str) -> bool:
        self.calls.append(("dataset_exists", slug))
        return self.exists

    def dataset_create(self, folder: Path, on_progress=None) -> None:
        self.calls.append(("dataset_create", str(folder), on_progress))

    def dataset_version(self, folder: Path, message: str, on_progress=None) -> None:
        self.calls.append(("dataset_version", str(folder), message, on_progress))


@pytest.fixture
def blend(tmp_path):
    p = tmp_path / "remember.blend"
    p.write_bytes(b"BLENDER" + b"\0" * 500)
    return p


def test_creates_when_absent(blend, tmp_path):
    c = FakeClient(exists=False)
    assert sync_blend(c, blend, "me/remember-blend", tmp_path / "stage") == "created"
    assert any(call[0] == "dataset_create" for call in c.calls)


def test_versions_when_present(blend, tmp_path):
    c = FakeClient(exists=True)
    assert sync_blend(c, blend, "me/remember-blend", tmp_path / "stage") == "versioned"
    assert any(call[0] == "dataset_version" for call in c.calls)


def test_metadata_written_correctly(blend, tmp_path):
    stage = tmp_path / "stage"
    sync_blend(FakeClient(), blend, "me/remember-blend", stage)
    meta = json.loads((stage / "dataset-metadata.json").read_text())
    assert meta["id"] == "me/remember-blend"
    assert (stage / "remember.blend").exists()


def test_staging_wiped_and_recreated(blend, tmp_path):
    stage = tmp_path / "stage"
    # Create a file in staging to verify it gets wiped
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "old_file.txt").write_text("old")

    sync_blend(FakeClient(), blend, "me/x", stage)

    # Old file should be gone
    assert not (stage / "old_file.txt").exists()
    # New files should be there
    assert (stage / "remember.blend").exists()
    assert (stage / "dataset-metadata.json").exists()


def test_metadata_title_formatted(blend, tmp_path):
    stage = tmp_path / "stage"
    sync_blend(FakeClient(), blend, "user/my-cool-blend", stage)
    meta = json.loads((stage / "dataset-metadata.json").read_text())
    assert meta["title"] == "my cool blend"


def test_on_progress_threaded_through_to_dataset_create(blend, tmp_path):
    c = FakeClient(exists=False)
    marker = object()
    sync_blend(c, blend, "me/remember-blend", tmp_path / "stage", on_progress=marker)
    create_call = next(call for call in c.calls if call[0] == "dataset_create")
    assert create_call[-1] is marker


def test_on_progress_threaded_through_to_dataset_version(blend, tmp_path):
    c = FakeClient(exists=True)
    marker = object()
    sync_blend(c, blend, "me/remember-blend", tmp_path / "stage", on_progress=marker)
    version_call = next(call for call in c.calls if call[0] == "dataset_version")
    assert version_call[-1] is marker


def test_truncated_staging_copy_refuses_locally_and_never_calls_client(
        blend, tmp_path, monkeypatch):
    """Second, independent defence against the empty-file 400: even if the
    upload step itself would have been fine, a staged copy that didn't land
    correctly (truncated write, disk full mid-copy, etc.) must be caught
    locally before the client -- and therefore Kaggle -- is ever touched."""
    import blendfleet.dataset_sync as dataset_sync

    def truncated_copy(src, dst):
        Path(dst).write_bytes(Path(src).read_bytes()[:10])  # short write

    monkeypatch.setattr(dataset_sync.shutil, "copy", truncated_copy)
    c = FakeClient(exists=False)

    with pytest.raises(SyncError, match="did not complete correctly"):
        sync_blend(c, blend, "me/remember-blend", tmp_path / "stage")

    assert c.calls == [], "client must never be touched once staging is bad"
