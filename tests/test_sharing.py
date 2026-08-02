"""Task 3: automatic private sharing.

Two traps, both measured live against two real Kaggle accounts (see
task-3-report.md), and both must be pinned by a test:

1. `update_dataset_metadata` REPLACES the whole settings object -- every
   write must resend title, `is_private=True`, and exactly one license,
   every single time, regardless of what the caller's `current_settings`
   looked like.
2. It returns `{"errors": [...]}` with HTTP 200 -- a non-empty array IS the
   failure; there is no exception or bad status code to catch instead.
"""
from kagglesdk.datasets.types.dataset_types import (
    DatasetCollaborator, DatasetSettings, DatasetSettingsFile, SettingsLicense)
from kagglesdk.users.types.users_enums import CollaboratorType
import pytest

from blendfleet.sharing import (
    ShareError, get_settings, grant_readers, list_collaborators,
    revoke_reader)


def collaborator(username: str, role: CollaboratorType) -> DatasetCollaborator:
    c = DatasetCollaborator()
    c.username = username
    c.role = role
    return c


class FakeDatasetApiClient:
    """Stands in for `sdk.datasets.dataset_api_client`. Captures every
    update request and returns a controllable errors array."""

    def __init__(self, errors=None, info=None):
        self._errors = errors or []
        self._info = info
        self.updated = []  # list of ApiUpdateDatasetMetadataRequest
        self.get_calls = []

    def get_dataset_metadata(self, request):
        self.get_calls.append((request.owner_slug, request.dataset_slug))

        class Resp:
            info = self._info
        return Resp()

    def update_dataset_metadata(self, request):
        self.updated.append(request)

        class Resp:
            errors = self._errors
        return Resp()


class FakeDatasets:
    def __init__(self, api_client):
        self.dataset_api_client = api_client


class FakeSdk:
    def __init__(self, errors=None, info=None):
        self.api_client = FakeDatasetApiClient(errors=errors, info=info)
        self.datasets = FakeDatasets(self.api_client)


def bare_current_settings() -> DatasetSettings:
    """A dataset with no title/license/collaborators yet -- the shape a
    brand-new dataset's metadata comes back as."""
    s = DatasetSettings()
    return s


# --------------------------------------------------------------------- 1 --

def test_grant_readers_always_sends_is_private_true():
    """Even if current_settings somehow reports is_private=False (it never
    should for our datasets, but the trap is that update REPLACES the
    object), the outgoing request must still set is_private=True."""
    sdk = FakeSdk()
    current = DatasetSettings()
    current.is_private = False  # simulate the risky case explicitly

    grant_readers(sdk, "owner", "slug", ["dansbecker"], current)

    sent = sdk.api_client.updated[0].settings
    assert sent.is_private is True


def test_grant_readers_sends_exactly_one_license_when_none_exist():
    sdk = FakeSdk()
    current = bare_current_settings()
    assert current.licenses == []

    grant_readers(sdk, "owner", "slug", ["dansbecker"], current)

    sent = sdk.api_client.updated[0].settings
    assert len(sent.licenses) == 1
    assert sent.licenses[0].name == "CC0-1.0"


def test_grant_readers_sends_exactly_one_license_when_current_has_several():
    """Kaggle only accepts exactly one -- trim rather than resend all."""
    sdk = FakeSdk()
    current = bare_current_settings()
    lic1, lic2 = SettingsLicense(), SettingsLicense()
    lic1.name, lic2.name = "CC0-1.0", "other-license"
    current.licenses = [lic1, lic2]

    grant_readers(sdk, "owner", "slug", ["dansbecker"], current)

    sent = sdk.api_client.updated[0].settings
    assert len(sent.licenses) == 1


def test_grant_readers_preserves_title_from_current_settings():
    sdk = FakeSdk()
    current = bare_current_settings()
    current.title = "my cool blend"

    grant_readers(sdk, "owner", "slug", ["dansbecker"], current)

    assert sdk.api_client.updated[0].settings.title == "my cool blend"


def _fully_described_current_settings() -> DatasetSettings:
    current = bare_current_settings()
    current.title = "my cool blend"
    current.subtitle = "a neat little scene"
    current.description = "Rendered nightly across three accounts."
    current.keywords = ["blender", "render-farm"]
    current.expected_update_frequency = "weekly"
    current.user_specified_sources = "my own render"
    f = DatasetSettingsFile()
    f.name = "scene.blend"
    f.description = "the actual .blend"
    current.data = [f]
    return current


def test_grant_readers_preserves_description_subtitle_keywords_and_more():
    """The unnamed sibling of trap 1: update_dataset_metadata replaces the
    WHOLE settings object, not just is_private/licenses. A description or
    keywords set through the Kaggle web UI must not be silently wiped the
    next time a reader is granted."""
    sdk = FakeSdk()
    current = _fully_described_current_settings()

    grant_readers(sdk, "owner", "slug", ["dansbecker"], current)

    sent = sdk.api_client.updated[0].settings
    assert sent.subtitle == "a neat little scene"
    assert sent.description == "Rendered nightly across three accounts."
    assert sent.keywords == ["blender", "render-farm"]
    assert sent.expected_update_frequency == "weekly"
    assert sent.user_specified_sources == "my own render"
    assert len(sent.data) == 1
    assert sent.data[0].name == "scene.blend"
    assert sent.data[0].description == "the actual .blend"


