"""ReadOnlyIMAP4_SSL on a fake socket.

Every call goes straight to the IMAP client, bypassing tool argument validation:
the guard is the second barrier and must hold on its own.
"""

import pytest
from fakeimap import verbs
from imap_tools import AND, MailMessageFlags

from imap_mcp import guard
from imap_mcp.guard import ReadOnlyMailBox, ReadOnlyViolation


@pytest.fixture
def box(sockets):
    """Logged-in mailbox with INBOX examined, as open_box leaves it."""
    box = ReadOnlyMailBox("imap.example.com")
    box.login("user@example.com", "pw", initial_folder=None)
    box.folder.set("INBOX", readonly=True)
    return box


@pytest.fixture
def client(box):
    return box.client


@pytest.fixture
def sock(box, sockets):
    return sockets[-1]


def assert_rejected(client, sock, call, match=None):
    """The call raises ReadOnlyViolation, writes nothing and leaves the socket closed."""
    sent_before = bytes(sock.sent)
    with pytest.raises(ReadOnlyViolation, match=match):
        call()
    assert bytes(sock.sent) == sent_before
    assert sock.closed
    assert client.broken
    assert client.literal is None


# --- EXAMINE instead of SELECT -----------------------------------------------


def test_folder_opened_with_examine_never_select(box, sock):
    assert verbs(sock) == [b"LOGIN", b"EXAMINE"]
    assert sock.commands[-1].endswith(b'EXAMINE "INBOX"')
    assert box.client.state == "SELECTED"


def test_folder_set_without_readonly_is_select_and_rejected(box, sock):
    assert_rejected(box.client, sock, lambda: box.folder.set("INBOX"), match="SELECT")


# --- command allowlist --------------------------------------------------------

WRITE_CALLS = {
    "SELECT": lambda c: c.select("INBOX"),
    "STORE": lambda c: c.store("1", "+FLAGS", "(\\Seen)"),
    "UID STORE": lambda c: c.uid("STORE", "1", "+FLAGS", "(\\Seen)"),
    "APPEND": lambda c: c.append("INBOX", None, None, b"Subject: x\r\n\r\nbody"),
    "COPY": lambda c: c.copy("1", "Trash"),
    "UID COPY": lambda c: c.uid("COPY", "1", "Trash"),
    "MOVE": lambda c: c._simple_command("MOVE", "1", "Trash"),
    "UID MOVE": lambda c: c.uid("MOVE", "1", "Trash"),
    "EXPUNGE": lambda c: c.expunge(),
    "UID EXPUNGE": lambda c: c._simple_command("UID", "EXPUNGE", "1"),
    "CLOSE": lambda c: c.close(),
    "CREATE": lambda c: c.create("Projects"),
    "DELETE": lambda c: c.delete("Projects"),
    "RENAME": lambda c: c.rename("Projects", "Old"),
    "SUBSCRIBE": lambda c: c.subscribe("Projects"),
    "UNSUBSCRIBE": lambda c: c.unsubscribe("Projects"),
    "SETACL": lambda c: c.setacl("INBOX", "anyone", "lrswipkxtea"),
}


@pytest.mark.parametrize("command", WRITE_CALLS)
def test_write_commands_rejected_before_socket(client, sock, command):
    assert_rejected(client, sock, lambda: WRITE_CALLS[command](client), match=command)


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.noop(),
        lambda c: c.list(),
        lambda c: c.status("INBOX", "(MESSAGES UIDNEXT)"),
        lambda c: c.uid("SEARCH", "ALL"),
        lambda c: c.search(None, "UNSEEN"),
    ],
)
def test_read_commands_pass(client, sock, call):
    typ, _ = call(client)
    assert typ == "OK"
    assert not client.broken


def test_rejected_connection_stays_unusable(client, sock):
    with pytest.raises(ReadOnlyViolation):
        client.store("1", "+FLAGS", "(\\Seen)")
    with pytest.raises(ReadOnlyViolation, match="closed after a violation"):
        client.noop()


# --- FETCH items --------------------------------------------------------------


@pytest.mark.parametrize(
    "items",
    [
        "(BODY.PEEK[HEADER])",
        "(BODY.PEEK[] UID FLAGS RFC822.SIZE)",
        "BODY.PEEK[1.2]<0.1024>",
        "(body.peek[text] uid)",
        "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])",
        "(UID FLAGS INTERNALDATE ENVELOPE BODYSTRUCTURE BODY RFC822.HEADER RFC822.SIZE)",
    ],
)
def test_fetch_side_effect_free_items_pass(client, sock, items):
    typ, _ = client.uid("FETCH", "1:3", items)
    assert typ == "OK"
    # Python 3.13+ wraps a single item in parentheses
    sent = sock.commands[-1]
    assert sent.endswith(f"UID FETCH 1:3 {items}".encode()) or sent.endswith(f"({items})".encode())


@pytest.mark.parametrize(
    "items",
    [
        "(BODY[])",
        "BODY[HEADER]",
        "(UID BODY[TEXT]<0.100>)",
        "RFC822",
        "(UID RFC822.TEXT)",
        "ALL",
        "FULL",
        "BINARY[1]",
        "BODY<0.10>",
        "()",
        "(UID (FLAGS))",
        '(BODY.PEEK[HEADER.FIELDS ("FROM")])',
    ],
)
def test_fetch_items_that_may_set_seen_rejected(client, sock, items):
    assert_rejected(client, sock, lambda: client.uid("FETCH", "1", items), match="UID FETCH")


