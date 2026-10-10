"""auth/store.py on an in-memory keyring and auth/http.py on a scripted opener."""

import json
import urllib.error

import keyring
import pytest
from fakehttp import FakeOpener, reply
from keyring.backends import fail
from keyring.backends.Windows import WinVaultKeyring

from imap_mcp.auth import http, store

TOKENS = store.Tokens(
    access_token="y0_access-token-value",
    refresh_token="1:refresh-token-value",
    expires_at=2_000_000_000.0,
    obtained_at=1_900_000_000.0,
    scope="mail:imap_ro",
)


# --- store ------------------------------------------------------------------------


def test_tokens_roundtrip_one_json_record_and_secret_apart(memory_keyring):
    store.save_tokens("yandex", TOKENS)
    store.save_client_secret("yandex", "client-secret-value")
    assert store.load_tokens("yandex") == TOKENS
    assert store.load_client_secret("yandex") == "client-secret-value"
    assert set(memory_keyring.data) == {
        ("imap-mcp", "yandex"),
        ("imap-mcp", "yandex:client_secret"),
    }
    assert json.loads(memory_keyring.data[("imap-mcp", "yandex")])["scope"] == "mail:imap_ro"


def test_missing_records_and_delete():
    assert store.load_tokens("yandex") is None
    assert store.load_client_secret("yandex") is None
    assert store.delete_tokens("yandex") is False
    store.save_tokens("yandex", TOKENS)
    assert store.delete_tokens("yandex") is True
    assert store.load_tokens("yandex") is None


def test_corrupt_record_is_reported(memory_keyring):
    memory_keyring.data[("imap-mcp", "yandex")] = "{not json"
    with pytest.raises(store.CorruptRecord, match="yandex"):
        store.load_tokens("yandex")


def test_record_over_credential_manager_limit_is_not_written(memory_keyring):
    long = store.Tokens(**{**TOKENS.__dict__, "access_token": "x" * 1300})
    with pytest.raises(store.StoreError) as e:
        store.save_tokens("yandex", long)
    assert "лимит 1280" in str(e.value)
    assert str(len(json.dumps(long.__dict__))) in str(e.value)
    assert memory_keyring.data == {}


def test_unavailable_backend_names_it():
    keyring.set_keyring(fail.Keyring())
    with pytest.raises(store.StoreError, match=r"keyring\.backends\.fail\.Keyring"):
        store.load_tokens("yandex")


class FakeWinVault(WinVaultKeyring):
    """WinVaultKeyring type without win32cred: persist is a plain attribute here."""

    persist = None

    def get_password(self, service, username):
        return None


def test_persist_local_machine_only_on_windows_backend(memory_keyring):
    store.load_tokens("yandex")
    assert not hasattr(memory_keyring, "persist")

    win = FakeWinVault()
    keyring.set_keyring(win)
    store.load_tokens("yandex")
    assert win.persist == "local_machine"


# --- http -------------------------------------------------------------------------

URL = "https://oauth.yandex.ru/token"


@pytest.fixture
def opener(monkeypatch):
    fake = FakeOpener()
    monkeypatch.setattr(http, "opener", fake)
    return fake


def test_post_form_returns_json_with_timeout(opener):
    opener.script[URL] = [reply(200, {"access_token": "t"})]
    assert http.post_form(URL, {"a": "1"}) == {"access_token": "t"}
    assert opener.requests == [(URL, {"a": "1"}, 15)]


def test_4xx_with_error_is_provider_error(opener):
    body = {"error": "invalid_grant", "error_description": "Code has expired"}
    opener.script[URL] = [reply(400, body)]
    with pytest.raises(http.ProviderError) as e:
        http.post_form(URL, {})
    assert (e.value.code, e.value.description) == ("invalid_grant", "Code has expired")


@pytest.mark.parametrize(
    "answer, kind",
    [
        (reply(503, {"error": "unavailable"}), "HTTP 503"),
        (reply(400, b"<html>bad request</html>"), "HTTP 400"),
        (reply(200, b"<html>captive portal</html>"), "ответ не JSON"),
        (TimeoutError("timed out"), "TimeoutError"),
        (urllib.error.URLError(ConnectionRefusedError()), "ConnectionRefusedError"),
        (urllib.error.URLError("getaddrinfo failed"), "getaddrinfo failed"),
    ],
)
def test_network_errors_name_host_and_kind(opener, answer, kind):
    opener.script[URL] = [answer]
    with pytest.raises(http.NetworkError) as e:
        http.post_form(URL, {})
    assert (e.value.host, e.value.kind) == ("oauth.yandex.ru", kind)
    assert "oauth.yandex.ru" in str(e.value)


def test_real_opener_does_not_follow_redirects():
    assert any(isinstance(h, http._NoRedirect) for h in http.opener.handlers)
    assert http._NoRedirect().redirect_request(None, None, 307, "", {}, "https://evil") is None


def test_redirect_answer_is_network_error(opener):
    opener.script[URL] = [reply(307, b"")]
    with pytest.raises(http.NetworkError, match="HTTP 307"):
        http.post_form(URL, {"client_secret": "s"})


def test_trace_is_redacted(opener):
    opener.script[URL] = [reply(200, {"access_token": "y0_secret-access-token"})]
    lines = []
    http.post_form(URL, {"client_secret": "secret-value-123", "code": "1234567"}, lines.append)
    assert lines[0].startswith(f"HTTP > POST {URL}")
    assert lines[1].startswith("HTTP < 200")
    joined = "\n".join(lines)
    assert "secret-value-123" not in joined
    assert "y0_secret-access-token" not in joined
