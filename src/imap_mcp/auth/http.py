"""Form POST to an OAuth provider on urllib.request, with error classification (D3).

Standard library on purpose: system certificates and proxies from the environment
and the registry, no extra dependency. Tests replace `opener`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from ..redact import redact

TIMEOUT = 15


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: a 307/308 would resend client_secret or a token to
    another host, a 301-303 would turn the POST into a GET (RFC 9700)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(_NoRedirect)


class NetworkError(Exception):
    """No usable answer: timeout, connection failure, 5xx, a body that is not JSON."""

    def __init__(self, host: str, kind: str):
        super().__init__(f"сетевая ошибка при обращении к {host}: {kind}")
        self.host = host
        self.kind = kind


class ProviderError(Exception):
    """A 4xx answer carrying an OAuth `error` code."""

    def __init__(self, code: str, description: str | None):
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description


def _json(body: bytes) -> dict | None:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def post_form(url: str, fields: dict[str, str], trace: Callable[[str], None] | None = None) -> dict:
    """POST url-encoded `fields` and return the JSON object of a 2xx answer."""
    host = urllib.parse.urlsplit(url).hostname or url
    body = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    if trace:
        trace(redact(f"HTTP > POST {url} {body.decode('ascii')}"))
    try:
        with opener.open(request, timeout=TIMEOUT) as resp:
            status, payload = resp.status, resp.read()
    # HTTPError is a subclass of URLError and carries the provider's answer.
    except urllib.error.HTTPError as e:
        try:
            status, payload = e.code, e.read()
        except (TimeoutError, OSError) as read_error:
            raise NetworkError(host, type(read_error).__name__) from None
    except urllib.error.URLError as e:
        reason = e.reason
        kind = type(reason).__name__ if isinstance(reason, BaseException) else str(reason)
        raise NetworkError(host, kind) from None
    except (TimeoutError, OSError) as e:
        raise NetworkError(host, type(e).__name__) from None
    if trace:
        trace(redact(f"HTTP < {status} {payload.decode('utf-8', 'replace')}"))

    data = _json(payload)
    if 400 <= status < 500 and data is not None and isinstance(data.get("error"), str):
        raise ProviderError(data["error"], data.get("error_description"))
    if status >= 300:
        raise NetworkError(host, f"HTTP {status}")
    if data is None:
        raise NetworkError(host, "ответ не JSON")
    return data
