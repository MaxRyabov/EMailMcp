"""Tool argument checks, done before any network access (D7).

This is the first barrier; the guard (guard.py) is the second and does not rely
on it. Every error names the parameter and the allowed range.

Integer parameters are declared to MCP as `IntArg`: the schema still says
"integer", but the value reaches the tool unvalidated, so `limit="много"` gets
this module's message instead of a pydantic validation error.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from typing import Annotated, Any

from pydantic import WithJsonSchema

from .accounts import KEY_PATTERN

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

IntArg = Annotated[Any, WithJsonSchema({"type": "integer"})]

LIMIT_DEFAULT = 25
LIMIT_MAX = 100
QUERY_MAX = 500
FOLDER_MAX = 500
MAX_CHARS_DEFAULT = 20_000
MAX_CHARS_MAX = 40_000
OFFSET_MAX = 100_000_000
# Folder aliases resolved per account through the special-use flag in LIST.
SPECIAL_USE = ("\\Sent", "\\Junk", "\\Trash", "\\Drafts", "\\Archive", "\\All")


class ArgumentError(ValueError):
    """A tool argument is out of range; the call makes no IMAP request."""

    def __init__(self, param: str, message: str):
        super().__init__(f"{param}: {message}")
        self.param = param


def _shown(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def integer(param: str, value: object, lo: int, hi: int | None = None) -> int:
    allowed = f"от {lo} до {hi}" if hi is not None else f"не меньше {lo}"
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArgumentError(param, f"ожидается целое число {allowed}, получено {_shown(value)}")
    if value < lo or (hi is not None and value > hi):
        raise ArgumentError(param, f"ожидается целое число {allowed}, получено {value}")
    return value


def limit(value: object) -> tuple[int, bool]:
    """(limit, capped): values above LIMIT_MAX are cut down, not rejected."""
    n = integer("limit", value, 1)
    return min(n, LIMIT_MAX), n > LIMIT_MAX


def _control(text: str) -> bool:
    return any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in text)


def text(param: str, value: object, max_len: int) -> str:
    if not isinstance(value, str):
        raise ArgumentError(param, f"ожидается строка, получено {_shown(value)}")
    if not value.strip():
        raise ArgumentError(param, "ожидается непустая строка, а не одни пробелы")
    if len(value) > max_len:
        raise ArgumentError(param, f"ожидается строка длиной от 1 до {max_len} символов")
    if _control(value):
        raise ArgumentError(param, "недопустимые управляющие символы (CR, LF, TAB и т. п.)")
    return value


def query(value: object) -> str:
    return text("query", value, QUERY_MAX)


def folder(value: object) -> str:
    """A folder name, or a special-use alias normalised to its canonical spelling."""
    name = text("folder", value, FOLDER_MAX)
    if name.startswith("\\"):
        for alias in SPECIAL_USE:
            if name.lower() == alias.lower():
                return alias
        raise ArgumentError(
            "folder", f"неизвестный псевдоним {name}; допустимы: {' '.join(SPECIAL_USE)}"
        )
    return name


def since(value: object) -> dt.date | None:
    if value is None or value == "":
        return None
    # fromisoformat alone also takes ISO weeks (2025-W15-3) and other forms.
    if isinstance(value, str) and _DATE.fullmatch(value):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            pass
    raise ArgumentError("since", f"ожидается дата YYYY-MM-DD, получено {_shown(value)}")


def account(value: object) -> str | None:
    """Format check only; whether the account exists is decided per call (4.12)."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not KEY_PATTERN.fullmatch(value):
        raise ArgumentError(
            "account", "ожидается ключ аккаунта из list_accounts ([a-z0-9_-], до 32)"
        )
    return value
