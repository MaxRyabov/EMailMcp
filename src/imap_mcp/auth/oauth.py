"""OAuth login: device flow for `imap-mcp auth`, XOAUTH2 for IMAP, `forget`.

Tokens are never refreshed automatically (D5): when the access token expires the
account becomes `reauth-required` and the user runs `imap-mcp auth` again.
"""

from __future__ import annotations

import base64
import datetime as dt
import getpass
import hashlib
import imaplib
import platform
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from imap_tools import BaseMailBox
from imap_tools.errors import MailboxLoginError

from ..guard import ReadOnlyMailBox
from ..redact import register
from . import http, store
from .base import CredentialUnavailable, OfflineStatus, fingerprint
from .profiles import PROVIDERS, ProviderProfile

if TYPE_CHECKING:
    from ..accounts import Account

Trace = Callable[[str], None] | None


class AuthFailed(Exception):
    """`imap-mcp auth` cannot finish; the message is for the user, secrets removed."""


def _date(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d")


def profile_for(account: Account) -> ProviderProfile:
    return PROVIDERS[account.oauth_provider or ""]


# --- XOAUTH2 ------------------------------------------------------------------


def xoauth2_string(email: str, token: str) -> str:
    raw = f"user={email}\1auth=Bearer {token}\1\1"
    register(token, base64.b64encode(raw.encode()).decode("ascii"))
    return raw


def xoauth2_login(box: BaseMailBox, email: str, token: str, inline: bool = False) -> None:
    """AUTHENTICATE XOAUTH2 without selecting a folder.

    Default is the two-step form imaplib speaks (command, `+`, then the string).
    `inline` sends the string with the command in one line (SASL-IR) and then
    marks the session authenticated, the way imap-tools' login() does (D17).
    """
    raw = xoauth2_string(email, token)
    if not inline:
        box.xoauth2(email, token, initial_folder=None)
        return
    client = box.client
    typ, data = client._simple_command(
        "AUTHENTICATE", "XOAUTH2", base64.b64encode(raw.encode()).decode("ascii")
    )
    if typ != "OK":
        raise client.error(f"AUTHENTICATE XOAUTH2 rejected: {typ}")
    client.state = "AUTH"
    box.login_result = (typ, data)


def _rejected(e: Exception) -> bool:
    """The server said NO to the login (as opposed to a dropped connection)."""
    return isinstance(e, MailboxLoginError) or (
        isinstance(e, imaplib.IMAP4.error) and not isinstance(e, imaplib.IMAP4.abort)
    )


# --- credential used by the server ---------------------------------------------


class OAuthCredential:
    def __init__(self, account: Account, now: Callable[[], float] | None = None):
        self.account = account
        self.profile = profile_for(account)
        self._now = now or time.time

    def _hint(self) -> str:
        return f"imap-mcp auth {self.account.key}"

    def _status(self, tokens: store.Tokens | None) -> OfflineStatus:
        if tokens is None:
            return OfflineStatus("no-credential", hint=f"выполните `{self._hint()}`")
        expires = dt.datetime.fromtimestamp(tokens.expires_at, dt.UTC)
        left = tokens.expires_at - self._now()
        if left <= 0:
            return OfflineStatus(
                "reauth-required",
                hint=f"токен истёк {_date(tokens.expires_at)}, выполните `{self._hint()}`",
                expires_at=expires,
            )
        warning = None
        if left < self.profile.expiry_warning.total_seconds():
            warning = (
                f"токен аккаунта {self.account.key} истекает {_date(tokens.expires_at)}, "
                f"обновите его командой `{self._hint()}`"
            )
        return OfflineStatus("ok", warning=warning, expires_at=expires)

    def offline_status(self) -> OfflineStatus:
        try:
            return self._status(store.load_tokens(self.account.key))
        except store.StoreError as e:
            return OfflineStatus("no-credential", hint=str(e))

    def login(self, box: BaseMailBox) -> None:
        # Read on every login: a token from a fresh `imap-mcp auth` applies without a restart.
        try:
            tokens = store.load_tokens(self.account.key)
        except store.StoreError as e:
            raise CredentialUnavailable("no-credential", f"{self.account.key}: {e}") from None
        status = self._status(tokens)
        if tokens is None or status.status != "ok":
            raise CredentialUnavailable(status.status, f"{self.account.key}: {status.hint}")
        try:
            xoauth2_login(
                box, self.account.email, tokens.access_token, inline=self.profile.xoauth2_inline
            )
        except Exception as e:
            if not _rejected(e):
                raise
            reasons = "; ".join(
                f"{i}) {r}" for i, r in enumerate(self.profile.valid_token_rejection_reasons, 1)
            )
            raise CredentialUnavailable(
                "reauth-required",
                f"{self.account.key}: IMAP отклонил действующий токен. Возможные причины: "
                f"{reasons}. Затем выполните `{self._hint()}`",
            ) from None

    def fingerprint(self) -> str:
        tokens = store.load_tokens(self.account.key)
        return fingerprint(self.account, tokens.obtained_at if tokens else None)

    def secrets(self) -> list[str]:
        tokens = store.load_tokens(self.account.key)
        secret = store.load_client_secret(self.account.key)
        values = [tokens.access_token, tokens.refresh_token] if tokens else []
        return [v for v in (*values, secret) if v]


# --- device flow ---------------------------------------------------------------


def device_id(account_key: str, machine: str | None = None) -> str:
    """Stable per machine and account: a repeated `auth` replaces the device token."""
    machine = machine if machine is not None else platform.node()
    return hashlib.sha256(f"{machine}{account_key}".encode()).hexdigest()[:32]


def device_name(account_key: str, machine: str | None = None) -> str:
    machine = machine if machine is not None else platform.node()
    return f"imap-mcp {account_key} on {machine}"[:100]


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_url: str
    interval: int
    expires_in: int


def _fatal(profile: ProviderProfile, e: http.ProviderError) -> AuthFailed:
    text = profile.fatal_errors.get(e.code)
    if text is None:
        text = f"неизвестный ответ провайдера: {e.code}"
        if e.description:
            text += f" ({e.description})"
    return AuthFailed(text)


def request_device_code(
    profile: ProviderProfile, account: Account, trace: Trace = None
) -> DeviceCode:
    fields = {
        "client_id": account.client_id or "",
        "device_id": device_id(account.key),
        "device_name": device_name(account.key),
        "scope": profile.scope,
    }
    try:
        resp = http.post_form(profile.device_code_url, fields, trace)
        dc = DeviceCode(
            device_code=str(resp["device_code"]),
            user_code=str(resp["user_code"]),
            verification_url=str(resp[profile.verification_url_field]),
            interval=int(resp.get("interval", 5)),
            expires_in=int(resp["expires_in"]),
        )
    except http.ProviderError as e:
        raise _fatal(profile, e) from None
    except http.NetworkError as e:
        raise AuthFailed(str(e)) from None
    except (KeyError, TypeError, ValueError):
        raise AuthFailed("провайдер вернул неполный ответ на запрос кода устройства") from None
    register(dc.device_code)
    return dc


def poll_token(
    profile: ProviderProfile,
    account: Account,
    client_secret: str | None,
    dc: DeviceCode,
    *,
    trace: Trace = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict:
    """Poll the token URL no more often than `interval` until a token or a fatal answer."""
    fields = {
        "grant_type": profile.device_grant_type,
        profile.device_code_param: dc.device_code,
        "client_id": account.client_id or "",
    }
    if client_secret:
        fields["client_secret"] = client_secret
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    interval = max(dc.interval, 1)
    deadline = clock() + dc.expires_in
    while True:
        sleep(interval)
        if clock() >= deadline:
            raise AuthFailed("код истёк, запустите `imap-mcp auth` заново")
        try:
            return http.post_form(profile.token_url, fields, trace)
        except http.ProviderError as e:
            if e.code not in profile.pending_errors:
                raise _fatal(profile, e) from None
            interval += profile.pending_errors[e.code]
        except http.NetworkError as e:
            raise AuthFailed(str(e)) from None


def tokens_from_response(profile: ProviderProfile, resp: dict, now: float) -> store.Tokens:
    if "scope" in resp:
        granted = str(resp["scope"])
        if set(granted.split()) != set(profile.scope.split()):
            raise AuthFailed(
                f"провайдер выдал права «{granted}» вместо «{profile.scope}», токен не сохранён"
            )
    try:
        tokens = store.Tokens(
            access_token=str(resp["access_token"]),
            refresh_token=str(resp["refresh_token"]) if resp.get("refresh_token") else None,
            expires_at=now + int(resp["expires_in"]),
            obtained_at=now,
            scope=profile.scope,
        )
    except (KeyError, TypeError, ValueError):
        raise AuthFailed("ответ провайдера без access_token или expires_in") from None
    register(tokens.access_token, tokens.refresh_token)
    return tokens


def trial_login(account: Account, profile: ProviderProfile, token: str, trace: Trace) -> None:
    """Log in with the new token and log out; raises AuthFailed with likely causes."""
    try:
        box = ReadOnlyMailBox(account.host, port=account.port, tracer=trace)
    except (OSError, imaplib.IMAP4.error) as e:
        raise AuthFailed(
            f"IMAP {account.host}:{account.port} недоступен: {type(e).__name__}"
        ) from None
    try:
        xoauth2_login(box, account.email, token, inline=profile.xoauth2_inline)
    except Exception as e:
        if not _rejected(e):
            raise AuthFailed(
                f"вход в IMAP {account.host}:{account.port} не выполнен: {type(e).__name__}"
            ) from None
        reasons = "\n".join(f"  - {r}" for r in profile.new_token_rejection_reasons)
        raise AuthFailed(
            f"токен получен, но IMAP отклонил вход, токен не сохранён. Вероятные причины:\n"
            f"{reasons}"
        ) from None
    finally:
        with suppress(Exception):
            box.logout()


def authorize(
    account: Account,
    out: Callable[[str], None],
    *,
    trace: Trace = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    now: Callable[[], float] | None = None,
    ask_secret: Callable[[str], str] = getpass.getpass,
) -> store.Tokens:
    """Run the device flow and store the tokens only after a successful IMAP login."""
    profile = profile_for(account)
    try:
        client_secret = store.load_client_secret(account.key)
    except store.StoreError as e:
        raise AuthFailed(str(e)) from None
    new_secret = None
    if client_secret is None and profile.client_secret_required:
        new_secret = client_secret = ask_secret("client_secret приложения: ").strip() or None
        register(new_secret)

    dc = request_device_code(profile, account, trace)
    out(f"Откройте {dc.verification_url} и введите код {dc.user_code}")
    out(f"Код действует {dc.expires_in // 60} мин. Жду подтверждения…")
    resp = poll_token(profile, account, client_secret, dc, trace=trace, sleep=sleep, clock=clock)
    tokens = tokens_from_response(profile, resp, (now or time.time)())
    trial_login(account, profile, tokens.access_token, trace)

    try:
        # The secret first: if the token then fails to save, "not saved" stays true.
        if new_secret:
            store.save_client_secret(account.key, new_secret)
        store.save_tokens(account.key, tokens)
    except store.StoreError as e:
        raise AuthFailed(f"{e}; токен не сохранён") from None
    out("авторизация выполнена")
    out(f"токен действует до {_date(tokens.expires_at)}")
    out(f"вход в IMAP {account.host}:{account.port}: ok")
    return tokens


def forget(account: Account, out: Callable[[str], None], trace: Trace = None) -> None:
    """Revoke the token if possible, then delete the account's records from the keyring."""
    profile = profile_for(account)
    try:
        try:
            tokens = store.load_tokens(account.key)
        except store.CorruptRecord:
            tokens = None  # nothing to revoke, the record is still deleted below
        client_secret = store.load_client_secret(account.key)
    except store.StoreError as e:
        raise AuthFailed(str(e)) from None

    manual = (
        "токен на стороне провайдера остаётся активным, пока вы не отзовёте доступ вручную: "
        f"{profile.access_management_url}"
    )
    if tokens is not None and client_secret is None:
        out(f"отзыв невозможен без client_secret; {manual}")
    elif tokens is not None:
        try:
            http.post_form(
                profile.revoke_url,
                {
                    "access_token": tokens.access_token,
                    "client_id": account.client_id or "",
                    "client_secret": client_secret,
                },
                trace,
            )
            out("токен отозван у провайдера")
        except (http.ProviderError, http.NetworkError) as e:
            out(f"отзыв не удался ({e}); {manual}")

    try:
        deleted = store.delete_tokens(account.key) | store.delete_client_secret(account.key)
    except store.StoreError as e:
        raise AuthFailed(str(e)) from None
    if deleted:
        out(f"записи аккаунта {account.key} удалены из системного хранилища")
    else:
        out("нечего удалять")
