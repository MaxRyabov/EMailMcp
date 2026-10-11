"""Canary for the internals guard.py relies on.

ReadOnlyIMAP4_SSL hooks imaplib.IMAP4._command (and send/_get_line for --trace),
ReadOnlyMailBox hooks MailBox._get_mailbox_client. If a Python or imap-tools
release changes them, or imaplib starts writing commands around _command, these
tests fail first.
"""

import imaplib
import inspect

import pytest
from imap_tools import BaseMailBox, MailBox


def test_command_signature():
    params = list(inspect.signature(imaplib.IMAP4._command).parameters.values())
    assert [p.name for p in params] == ["self", "name", "args"]
    assert params[2].kind is inspect.Parameter.VAR_POSITIONAL


def test_mailbox_client_factory():
    assert list(inspect.signature(MailBox._get_mailbox_client).parameters) == ["self"]
    assert "self._get_mailbox_client()" in inspect.getsource(BaseMailBox.__init__)


class _Sent(Exception):
    pass


class _Recorder(imaplib.IMAP4):
    """IMAP4 without a socket: records what reaches _command, fails on direct send."""

    def __init__(self):  # no network
        self.debug = 0
        self.state = "SELECTED"
        self.literal = None
        self.tagged_commands = {}
        self.untagged_responses = {}
        self.is_readonly = False
        self.tagnum = 0
        self._mode_ascii()
        self.commands = []

    def _command(self, name, *args):
        self.commands.append((name, *args))
        raise _Sent

    def send(self, data):
        raise AssertionError(f"imaplib wrote {data!r} without _command")


@pytest.mark.parametrize(
    "call, expected",
    [
        (lambda c: c.select("INBOX"), "SELECT"),
        (lambda c: c.select("INBOX", readonly=True), "EXAMINE"),
        (lambda c: c.uid("FETCH", "1", "(UID)"), "UID"),
        (lambda c: c.append("INBOX", None, None, b"body"), "APPEND"),
        (lambda c: c.store("1", "+FLAGS", "(\\Seen)"), "STORE"),
        (lambda c: c.copy("1", "Trash"), "COPY"),
    ],
)
def test_commands_go_through_command(call, expected):
    client = _Recorder()
    with pytest.raises(_Sent):
        call(client)
    assert client.commands[0][0] == expected


def test_trace_hooks_signature():
    """--trace hooks IMAP4.send and IMAP4._get_line (guard.ReadOnlyIMAP4_SSL)."""
    assert list(inspect.signature(imaplib.IMAP4.send).parameters) == ["self", "data"]
    assert list(inspect.signature(imaplib.IMAP4._get_line).parameters) == ["self"]