def test_revoke_reader_preserves_description_subtitle_keywords_and_more():
    """Same replace-clobbers-what-isn't-resent trap applies to revoke."""
    sdk = FakeSdk()
    current = _fully_described_current_settings()
    current.collaborators = [collaborator("bob", CollaboratorType.READER)]

    revoke_reader(sdk, "owner", "slug", "bob", current)

    sent = sdk.api_client.updated[0].settings
    assert sent.subtitle == "a neat little scene"
    assert sent.description == "Rendered nightly across three accounts."
    assert sent.keywords == ["blender", "render-farm"]
    assert sent.expected_update_frequency == "weekly"
    assert sent.user_specified_sources == "my own render"
    assert len(sent.data) == 1 and sent.data[0].name == "scene.blend"


# --------------------------------------------------------------------- 2 --

def test_grant_readers_raises_on_nonempty_errors_even_though_http_ok():
    """The fake's update_dataset_metadata returns cleanly (no exception, no
    bad status) -- the only signal of failure is the errors array."""
    sdk = FakeSdk(errors=["The licenses array must specify exactly one license."])
    current = bare_current_settings()

    with pytest.raises(ShareError, match="exactly one license"):
        grant_readers(sdk, "owner", "slug", ["dansbecker"], current)


def test_grant_readers_succeeds_silently_on_empty_errors():
    sdk = FakeSdk(errors=[])
    current = bare_current_settings()
    grant_readers(sdk, "owner", "slug", ["dansbecker"], current)  # must not raise


# ------------------------------------------------- collaborator handling --

def test_grant_readers_preserves_existing_collaborators():
    sdk = FakeSdk()
    current = bare_current_settings()
    current.collaborators = [collaborator("alice", CollaboratorType.WRITER)]

    grant_readers(sdk, "owner", "slug", ["bob"], current)

    sent = sdk.api_client.updated[0].settings
    by_name = {c.username: c.role for c in sent.collaborators}
    assert by_name["alice"] == CollaboratorType.WRITER, "existing collaborator clobbered"
    assert by_name["bob"] == CollaboratorType.READER


def test_grant_readers_does_not_downgrade_an_existing_higher_role():
    sdk = FakeSdk()
    current = bare_current_settings()
    current.collaborators = [collaborator("bob", CollaboratorType.WRITER)]

    grant_readers(sdk, "owner", "slug", ["bob"], current)

    sent = sdk.api_client.updated[0].settings
    by_name = {c.username: c.role for c in sent.collaborators}
    assert by_name["bob"] == CollaboratorType.WRITER


def test_grant_readers_grants_several_usernames_at_once():
    sdk = FakeSdk()
    current = bare_current_settings()

    grant_readers(sdk, "owner", "slug", ["alice", "bob"], current)

    sent = sdk.api_client.updated[0].settings
    names = {c.username for c in sent.collaborators}
    assert names == {"alice", "bob"}


def test_grant_readers_sends_the_right_owner_and_dataset_slug():
    sdk = FakeSdk()
    grant_readers(sdk, "stivestivewithani", "bf-share-test", ["dansbecker"],
                   bare_current_settings())

    req = sdk.api_client.updated[0]
    assert req.owner_slug == "stivestivewithani"
    assert req.dataset_slug == "bf-share-test"


# ----------------------------------------------------------------- revoke --

def test_revoke_reader_removes_only_the_named_username():
    sdk = FakeSdk()
    current = bare_current_settings()
    current.collaborators = [
        collaborator("alice", CollaboratorType.WRITER),
        collaborator("bob", CollaboratorType.READER),
    ]

    revoke_reader(sdk, "owner", "slug", "bob", current)

    sent = sdk.api_client.updated[0].settings
    names = {c.username for c in sent.collaborators}
    assert names == {"alice"}


def test_revoke_reader_still_sends_is_private_true_and_one_license():
    sdk = FakeSdk()
    current = bare_current_settings()
    current.collaborators = [collaborator("bob", CollaboratorType.READER)]

    revoke_reader(sdk, "owner", "slug", "bob", current)

    sent = sdk.api_client.updated[0].settings
    assert sent.is_private is True
    assert len(sent.licenses) == 1


def test_revoke_reader_raises_on_nonempty_errors():
    sdk = FakeSdk(errors=["something went wrong"])
    with pytest.raises(ShareError, match="something went wrong"):
        revoke_reader(sdk, "owner", "slug", "bob", bare_current_settings())


# ------------------------------------------------------------- reading it --

def test_get_settings_returns_info_not_settings_attribute():
    """Confirms the response really is read from `.info` -- a stub response
    that only HAS `.info` (no `.settings` attribute at all) must work."""
    info = bare_current_settings()
    sdk = FakeSdk(info=info)
    assert get_settings(sdk, "owner", "slug") is info


def test_get_settings_sends_owner_and_dataset_slug():
    sdk = FakeSdk(info=bare_current_settings())
    get_settings(sdk, "stivestivewithani", "bf-share-test")
    assert sdk.api_client.get_calls == [("stivestivewithani", "bf-share-test")]


def test_list_collaborators_returns_username_role_tuples():
    info = bare_current_settings()
    info.collaborators = [
        collaborator("dansbecker", CollaboratorType.READER),
        collaborator("alice", CollaboratorType.WRITER),
    ]
    sdk = FakeSdk(info=info)

    assert list_collaborators(sdk, "owner", "slug") == [
        ("dansbecker", "READER"), ("alice", "WRITER")]


def test_list_collaborators_empty_when_none_granted():
    sdk = FakeSdk(info=bare_current_settings())
    assert list_collaborators(sdk, "owner", "slug") == []
