import textwrap

import keyring
import pytest
from fakeimap import FakeSocket
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from imap_mcp import accounts, redact
from imap_mcp.guard import ReadOnlyIMAP4_SSL


class MemoryKeyring(KeyringBackend):
    """In-memory keyring: no test may touch the real Credential Manager."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.data = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        if self.data.pop((service, username), None) is None:
            raise PasswordDeleteError(username)


@pytest.fixture(autouse=True)
def forget_registered_secrets():
    with redact._lock:
        redact._secrets.clear()
    yield
    with redact._lock:
        redact._secrets.clear()


@pytest.fixture(autouse=True)
def memory_keyring():
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    yield kr
    keyring.set_keyring(previous)


CONFIG = textwrap.dedent("""\
    [[account]]
    key = "alpha"
    label = "alpha@example.com (Test)"
    email = "alpha@example.com"
    host = "imap.example.com"
    port = 1993
    password_env = "ALPHA_PW"
    enabled = true

    [[account]]
    key = "gmailish"
    email = "beta@gmail.com"
    host = "imap.gmail.com"
    password_env = "BETA_PW"

    [[account]]
    key = "off"
    label = "off@example.com"
    email = "off@example.com"
    host = "imap.example.com"
    password_env = "OFF_PW"
    enabled = false
""")


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point the registry at a three-account test config (one disabled)."""
    path = tmp_path / "accounts.toml"
    path.write_text(CONFIG)
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(path))
    for env in ("ALPHA_PW", "BETA_PW", "OFF_PW"):  # a developer may have them exported
        monkeypatch.delenv(env, raising=False)
    accounts.clear_cache()
    yield path
    accounts.clear_cache()


@pytest.fixture
def sockets(monkeypatch):
    """Every ReadOnlyIMAP4_SSL connects to a new FakeSocket; the list collects them."""
    created = []

    def fake_create_socket(self, timeout):
        created.append(FakeSocket(timeout))
        return created[-1]

    monkeypatch.setattr(ReadOnlyIMAP4_SSL, "_create_socket", fake_create_socket)
    return created
