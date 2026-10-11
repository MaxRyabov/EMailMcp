"""Opaque message id: v1.<account>.<base64url folder>.<UIDVALIDITY>.<UID> (D8).

The account key cannot contain a dot (KEY_PATTERN) and base64url has none either,
so the id splits unambiguously. A future Gmail API backend gets the prefix `g1.`.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from . import validate

_UINT32_MAX = 2**32 - 1
_ID = re.compile(
    r"v1\.(?P<account>[a-z0-9_-]{1,32})\.(?P<folder>[A-Za-z0-9_-]{1,2000})"
    r"\.(?P<uidvalidity>[0-9]{1,10})\.(?P<uid>[0-9]{1,10})"
)


@dataclass(frozen=True)
class MessageId:
    account: str
    folder: str
    uidvalidity: int
    uid: int

    def __str__(self) -> str:
        b64 = base64.urlsafe_b64encode(self.folder.encode("utf-8")).rstrip(b"=").decode("ascii")
        return f"v1.{self.account}.{b64}.{self.uidvalidity}.{self.uid}"


def make(account: str, folder: str, uidvalidity: int, uid: int) -> str:
    return str(MessageId(account, folder, uidvalidity, uid))


def _bad() -> validate.ArgumentError:
    return validate.ArgumentError(
        "id", "неверный формат id; используйте id из list_emails или search_emails"
    )


def parse(value: object) -> MessageId:
    """Raises ArgumentError for anything that is not an id this server produced."""
    m = _ID.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        raise _bad()
    b64 = m["folder"]
    try:
        folder = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise _bad() from None
    uidvalidity, uid = int(m["uidvalidity"]), int(m["uid"])
    if not (0 < uidvalidity <= _UINT32_MAX and 0 < uid <= _UINT32_MAX):
        raise _bad()
    try:
        validate.text("folder", folder, validate.FOLDER_MAX)
    except validate.ArgumentError:
        raise _bad() from None
    mid = MessageId(m["account"], folder, uidvalidity, uid)
    if str(mid) != value:  # non-canonical base64 (stray bits) or leading zeros
        raise _bad()
    return mid
