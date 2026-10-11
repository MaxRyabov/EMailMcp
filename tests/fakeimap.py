"""Fake IMAP server behind a socket, shared by the guard and auth tests."""

import re


class _FakeFile:
    def __init__(self, sock):
        self.sock = sock

    def readline(self, limit=-1):
        i = self.sock.inbox.find(b"\n")
        return self.sock.take(len(self.sock.inbox) if i < 0 else i + 1)

    def read(self, n=-1):
        return self.sock.take(len(self.sock.inbox) if n < 0 else n)

    def close(self):
        pass


class FakeSocket:
    """Minimal IMAP server behind a socket: answers OK, honours literals and AUTHENTICATE.

    Python 3.12/3.13 read through makefile(), 3.14 through recv(); both are served
    from the same buffer.
    """

    GREETING = b"* OK [CAPABILITY IMAP4rev1 AUTH=XOAUTH2] fake ready\r\n"
    UNTAGGED = {
        b"CAPABILITY": b"* CAPABILITY IMAP4rev1 AUTH=XOAUTH2\r\n",
        b"LOGOUT": b"* BYE bye\r\n",
        b"EXAMINE": b"* 2 EXISTS\r\n* OK [UIDVALIDITY 7] ok\r\n",
        b"SELECT": b"* 2 EXISTS\r\n* OK [UIDVALIDITY 7] ok\r\n",
        b"UID SEARCH": b"* SEARCH 1 2\r\n",
        b"UID FETCH": b"* 1 FETCH (UID 1 FLAGS (\\Seen))\r\n",
    }

    reject_auth = False  # answer NO to LOGIN and AUTHENTICATE

    def __init__(self, timeout):
        self.timeout = timeout
        self.sent = bytearray()
        self.commands = []  # complete commands, tag included, literals inlined
        self.closed = False
        self.inbox = bytearray(self.GREETING)
        self._pending = bytearray()
        self._command = bytearray()
        self._literal_left = 0
        self._auth_tag = None
        self.auth_responses = []  # client lines sent after the AUTHENTICATE continuation

    # client -> server
    def sendall(self, data):
        if self.closed:
            raise OSError("socket closed")
        self.sent += data
        self._pending += data
        while True:
            if self._literal_left:
                chunk = self._pending[: self._literal_left]
                del self._pending[: len(chunk)]
                self._command += chunk
                self._literal_left -= len(chunk)
                if self._literal_left:
                    return
            end = self._pending.find(b"\r\n")
            if end < 0:
                return
            line = bytes(self._pending[:end])
            del self._pending[: end + 2]
            self._command += line
            m = re.search(rb"\{(\d+)\}$", line)
            if m:
                self._literal_left = int(m.group(1))
                self.inbox += b"+ go ahead\r\n"
                continue
            self._reply(bytes(self._command))
            self._command = bytearray()

    def _reply(self, cmd):
        if self._auth_tag is not None:
            tag, self._auth_tag = self._auth_tag, None
            self.auth_responses.append(cmd)
            self._finish_auth(tag)
            return
        self.commands.append(cmd)
        tag, verb = command_verb(cmd)
        if verb == b"AUTHENTICATE":
            if len(cmd.split(b" ")) > 3:  # one-line form: AUTHENTICATE XOAUTH2 <b64>
                self._finish_auth(tag)
                return
            self._auth_tag = tag
            self.inbox += b"+ \r\n"
            return
        if verb == b"LOGIN" and self.reject_auth:
            self._finish_auth(tag)
            return
        code = b"[READ-ONLY] " if verb == b"EXAMINE" else b""
        self.inbox += self.UNTAGGED.get(verb, b"") + tag + b" OK " + code + b"done\r\n"

    def _finish_auth(self, tag):
        if self.reject_auth:
            self.inbox += tag + b" NO [AUTHENTICATIONFAILED] invalid credentials\r\n"
        else:
            self.inbox += tag + b" OK authenticated\r\n"

    # server -> client
    def take(self, n):
        data = bytes(self.inbox[:n])
        del self.inbox[:n]
        return data

    def recv(self, n):
        return self.take(n)

    def makefile(self, mode):
        return _FakeFile(self)

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


def command_verb(cmd):
    tag, _, rest = cmd.partition(b" ")
    words = rest.split(b" ")
    verb = words[0].upper()
    if verb == b"UID":
        verb += b" " + words[1].upper()
    return tag, verb


def verbs(sock):
    """Command names sent so far. CAPABILITY is skipped: Python versions differ in
    whether they ask for it or take it from the greeting."""
    return [v for v in (command_verb(c)[1] for c in sock.commands) if v != b"CAPABILITY"]
