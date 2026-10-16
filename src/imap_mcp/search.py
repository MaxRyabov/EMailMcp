"""UID SEARCH for the tools, including queries in Cyrillic (D9).

imaplib sends at most one literal per command, at its end, while the criterion
`OR OR FROM q SUBJECT q TEXT q` holds the query three times. A non-ASCII query is
therefore sent as three commands, one per field, each with the UTF-8 query as a
trailing literal; the UIDs are merged here. An ASCII query keeps the imap-tools
path: one `UID SEARCH CHARSET US-ASCII` without a literal, as upstream does.
"""

from __future__ import annotations

import datetime as dt

from imap_tools import AND, OR, MailBox

from .pool import MailError

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FIELDS = ("FROM", "SUBJECT", "TEXT")


def criteria(query: str | None, since: dt.date | None, unread_only: bool):
    """imap-tools criteria for the ASCII path."""
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


def _imap_date(d: dt.date) -> str:
    return f"{d.day}-{_MONTHS[d.month - 1]}-{d.year}"  # not strftime: locale-independent


def uids(
    box: MailBox, query: str | None = None, since: dt.date | None = None, unread_only: bool = False
) -> list[int]:
    """UIDs matching the criteria in the examined folder, newest (largest) first."""
    if query is None or query.isascii():
        found = {int(u) for u in box.uids(criteria(query, since, unread_only))}
    else:
        common = []
        if since:
            common += ["SINCE", _imap_date(since)]
        if unread_only:
            common.append("UNSEEN")
        client = box.client
        found = set()
        for field in _FIELDS:
            client.literal = query.encode("utf-8")
            typ, data = client.uid("SEARCH", "CHARSET", "UTF-8", *common, field)
            if typ != "OK":
                raise MailError(f"сервер отклонил поиск по {field}: {_text(data)}")
            for line in data:
                if line:
                    found.update(int(u) for u in line.split())
    return sorted(found, reverse=True)


def _text(data) -> str:
    return " ".join(d.decode("utf-8", "replace") for d in data if isinstance(d, bytes))
