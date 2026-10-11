"""One reusable IMAP connection per account, with a per-call deadline (D13).

Locks are taken in one order only: `_pool_lock` (the slot dictionary, held for
microseconds) to find the account's slot, then, with `_pool_lock` released, the
slot's own lock for the whole operation. A slot's connection is touched only
under its lock, so no code path holds a slot lock and asks for `_pool_lock`.
"""

from __future__ import annotations

import imaplib
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TypeVar

from imap_tools import MailBox
from imap_tools.errors import UnexpectedCommandStatusError

from .accounts import Account
from .auth import CredentialProvider, CredentialUnavailable, credential_for
from .guard import ReadOnlyMailBox
from .validate import ArgumentError

T = TypeVar("T")

DEADLINE = 45.0  # seconds per account within one tool call
SOCKET_TIMEOUT = 30.0
IDLE_TIMEOUT = 120.0
_CLOSE_TIMEOUT = 5.0


class MailError(Exception):
    """An expected failure with a ready-made message (missing folder, stale id, ...)."""


class Deadline:
    """Checked before every IMAP command; also caps the socket timeout to what is left."""

    def __init__(self, seconds: float = DEADLINE, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._at = clock() + seconds

    def remaining(self) -> float:
        return self._at - self._clock()

    def __call__(self, client: imaplib.IMAP4) -> None:
        left = self.remaining()
        if left <= 0:
            raise TimeoutError("deadline exceeded")
        sock = getattr(client, "sock", None)
        if sock is not None:
            sock.settimeout(min(SOCKET_TIMEOUT, left))


def _retryable(e: Exception) -> bool:
    """A dropped connection, worth one reconnect. A timeout never is (D13)."""
    return isinstance(e, imaplib.IMAP4.abort | OSError) and not isinstance(e, TimeoutError)


def _keeps_connection(e: Exception) -> bool:
    """Errors after which the connection is still in sync: a NO/BAD reply or our own checks."""
    if isinstance(e, imaplib.IMAP4.abort):
        return False
    return isinstance(
        e, imaplib.IMAP4.error | UnexpectedCommandStatusError | ArgumentError | MailError
    )


@dataclass
class _Connection:
    box: MailBox
    fingerprint: str
    last_used: float


class _Slot:
    def __init__(self):
        self.lock = threading.Lock()
        self.conn: _Connection | None = None


def _close(box: MailBox) -> None:
    client = box.client
    client.before_command = None
    if not client.broken:
        with suppress(Exception):
            client.sock.settimeout(_CLOSE_TIMEOUT)
            box.logout()
    with suppress(Exception):
        client.shutdown()


class ConnectionPool:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        deadline: float = DEADLINE,
        idle_timeout: float = IDLE_TIMEOUT,
        janitor: bool = True,
    ):
        self._clock = clock
        self._deadline = deadline
        self._idle_timeout = idle_timeout
        self._pool_lock = threading.Lock()
        self._slots: dict[str, _Slot] = {}
        self._janitor = janitor

    def _slot(self, key: str) -> _Slot:
        with self._pool_lock:
            slot = self._slots.get(key)
            if slot is None:
                slot = self._slots[key] = _Slot()
            start_janitor = self._janitor
            self._janitor = False
        if start_janitor:
            threading.Thread(target=self._janitor_loop, name="imap-pool-idle", daemon=True).start()
        return slot

    def run(self, account: Account, op: Callable[[MailBox], T]) -> T:
        """Run `op` on the account's connection under its lock and the call deadline.

        The credential is read from its store on every call: a changed fingerprint
        (config edit, new `auth`) replaces the connection, a missing one (`forget`)
        closes it. A dropped connection is reconnected once; after a timeout the
        call fails without reconnecting.
        """
        deadline = Deadline(self._deadline, self._clock)
        slot = self._slot(account.key)
        if not slot.lock.acquire(timeout=max(deadline.remaining(), 0)):
            raise TimeoutError("account is busy")
        try:
            credential = credential_for(account)
            status = credential.offline_status()
            if status.status != "ok":
                self._drop(slot)
                raise CredentialUnavailable(status.status, f"{account.key}: {status.hint}")
            fingerprint = credential.fingerprint()
            reconnected = False
            while True:
                box = self._checkout(slot, account, credential, fingerprint, deadline)
                try:
                    return op(box)
                except Exception as e:
                    if deadline.remaining() <= 0 and not isinstance(e, TimeoutError):
                        self._drop(slot)
                        raise TimeoutError("deadline exceeded") from e
                    if not _keeps_connection(e):
                        self._drop(slot)
                    if _retryable(e) and not reconnected:
                        reconnected = True
                        continue
                    raise
        finally:
            conn = slot.conn
            if conn is not None:
                if conn.box.client.broken:
                    self._drop(slot)
                else:
                    conn.box.client.before_command = None
                    conn.last_used = self._clock()
            slot.lock.release()

    def _checkout(
        self,
        slot: _Slot,
        account: Account,
        credential: CredentialProvider,
        fingerprint: str,
        deadline: Deadline,
    ) -> MailBox:
        conn = slot.conn
        if conn is not None and (
            conn.fingerprint != fingerprint
            or conn.box.client.broken
            or self._clock() - conn.last_used > self._idle_timeout
        ):
            self._drop(slot)
            conn = None
        if conn is not None:
            conn.box.client.before_command = deadline
            try:
                conn.box.client.noop()
                return conn.box
            except TimeoutError:
                self._drop(slot)
                raise
            except Exception:  # noqa: BLE001 -- a stale connection, open a new one
                self._drop(slot)
        if deadline.remaining() <= 0:
            raise TimeoutError("deadline exceeded")
        box = ReadOnlyMailBox(
            account.host, port=account.port, timeout=min(SOCKET_TIMEOUT, deadline.remaining())
        )
        box.client.before_command = deadline
        try:
            credential.login(box)
        except BaseException:
            _close(box)
            raise
        slot.conn = _Connection(box, fingerprint, self._clock())
        return box

    @staticmethod
    def _drop(slot: _Slot) -> None:
        conn, slot.conn = slot.conn, None
        if conn is not None:
            _close(conn.box)

    def sweep(self) -> None:
        """Close connections idle longer than the idle timeout; busy slots are skipped."""
        with self._pool_lock:
            slots = list(self._slots.values())
        for slot in slots:
            if not slot.lock.acquire(blocking=False):
                continue
            try:
                conn = slot.conn
                if conn is not None and self._clock() - conn.last_used > self._idle_timeout:
                    self._drop(slot)
            finally:
                slot.lock.release()

    def close_all(self) -> None:
        with self._pool_lock:
            slots = list(self._slots.values())
        for slot in slots:
            with slot.lock:
                self._drop(slot)

    def _janitor_loop(self) -> None:
        while True:
            time.sleep(self._idle_timeout / 4)
            self.sweep()
