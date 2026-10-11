import datetime as dt
from types import SimpleNamespace

import pytest

from imap_mcp import accounts, imap, search
from imap_mcp.auth import CredentialUnavailable, base
from imap_mcp.redact import redact


class FakeFolderManager:
    def __init__(self):
        self.set_calls = []

    def set(self, folder, readonly=False):
        self.set_calls.append((folder, readonly))


class FakeMailBox:
    """Stands in for ReadOnlyMailBox; records login/fetch args, no network."""

    instances = []
    messages = []

    def __init__(self, host, port=993):
        self.host = host
        self.port = port
        self.login_args = None
        self.fetch_kwargs = None
        self.logged_out = False
        self.folder = FakeFolderManager()
        FakeMailBox.instances.append(self)

    def login(self, email, password, initial_folder="INBOX"):
        self.login_args = (email, password, initial_folder)
        return self

    def fetch(self, criteria, **kwargs):
        self.fetch_criteria = criteria
        self.fetch_kwargs = kwargs
        return iter(FakeMailBox.messages)

    def logout(self):
        self.logged_out = True


@pytest.fixture(autouse=True)
def fake_mailbox(monkeypatch):
    FakeMailBox.instances = []
    FakeMailBox.messages = []
    monkeypatch.setattr(imap, "ReadOnlyMailBox", FakeMailBox)


def fake_msg(**overrides):
    base = dict(
        uid="42",
        from_="sender@example.com",
        to=("rcpt@example.com",),
        cc=(),
        subject="Hello",
        date=dt.datetime(2026, 7, 30, 12, 0, tzinfo=dt.UTC),
        flags=("\\Seen",),
        text="line one\nline two",
        html="",
        headers={"message-id": ["<abc@example.com>"]},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_open_box_requires_password(config):
    alpha = accounts.get_account("alpha")
    with pytest.raises(CredentialUnavailable, match="ALPHA_PW") as e, imap.open_box(alpha):
        pass
    assert e.value.status == "no-credential"
    assert FakeMailBox.instances == []  # no connection without a credential


def test_open_box_logs_in_and_out(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "s3cret")
    with imap.open_box(accounts.get_account("alpha")) as box:
        assert box.login_args == ("alpha@example.com", "s3cret", None)
        assert box.folder.set_calls == [("INBOX", True)]
        assert (box.host, box.port) == ("imap.example.com", 1993)
        assert not box.logged_out
    assert box.logged_out


def test_open_box_examines_requested_folder(config, monkeypatch):
    monkeypatch.setenv("ALPHA_PW", "pw")
    with imap.open_box(accounts.get_account("alpha"), "Archive") as box:
        assert box.folder.set_calls == [("Archive", True)]


def test_open_box_strips_spaces_for_gmail_only(config, monkeypatch):
    monkeypatch.setenv("BETA_PW", "abcd efgh ijkl mnop")
    with imap.open_box(accounts.get_account("gmailish")) as box:
        assert box.login_args[1] == "abcdefghijklmnop"

    monkeypatch.setenv("ALPHA_PW", "pass with spaces")
    with imap.open_box(accounts.get_account("alpha")) as box:
        assert box.login_args[1] == "pass with spaces"


def test_gmail_host_match_is_exact():
    assert base._is_gmail("imap.gmail.com")
    assert base._is_gmail("GMAIL.COM.")
    assert not base._is_gmail("imap.mygmail.com")


def test_both_password_forms_are_secrets(config, monkeypatch):
    monkeypatch.setenv("BETA_PW", "abcd efgh ijkl mnop")
    cred = base.PasswordCredential(accounts.get_account("gmailish"))
    assert cred.secrets() == ["abcd efgh ijkl mnop", "abcdefghijklmnop"]
    assert "abcdefghijklmnop" not in redact("LOGIN beta@gmail.com abcdefghijklmnop")


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        (dict(query=None, since=None, unread_only=False), "(ALL)"),
        (dict(query=None, since=dt.date(2026, 7, 1), unread_only=False), "(SINCE 1-Jul-2026)"),
        (dict(query=None, since=None, unread_only=True), "(UNSEEN)"),
    ],
)
def test_criteria_simple(kwargs, expected):
    assert str(search.criteria(**kwargs)) == expected


def test_criteria_combines_with_and():
    crit = str(search.criteria("invoice", dt.date(2026, 7, 1), True))
    for token in ("OR OR FROM", "invoice", "SINCE 1-Jul-2026", "UNSEEN"):
        assert token in crit
