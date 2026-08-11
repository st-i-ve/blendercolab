from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

from blendfleet.platform_paths import config_dir

TOKEN_RE = re.compile(r"^KGAT_[0-9a-fA-F]{32}$")
FILENAME = "accounts.json"

# A verifier is any callable token -> username-or-None, or raising on
# failure. `username is None` means "token is valid but Kaggle could not
# resolve a handle" (an account with no notebooks yet -- see
# kaggle_client.verify_token) -- that is NOT a failure and must still be
# accepted. Anything the callable raises IS a failure: format problems,
# revoked tokens, network errors all propagate unchanged so the caller
# (the setup dialog) can show the real message.
Verifier = Callable[[str], "str | None"]


class CorruptAccountsError(RuntimeError):
    """accounts.json is empty or unparseable.

    Raised rather than returning an empty store: a silent empty list looks
    identical to "every account vanished", and the user would add them all
    again on top of a file that may still be recoverable.
    """


class TokenFormatError(ValueError):
    """Token is not in the KGAT_<32 hex> form Kaggle issues."""


@dataclass
class Account:
    label: str
    token: str
    username: str | None = None
    # False for every account loaded from an accounts.json written before
    # this field existed (dataclass default fills the missing key in) --
    # they show up as "not verified" until the user re-verifies, rather
    # than crashing load() or lying about a state we never checked.
    verified: bool = False


class AccountStore:
    def __init__(self, accounts: list[Account] | None = None) -> None:
        self._accounts: list[Account] = list(accounts or [])

    def validate(self, account: Account) -> None:
        """Every rule an account must satisfy that costs no I/O at all.

        Split out of add() so a caller that has to verify on a background
        thread (the setup dialog) can still run these checks FIRST, on its
        own thread, and keep the original guarantee that a bad format or a
        duplicate never triggers a network call. Raises; returns None when
        the account is acceptable.
        """
        if not TOKEN_RE.match(account.token):
            raise TokenFormatError(
                "Expected a token like KGAT_ followed by 32 hex characters. "
                "Generate one at kaggle.com -> Settings -> API -> Generate New Token."
            )
        if any(a.token == account.token for a in self._accounts):
            raise ValueError("that token is already registered")
        # The label -- not the token -- is the join key used by fleet.poll(),
        # fleet.cancel_all(), collector.collect() and the dashboard table.
        # Two accounts sharing a label means one worker gets driven with the
        # wrong account's token: polled, cancelled and collected against
        # somebody else's kernel.
        if any(a.label == account.label for a in self._accounts):
            raise ValueError(
                f"the label {account.label!r} is already in use. Labels must be "
                "unique: they are how each render worker is matched back to "
                "its account.")

    def add(self, account: Account, verifier: Verifier | None = None) -> None:
        self.validate(account)
        # verifier=None (the default) skips verification entirely -- kept so
        # callers that only care about format/duplicate rules (and every
        # pre-existing test) don't need a network stub. The setup dialog
        # always passes a real verifier; this only runs after every local
        # check above has passed, so a bad format/duplicate never triggers
        # a network call. Raising here means the account is NOT appended --
        # a failed verification must not silently add the account.
        if verifier is not None:
            account.username = verifier(account.token)
            account.verified = True
        self._accounts.append(account)

    def reverify(self, label: str, verifier: Verifier) -> None:
        """Re-check an existing account's token without removing/re-adding
        it. Tokens get revoked after they're added; this is how the user
        finds out without losing the account's label/position."""
        for a in self._accounts:
            if a.label == label:
                try:
                    username = verifier(a.token)
                except Exception:
                    a.verified = False
                    raise
                a.username = username
                a.verified = True
                return
        raise ValueError(f"no account labeled {label!r}")

    def remove(self, label: str) -> None:
        self._accounts = [a for a in self._accounts if a.label != label]

    def list(self) -> list[Account]:
        return list(self._accounts)

    def _path(self) -> Path:
        return config_dir() / FILENAME

    def save(self) -> None:
        p = self._path()
        # Written via a temporary file and replaced: an interrupted save
        # used to leave a truncated accounts.json, which reads back as
        # "Expecting value: line 1 column 1 (char 0)" from wherever the
        # next load happens to be -- and looks like every account vanished.
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps([asdict(a) for a in self._accounts], indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)
        try:
            os.chmod(p, 0o600)      # no-op on Windows, meaningful on Linux
        except OSError:
            pass

    @classmethod
    def load(cls) -> "AccountStore":
        p = config_dir() / FILENAME
        if not p.exists():
            return cls()
        raw = p.read_text(encoding="utf-8").strip()
        if not raw:
            # Silently returning an empty store would look exactly like
            # every account having vanished, and the user would re-add
            # them over the top of a file that may still be recoverable.
            raise CorruptAccountsError(
                f"the accounts file at {p} is empty. Nothing has been lost "
                "on Kaggle -- only this app's list of which accounts to "
                "use. Restore it from a backup if you have one, or delete "
                "it and add the accounts again.")
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CorruptAccountsError(
                f"the accounts file at {p} is not valid JSON ({e}). Nothing "
                "has been lost on Kaggle -- only this app's list of which "
                "accounts to use. Fix or delete the file and add the "
                "accounts again.") from e
        return cls([Account(**d) for d in entries])
