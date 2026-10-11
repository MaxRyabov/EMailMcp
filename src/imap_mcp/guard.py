"""Read-only guard for IMAP: the second barrier after the provider's own scope.

`ReadOnlyIMAP4_SSL` overrides `imaplib.IMAP4._command`, the single point where
imaplib turns a command into bytes on the socket. Before anything is written it
checks the command against an allowlist, rejects CR/LF/NUL in every argument and
lets FETCH request only items that cannot set \\Seen. Any violation drops the
pending literal, closes the socket and marks the connection unusable.

The guard relies on imaplib/imap-tools internals; tests/test_canary.py fails if
they change.
"""

from __future__ import annotations

import imaplib
import re
from collections.abc import Callable
from contextlib import suppress

from imap_tools import MailBox

from .redact import redact

# Commands allowed regardless of the selected folder.
_SESSION_COMMANDS = frozenset(
    {"CAPABILITY", "NOOP", "LOGIN", "AUTHENTICATE", "LOGOUT", "ID", "NAMESPACE", "LIST", "STATUS"}
)
# Commands working with a folder and its messages. UID is checked by subcommand.
_FOLDER_COMMANDS = frozenset({"EXAMINE", "SEARCH", "FETCH", "UID"})
_ALLOWED_COMMANDS = _SESSION_COMMANDS | _FOLDER_COMMANDS
_ALLOWED_UID_SUBCOMMANDS = frozenset({"SEARCH", "FETCH"})

# FETCH data items without side effects. BODY without a section is BODYSTRUCTURE
# without extensions; any section is allowed only via BODY.PEEK.
_FETCH_SIMPLE_ITEMS = frozenset(
    {
        "UID",
        "FLAGS",
        "INTERNALDATE",
        "RFC822.SIZE",
        "RFC822.HEADER",
        "ENVELOPE",
        "BODYSTRUCTURE",
        "BODY",
    }
)
# NAME[section]<partial>. Sections hold no quoted strings or literals: header
# field names are atoms here, anything fancier is rejected (fail-closed).
_FETCH_ITEM = re.compile(
    r"(?P<name>[A-Za-z0-9.]+)(?P<section>\[[A-Za-z0-9.\-() ]*\])?(?P<partial><\d+(?:\.\d+)?>)?"
)
_SEQUENCE_SET = re.compile(r"[0-9*:,]+")
# A line ending in {n} or {n+} makes the server treat the next bytes as a literal
# and desynchronises it from imaplib. Only imaplib itself may append a literal.
_TRAILING_LITERAL = re.compile(rb"\{\d+\+?\}\Z")
_FORBIDDEN_CHARS = ("\r", "\n", "\0")


class ReadOnlyViolation(Exception):
    """A command that could change the mailbox was stopped before the socket."""

    def __init__(self, command: str, reason: str):
        super().__init__(f"{command}: {reason}")
        self.command = command
        self.reason = reason


