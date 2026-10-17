"""Read-only IMAP operations built on imap-tools.

Every fetch uses mark_seen=False, so imap-tools issues BODY.PEEK and reading a
message never sets the \\Seen flag in the real mailbox. Folders are opened with
EXAMINE, and ReadOnlyMailBox (guard.py) stops every other command at the socket.

The functions taking a `box` run inside ConnectionPool.run (pool.py).
"""

from __future__ import annotations

import datetime as dt
import imaplib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress

from imap_tools import MailBox, MailMessage
from imap_tools.errors import UnexpectedCommandStatusError

from . import attachments, htmltext, ids
from .accounts import Account
from .auth import CredentialUnavailable, credential_for
from .guard import ReadOnlyMailBox
from .pool import MailError
from .redact import redact

SNIPPET_LENGTH = 200
SNIPPET_MAX_SIZE = 128 * 1024  # larger messages get no snippet (D10)
SNIPPET_BUDGET = 3 * 1024 * 1024  # second-pass bytes per account and call
BODY_MAX_SIZE = 40 * 1024 * 1024  # get_email does not download larger messages
TOO_LARGE_NOTE = "письмо больше 40 МБ, тело не загружено"
_UNSELECTABLE = ("\\noselect", "\\nonexistent")


@contextmanager
def open_box(account: Account, folder: str = "INBOX") -> Iterator[MailBox]:
    """Yield a fresh logged-in mailbox with `folder` opened read-only (list_accounts).

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


# --- folders -------------------------------------------------------------------


def _selectable(flags: Iterable[str]) -> bool:
    return not any(f.lower() in _UNSELECTABLE for f in flags)


def folders(box: MailBox) -> list[dict]:
    """LIST with names decoded from modified UTF-7; STATUS only for selectable folders."""
    rows = []
    for info in box.folder.list():
        row = {
            "name": info.name,
            "delimiter": None if info.delim.upper() == "NIL" else info.delim,
            "flags": list(info.flags),
            "selectable": _selectable(info.flags),
        }
        if row["selectable"]:
            try:
                status = box.folder.status(info.name, ["MESSAGES", "UNSEEN"])
                row["messages"] = status.get("MESSAGES")
                row["unseen"] = status.get("UNSEEN")
            except imaplib.IMAP4.abort:
                raise
            except (imaplib.IMAP4.error, UnexpectedCommandStatusError) as e:
                row["status_error"] = redact(f"{type(e).__name__}: {e}")
        rows.append(row)
    return rows


def resolve_folder(box: MailBox, folder: str) -> str:
    """A special-use alias (\\Junk, ...) becomes this account's folder with that flag."""
    if not folder.startswith("\\"):
        return folder
    for info in box.folder.list():
        if any(f.lower() == folder.lower() for f in info.flags):
            return info.name
    raise MailError(f"у аккаунта нет папки с флагом {folder}; список папок — list_folders")


def examine(box: MailBox, folder: str) -> int:
    """Open `folder` read-only and return its UIDVALIDITY."""
    try:
        box.folder.set(folder, readonly=True)  # EXAMINE
    except UnexpectedCommandStatusError:
        raise MailError(
            f"папка {folder!r} не найдена или недоступна; список папок — list_folders"
        ) from None
    # imaplib's select() empties untagged_responses first, so this is the fresh value.
    values = box.client.untagged_responses.get("UIDVALIDITY") or [b""]
    try:
        return int(values[-1])
    except (TypeError, ValueError):
        raise MailError(f"сервер не сообщил UIDVALIDITY папки {folder!r}") from None


# --- messages ------------------------------------------------------------------


def message_date(msg: MailMessage) -> dt.datetime | None:
    """Date header; unparsed (imap-tools gives 1900-01-01) is None, no zone means UTC."""
    d = msg.date
    if d.tzinfo is None and (d.year, d.month, d.day) == (1900, 1, 1):
        return None
    return d if d.tzinfo is not None else d.replace(tzinfo=dt.UTC)


def sort_rows(rows: list[dict]) -> list[dict]:
    """Newest first by UTC; rows without a date go last."""

    def key(row: dict):
        date = row.get("date")
        if date is None:
            return (1, 0.0)
        return (0, -dt.datetime.fromisoformat(date).timestamp())

    return sorted(rows, key=key)


