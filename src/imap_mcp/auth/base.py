"""How an account logs in: the CredentialProvider strategy (D1) and password login."""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from imap_tools import BaseMailBox

from ..redact import register

if TYPE_CHECKING:
    from ..accounts import Account


@dataclass(frozen=True)
class OfflineStatus:
    """What can be said about a credential without touching the network."""

    status: str  # ok | no-credential | reauth-required
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


def fingerprint(account: Account, secret: str | float | None) -> str:
    return hashlib.sha256(f"{account!r}\0{secret}".encode()).hexdigest()


class PasswordCredential:
    """Upstream behaviour: LOGIN with a password from an environment variable."""

    def __init__(self, account: Account):
        self.account = account

    def _password(self) -> str | None:
        pw = self.account.password()
        register(pw)
        # Google shows app passwords with spaces for readability; IMAP wants them bare.
        if pw and self.account.host.endswith("gmail.com"):
            pw = pw.replace(" ", "")
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
        pw = self.account.password()
        return [pw] if pw else []
