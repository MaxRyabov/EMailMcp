"""ConnectionPool (D13): reuse, fingerprints, broken connections, locks, deadlines."""

import imaplib
import os
import threading
import time

import pytest
from fakeimap import command_verb
from fakemail import make_message

from imap_mcp import accounts, imap, server
from imap_mcp.auth import CredentialUnavailable, store
from imap_mcp.guard import ReadOnlyIMAP4_SSL, ReadOnlyViolation
from imap_mcp.pool import AccountBusy, ConnectionPool, Deadline

OAUTH_ACCOUNT = """
[[account]]
key = "yandex"
email = "me@yandex.ru"
auth = "oauth"
oauth_provider = "yandex"
client_id = "client-id-1"
"""


def verbs(state):
    return [command_verb(c)[1] for s in state.sockets for c in s.commands]


def touch(path, text=None):
    """Rewrite the config so that its mtime surely changes."""
    if text is not None:
        path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def save_token(key="yandex", obtained_at=None):
    now = time.time()
    store.save_tokens(
        key,
        store.Tokens(
            access_token="y0_token-for-pool-tests",
            refresh_token=None,
            expires_at=now + 100 * 86400,
            obtained_at=obtained_at or now,
            scope="mail:imap_ro",
        ),
    )


def test_twenty_reads_one_login(mail):
    alpha = mail["alpha"]
    alpha.add(make_message())
    mid = server.list_emails(account="alpha")["rows"][0]["id"]
    for _ in range(20):
        assert server.get_email(mid)["body"] == "plain body"
    assert alpha.logins() == 1
    assert len(alpha.sockets) == 1
    assert verbs(alpha).count(b"NOOP") == 20  # before every reuse


def test_config_edit_replaces_connection(mail, config):
    server.list_emails(account="alpha")
    first = mail["alpha"].sockets[0]
    touch(config, config.read_text().replace("alpha@example.com (Test)", "Alpha renamed"))
    server.list_emails(account="alpha")
    assert mail["alpha"].logins() == 2
    assert first.closed


def test_new_password_replaces_connection(mail, monkeypatch):
    server.list_emails(account="alpha")
    monkeypatch.setenv("ALPHA_PW", "rotated-password")
    server.list_emails(account="alpha")
    assert mail["alpha"].logins() == 2


def test_forget_closes_connection_and_reports_no_credential(mail, config):
    touch(config, config.read_text() + OAUTH_ACCOUNT)
    save_token()
    assert server.list_emails(account="yandex")["errors"] == []
    sock = mail["yandex"].sockets[0]

    store.delete_tokens("yandex")  # what `imap-mcp forget yandex` does
    out = server.list_emails(account="yandex")
    assert [e["error"].split(":")[0] for e in out["errors"]] == ["no-credential"]
    assert sock.closed
    assert len(mail["yandex"].sockets) == 1  # no new connection


def test_new_auth_replaces_connection(mail, config):
    touch(config, config.read_text() + OAUTH_ACCOUNT)
    save_token(obtained_at=1_000)
    server.list_emails(account="yandex")
    save_token(obtained_at=2_000)  # a fresh `imap-mcp auth`
    server.list_emails(account="yandex")
    assert mail["yandex"].logins() == 2


def test_broken_connection_is_not_returned_to_the_pool(mail):
    a = accounts.get_account("alpha")
    with pytest.raises(ReadOnlyViolation):
        mail.pool.run(a, lambda box: box.client.select("INBOX"))  # SELECT is not allowed
    assert mail["alpha"].sockets[0].closed
    mail.pool.run(a, lambda box: box.client.noop())
    assert len(mail["alpha"].sockets) == 2


def test_dropped_connection_is_replaced_before_use(mail):
    server.list_emails(account="alpha")
    mail["alpha"].sockets[0].closed = True  # server went away while idle: NOOP fails
    assert server.list_emails(account="alpha")["errors"] == []
    assert len(mail["alpha"].sockets) == 2


def test_one_reconnect_on_connection_error(mail, monkeypatch):
    calls = []
    real = imap.examine

    def flaky(box, folder):
        calls.append(folder)
        if len(calls) == 1:
            raise imaplib.IMAP4.abort("socket error: EOF")
        return real(box, folder)

    monkeypatch.setattr(imap, "examine", flaky)
    assert server.list_emails(account="alpha")["errors"] == []
    assert len(calls) == 2
    assert len(mail["alpha"].sockets) == 2


def test_failed_first_connect_is_retried_once(mail, monkeypatch):
    real = ReadOnlyIMAP4_SSL._create_socket
    attempts = []

    def flaky(self, timeout):
        attempts.append(self.host)
        if len(attempts) == 1:
            raise ConnectionResetError("reset by peer")
        return real(self, timeout)

    monkeypatch.setattr(ReadOnlyIMAP4_SSL, "_create_socket", flaky)
    assert server.list_emails(account="alpha")["errors"] == []
    assert len(attempts) == 2