def body_text(msg: MailMessage) -> str:
    """text/plain when present, otherwise the HTML part as text (D11)."""
    text = msg.text.strip()
    if text:
        return text.replace("\r\n", "\n")
    return htmltext.html_to_text(msg.html) if msg.html else ""


def snippet(msg: MailMessage) -> str:
    return " ".join(body_text(msg).split())[:SNIPPET_LENGTH]


def _fetch(box: MailBox, uids: list[int], headers_only: bool) -> dict[int, MailMessage]:
    if not uids:  # imap-tools would run SEARCH ALL for an empty uid_list
        return {}
    msgs = box.fetch(
        uid_list=[str(u) for u in uids],
        headers_only=headers_only,
        mark_seen=False,  # BODY.PEEK -- never touch \Seen
        bulk=True,
    )
    return {int(m.uid): m for m in msgs if m.uid}


def _row(msg: MailMessage, account_key: str, folder: str, uidvalidity: int) -> dict:
    date = message_date(msg)
    return {
        "account": account_key,
        "folder": folder,
        "id": ids.make(account_key, folder, uidvalidity, int(msg.uid)),
        "from": msg.from_,
        "to": list(msg.to),
        "subject": msg.subject,
        "date": date.isoformat() if date else None,
        "unread": "\\Seen" not in msg.flags,
        "size": msg.size_rfc822,
        "snippet": None,
    }


def rows(
    box: MailBox, account_key: str, folder: str, uidvalidity: int, uids: list[int]
) -> list[dict]:
    """Rows for `uids` (newest first) in two passes (D10).

    Pass 1 fetches headers, flags and RFC822.SIZE. Pass 2 fetches whole messages
    for snippets: only those of at most 128 KB by RFC822.SIZE, newest first, while
    the pass stays within 3 MB. The rest keep `snippet: null`.
    """
    heads = _fetch(box, uids, headers_only=True)
    chosen, budget = [], SNIPPET_BUDGET
    for uid in uids:
        msg = heads.get(uid)
        size = msg.size_rfc822 if msg else 0
        if not size or size > SNIPPET_MAX_SIZE:  # 0: the server did not say
            continue
        if size > budget:
            break
        budget -= size
        chosen.append(uid)
    full = _fetch(box, chosen, headers_only=False)
    out = []
    for uid in uids:
        if uid not in heads:
            continue
        row = _row(heads[uid], account_key, folder, uidvalidity)
        if uid in full:
            row["snippet"] = snippet(full[uid])
        out.append(row)
    return out


def fetch_message(box: MailBox, mid: ids.MessageId) -> tuple[MailMessage, bool]:
    """The message by id and whether it was downloaded whole (not over 40 MB)."""
    if examine(box, mid.folder) != mid.uidvalidity:
        raise MailError("список устарел, повторите list_emails/search_emails")
    head = _fetch(box, [mid.uid], headers_only=True).get(mid.uid)
    if head is None:
        raise MailError("письмо не найдено: удалено или перемещено")
    if head.size_rfc822 > BODY_MAX_SIZE:
        return head, False
    msg = _fetch(box, [mid.uid], headers_only=False).get(mid.uid)
    if msg is None:
        raise MailError("письмо не найдено: удалено или перемещено")
    return msg, True


def message_fields(msg: MailMessage, mid: ids.MessageId, whole: bool) -> dict:
    """get_email fields; the body is the whole text, the caller cuts the window."""
    date = message_date(msg)
    out = {
        "account": mid.account,
        "id": str(mid),
        "folder": mid.folder,
        "from": msg.from_,
        "to": list(msg.to),
        "cc": list(msg.cc),
        "reply_to": list(msg.reply_to),
        "subject": msg.subject,
        "date": date.isoformat() if date else None,
        "unread": "\\Seen" not in msg.flags,
        "message_id": msg.headers.get("message-id", [None])[0],
        "size": msg.size_rfc822,
    }
    if whole:
        out.update(body=body_text(msg), attachments=attachments.metadata(msg))
    else:
        out.update(body=None, attachments=None, note=TOO_LARGE_NOTE)
    return out
