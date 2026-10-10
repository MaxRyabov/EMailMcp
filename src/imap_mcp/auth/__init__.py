"""Login methods: password from the environment or OAuth tokens from the keyring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import CredentialProvider, CredentialUnavailable, OfflineStatus, PasswordCredential

if TYPE_CHECKING:
    from ..accounts import Account

__all__ = [
    "CredentialProvider",
    "CredentialUnavailable",
    "OfflineStatus",
    "PasswordCredential",
    "credential_for",
]


def credential_for(account: Account) -> CredentialProvider:
    if account.auth == "oauth":
        from .oauth import OAuthCredential

        return OAuthCredential(account)
    return PasswordCredential(account)
