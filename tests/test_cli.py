"""imap-mcp command line: serve, auth, status, forget."""

import socket

import pytest
from fakehttp import FakeOpener, reply
from test_auth_oauth import ACCESS_TOKEN, DEVICE_CODE_RESPONSE, PENDING, TOKEN_RESPONSE

from imap_mcp import accounts, cli, server
from imap_mcp.auth import http, oauth, store
from imap_mcp.auth.profiles import YANDEX

CONFIG = """
[[account]]
key = "yandex"
email = "me@yandex.ru"
auth = "oauth"
oauth_provider = "yandex"
client_id = "client-id-1"

[[account]]
key = "work"
email = "me@example.com"
host = "imap.example.com"
password_env = "WORK_PW"

[[account]]
key = "off"
email = "off@example.com"
host = "imap.example.com"
password_env = "OFF_PW"
enabled = false
"""


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    path = tmp_path / "accounts.toml"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(path))
    accounts.clear_cache()
    yield path
    accounts.clear_cache()


@pytest.fixture
def opener(monkeypatch):
    fake = FakeOpener()
    monkeypatch.setattr(http, "opener", fake)
    return fake


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(oauth.time, "sleep", lambda s: None)


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(http, "opener", FakeOpener())


@pytest.mark.parametrize("argv", [[], ["serve"]])
def test_without_arguments_runs_server(monkeypatch, argv):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("run"))
    assert cli.main(argv) == 0
    assert calls == ["run"]


def test_auth_for_password_account_makes_no_requests(no_network, capsys):
    assert cli.main(["auth", "work"]) == 1
    assert "WORK_PW" in capsys.readouterr().err


def test_auth_unknown_account(no_network, capsys):
    assert cli.main(["auth", "nope"]) == 1
    assert "unknown account 'nope'" in capsys.readouterr().err


def test_auth_with_trace_prints_no_secrets(opener, sockets, no_sleep, capsys):
    opener.script[YANDEX.device_code_url] = [reply(200, DEVICE_CODE_RESPONSE)]
    opener.script[YANDEX.token_url] = [PENDING, reply(200, TOKEN_RESPONSE)]
    assert cli.main(["auth", "yandex", "--trace"]) == 0
    captured = capsys.readouterr()
    assert "авторизация выполнена" in captured.out
    assert "HTTP > POST" in captured.err and "IMAP > " in captured.err
    for secret in (ACCESS_TOKEN, TOKEN_RESPONSE["refresh_token"], "auth=Bearer y0"):
        assert secret not in captured.out + captured.err
    assert store.load_tokens("yandex").access_token == ACCESS_TOKEN


def test_auth_failure_exit_code(opener, no_sleep, capsys):
    opener.script[YANDEX.device_code_url] = [reply(200, DEVICE_CODE_RESPONSE)]
    opener.script[YANDEX.token_url] = [reply(400, {"error": "access_denied"})]
    assert cli.main(["auth", "yandex"]) == 1
    assert "доступ не предоставлен, токен не получен" in capsys.readouterr().err


def test_ctrl_c_while_polling_exits_130_and_saves_nothing(opener, no_sleep, memory_keyring):
    opener.script[YANDEX.device_code_url] = [reply(200, DEVICE_CODE_RESPONSE)]
    opener.script[YANDEX.token_url] = [KeyboardInterrupt()]
    assert cli.main(["auth", "yandex"]) == 130
    assert memory_keyring.data == {}


def test_status_offline_opens_no_sockets(no_network, monkeypatch, capsys):
    monkeypatch.setenv("WORK_PW", "pw")
    assert cli.main(["status", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "yandex: no-credential — выполните `imap-mcp auth yandex`" in out
    assert "work: not checked" in out
    assert "off: disabled" in out


def test_status_live_logs_in(sockets, monkeypatch, capsys):
    monkeypatch.setenv("WORK_PW", "pw")
    assert cli.main(["status"]) == 0
    assert "work: ok" in capsys.readouterr().out
    assert len(sockets) == 1


def test_forget_twice(opener, capsys):
    store.save_tokens(
        "yandex",
        store.Tokens(ACCESS_TOKEN, None, expires_at=4e9, obtained_at=1.0, scope="mail:imap_ro"),
    )
    assert cli.main(["forget", "yandex"]) == 0
    out = capsys.readouterr().out
    assert "https://id.yandex.ru/personal/data-access" in out
    assert cli.main(["forget", "yandex"]) == 0
    assert capsys.readouterr().out.strip() == "нечего удалять"
    assert cli.main(["status", "--offline"]) == 0
    assert "yandex: no-credential" in capsys.readouterr().out
