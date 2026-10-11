"""How an account logs in: the CredentialProvider strategy (D1) and password login."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets as _random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from imap_tools import BaseMailBox

from ..redact import register

if TYPE_CHECKING:
    from ..accounts import Account


@dataclass(frozen=True)
class OfflineStatus:
    """What can be said about a credential without touching the network."""

    status: Literal["ok", "no-credential", "reauth-required"]
    hint: str | None = None
    warning: str | None = None
    expires_at: dt.datetime | None = None


class CredentialUnavailable(Exception):
    """No usable credential right now; status and hint say what to do."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


class CredentialProvider(Protocol):
    def offline_status(self) -> OfflineStatus: ...

    def login(self, box: BaseMailBox) -> None:
        """Log `box` in without selecting a folder. Raises CredentialUnavailable
        when the stored credential must be renewed by the user."""
        ...

    def fingerprint(self) -> str:
        """Changes whenever the account entry or its secret changes (connection pool)."""
        ...

    def secrets(self) -> list[str]: ...


# Fingerprints only need to be stable within one process; a random key keeps a
# leaked fingerprint from being brute-forced back into a weak password.
_FINGERPRINT_KEY = _random.token_bytes(32)


def fingerprint(account: Account, secret: str | float | None) -> str:
    data = f"{account!r}\0{secret}".encode()
    return hmac.new(_FINGERPRINT_KEY, data, hashlib.sha256).hexdigest()


def _is_gmail(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == "gmail.com" or host.endswith(".gmail.com")


class PasswordCredential:
    """Upstream behaviour: LOGIN with a password from an environment variable."""

    def __init__(self, account: Account):
        self.account = account

    def _password(self) -> str | None:
        raw = self.account.password()
        # Google shows app passwords with spaces for readability; IMAP wants them bare.
        pw = raw.replace(" ", "") if raw and _is_gmail(self.account.host) else raw
        register(raw, pw)
        return pw

    def offline_status(self) -> OfflineStatus:
        if self._password():
            return OfflineStatus("ok")
        return OfflineStatus(
            "no-credential", hint=f"set environment variable {self.account.password_env}"
        )

    def login(self, box: BaseMailBox) -> None:
        pw = self._password()
        if not pw:
            raise CredentialUnavailable(
                "no-credential",
                f"no password for {self.account.key} (env {self.account.password_env} unset)",
            )
        # initial_folder=None: imap-tools would otherwise send SELECT right after login.
        box.login(self.account.email, pw, initial_folder=None)

    def fingerprint(self) -> str:
        return fingerprint(self.account, self._password())

    def secrets(self) -> list[str]:
        raw, pw = self.account.password(), self._password()
        return [v for v in dict.fromkeys((raw, pw)) if v]
