import pytest
import blendfleet.platform_paths as pp
from blendfleet.accounts import Account, AccountStore, TokenFormatError


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


VALID = "KGAT_" + "a" * 32


def test_rejects_malformed_token():
    s = AccountStore()
    with pytest.raises(TokenFormatError):
        s.add(Account(label="bad", token="not-a-token"))


def test_accepts_kgat_token():
    s = AccountStore()
    s.add(Account(label="me", token=VALID))
    assert len(s.list()) == 1


def test_rejects_duplicate_token():
    s = AccountStore()
    s.add(Account(label="me", token=VALID))
    with pytest.raises(ValueError, match="already"):
        s.add(Account(label="other", token=VALID))


def test_rejects_duplicate_label():
    """The label -- not the token -- is the join key used by fleet.poll(),
    cancel_all(), collect() and the dashboard. Two accounts sharing one
    label means a worker driven with the wrong person's token."""
    s = AccountStore()
    s.add(Account(label="me", token=VALID))
    with pytest.raises(ValueError, match="label"):
        s.add(Account(label="me", token="KGAT_" + "b" * 32))
    assert len(s.list()) == 1


def test_duplicate_label_error_names_the_label():
    s = AccountStore()
    s.add(Account(label="james", token=VALID))
    with pytest.raises(ValueError, match="james"):
        s.add(Account(label="james", token="KGAT_" + "c" * 32))


def test_different_labels_are_fine():
    s = AccountStore()
    s.add(Account(label="me", token=VALID))
    s.add(Account(label="friend", token="KGAT_" + "b" * 32))
    assert [a.label for a in s.list()] == ["me", "friend"]


def test_roundtrips_through_disk():
    s = AccountStore()
    s.add(Account(label="me", token=VALID, username="stivestivewithani"))
    s.add(Account(label="friend", token="KGAT_" + "b" * 32))
    s.save()
    assert [a.label for a in AccountStore.load().list()] == ["me", "friend"]


def test_remove_by_label():
    s = AccountStore()
    s.add(Account(label="me", token=VALID))
    s.remove("me")
    assert s.list() == []


def test_load_with_no_file_is_empty():
    assert AccountStore.load().list() == []
