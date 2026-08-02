"""Grant/revoke Kaggle dataset collaborators.

Dataset sharing IS automatable (proven live against two real accounts --
see task-3-report.md): `update_dataset_metadata` can add a
`DatasetCollaborator` with `CollaboratorType.READER` to an existing private
dataset, and the grant is genuinely enforced -- a friend account could not
see the dataset before the grant and could see it after. That means one
upload to the owner's account plus a grant per friend, instead of every
account uploading its own copy of a (potentially huge) .blend.

`client` throughout this module is a raw `kagglesdk.KaggleClient` (or
anything duck-typed the same way): calls go through
`client.datasets.dataset_api_client.{get_dataset_metadata,
update_dataset_metadata}`, mirroring how blendfleet/kaggle_client.py already
reaches `sdk.blobs.blob_api_client`/`sdk.kernels.kernels_api_client` for
calls with no equivalent in the older `KaggleApi`.

Two traps here, both measured live -- every write in this module guards
both:

1. `update_dataset_metadata` REPLACES the whole settings object; it is not
   a patch. Omitting `is_private=True` would silently make somebody's
   private .blend public. Omitting licenses gets back "The licenses array
   must specify exactly one license." So every call here resends title,
   `is_private=True` (unconditionally -- never trust what the caller's
   `current_settings` says), exactly one license, and the FULL
   collaborator list (existing collaborators carried over untouched, plus
   whatever this call is adding/removing) -- never a partial update.
2. The response is `{"errors": [...]}` with HTTP 200 -- there is no
   exception and no non-200 status to catch a failure from. A non-empty
   `errors` list IS the failure and must be inspected explicitly.
"""
from __future__ import annotations

DEFAULT_LICENSE = "CC0-1.0"


class ShareError(Exception):
    """`update_dataset_metadata` returned a non-empty `errors` array.

    Deliberately not `blendfleet.kaggle_client.KaggleError`: this failure
    is read out of a 200 response body, never an HTTPError, so there is no
    exception chain to reuse that class's HTTPError-shaped constructor for.
    """


def get_settings(client, owner: str, slug: str):
    """Fetch the dataset's current metadata.

    Returns `response.info` (a `DatasetInfo`) -- NOT `response.settings`,
    which does not exist on `ApiGetDatasetMetadataResponse`. Callers pass
    this straight into `grant_readers`/`revoke_reader` as `current_settings`
    so the update call that follows can resend the whole object (trap 1).
    """
    from kagglesdk.datasets.types.dataset_api_service import (
        ApiGetDatasetMetadataRequest)

    request = ApiGetDatasetMetadataRequest()
    request.owner_slug = owner
    request.dataset_slug = slug
    response = client.datasets.dataset_api_client.get_dataset_metadata(request)
    return response.info


def list_collaborators(client, owner: str, slug: str) -> list[tuple[str, str]]:
    """Current (username, role) pairs, e.g. [("dansbecker", "READER")]."""
    info = get_settings(client, owner, slug)
    return [(c.username, c.role.name)
            for c in (info.collaborators or []) if c.username]


def _one_license(current_settings):
    """Exactly one license, always -- Kaggle 400s on zero, and the API only
    accepts one regardless of how many `current_settings` happens to carry."""
    from kagglesdk.datasets.types.dataset_types import SettingsLicense

    existing = list(getattr(current_settings, "licenses", None) or [])
    if existing:
        return [existing[0]]
    lic = SettingsLicense()
    lic.name = DEFAULT_LICENSE
    return [lic]


def _build_settings(current_settings, collaborators):
    from kagglesdk.datasets.types.dataset_types import DatasetSettings

    settings = DatasetSettings()
    settings.title = getattr(current_settings, "title", "") or ""
    # ALWAYS True, no matter what current_settings reports: this call
    # REPLACES the settings object (trap 1), so omitting this would
    # silently publish someone's private Blender project.
    settings.is_private = True
    settings.licenses = _one_license(current_settings)
    settings.collaborators = collaborators
    return settings


def _send(client, owner: str, slug: str, settings, action: str) -> None:
    from kagglesdk.datasets.types.dataset_api_service import (
        ApiUpdateDatasetMetadataRequest)

    request = ApiUpdateDatasetMetadataRequest()
    request.owner_slug = owner
    request.dataset_slug = slug
    request.settings = settings
    response = client.datasets.dataset_api_client.update_dataset_metadata(request)
    # Trap 2: this is HTTP 200 whether it worked or not. Success means this
    # array is empty -- there is no status code or exception to check instead.
    errors = list(getattr(response, "errors", None) or [])
    if errors:
        raise ShareError(
            f"{action} {owner}/{slug} failed: {'; '.join(errors)}")


def grant_readers(client, owner: str, slug: str, usernames: list[str],
                   current_settings) -> None:
    """Grant every username in `usernames` READER access.

    Every collaborator already on the dataset is carried over untouched --
    an existing WRITER/ADMIN/other READER is never downgraded or dropped --
    and a username already present is left at its current role rather than
    being re-added.
    """
    from kagglesdk.datasets.types.dataset_types import DatasetCollaborator
    from kagglesdk.users.types.users_enums import CollaboratorType

    existing = list(getattr(current_settings, "collaborators", None) or [])
    collaborators = list(existing)
    have = {c.username for c in existing if c.username}
    for username in usernames:
        if username in have:
            continue
        collaborator = DatasetCollaborator()
        collaborator.username = username
        collaborator.role = CollaboratorType.READER
        collaborators.append(collaborator)
        have.add(username)

    settings = _build_settings(current_settings, collaborators)
    _send(client, owner, slug, settings,
          f"granting {', '.join(usernames)} reader access to")


def revoke_reader(client, owner: str, slug: str, username: str,
                   current_settings) -> None:
    """Remove `username` from the collaborator list, preserving everyone
    else on the dataset untouched."""
    existing = list(getattr(current_settings, "collaborators", None) or [])
    collaborators = [c for c in existing if c.username != username]

    settings = _build_settings(current_settings, collaborators)
    _send(client, owner, slug, settings, f"revoking {username}'s access to")
