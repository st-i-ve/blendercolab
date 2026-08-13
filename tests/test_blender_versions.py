"""Which Blender does the rendering.

RenderSettings has carried blender_version since the beginning, and the
notebook builds its download URL from it -- but nothing ever let a user
choose, and nothing validated the string. A typo there is not caught
until the kernel is running, where it costs a session's startup to find
out (wget 404 -> the notebook's own assert).
"""
import pytest

from blendfleet.blender_versions import (DEFAULT_VERSION, KNOWN_VERSIONS,
                                         download_url, validate_version)


def test_the_default_is_one_of_the_offered_versions():
    assert DEFAULT_VERSION in KNOWN_VERSIONS


def test_a_known_version_validates():
    assert validate_version("4.2.0") == "4.2.0"


def test_whitespace_is_forgiven():
    assert validate_version("  5.2.0 ") == "5.2.0"


def test_an_unknown_but_well_formed_version_is_allowed():
    """Blender releases faster than this app does. A version that LOOKS
    like a version is accepted, because refusing it would mean a new
    release cannot be used until BlendFleet ships again."""
    assert validate_version("6.1.3") == "6.1.3"


@pytest.mark.parametrize("bad", ["", "latest", "5.2", "v5.2.0", "5.2.0-beta",
                                 "5.2.0; rm -rf /"])
def test_a_string_that_is_not_a_version_is_refused(bad):
    with pytest.raises(ValueError) as excinfo:
        validate_version(bad)
    message = str(excinfo.value)
    assert "major.minor.patch" in message, "must say what shape is expected"
    assert "4.2.0" in message or DEFAULT_VERSION in message, \
        "must show a real example"


def test_digits_from_another_script_are_not_a_version():
    """Python's \\d matches any Unicode decimal, so "٥.٢.٠" once passed
    validation and produced a download URL with no matching file --
    exactly the 404-inside-a-running-session this module exists to
    prevent."""
    with pytest.raises(ValueError):
        validate_version("٥.٢.٠")


def test_the_url_follows_blenders_own_layout():
    # download.blender.org/release/Blender5.2/blender-5.2.0-linux-x64.tar.xz
    url = download_url("5.2.0")
    assert url == ("https://download.blender.org/release/Blender5.2/"
                   "blender-5.2.0-linux-x64.tar.xz")


def test_the_url_refuses_a_version_that_did_not_validate():
    with pytest.raises(ValueError):
        download_url("latest")
