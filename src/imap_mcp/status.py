"""Account statuses for `list_accounts` and `imap-mcp status`.

Computed on every call, never cached. Without a login only disabled,
config-error, no-credential and an expired token are known; `ok` means a login
has just succeeded.
"""

from __future__ import annotations

from typing import TypedDict

from . import accounts as acct
from . import imap
from .auth import CredentialUnavailable, credential_for
from .redact import redact

NOT_CHECKED = "not checked"


class AccountsReport(TypedDict):
    accounts: list[dict]
    warnings: list[str]


def _entry(a: acct.Account | acct.BrokenAccount, auth: str | None) -> dict:
    entry = {"account": a.key, "label": a.label, "email": a.email, "enabled": a.enabled}
    if auth:
        entry["auth"] = auth
    return entry


def account_status(account: acct.Account, live: bool) -> dict:
    entry = _entry(account, account.auth)
    if not account.enabled:
        entry["status"] = "disabled"
        return entry
    offline = credential_for(account).offline_status()
    if account.auth == "oauth":
        entry["expires_at"] = offline.expires_at.date().isoformat() if offline.expires_at else None
    if offline.warning:
        entry["warning"] = offline.warning
    if offline.status != "ok":
        entry["status"] = offline.status
        entry["hint"] = offline.hint
        return entry
    if not live:
        entry["status"] = NOT_CHECKED
        return entry
    try:
        with imap.open_box(account):
            entry["status"] = "ok"
    except CredentialUnavailable as e:
        entry["status"] = e.status
        entry["hint"] = redact(e)
    except Exception as e:  # noqa: BLE001 -- report, don't crash
        entry["status"] = f"unreachable: {type(e).__name__} ({account.host}:{account.port})"
    return entry


def report(live: bool) -> AccountsReport:
    registry = acct.load()
    entries = [account_status(a, live) for a in registry.accounts.values()]
    for b in registry.broken:
        entry = _entry(b, None)
        entry["status"] = b.status
        entries.append(entry)
    warnings = [e["warning"] for e in entries if e.get("warning")]
    if not any(a.enabled for a in registry.accounts.values()):
        warnings.append(f"нет включённых аккаунтов, конфиг: {registry.path}")
    return {"accounts": entries, "warnings": warnings}
