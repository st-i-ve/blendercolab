from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path

from blendfleet.platform_paths import config_dir

TOKEN_RE = re.compile(r"^KGAT_[0-9a-fA-F]{32}$")
FILENAME = "accounts.json"


class TokenFormatError(ValueError):
    """Token is not in the KGAT_<32 hex> form Kaggle issues."""


@dataclass
class Account:
    label: str
    token: str
    username: str | None = None


class AccountStore:
    def __init__(self, accounts: list[Account] | None = None) -> None:
        self._accounts: list[Account] = list(accounts or [])

    def add(self, account: Account) -> None:
        if not TOKEN_RE.match(account.token):
            raise TokenFormatError(
                "Expected a token like KGAT_ followed by 32 hex characters. "
                "Generate one at kaggle.com -> Settings -> API -> Generate New Token."
            )
        if any(a.token == account.token for a in self._accounts):
            raise ValueError("that token is already registered")
        self._accounts.append(account)

    def remove(self, label: str) -> None:
        self._accounts = [a for a in self._accounts if a.label != label]

    def list(self) -> list[Account]:
        return list(self._accounts)

    def _path(self) -> Path:
        return config_dir() / FILENAME

    def save(self) -> None:
        p = self._path()
        p.write_text(json.dumps([asdict(a) for a in self._accounts], indent=2),
                     encoding="utf-8")
        try:
            os.chmod(p, 0o600)      # no-op on Windows, meaningful on Linux
        except OSError:
            pass

    @classmethod
    def load(cls) -> "AccountStore":
        p = config_dir() / FILENAME
        if not p.exists():
            return cls()
        return cls([Account(**d) for d in json.loads(p.read_text(encoding="utf-8"))])
