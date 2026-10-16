"""A fake IMAP server with a real mailbox behind it: folders, messages, LIST,
STATUS, EXAMINE, UID SEARCH and UID FETCH. Runs behind the real guard and imaplib."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from email.message import EmailMessage

from fakeimap import FakeSocket, command_verb
from imap_tools import MailMessage
from imap_tools.imap_utf7 import utf7_decode, utf7_encode


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_message(
    subject="Hello",
    from_="Sender <sender@example.com>",
    to="rcpt@example.com",
    date="Mon, 05 Oct 2026 10:00:00 +0000",
    text: str | None = "plain body",
    html: str | None = None,
    attachments=(),
    headers=None,
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_
    msg["To"] = to
    if date is not None:
        msg["Date"] = date
    msg["Message-ID"] = "<m@example.com>"
    for k, v in (headers or {}).items():
        msg[k] = v
    if text is not None:
        msg.set_content(text)
        if html is not None:
            msg.add_alternative(html, subtype="html")
    elif html is not None:
        msg.set_content(html, subtype="html")
    for name, ctype, data, inline, *rest in attachments:
        (params,) = rest or [None]  # optional MIME parameters: {"charset": ...}
        maintype, subtype = ctype.split("/")
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=name,
            disposition="inline" if inline else "attachment",
            params=params,
        )
    return msg.as_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


@dataclass
class Message:
    uid: int
    raw: bytes
    flags: tuple[str, ...] = ()
    size: int | None = None  # RFC822.SIZE as the server reports it

    @property
    def rfc822_size(self) -> int:
        return len(self.raw) if self.size is None else self.size

    @property
    def parsed(self) -> MailMessage:
        return MailMessage.from_bytes(self.raw)


@dataclass
class Folder:
    name: str
    flags: tuple[str, ...] = ("\\HasNoChildren",)
    uidvalidity: int = 7
    messages: list[Message] = field(default_factory=list)
    status_fails: bool = False

    @property
    def selectable(self) -> bool:
        return not any(f.lower() in ("\\noselect", "\\nonexistent") for f in self.flags)


class MailState:
    """One mailbox; every connection to its host sees the same state."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.folders: dict[str, Folder] = {"INBOX": Folder("INBOX")}
        self.delay = 0.0  # seconds each command takes to answer
        self.sockets: list[FakeMailSocket] = []
        self.searches: list[bytes] = []
        self.fetches: list[bytes] = []

    def folder(self, name: str, flags=("\\HasNoChildren",), uidvalidity: int = 7) -> Folder:
        self.folders[name] = Folder(name, tuple(flags), uidvalidity)
        return self.folders[name]

    def add(self, raw: bytes, folder: str = "INBOX", flags=(), size=None) -> Message:
        f = self.folders[folder]
        uid = max((m.uid for m in f.messages), default=0) + 1
        f.messages.append(Message(uid, raw, tuple(flags), size))
        return f.messages[-1]

    def logins(self) -> int:
        verbs = [command_verb(c)[1] for s in self.sockets for c in s.commands]
        return sum(v in (b"LOGIN", b"AUTHENTICATE") for v in verbs)


def _tokens(data: bytes) -> list:
    """IMAP arguments: atoms, quoted strings, inlined literals, parenthesised lists."""
    stack: list[list] = [[]]
    i = 0
    while i < len(data):
        c = data[i : i + 1]
        if c == b" ":
            i += 1
        elif c == b"(":
            stack.append([])
            i += 1
        elif c == b")":
            inner = stack.pop()
            stack[-1].append(inner)
            i += 1
        elif c == b'"':
            buf, i = bytearray(), i + 1
            while data[i : i + 1] != b'"':
                if data[i : i + 1] == b"\\":
                    i += 1
                buf += data[i : i + 1]
                i += 1
            stack[-1].append(bytes(buf))
            i += 1
        elif c == b"{":
            m = re.match(rb"\{(\d+)\}", data[i:])
            start = i + m.end()
            stack[-1].append(data[start : start + int(m.group(1))])
            i = start + int(m.group(1))
        else:
            m = re.match(rb"[^ ()]+", data[i:])
            stack[-1].append(m.group())
            i += m.end()
    return stack[0]


def _quote(name: str) -> bytes:
    raw = utf7_encode(name)
    return b'"' + raw.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def _uid_set(spec: bytes, uids: list[int]) -> set[int]:
    out = set()
    top = max(uids, default=0)
    for part in spec.split(b","):
        if b":" in part:
            a, b = (top if x == b"*" else int(x) for x in part.split(b":"))
            out.update(range(min(a, b), max(a, b) + 1))
        else:
            out.add(top if part == b"*" else int(part))
    return out


