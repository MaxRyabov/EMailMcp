"""Secret redaction for everything the server returns or prints (D15).

Two layers: every secret the process loads (password, tokens, client_secret,
device code, XOAUTH2 string) is registered and replaced verbatim; patterns catch
the shapes a secret takes in protocol text even if it was never registered.
"""

from __future__ import annotations

import base64
import binascii
import re
import threading
from collections.abc import Iterable

MASK = "***"
# Shorter values would mask ordinary words; real secrets are far longer.
_MIN_SECRET_LEN = 6

_lock = threading.Lock()
_secrets: set[str] = set()

_PATTERNS = (
    # XOAUTH2 initial response, in either the raw or the AUTHENTICATE form.
    (re.compile(r"(?i)(auth=Bearer\s+)[^\s\x01\"'\\]+"), r"\1" + MASK),
    (re.compile(r"(?i)(\bBearer\s+)[^\s\x01\"'\\]+"), r"\1" + MASK),
    (re.compile(r"(?i)(AUTHENTICATE\s+XOAUTH2\s+)\S+"), r"\1" + MASK),
    # JSON and form fields that carry secrets.
    (
        re.compile(
            r"(?i)([\"']?\b(?:access_token|refresh_token|client_secret|device_code|password)\b"
            r"[\"']?\s*[:=]\s*[\"']?)[^\"'&\s,}]+"
        ),
        r"\1" + MASK,
    ),
)
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")


def register(*values: str | None) -> None:
    """Remember secrets so that redact() masks them wherever they appear."""
    with _lock:
        for v in values:
            if v and len(v) >= _MIN_SECRET_LEN:
                _secrets.add(v)


def _mask_xoauth2_base64(m: re.Match) -> str:
    run = m.group(0)
    try:
        decoded = base64.b64decode(run + "=" * (-len(run) % 4), validate=True)
    except (binascii.Error, ValueError):
        return run
    return MASK if b"auth=Bearer" in decoded else run


def redact(text: object, extra: Iterable[str | None] = ()) -> str:
    """Text with every known secret and secret-shaped value replaced by ***."""
    out = str(text)
    with _lock:
        known = _secrets | {v for v in extra if v and len(v) >= _MIN_SECRET_LEN}
    # Longest first, so a secret that contains another is masked whole.
    for secret in sorted(known, key=len, reverse=True):
        out = out.replace(secret, MASK)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return _BASE64_RUN.sub(_mask_xoauth2_base64, out)