class ReadOnlyIMAP4_SSL(imaplib.IMAP4_SSL):  # noqa: N801 -- mirrors imaplib.IMAP4_SSL
    """IMAP4_SSL that only lets read-only commands reach the socket."""

    # Class default: _command runs from IMAP4.__init__ before instance attrs exist.
    broken = False

    def __init__(self, *args, tracer: Callable[[str], None] | None = None, **kwargs):
        # Set before super().__init__, which already reads the greeting.
        self.tracer = tracer
        super().__init__(*args, **kwargs)

    # --trace: the IMAP exchange as it is on the wire, secrets redacted.
    def send(self, data):
        if self.tracer:
            text = bytes(data).decode("utf-8", "replace").rstrip("\r\n")
            self.tracer(redact(f"IMAP > {text}"))
        return super().send(data)

    def _get_line(self):
        line = super()._get_line()
        if self.tracer:
            self.tracer(redact(f"IMAP < {line.decode('utf-8', 'replace')}"))
        return line

    def _command(self, name, *args):
        try:
            self._check(name, args)
        except ReadOnlyViolation:
            self._fail()
            raise
        except Exception as e:  # a check that cannot decide must not let the command through
            self._fail()
            raise ReadOnlyViolation(str(name), f"guard error: {type(e).__name__}") from e
        return super()._command(name, *args)

    def _check(self, name, args) -> None:
        if self.broken:
            raise ReadOnlyViolation(str(name), "connection closed after a violation")
        if not isinstance(name, str) or name not in _ALLOWED_COMMANDS:
            raise ReadOnlyViolation(str(name), "command not in allowlist")
        command = name
        if name == "UID":
            sub = args[0] if args else None
            if not isinstance(sub, str) or sub not in _ALLOWED_UID_SUBCOMMANDS:
                raise ReadOnlyViolation(f"UID {sub}", "UID subcommand not in allowlist")
            command = f"UID {sub}"
            args = args[1:]

        rendered = []
        for arg in args:
            if arg is None:
                continue
            if isinstance(arg, str):
                if any(c in arg for c in _FORBIDDEN_CHARS):
                    raise ReadOnlyViolation(command, "CR, LF or NUL in argument")
                rendered.append(arg.encode("utf-8"))
            elif isinstance(arg, bytes | bytearray):
                if any(c.encode() in arg for c in _FORBIDDEN_CHARS):
                    raise ReadOnlyViolation(command, "CR, LF or NUL in argument")
                rendered.append(bytes(arg))
            else:
                raise ReadOnlyViolation(command, f"unexpected argument type {type(arg).__name__}")
        if rendered and _TRAILING_LITERAL.search(rendered[-1]):
            raise ReadOnlyViolation(command, "argument ends with a literal marker")

        literal = self.literal
        if literal is not None:
            if callable(literal):
                if name != "AUTHENTICATE":
                    raise ReadOnlyViolation(command, "callable literal outside AUTHENTICATE")
            elif not isinstance(literal, bytes | bytearray):
                kind = type(literal).__name__
                raise ReadOnlyViolation(command, f"unexpected literal type {kind}")

        if command in ("FETCH", "UID FETCH"):
            _check_fetch(command, rendered)

    def _fail(self) -> None:
        """Drop the pending literal and close the socket; the connection is not reused."""
        self.literal = None
        self.broken = True
        with suppress(Exception):
            self.shutdown()


def _check_fetch(command: str, args: list[bytes]) -> None:
    if len(args) != 2:
        raise ReadOnlyViolation(command, "expected a sequence set and an item list")
    seq, items = (a.decode("ascii") for a in args)
    if not _SEQUENCE_SET.fullmatch(seq):
        raise ReadOnlyViolation(command, "malformed sequence set")
    for item in _fetch_items(command, items):
        name, section, partial = item.group("name", "section", "partial")
        name = name.upper()
        if section is not None:
            if name != "BODY.PEEK":
                raise ReadOnlyViolation(command, f"{name}[...] may set \\Seen, use BODY.PEEK")
        elif partial is not None or name not in _FETCH_SIMPLE_ITEMS:
            raise ReadOnlyViolation(command, f"FETCH item {item.group(0)} not in allowlist")


def _fetch_items(command: str, items: str) -> list[re.Match]:
    if items.startswith("(") and items.endswith(")"):
        items = items[1:-1]
    found, pos = [], 0
    while True:
        m = _FETCH_ITEM.match(items, pos)
        if m is None:
            raise ReadOnlyViolation(command, "malformed FETCH item list")
        found.append(m)
        pos = m.end()
        if pos == len(items):
            return found
        if items[pos] != " ":
            raise ReadOnlyViolation(command, "malformed FETCH item list")
        pos += 1


class ReadOnlyMailBox(MailBox):
    """imap-tools MailBox on top of ReadOnlyIMAP4_SSL, with a connection timeout."""

    def __init__(
        self,
        host: str = "",
        port: int = 993,
        timeout: float = 30,
        ssl_context=None,
        tracer: Callable[[str], None] | None = None,
    ):
        self._tracer = tracer
        super().__init__(host, port=port, timeout=timeout, ssl_context=ssl_context)

    def _get_mailbox_client(self) -> imaplib.IMAP4:
        return ReadOnlyIMAP4_SSL(
            self._host,
            self._port,
            ssl_context=self._ssl_context,
            timeout=self._timeout,
            tracer=self._tracer,
        )
