"""Read-only IMAP helpers built on imap-tools.

Every fetch uses mark_seen=False, so imap-tools issues BODY.PEEK and reading a
message never sets the \\Seen flag in the real mailbox. Folders are opened with
EXAMINE, and ReadOnlyMailBox (guard.py) stops every other command at the socket.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from imap_tools import AND, OR, MailBox

from .accounts import Account
from .auth import CredentialUnavailable, credential_for
from .guard import ReadOnlyMailBox


@contextmanager
def open_box(account: Account, folder: str = "INBOX") -> Iterator[MailBox]:
    """Yield a logged-in mailbox with `folder` opened read-only. Raises on failure.

    The credential is read on every call, so a new password or a token from a fresh
    `imap-mcp auth` applies without a restart. An expired or missing credential
    raises CredentialUnavailable before any connection is made.
    """
    credential = credential_for(account)
    status = credential.offline_status()
    if status.status != "ok":
        raise CredentialUnavailable(status.status, f"{account.key}: {status.hint}")
    box = ReadOnlyMailBox(account.host, port=account.port)
    try:
        credential.login(box)  # no folder selected yet
        box.folder.set(folder, readonly=True)  # EXAMINE
        yield box
    finally:
        with suppress(Exception):
            box.logout()


def _snippet(msg, length: int = 200) -> str:
    body = (msg.text or msg.html or "").strip().replace("\r", " ").replace("\n", " ")
    return body[:length]


def row(msg, account_key: str) -> dict:
    """Compact metadata row for list/search results."""
    return {
        "account": account_key,
        "id": msg.uid,
        "from": msg.from_,
        "to": list(msg.to),
        "subject": msg.subject,
        "date": msg.date.isoformat() if msg.date else None,
        "unread": "\\Seen" not in msg.flags,
        "snippet": _snippet(msg),
    }


def _criteria(query: str | None, since: dt.date | None, unread_only: bool):
    parts = []
    if query:
        parts.append(OR(from_=query, subject=query, text=query))
    if since:
        parts.append(AND(date_gte=since))
    if unread_only:
        parts.append(AND(seen=False))
    if not parts:
        return AND(all=True)
    return AND(*parts) if len(parts) > 1 else parts[0]


def fetch_rows(
    account: Account,
    *,
    query: str | None = None,
    since: dt.date | None = None,
    unread_only: bool = False,
    limit: int = 25,
) -> list[dict]:
    crit = _criteria(query, since, unread_only)
    with open_box(account) as box:
        msgs = box.fetch(
            crit,
            limit=limit,
            reverse=True,  # newest first
            mark_seen=False,  # BODY.PEEK -- never touch \Seen
            bulk=True,
            headers_only=False,
        )
        return [row(m, account.key) for m in msgs]


def fetch_one(account: Account, uid: str) -> dict | None:
    """Full message by UID (INBOX). Returns None if not found."""
    with open_box(account) as box:
        msgs = list(box.fetch(AND(uid=uid), mark_seen=False, bulk=False))
        if not msgs:
            return None
        m = msgs[0]
        return {
            "account": account.key,
            "id": m.uid,
            "from": m.from_,
            "to": list(m.to),
            "cc": list(m.cc),
            "subject": m.subject,
            "date": m.date.isoformat() if m.date else None,
            "unread": "\\Seen" not in m.flags,
            "message_id": m.headers.get("message-id", [None])[0],
            "body": (m.text or m.html or "").strip(),
        }
