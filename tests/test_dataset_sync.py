import json
from pathlib import Path
import pytest
from blendfleet.dataset_sync import sync_blend


class FakeClient:
    def __init__(self, exists=False):
        self.exists = exists
        self.calls = []

    def dataset_exists(self, slug: str) -> bool:
        self.calls.append(("dataset_exists", slug))
        return self.exists

    def dataset_create(self, folder: Path) -> None:
        self.calls.append(("dataset_create", str(folder)))

    def dataset_version(self, folder: Path, message: str) -> None:
        self.calls.append(("dataset_version", str(folder), message))


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
