"""Account registry. Loads non-secret config from accounts.toml.

Passwords come from the environment at runtime (export it, use direnv, a secrets
manager like 1Password's `op run`, whatever fits your setup); OAuth tokens come
from the system keyring (auth/store.py).

The file is parsed account by account: a broken entry gets status
`config-error: <reason>` and the others keep working. The registry is re-read
when the file's mtime changes, so edits apply without a restart.
"""

from __future__ import annotations

import os
import re
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .auth.profiles import PROVIDERS

# Config resolution order: $IMAP_MCP_ACCOUNTS if set, else accounts.toml at the
# project root (src/imap_mcp/accounts.py -> two parents up).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "accounts.toml"

# The key is part of every message id (v1.<key>.<...>), so it must not contain dots.
KEY_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")
AUTH_METHODS = ("password", "oauth")


def config_path() -> Path:
    override = os.environ.get("IMAP_MCP_ACCOUNTS")
    return Path(override) if override else DEFAULT_CONFIG_PATH


class ConfigError(Exception):
    """The config file is missing or is not valid TOML."""


class AccountConfigError(Exception):
    """The account exists in the config but its entry is invalid."""


@dataclass(frozen=True)
class Account:
    key: str
    label: str
    email: str
    host: str
    port: int
    password_env: str | None
    enabled: bool
    auth: str = "password"
    oauth_provider: str | None = None
    client_id: str | None = None

    def password(self) -> str | None:
        """Password from the named env var, or None if unset/empty."""
        if not self.password_env:
            return None
        return os.environ.get(self.password_env) or None


@dataclass(frozen=True)
class BrokenAccount:
    """An [[account]] entry that failed validation; never used for mail access."""

    key: str
    label: str
    email: str
    enabled: bool
    error: str

    @property
    def status(self) -> str:
        return f"config-error: {self.error}"


@dataclass(frozen=True)
class Registry:
    path: Path
    accounts: dict[str, Account]
    broken: list[BrokenAccount]


def _parse(raw: object) -> Account:
    """One validated account; raises AccountConfigError with a short reason."""
    if not isinstance(raw, dict):
        raise AccountConfigError("entry is not a table")
    key = raw.get("key")
    if not isinstance(key, str) or not key:
        raise AccountConfigError("missing key")
    if not KEY_PATTERN.fullmatch(key):
        raise AccountConfigError(f"invalid key, expected {KEY_PATTERN.pattern}")
    if "scope" in raw:
        raise AccountConfigError(
            "scope is not allowed: access rights come only from the provider profile"
        )
    auth = raw.get("auth", "password")
    if auth not in AUTH_METHODS:
        expected = ", ".join(AUTH_METHODS)
        raise AccountConfigError(f"invalid auth {auth!r}, expected one of {expected}")
    email = raw.get("email")
    if not isinstance(email, str) or not email:
        raise AccountConfigError("missing email")

    host, port = raw.get("host"), raw.get("port")
    provider = raw.get("oauth_provider")
    client_id = raw.get("client_id")
    password_env = raw.get("password_env")
    if auth == "oauth":
        if not provider:
            raise AccountConfigError("missing oauth_provider")
        if provider not in PROVIDERS:
            raise AccountConfigError(
                f"unknown oauth_provider {provider!r}, supported: {', '.join(sorted(PROVIDERS))}"
            )
        if not isinstance(client_id, str) or not client_id:
            raise AccountConfigError("missing client_id")
        profile = PROVIDERS[provider]
        host = host or profile.imap_host
        port = port or profile.imap_port
    elif not isinstance(password_env, str) or not password_env:
        raise AccountConfigError("missing password_env")
    if not isinstance(host, str) or not host:
        raise AccountConfigError("missing host")
    try:
        port = int(port or 993)
    except (TypeError, ValueError):
        raise AccountConfigError("invalid port") from None

    return Account(
        key=key,
        label=str(raw.get("label") or email),
        email=email,
        host=host,
        port=port,
        password_env=password_env if auth == "password" else None,
        enabled=bool(raw.get("enabled", True)),
        auth=auth,
        oauth_provider=provider if auth == "oauth" else None,
        client_id=client_id if auth == "oauth" else None,
    )


def _broken(index: int, raw: object, error: str) -> BrokenAccount:
    raw = raw if isinstance(raw, dict) else {}
    key = raw.get("key")
    key = key if isinstance(key, str) and key else f"#{index + 1}"
    email = str(raw.get("email") or "")
    return BrokenAccount(
        key=key,
        label=str(raw.get("label") or email),
        email=email,
        enabled=bool(raw.get("enabled", True)),
        error=error,
    )


def parse(path: Path, raw: dict) -> Registry:
    entries = raw.get("account", [])
    if not isinstance(entries, list):
        entries = [entries]
    parsed: list[Account | BrokenAccount] = []
    for i, entry in enumerate(entries):
        try:
            parsed.append(_parse(entry))
        except AccountConfigError as e:
            parsed.append(_broken(i, entry, str(e)))

    keys = [p.key for p in parsed]
    accounts: dict[str, Account] = {}
    broken: list[BrokenAccount] = []
    for i, p in enumerate(parsed):
        if isinstance(p, Account) and keys.count(p.key) > 1:
            p = _broken(i, entries[i], f"duplicate key {p.key!r}")
        if isinstance(p, BrokenAccount):
            broken.append(p)
        else:
            accounts[p.key] = p
    return Registry(path=path, accounts=accounts, broken=broken)


_cache_lock = threading.Lock()
_cache: tuple[Path, int, Registry] | None = None


def load() -> Registry:
    """The registry for the current config file, re-read when its mtime changes."""
    global _cache
    path = config_path()
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        raise ConfigError(
            f"no account config at {path}; copy accounts.example.toml to "
            "accounts.toml and fill in your accounts (or point "
            "IMAP_MCP_ACCOUNTS at a config file)"
        ) from None
    except OSError as e:  # no access, a file where a directory should be, ...
        raise ConfigError(f"cannot read account config {path}: {type(e).__name__}") from None
    with _cache_lock:
        if _cache is not None and _cache[0] == path and _cache[1] == mtime:
            return _cache[2]
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"account config {path} is not valid TOML: {e}") from None
    except OSError as e:
        raise ConfigError(f"cannot read account config {path}: {type(e).__name__}") from None
    registry = parse(path, raw)
    with _cache_lock:
        _cache = (path, mtime, registry)
    return registry


def clear_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def all_accounts() -> list[Account]:
    return list(load().accounts.values())


def enabled_accounts() -> list[Account]:
    return [a for a in all_accounts() if a.enabled]


def get_account(key: str) -> Account:
    registry = load()
    if key in registry.accounts:
        return registry.accounts[key]
    for b in registry.broken:
        if b.key == key:
            raise AccountConfigError(f"account {key!r}: {b.status}")
    known = [*registry.accounts, *(b.key for b in registry.broken)]
    raise KeyError(f"unknown account {key!r}; known: {', '.join(known)}")
