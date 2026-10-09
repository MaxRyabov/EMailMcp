"""Unified read-only email MCP server. Reads across all configured IMAP
mailboxes and exposes list/get/search tools to an MCP client."""

from __future__ import annotations

import datetime as dt
import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import accounts as acct
from . import imap, status
from .redact import redact

mcp = MCPServer("imap-mcp", version="0.1.0")


def safe_tool(fn):
    """Turn any exception into a tool error with secrets redacted (D15).

    mcp 2.0.0 validates a returned CallToolResult against the tool's output schema,
    so the error is raised as ToolError: the client gets `isError: true` with this
    text. `from None` keeps the original exception, and its secrets, out of the chain.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            raise ToolError(redact(f"{type(e).__name__}: {e}")) from None

    return wrapper


def _error_row(key: str, e: Exception) -> dict:
    return {"account": key, "error": redact(f"{type(e).__name__}: {e}")}


def _broken_rows(account: str | None) -> list[dict]:
    """Errors of broken config entries when merging all accounts."""
    if account:
        return []
    return [{"account": b.key, "error": b.status} for b in acct.load().broken if b.enabled]


def _parse_since(since: str | None) -> dt.date | None:
    if not since:
        return None
    return dt.date.fromisoformat(since)  # raises ValueError on bad input


def _targets(account: str | None) -> list[acct.Account]:
    if account:
        a = acct.get_account(account)
        return [a] if a.enabled else []
    return acct.enabled_accounts()


@mcp.tool()
@safe_tool
def list_accounts() -> status.AccountsReport:
    """List configured mail accounts and whether each is reachable right now.

    Performs a live IMAP login per enabled account, so `ok` means a login has just
    succeeded. OAuth accounts also report the token expiry date. `warnings` lists
    tokens that expire soon and the absence of enabled accounts.
    """
    return status.report(live=True)


@mcp.tool()
@safe_tool
def list_emails(
    account: str | None = None,
    since: str | None = None,
    unread_only: bool = False,
    limit: int = 25,
) -> list[dict]:
    """List recent emails, newest first.

    account: account key (see list_accounts). Omit to merge ALL enabled accounts.
    since: ISO date (YYYY-MM-DD) lower bound, optional.
    unread_only: only unseen messages.
    limit: max rows (per account when merging).
    """
    since_d = _parse_since(since)
    rows: list[dict] = _broken_rows(account)
    for a in _targets(account):
        try:
            rows.extend(imap.fetch_rows(a, since=since_d, unread_only=unread_only, limit=limit))
        except Exception as e:  # noqa: BLE001 -- per-account fail-safe
            rows.append(_error_row(a.key, e))
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    return rows


@mcp.tool()
@safe_tool
def search_emails(
    query: str,
    account: str | None = None,
    since: str | None = None,
    limit: int = 25,
) -> list[dict]:
    """Search emails across from/subject/body text, newest first.

    query: free text; matched against sender, subject, and body (IMAP OR).
    account: omit to search ALL enabled accounts.
    since: ISO date (YYYY-MM-DD) lower bound, optional.
    limit: max rows (per account when merging).
    """
    since_d = _parse_since(since)
    rows: list[dict] = _broken_rows(account)
    for a in _targets(account):
        try:
            rows.extend(imap.fetch_rows(a, query=query, since=since_d, limit=limit))
        except Exception as e:  # noqa: BLE001
            rows.append(_error_row(a.key, e))
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    return rows


@mcp.tool()
@safe_tool
def get_email(account: str, id: str) -> dict:
    """Fetch one full email (plain-text body preferred) by account + message id.

    id is the value returned in list_emails/search_emails rows (IMAP UID).
    Reading does not mark the message as read.
    """
    a = acct.get_account(account)
    msg = imap.fetch_one(a, id)
    if msg is None:
        return {"account": account, "id": id, "error": "not found in INBOX"}
    return msg
