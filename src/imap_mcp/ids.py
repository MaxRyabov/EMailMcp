"""Opaque message id: v1.<account>.<base64url folder>.<UIDVALIDITY>.<UID> (D8).

The account key cannot contain a dot (KEY_BODY) and base64url has none either,
so the id splits unambiguously. A future Gmail API backend gets the prefix `g1.`.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from . import validate
from .accounts import KEY_BODY

_UINT32_MAX = 2**32 - 1
# Base64 of the longest folder name: FOLDER_MAX characters of up to 4 UTF-8 bytes.
_FOLDER_B64_MAX = (validate.FOLDER_MAX * 4 * 4 + 2) // 3
_ID = re.compile(
    rf"v1\.(?P<account>(?:{KEY_BODY}))"
    rf"\.(?P<folder>[A-Za-z0-9_-]{{1,{_FOLDER_B64_MAX}}})"
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


def _bad(value: object) -> validate.ArgumentError:
    return validate.ArgumentError(
        "id",
        f"неверный формат id ({validate.shown(value)}); "
        "используйте id из list_emails или search_emails",
    )


def parse(value: object) -> MessageId:
    """Raises ArgumentError for anything that is not an id this server produced."""
    m = _ID.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        raise _bad(value)
    b64 = m["folder"]
    try:
        folder = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise _bad(value) from None
    uidvalidity, uid = int(m["uidvalidity"]), int(m["uid"])
    if not (0 < uidvalidity <= _UINT32_MAX and 0 < uid <= _UINT32_MAX):
        raise _bad(value)
    try:
        validate.text("folder", folder, validate.FOLDER_MAX)
    except validate.ArgumentError:
        raise _bad(value) from None
    mid = MessageId(m["account"], folder, uidvalidity, uid)
    if str(mid) != value:  # non-canonical base64 (stray bits) or leading zeros
        raise _bad(value)
    return mid