def test_busy_account_has_its_own_message():
    assert server.describe(AccountBusy("account is busy")) == (
        "unreachable: timeout (аккаунт занят другим вызовом)"
    )


def test_only_one_reconnect(mail, monkeypatch):
    def dead(box, folder):
        raise imaplib.IMAP4.abort("socket error: EOF")

    monkeypatch.setattr(imap, "examine", dead)
    out = server.list_emails(account="alpha")
    assert out["errors"] == [
        {"account": "alpha", "error": "unreachable: abort (imap.example.com:1993)"}
    ]
    assert len(mail["alpha"].sockets) == 2


def test_no_reconnect_after_timeout(mail, monkeypatch):
    def slow(box, folder):
        raise TimeoutError("timed out")

    monkeypatch.setattr(imap, "examine", slow)
    out = server.list_emails(account="alpha")
    assert out["errors"] == [{"account": "alpha", "error": "unreachable: timeout"}]
    assert len(mail["alpha"].sockets) == 1
    assert mail["alpha"].sockets[0].closed


def test_slow_server_times_out_within_deadline(mail):
    mail["alpha"].add(make_message(subject="alpha"))
    mail["gmailish"].add(make_message(subject="beta"))
    mail["alpha"].delay = 20  # every answer takes 20 s
    start = mail.clock()
    out = server.list_emails()
    assert mail.clock() - start <= 45
    assert out["errors"] == [{"account": "alpha", "error": "unreachable: timeout"}]
    assert [r["subject"] for r in out["rows"]] == ["beta"]
    assert len(mail["alpha"].sockets) == 1  # no reconnect after the timeout


def test_server_hanging_mid_answer_times_out(mail):
    mail["alpha"].delay = 60  # longer than the socket timeout
    start = mail.clock()
    out = server.list_emails(account="alpha")
    assert out["errors"] == [{"account": "alpha", "error": "unreachable: timeout"}]
    assert mail.clock() - start <= 45


def test_idle_connection_is_closed(mail):
    server.list_emails(account="alpha")
    first = mail["alpha"].sockets[0]
    mail.clock.advance(119)
    mail.pool.sweep()
    assert not first.closed
    mail.clock.advance(2)
    mail.pool.sweep()
    assert first.closed
    server.list_emails(account="alpha")
    assert mail["alpha"].logins() == 2


def test_idle_connection_is_not_reused(mail):
    server.list_emails(account="alpha")
    mail.clock.advance(121)
    server.list_emails(account="alpha")
    assert mail["alpha"].logins() == 2
    assert b"NOOP" not in verbs(mail["alpha"])


def test_threads_on_one_account_do_not_deadlock(mail):
    for i in range(3):
        mail["alpha"].add(make_message(subject=f"a{i}"))
        mail["gmailish"].add(make_message(subject=f"b{i}"))
    results, failures = [], []

    def worker(account):
        try:
            for _ in range(5):
                results.append(server.list_emails(account=account))
        except Exception as e:  # noqa: BLE001
            failures.append(e)

    threads = [threading.Thread(target=worker, args=(k,)) for k in ("alpha", "alpha", "gmailish")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not any(t.is_alive() for t in threads)
    assert failures == []
    assert len(results) == 15
    assert all(len(r["rows"]) == 3 and r["errors"] == [] for r in results)
    assert mail["alpha"].logins() == 1  # the two alpha threads shared one connection


def test_busy_account_times_out_instead_of_waiting_forever():
    pool = ConnectionPool(deadline=0.2, janitor=False)
    slot = pool._slot("alpha")
    slot.lock.acquire()
    try:
        account = accounts.Account("alpha", "a", "a@example.com", "h", 993, "X", True)
        with pytest.raises(AccountBusy):
            pool.run(account, lambda box: None)
    finally:
        slot.lock.release()


def test_credential_problem_raises_before_connecting(mail, monkeypatch):
    monkeypatch.delenv("ALPHA_PW")
    with pytest.raises(CredentialUnavailable):
        mail.pool.run(accounts.get_account("alpha"), lambda box: None)
    assert mail["alpha"].sockets == []


def test_deadline_caps_socket_timeout():
    now = [0.0]
    deadline = Deadline(45, clock=lambda: now[0])

    class Sock:
        timeout = None

        def settimeout(self, value):
            self.timeout = value

    client = type("Client", (), {"sock": Sock()})()
    deadline(client)
    assert client.sock.timeout == 30
    now[0] = 40
    deadline(client)
    assert client.sock.timeout == 5
    now[0] = 45
    with pytest.raises(TimeoutError):
        deadline(client)
