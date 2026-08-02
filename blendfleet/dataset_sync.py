"""Upload a .blend file as a Kaggle dataset, creating or versioning as needed."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable


class SyncError(Exception):
    """Raised when sync_blend refuses to submit to Kaggle locally -- e.g. the
    staged copy of the .blend did not land correctly. Deliberately distinct
    from KaggleError (blendfleet.kaggle_client): this failure never reaches
    the network at all, so there is no HTTPError/upload response to report.
    """


def sync_blend(client, blend: Path, slug: str, staging: Path,
                on_progress: Callable | None = None) -> str:
    """Upload `blend` as dataset `slug`, creating it if it does not exist.

    Returns "created" or "versioned". The client already ensures dir_mode="skip"
    is used, so the payload is never nested and /kaggle/input path resolution
    works correctly in notebooks.

    Args:
        client: A KaggleClient with dataset_exists(), dataset_create(), dataset_version()
        blend: Path to the .blend file
        slug: Dataset slug in format "owner/name"
        staging: Path to a temporary staging folder (will be wiped and recreated)
        on_progress: optional callback threaded through to the client's
            upload, called with blendfleet.uploader.UploadProgress ticks

    Returns:
        "created" if this was a new dataset, "versioned" if updated

    Raises:
        SyncError: the staged copy of `blend` did not land correctly (e.g.
            a truncated write), so sync_blend refuses locally rather than
            handing Kaggle a request that references a broken/missing file.
        KaggleError: (from client.dataset_create/dataset_version) the real
            upload to Kaggle did not complete -- raised BEFORE any create/
            version request is sent, never after, so Kaggle never sees an
            empty file list.
    """
    # Wipe and recreate the staging folder
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    # Copy the .blend file into staging
    staged_blend = staging / blend.name
    shutil.copy(blend, staged_blend)

    # Independent local guard: never let a request go out that references a
    # file which didn't actually land in staging correctly. This is on top
    # of (not instead of) client.dataset_create/dataset_version's own
    # refusal when the real network upload doesn't complete -- see this
    # module's docstring and blendfleet/kaggle_client.py's _preflight_upload.
    expected_size = blend.stat().st_size
    actual_size = staged_blend.stat().st_size if staged_blend.exists() else None
    if actual_size != expected_size:
        raise SyncError(
            f"staging copy of {blend.name} did not complete correctly "
            f"(expected {expected_size} bytes, got {actual_size!r}); "
            "refusing to upload. Retry the render."
        )

    # Write dataset metadata
    # Extract title from slug: "owner/my-cool-blend" -> "my cool blend"
    title = slug.split("/", 1)[1].replace("-", " ")
    metadata = {
        "title": title,
        "id": slug,
        "licenses": [{"name": "CC0-1.0"}],
    }
    (staging / "dataset-metadata.json").write_text(
        json.dumps(metadata, indent=2),
        encoding="utf-8"
    )

    # Check if dataset exists and create or version accordingly
    if client.dataset_exists(slug):
        client.dataset_version(staging, f"update {blend.name}",
                               on_progress=on_progress)
        return "versioned"
    else:
        client.dataset_create(staging, on_progress=on_progress)
        return "created"