class FakeMailSocket(FakeSocket):
    def __init__(self, state: MailState, timeout):
        super().__init__(timeout)
        self.state = state
        self.sock_timeout = timeout
        self.stalled = False
        self.selected: Folder | None = None
        state.sockets.append(self)

    def settimeout(self, value):
        self.sock_timeout = value

    def take(self, n):
        if self.stalled and not self.inbox:
            raise TimeoutError("timed out")
        return super().take(n)

    def _reply(self, cmd):
        if self.stalled:
            return
        if self.state.delay:
            wait = self.state.delay
            if self.sock_timeout is not None and wait > self.sock_timeout:
                self.state.clock.advance(self.sock_timeout)
                self.stalled = True
                return
            self.state.clock.advance(wait)
        if self._auth_tag is not None:
            return super()._reply(cmd)
        tag, verb = command_verb(cmd)
        handler = {
            b"LIST": self._list,
            b"STATUS": self._status,
            b"EXAMINE": None,
            b"UID SEARCH": self._search,
            b"UID FETCH": self._fetch,
        }.get(verb, False)
        if handler is False:
            return super()._reply(cmd)
        self.commands.append(cmd)
        args = _tokens(cmd)[2:]
        if verb.startswith(b"UID"):
            args = args[1:]
        if verb == b"EXAMINE":
            self.inbox += self._examine(tag, args)
            return
        try:
            data = handler(args)
        except _No:
            self.inbox += tag + b" NO failed\r\n"
            return
        self.inbox += data + tag + b" OK done\r\n"

    def _list(self, args) -> bytes:
        out = b""
        for f in self.state.folders.values():
            flags = " ".join(f.flags).encode()
            out += b"* LIST (" + flags + b') "/" ' + _quote(f.name) + b"\r\n"
        return out

    def _folder(self, raw: bytes) -> Folder | None:
        name = utf7_decode(raw)
        if name.upper() == "INBOX":
            name = "INBOX"
        return self.state.folders.get(name)

    def _status(self, args) -> bytes:
        f = self._folder(args[0])
        if f is None or not f.selectable or f.status_fails:
            raise _No()
        unseen = sum("\\Seen" not in m.flags for m in f.messages)
        return (
            b"* STATUS "
            + _quote(f.name)
            + b" (MESSAGES %d UNSEEN %d)\r\n"
            % (
                len(f.messages),
                unseen,
            )
        )

    def _examine(self, tag, args) -> bytes:
        f = self._folder(args[0])
        if f is None or not f.selectable:
            self.selected = None
            return tag + b" NO no such folder\r\n"
        self.selected = f
        return (
            b"* %d EXISTS\r\n* OK [UIDVALIDITY %d] ok\r\n" % (len(f.messages), f.uidvalidity)
            + tag
            + b" OK [READ-ONLY] done\r\n"
        )

    # --- SEARCH -----------------------------------------------------------------

    def _search(self, args) -> bytes:
        self.state.searches.append(self.commands[-1])
        if args and args[0].upper() == b"CHARSET":
            args = args[2:]
        found = [m.uid for m in self.selected.messages if _matches(m, iter(args), all_=True)]
        return b"* SEARCH" + b"".join(b" %d" % u for u in found) + b"\r\n"

    # --- FETCH ------------------------------------------------------------------

    def _fetch(self, args) -> bytes:
        self.state.fetches.append(self.commands[-1])
        spec, items = args[0], args[1]
        items = [i.upper() for i in (items if isinstance(items, list) else [items])]
        msgs = self.selected.messages
        wanted = _uid_set(spec, [m.uid for m in msgs])
        out = b""
        for seq, m in enumerate(msgs, 1):
            if m.uid not in wanted:
                continue
            parts = [b"UID %d" % m.uid, b"FLAGS (" + " ".join(m.flags).encode() + b")"]
            parts.append(b"RFC822.SIZE %d" % m.rfc822_size)
            head = b"* %d FETCH (" % seq + b" ".join(parts)
            if b"BODY.PEEK[HEADER]" in items:
                end = m.raw.find(b"\r\n\r\n")
                data, label = m.raw[: end + 4], b"BODY[HEADER]"
            elif b"BODY.PEEK[]" in items:
                data, label = m.raw, b"BODY[]"
            else:
                out += head + b")\r\n"
                continue
            out += head + b" " + label + b" {%d}\r\n" % len(data) + data + b")\r\n"
        return out


class _No(Exception):
    pass


def _text(m: Message, field_: bytes) -> str:
    p = m.parsed
    if field_ == b"FROM":
        v = p.from_values
        return f"{v.name} {v.email}" if v else ""
    if field_ == b"SUBJECT":
        return p.subject
    return f"{p.subject}\n{p.text}\n{p.html}"


def _matches(m: Message, it, all_: bool = False) -> bool:
    if all_:
        return all(_key(m, t, it) for t in it)
    return _key(m, next(it), it)


def _key(m: Message, token, it) -> bool:
    if isinstance(token, list):
        return _matches(m, iter(token), all_=True)
    key = token.upper()
    if key == b"ALL":
        return True
    if key == b"UNSEEN":
        return "\\Seen" not in m.flags
    if key == b"SEEN":
        return "\\Seen" in m.flags
    if key == b"NOT":
        return not _matches(m, it)
    if key == b"OR":
        a = _matches(m, it)
        b = _matches(m, it)
        return a or b
    if key == b"SINCE":
        day = dt.datetime.strptime(next(it).decode(), "%d-%b-%Y").date()
        d = m.parsed.date
        return d.year > 1900 and d.date() >= day
    if key in (b"FROM", b"SUBJECT", b"TEXT"):
        needle = next(it).decode("utf-8").lower()
        return needle in _text(m, key).lower()
    raise ValueError(f"fake server: unsupported search key {key!r}")
