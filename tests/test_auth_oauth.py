"""OAuth: Yandex profile, device flow, XOAUTH2 login, token lifetime, forget.

HTTP goes to a scripted opener, IMAP to the fake server behind the real guard.
Provider answers follow the field lists of the Yandex OAuth documentation
(device code: yandex.ru/dev/id/doc/ru/codes/screen-code-oauth, revoke:
yandex.ru/dev/id/doc/ru/tokens/token-invalidate).
"""

import base64
import dataclasses
import datetime as dt
import re
import time
from types import SimpleNamespace

import keyring
import pytest
from fakehttp import FakeOpener, reply
from fakeimap import FakeSocket, verbs
from keyring.backends import fail

from imap_mcp import accounts, imap
from imap_mcp.auth import CredentialUnavailable, http, oauth, store
from imap_mcp.auth.profiles import PROVIDERS, YANDEX
from imap_mcp.guard import ReadOnlyMailBox

DEVICE_CODE_RESPONSE = {
    "device_code": "4c57a2e0b5e74d2f9d3b8f1a2c3d4e5f",
    "user_code": "1234567",
    "verification_url": "https://oauth.yandex.ru/device",
    "interval": 5,
    "expires_in": 300,
}
ACCESS_TOKEN = "y0_AgAAAAAAeXFOAAG8XgAAAADqP5mJvTq7Zx9yHkLm3pQrStUvW"
TOKEN_RESPONSE = {
    "token_type": "bearer",
    "access_token": ACCESS_TOKEN,
    "expires_in": 31536000,
    "refresh_token": "1:qY8qQ0kXbz3s7N7W:refresh-token-body:2Rk7DnB8C4kH2Gq9ZLpQ1A",
}
PENDING = reply(
    400,
    {
        "error": "authorization_pending",
        "error_description": "User has not yet authorized your application",
    },
)
NOW = 1_800_000_000.0

CONFIG = """
[[account]]
key = "yandex"
email = "me@yandex.ru"
auth = "oauth"
oauth_provider = "yandex"
client_id = "client-id-1"
"""


@pytest.fixture(autouse=True)
def frozen_time(monkeypatch):
    """Stored token lifetimes are relative to NOW; the real clock must not decide."""
    monkeypatch.setattr(
        oauth, "time", SimpleNamespace(time=lambda: NOW, sleep=time.sleep, monotonic=time.monotonic)
    )


@pytest.fixture
def account(tmp_path, monkeypatch):
    path = tmp_path / "accounts.toml"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(path))
    accounts.clear_cache()
    yield accounts.get_account("yandex")
    accounts.clear_cache()


@pytest.fixture
def opener(monkeypatch):
    fake = FakeOpener()
    monkeypatch.setattr(http, "opener", fake)
    return fake


