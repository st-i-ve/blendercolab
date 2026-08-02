"""Upload a .blend file as a Kaggle dataset, creating or versioning as needed."""
from __future__ import annotations

import json
import shutil
from pathlib import Path


def sync_blend(client, blend: Path, slug: str, staging: Path) -> str:
    """Upload `blend` as dataset `slug`, creating it if it does not exist.

    Returns "created" or "versioned". The client already ensures dir_mode="skip"
    is used, so the payload is never nested and /kaggle/input path resolution
    works correctly in notebooks.

    Args:
        client: A KaggleClient with dataset_exists(), dataset_create(), dataset_version()
        blend: Path to the .blend file
        slug: Dataset slug in format "owner/name"
        staging: Path to a temporary staging folder (will be wiped and recreated)

    Returns:
        "created" if this was a new dataset, "versioned" if updated
    """
    # Wipe and recreate the staging folder
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    # Copy the .blend file into staging
    shutil.copy(blend, staging / blend.name)

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
        client.dataset_version(staging, f"update {blend.name}")
        return "versioned"
    else:
        client.dataset_create(staging)
        return "created"