def test_plain_fetch_body_rejected(client, sock):
    assert_rejected(client, sock, lambda: client.fetch("1", "(BODY[])"), match="FETCH")


def test_fetch_sequence_set_cannot_carry_items(client, sock):
    assert_rejected(client, sock, lambda: client.uid("FETCH", "1 BODY[]", "(UID)"))


def test_fetch_extra_arguments_rejected(client, sock):
    # straight to _simple_command: Python 3.13+ uid() refuses extra arguments itself
    call = lambda: client._simple_command("UID", "FETCH", "1", "(UID)", "(CHANGEDSINCE 1)")  # noqa: E731
    assert_rejected(client, sock, call)


def test_imap_tools_fetch_with_mark_seen_rejected(box, sock):
    with pytest.raises(ReadOnlyViolation, match="UID FETCH"):
        list(box.fetch(AND(all=True), mark_seen=True))
    # the UID SEARCH before it went through, the BODY[] fetch did not
    assert verbs(sock)[-1] == b"UID SEARCH"
    assert b"BODY[" not in sock.sent
    assert sock.closed


# --- CR/LF/NUL and literals ---------------------------------------------------


def test_crlf_in_search_argument_rejected(client, sock):
    evil = 'x\r\nZ1 DELETE "Проекты"'
    call = lambda: client.uid("SEARCH", "CHARSET", "UTF-8", "TEXT", evil)  # noqa: E731
    assert_rejected(client, sock, call, match="CR, LF or NUL")
    assert b"DELETE" not in sock.sent


@pytest.mark.parametrize("arg", ["x\nZ", "x\rZ", "x\0Z", b"x\nZ", b"x\0Z", bytearray(b"x\rZ")])
def test_forbidden_chars_in_any_argument(client, sock, arg):
    assert_rejected(client, sock, lambda: client.uid("SEARCH", "TEXT", arg))


def test_newline_in_folder_name_rejected_before_examine(client, sock):
    examines = verbs(sock).count(b"EXAMINE")
    call = lambda: client.select("INBOX\nZ2 DELETE INBOX", readonly=True)  # noqa: E731
    assert_rejected(client, sock, call, match="EXAMINE")
    assert verbs(sock).count(b"EXAMINE") == examines


def test_argument_ending_in_literal_marker_rejected(client, sock):
    assert_rejected(client, sock, lambda: client.uid("SEARCH", "TEXT", "x {12}"))


def test_unexpected_argument_type_rejected(client, sock):
    assert_rejected(client, sock, lambda: client.uid("SEARCH", "UID", 5))


def test_bytes_literal_passes_for_search(client, sock):
    client.literal = "Проекты".encode()
    typ, data = client.uid("SEARCH", "CHARSET", "UTF-8", "TEXT")
    assert typ == "OK"
    assert data == [b"1 2"]
    assert sock.commands[-1].endswith(b"TEXT {14}" + "Проекты".encode())


def test_callable_literal_rejected_outside_authenticate(client, sock):
    client.literal = lambda response: b"x"
    call = lambda: client.uid("SEARCH", "CHARSET", "UTF-8", "TEXT")  # noqa: E731
    assert_rejected(client, sock, call, match="callable literal")


def test_callable_literal_allowed_for_authenticate(sockets):
    box = ReadOnlyMailBox("imap.example.com")
    box.xoauth2("user@example.com", "token", initial_folder=None)
    assert verbs(sockets[-1]) == [b"AUTHENTICATE"]
    assert box.client.state == "AUTH"


def test_rejected_append_never_sends_its_literal(client, sock):
    message = b"Subject: planted\r\n\r\nbody"
    assert_rejected(client, sock, lambda: client.append("INBOX", None, None, message))
    assert b"planted" not in sock.sent


# --- imap-tools MailBox operations --------------------------------------------

BOX_WRITES = {
    "flag": lambda b: b.flag("1", MailMessageFlags.SEEN, True),
    "delete": lambda b: b.delete("1"),
    "move": lambda b: b.move("1", "Trash"),
    "append": lambda b: b.append(b"Subject: x\r\n\r\nbody", "INBOX"),
    "folder.create": lambda b: b.folder.create("Projects"),
}


@pytest.mark.parametrize("op", BOX_WRITES)
def test_mailbox_write_operations_rejected(box, sock, op):
    assert_rejected(box.client, sock, lambda: BOX_WRITES[op](box))


# --- connection settings ------------------------------------------------------


def test_mailbox_passes_default_timeout(sockets):
    ReadOnlyMailBox("imap.example.com")
    assert sockets[-1].timeout == 30


def test_mailbox_passes_explicit_timeout(sockets):
    box = ReadOnlyMailBox("imap.example.com", port=1993, timeout=5)
    assert sockets[-1].timeout == 5
    assert isinstance(box.client, guard.ReadOnlyIMAP4_SSL)
    assert box.client.port == 1993
