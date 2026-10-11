"""OAuth secrets in the system keyring (D4), never in files.

One JSON record per account under service `imap-mcp`, client_secret as a
separate record. On Windows records are created with CRED_PERSIST_LOCAL_MACHINE
so they do not roam with a domain profile.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import keyring
import keyring.errors
from keyring.backends.Windows import WinVaultKeyring  # importable on every platform

from ..redact import register

SERVICE = "imap-mcp"
# Credential Manager caps a blob at 2560 bytes, i.e. 1280 UTF-16 characters.
MAX_RECORD_CHARS = 1280


class StoreError(Exception):
    """The keyring cannot be used, or a record cannot be written."""


class CorruptRecord(StoreError):
    """The keyring answered, but the account's record is not valid JSON of Tokens."""


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float  # unix time
    obtained_at: float  # unix time
    scope: str


def _secret_user(key: str) -> str:
    return f"{key}:client_secret"


def backend():
    """The active keyring backend, set to machine-local persistence on Windows."""
    kr = keyring.get_keyring()
    if isinstance(kr, WinVaultKeyring):
        kr.persist = "local_machine"
    return kr


def backend_name() -> str:
    kr = keyring.get_keyring()
    return f"{type(kr).__module__}.{type(kr).__name__}"


def _call(fn, *args):
    try:
        return fn(*args)
    except keyring.errors.PasswordDeleteError:
        raise
    except (keyring.errors.KeyringError, OSError, RuntimeError) as e:
        raise StoreError(
            f"системное хранилище секретов недоступно (keyring backend {backend_name()}): "
            f"{type(e).__name__}"
        ) from None


def _get(user: str) -> str | None:
    return _call(backend().get_password, SERVICE, user)


def _set(user: str, value: str) -> None:
    length = len(value.encode("utf-16-le")) // 2
    if length > MAX_RECORD_CHARS:
        raise StoreError(
            f"запись для keyring слишком длинная: {length} символов, лимит {MAX_RECORD_CHARS}"
        )
    _call(backend().set_password, SERVICE, user, value)


def _delete(user: str) -> bool:
    """True if the record existed and is gone, False if there was none."""
    if _get(user) is None:
        return False
    try:
        _call(backend().delete_password, SERVICE, user)
    except keyring.errors.PasswordDeleteError:
        if _get(user) is None:  # removed in between
            return True
        raise StoreError(
            f"не удалось удалить запись {user} из keyring (backend {backend_name()})"
        ) from None
    return True


def load_tokens(key: str) -> Tokens | None:
    raw = _get(key)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        tokens = Tokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=float(data["expires_at"]),
            obtained_at=float(data["obtained_at"]),
            scope=data.get("scope", ""),
        )
    except (ValueError, KeyError, TypeError):
        raise CorruptRecord(f"запись токенов аккаунта {key} в keyring повреждена") from None
    register(tokens.access_token, tokens.refresh_token)
    return tokens


def save_tokens(key: str, tokens: Tokens) -> None:
    register(tokens.access_token, tokens.refresh_token)
    _set(key, json.dumps(asdict(tokens)))


def delete_tokens(key: str) -> bool:
    return _delete(key)


def load_client_secret(key: str) -> str | None:
    secret = _get(_secret_user(key))
    register(secret)
    return secret


def save_client_secret(key: str, secret: str) -> None:
    register(secret)
    _set(_secret_user(key), secret)


def delete_client_secret(key: str) -> bool:
    return _delete(_secret_user(key))