class Clock:
    """Fake time: sleep() advances monotonic time and records the interval."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def __call__(self):
        return self.t


def authorize(account, opener, *token_answers, **kwargs):
    opener.script[YANDEX.device_code_url] = [reply(200, DEVICE_CODE_RESPONSE)]
    opener.script[YANDEX.token_url] = list(token_answers)
    clock = Clock()
    out = []
    kwargs.setdefault("trace", None)
    tokens = oauth.authorize(
        account, out.append, sleep=clock.sleep, clock=clock, now=lambda: NOW, **kwargs
    )
    return tokens, out, clock


def store_tokens(expires_in_days, obtained_at=NOW):
    store.save_tokens(
        "yandex",
        store.Tokens(
            access_token=ACCESS_TOKEN,
            refresh_token=None,
            expires_at=NOW + expires_in_days * 86400,
            obtained_at=obtained_at,
            scope="mail:imap_ro",
        ),
    )


# --- profile ----------------------------------------------------------------------


def test_yandex_profile_matches_documentation():
    assert PROVIDERS == {"yandex": YANDEX}
    assert YANDEX.device_code_url == "https://oauth.yandex.ru/device/code"
    assert YANDEX.token_url == "https://oauth.yandex.ru/token"
    assert YANDEX.revoke_url == "https://oauth.yandex.ru/revoke_token"
    assert YANDEX.scope == "mail:imap_ro"
    assert (YANDEX.imap_host, YANDEX.imap_port) == ("imap.yandex.com", 993)
    assert (YANDEX.device_grant_type, YANDEX.device_code_param) == ("device_code", "code")
    assert YANDEX.verification_url_field == "verification_url"
    assert dict(YANDEX.pending_errors) == {"authorization_pending": 0, "slow_down": 5}
    assert set(YANDEX.fatal_errors) == {
        "invalid_grant",
        "invalid_client",
        "invalid_request",
        "invalid_scope",
        "unauthorized_client",
        "unsupported_grant_type",
        "bad_verification_code",
        "access_denied",
        "expired_token",
    }
    assert YANDEX.client_secret_required is False
    assert YANDEX.expiry_warning == dt.timedelta(days=30)


def test_device_id_is_stable_printable_and_per_account():
    a = oauth.device_id("yandex", machine="PC-1")
    assert a == oauth.device_id("yandex", machine="PC-1")
    assert a != oauth.device_id("work", machine="PC-1")
    assert re.fullmatch(r"[0-9a-f]{32}", a)  # 6-50 printable ASCII
    assert oauth.device_name("yandex", machine="PC-1") == "imap-mcp yandex on PC-1"


# --- device flow ------------------------------------------------------------------


def test_successful_authorization(account, opener, sockets):
    tokens, out, clock = authorize(account, opener, PENDING, reply(200, TOKEN_RESPONSE))

    code_req = opener.fields(YANDEX.device_code_url)[0]
    assert code_req["client_id"] == "client-id-1"
    assert code_req["scope"] == "mail:imap_ro"
    assert code_req["device_id"] == oauth.device_id("yandex")
    assert code_req["device_name"] == oauth.device_name("yandex")
    # Yandex names, not RFC 8628 ones
    assert opener.fields(YANDEX.token_url)[0] == {
        "grant_type": "device_code",
        "code": DEVICE_CODE_RESPONSE["device_code"],
        "client_id": "client-id-1",
    }
    assert clock.sleeps == [5, 5]

    assert "https://oauth.yandex.ru/device" in out[0] and "1234567" in out[0]
    assert "авторизация выполнена" in out
    expires = dt.datetime.fromtimestamp(NOW + 31536000, dt.UTC)
    assert f"токен действует до {expires:%Y-%m-%d}" in out
    assert "вход в IMAP imap.yandex.com:993: ok" in out

    assert store.load_tokens("yandex") == tokens
    assert tokens.expires_at == NOW + 31536000
    assert tokens.refresh_token == TOKEN_RESPONSE["refresh_token"]  # kept, not used

    sock = sockets[-1]
    assert verbs(sock) == [b"AUTHENTICATE", b"LOGOUT"]
    sasl = base64.b64decode(sock.auth_responses[0])
    assert sasl == f"user=me@yandex.ru\1auth=Bearer {ACCESS_TOKEN}\1\1".encode()


def test_imap_rejects_new_token_nothing_saved(account, opener, sockets, monkeypatch):
    monkeypatch.setattr(FakeSocket, "reject_auth", True)
    with pytest.raises(oauth.AuthFailed) as e:
        authorize(account, opener, reply(200, TOKEN_RESPONSE))
    text = str(e.value)
    assert "IMAP с OAuth-токенами" in text and "Почтовые программы" in text
    assert "Яндекс 360" in text
    assert store.load_tokens("yandex") is None


def test_code_expired_before_user_entered_it(account, opener, sockets):
    with pytest.raises(oauth.AuthFailed, match="код истёк"):
        authorize(account, opener, PENDING)
    assert store.load_tokens("yandex") is None
    assert sockets == []


def test_slow_down_adds_five_seconds(account, opener, sockets):
    slow = reply(400, {"error": "slow_down"})
    _, _, clock = authorize(account, opener, slow, PENDING, reply(200, TOKEN_RESPONSE))
    assert clock.sleeps == [5, 10, 10]


@pytest.mark.parametrize(
    "code, text",
    [
        ("invalid_client", "client_id или client_secret"),
        ("unauthorized_client", "на модерации или заблокировано"),
        ("invalid_scope", "только «Доступ на чтение писем"),
        ("invalid_grant", "код неверен или просрочен"),
        ("bad_verification_code", "код неверен или просрочен"),
        ("expired_token", "код неверен или просрочен"),
        ("access_denied", "доступ не предоставлен, токен не получен"),
    ],
)
def test_fatal_provider_errors(account, opener, sockets, code, text):
    with pytest.raises(oauth.AuthFailed, match=text):
        authorize(account, opener, reply(400, {"error": code, "error_description": "d"}))
    assert store.load_tokens("yandex") is None
    assert sockets == []


def test_unknown_error_code_is_fatal(account, opener, sockets):
    weird = reply(400, {"error": "weird_new_code", "error_description": "Something new"})
    with pytest.raises(oauth.AuthFailed) as e:
        authorize(account, opener, weird)
    assert str(e.value) == "неизвестный ответ провайдера: weird_new_code (Something new)"


def test_narrower_scope_is_not_saved(account, opener, sockets):
    granted = dict(TOKEN_RESPONSE, scope="login:email")
    with pytest.raises(oauth.AuthFailed, match="login:email"):
        authorize(account, opener, reply(200, granted))
    assert store.load_tokens("yandex") is None
    assert sockets == []


def test_requested_scope_in_answer_is_accepted(account, opener, sockets):
    authorize(account, opener, reply(200, dict(TOKEN_RESPONSE, scope="mail:imap_ro")))
    assert store.load_tokens("yandex") is not None


def test_network_failure_names_host(account, opener):
    opener.script[YANDEX.device_code_url] = [TimeoutError()]
    with pytest.raises(oauth.AuthFailed, match="oauth.yandex.ru: TimeoutError"):
        oauth.authorize(account, print)
    assert store.load_tokens("yandex") is None


def test_reauth_replaces_tokens_and_failure_keeps_old(account, opener, sockets, monkeypatch):
    store_tokens(10, obtained_at=1.0)
    old = store.load_tokens("yandex")
    monkeypatch.setattr(FakeSocket, "reject_auth", True)
    with pytest.raises(oauth.AuthFailed):
        authorize(account, opener, reply(200, dict(TOKEN_RESPONSE, access_token="y0_newer")))
    assert store.load_tokens("yandex") == old

    monkeypatch.setattr(FakeSocket, "reject_auth", False)
    new, _, _ = authorize(account, opener, reply(200, TOKEN_RESPONSE))
    assert store.load_tokens("yandex") == new != old


def test_unavailable_keyring_fails_before_network(account, opener):
    keyring.set_keyring(fail.Keyring())
    with pytest.raises(oauth.AuthFailed, match=r"keyring\.backends\.fail\.Keyring"):
        oauth.authorize(account, print)
    assert opener.requests == []


def test_required_client_secret_is_asked_once_and_stored(account, opener, sockets, monkeypatch):
    profile = dataclasses.replace(YANDEX, client_secret_required=True)
    monkeypatch.setitem(PROVIDERS, "yandex", profile)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return "client-secret-value"

    authorize(account, opener, reply(200, TOKEN_RESPONSE), ask_secret=ask)
    assert len(asked) == 1
    assert opener.fields(YANDEX.token_url)[0]["client_secret"] == "client-secret-value"
    assert store.load_client_secret("yandex") == "client-secret-value"

    authorize(account, opener, reply(200, TOKEN_RESPONSE), ask_secret=ask)
    assert len(asked) == 1  # the stored secret is reused


def test_trace_shows_http_and_imap_without_secrets(account, opener, sockets):
    lines = []
    authorize(account, opener, PENDING, reply(200, TOKEN_RESPONSE), trace=lines.append)
    joined = "\n".join(lines)
    assert "HTTP > POST https://oauth.yandex.ru/device/code" in joined
    assert "IMAP > " in joined and "AUTHENTICATE XOAUTH2" in joined
    xoauth2 = base64.b64encode(f"user=me@yandex.ru\1auth=Bearer {ACCESS_TOKEN}\1\1".encode())
    for secret in (
        ACCESS_TOKEN,
        TOKEN_RESPONSE["refresh_token"],
        DEVICE_CODE_RESPONSE["device_code"],
        xoauth2.decode(),
    ):
        assert secret not in joined


# --- XOAUTH2 in one line (3.9) ------------------------------------------------------


def test_inline_xoauth2_then_examine(sockets):
    box = ReadOnlyMailBox("imap.yandex.com")
    oauth.xoauth2_login(box, "me@yandex.ru", ACCESS_TOKEN, inline=True)
    assert box.client.state == "AUTH"
    box.folder.set("INBOX", readonly=True)
    assert box.client.state == "SELECTED"

    sock = sockets[-1]
    assert verbs(sock) == [b"AUTHENTICATE", b"EXAMINE"]
    assert sock.auth_responses == []  # no continuation round trip
    _, verb, mech, b64 = sock.commands[-2].split(b" ")
    assert (verb, mech) == (b"AUTHENTICATE", b"XOAUTH2")
    assert ACCESS_TOKEN.encode() in base64.b64decode(b64)


def test_inline_xoauth2_rejected_raises_imap_error(sockets, monkeypatch):
    monkeypatch.setattr(FakeSocket, "reject_auth", True)
    box = ReadOnlyMailBox("imap.yandex.com")
    with pytest.raises(box.client.error):
        oauth.xoauth2_login(box, "me@yandex.ru", ACCESS_TOKEN, inline=True)
    assert box.client.state == "NONAUTH"


# --- token lifetime (D5) ---------------------------------------------------------


def test_not_authorized_yet_is_no_credential(account, opener):
    st = oauth.OAuthCredential(account, now=lambda: NOW).offline_status()
    assert st.status == "no-credential"
    assert "imap-mcp auth yandex" in st.hint
    assert opener.requests == []


def test_token_expiring_in_20_days_warns_but_works(account, sockets):
    store_tokens(20)
    st = oauth.OAuthCredential(account, now=lambda: NOW).offline_status()
    assert st.status == "ok"
    assert st.expires_at == dt.datetime.fromtimestamp(NOW + 20 * 86400, dt.UTC)
    assert f"{st.expires_at:%Y-%m-%d}" in st.warning
    assert "imap-mcp auth yandex" in st.warning
    with imap.open_box(account):
        pass
    assert verbs(sockets[-1])[:2] == [b"AUTHENTICATE", b"EXAMINE"]


def test_token_far_from_expiry_has_no_warning(account):
    store_tokens(31)
    assert oauth.OAuthCredential(account, now=lambda: NOW).offline_status().warning is None


def test_expired_token_is_reauth_required_without_imap(account, sockets, monkeypatch):
    store_tokens(-1)
    monkeypatch.setattr(oauth.time, "time", lambda: NOW)
    st = oauth.OAuthCredential(account).offline_status()
    assert st.status == "reauth-required"
    assert "imap-mcp auth yandex" in st.hint
    with pytest.raises(CredentialUnavailable) as e, imap.open_box(account):
        pass
    assert e.value.status == "reauth-required"
    assert sockets == []


def test_rejected_valid_token_lists_both_reasons(account, sockets, monkeypatch):
    store_tokens(100)
    monkeypatch.setattr(FakeSocket, "reject_auth", True)
    with pytest.raises(CredentialUnavailable) as e, imap.open_box(account):
        pass
    assert e.value.status == "reauth-required"
    assert "отозван в Яндекс ID" in str(e.value)
    assert "IMAP с OAuth-токенами" in str(e.value)


def test_new_token_applies_without_restart(account, sockets):
    store_tokens(100)
    with imap.open_box(account):
        pass
    store.save_tokens(
        "yandex", dataclasses.replace(store.load_tokens("yandex"), access_token="y0_fresh_token")
    )
    with imap.open_box(account):
        pass
    assert b"y0_fresh_token" in base64.b64decode(sockets[-1].auth_responses[0])


def test_fingerprint_changes_with_new_tokens(account):
    cred = oauth.OAuthCredential(account)
    store_tokens(100, obtained_at=1.0)
    first = cred.fingerprint()
    store_tokens(100, obtained_at=2.0)
    assert cred.fingerprint() != first
    assert ACCESS_TOKEN in cred.secrets()


# --- forget -----------------------------------------------------------------------


def test_forget_with_client_secret_revokes_and_deletes(account, opener, memory_keyring):
    store_tokens(100)
    store.save_client_secret("yandex", "client-secret-value")
    opener.script[YANDEX.revoke_url] = [reply(200, {"status": "ok"})]
    out = []
    oauth.forget(account, out.append)
    assert opener.fields(YANDEX.revoke_url) == [
        {
            "access_token": ACCESS_TOKEN,
            "client_id": "client-id-1",
            "client_secret": "client-secret-value",
        }
    ]
    assert "токен отозван у провайдера" in out
    assert memory_keyring.data == {}
    status = oauth.OAuthCredential(account).offline_status().status
    assert status == "no-credential"


def test_forget_without_client_secret_explains_manual_revoke(account, opener, memory_keyring):
    store_tokens(100)
    out = []
    oauth.forget(account, out.append)
    assert opener.requests == []
    assert "https://id.yandex.ru/personal/data-access" in out[0]
    assert "остаётся активным" in out[0]
    assert memory_keyring.data == {}


def test_forget_failed_revoke_still_deletes(account, opener, memory_keyring):
    store_tokens(100)
    store.save_client_secret("yandex", "client-secret-value")
    opener.script[YANDEX.revoke_url] = [reply(400, {"error": "unsupported_token_type"})]
    out = []
    oauth.forget(account, out.append)
    assert "отзыв не удался" in out[0] and "unsupported_token_type" in out[0]
    assert memory_keyring.data == {}


def test_forget_twice_has_nothing_to_delete(account, opener):
    store_tokens(100)
    oauth.forget(account, lambda line: None)
    out = []
    oauth.forget(account, out.append)
    assert out == ["нечего удалять"]
