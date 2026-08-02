import json

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


# ---------------- verification ----------------

def test_add_with_passing_verifier_stores_username_and_marks_verified():
    s = AccountStore()
    s.add(Account(label="me", token=VALID), verifier=lambda t: "stivestivewithani")
    acct = s.list()[0]
    assert acct.username == "stivestivewithani"
    assert acct.verified is True


def test_add_with_failing_verifier_does_not_add_account():
    s = AccountStore()

    def boom(token):
        raise ValueError("revoked token")

    with pytest.raises(ValueError, match="revoked"):
        s.add(Account(label="me", token=VALID), verifier=boom)
    assert s.list() == []


def test_add_accepts_valid_token_with_no_notebooks():
    """whoami() raises KaggleError('...no notebooks...') for a VALID token
    on an account that has simply never created a notebook. The verifier
    contract turns that into a returned None (not a raise) -- add() must
    still accept the account, just with an unresolved username."""
    s = AccountStore()
    s.add(Account(label="me", token=VALID), verifier=lambda t: None)
    acct = s.list()[0]
    assert acct.username is None
    assert acct.verified is True
    assert len(s.list()) == 1


def test_add_without_verifier_leaves_unverified():
    s = AccountStore()
    s.add(Account(label="me", token=VALID))
    assert s.list()[0].verified is False


def test_reverify_updates_username_and_verified():
    s = AccountStore()
    s.add(Account(label="me", token=VALID))
    s.reverify("me", verifier=lambda t: "stivestivewithani")
    acct = s.list()[0]
    assert acct.username == "stivestivewithani"
    assert acct.verified is True


def test_reverify_failure_marks_unverified_and_raises():
    s = AccountStore()
    s.add(Account(label="me", token=VALID), verifier=lambda t: "stivestivewithani")

    def boom(token):
        raise ValueError("revoked token")

    with pytest.raises(ValueError, match="revoked"):
        s.reverify("me", verifier=boom)
    acct = s.list()[0]
    assert acct.verified is False
    # a failed re-verify must not erase the last-known username
    assert acct.username == "stivestivewithani"


def test_reverify_unknown_label_raises():
    s = AccountStore()
    with pytest.raises(ValueError, match="no account"):
        s.reverify("ghost", verifier=lambda t: "x")


def test_load_accepts_accounts_json_without_verified_field():
    """The user already has an accounts.json on disk from before this field
    existed. load() must not crash on it, and must default it to False
    rather than lying about a state that was never checked."""
    p = pp.config_dir() / "accounts.json"
    p.write_text(json.dumps([
        {"label": "me", "token": VALID, "username": "stivestivewithani"}
    ]), encoding="utf-8")

    s = AccountStore.load()
    assert [a.label for a in s.list()] == ["me"]
    assert s.list()[0].verified is False
    assert s.list()[0].username == "stivestivewithani"
