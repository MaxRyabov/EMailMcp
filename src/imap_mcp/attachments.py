"""Attachments: type by signature, text extraction in a child process, message cache.

The type comes from the content (spec attachment-text, D12); a generic declared
type such as application/octet-stream is ignored. PDF and DOCX are parsed by
_extract_worker.py in a spawn process per call, under a memory limit (Job Object
on Windows, RLIMIT_AS on Linux, none on macOS) and a 30 s timeout. Text
attachments are decoded here: decoding has no parser to attack.
"""

from __future__ import annotations

import multiprocessing
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from imap_tools import MailAttachment, MailMessage

from . import _extract_worker as worker
from .pool import MailError

MAX_SIZE = 15 * 1024 * 1024  # decoded bytes, checked before any parsing
TIMEOUT = 30.0
WINDOWS_MEMORY = 1024**3  # Job Object limit on the committed memory of the process

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TEXT = "text/plain"

GENERIC = frozenset(
    {"", "application/octet-stream", "application/x-download", "application/force-download"}
)
# Declared types that agree with a found signature; any other concrete type is a mismatch.
_AGREES = {
    PDF: frozenset({PDF, "application/x-pdf", "application/acrobat"}),
    DOCX: frozenset(
        {DOCX, "application/msword", "application/zip", "application/x-zip-compressed"}
    ),
}
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),  # doc, xls
    (b"Rar!\x1a\x07", "application/vnd.rar"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"\x1f\x8b", "application/gzip"),
)
_TEXT_TYPES = frozenset({"text/plain", "text/csv"})
_TEXT_SUFFIXES = (".txt", ".csv")
_NUL_WINDOW = 8 * 1024


def detect(data: bytes, declared: str, filename: str | None) -> str | None:
    """The type by content, or None if nothing is recognised.

    PDF: `%PDF-` within the first 1024 bytes. DOCX: a ZIP archive naming
    word/document.xml (the worker opens it properly). Text: declared text/plain or
    text/csv, or a .txt/.csv name, no binary signature, and a BOM or no NUL bytes
    in the first 8 KB.
    """
    if b"%PDF-" in data[:1024]:
        return PDF
    if data.startswith(b"PK\x03\x04"):
        if b"word/document.xml" in data:
            return DOCX
        return XLSX if b"xl/workbook.xml" in data else "application/zip"
    for magic, kind in _SIGNATURES:
        if data.startswith(magic):
            return kind
    textual = declared in _TEXT_TYPES or (filename or "").lower().endswith(_TEXT_SUFFIXES)
    if textual and (worker.bom(data) or b"\x00" not in data[:_NUL_WINDOW]):
        return TEXT
    return None


@dataclass(frozen=True)
class Check:
    detected: str | None
    reason: str | None = None  # None: the text can be extracted
    detail: str = ""

    @property
    def error(self) -> str:
        return f"{self.reason}: {self.detail}"


def check(att: MailAttachment) -> Check:
    data, declared = att.payload, att.content_type
    detected = detect(data, declared, att.filename)
    if detected in _AGREES:
        if declared not in GENERIC and declared not in _AGREES[detected]:
            return Check(detected, "type mismatch", f"заявлен {declared}, определён {detected}")
    elif detected != TEXT:
        found = detected or "не распознан"
        return Check(detected, "unsupported type", f"{found} (заявлен {declared or 'пустой'})")
    if len(data) > MAX_SIZE:
        return Check(detected, "too large", f"{len(data)} байт, предел {MAX_SIZE}")
    return Check(detected)


def metadata(msg: MailMessage) -> list[dict]:
    """The `attachments` of get_email; contents are never included."""
    out = []
    for i, att in enumerate(msg.attachments):
        verdict = check(att)
        row = {
            "index": i,
            "filename": att.filename,
            "content_type": att.content_type,
            "size": att.size,
            "inline": att.content_disposition == "inline" or bool(att.content_id),
            "extractable": verdict.reason is None,
        }
        if verdict.reason:
            row["reason"] = verdict.reason
        out.append(row)
    return out


def extract(
    att: MailAttachment, *, timeout: float = TIMEOUT, target: Callable = worker.main
) -> dict:
    """{text, detected_type, [pages_total, pages_read], [note]} or MailError."""
    verdict = check(att)
    if verdict.reason:
        raise MailError(verdict.error)
    if verdict.detected == TEXT:
        result = {"text": worker.decode_text(att.payload, att.part.get_content_charset())}
    else:
        kind = "pdf" if verdict.detected == PDF else "docx"
        result = run({"kind": kind, "data": att.payload}, timeout=timeout, target=target)
    result["detected_type"] = verdict.detected
    return result


# --- the child process ---------------------------------------------------------------


def run(job: dict, *, timeout: float = TIMEOUT, target: Callable = worker.main) -> dict:
    """Run `target` on `job` in a fresh spawn process; kill it after `timeout`.

    The child waits for the job before doing anything, so on Windows the Job
    Object is assigned first. The answer is awaited with poll(), not join(): a
    child blocked on sending a long text would never exit while nobody reads.
    """
    deadline = time.monotonic() + timeout
    ctx = multiprocessing.get_context("spawn")
    here, there = ctx.Pipe()
    proc = ctx.Process(target=target, args=(there,), daemon=True, name="imap-mcp-extract")
    proc.start()
    there.close()
    job_object = None
    try:
        if sys.platform == "win32":
            job_object = _JobObject(proc.sentinel, WINDOWS_MEMORY)
        here.send(job)
        if not here.poll(max(0.0, deadline - time.monotonic())):
            raise MailError(f"timeout: разбор вложения дольше {timeout:g} с, процесс остановлен")
        result = here.recv()
    except (EOFError, BrokenPipeError, ConnectionResetError):
        result = None  # the child died without an answer
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join()
        here.close()
        if job_object is not None:
            job_object.close()
    if result is None:
        raise MailError(f"parse error: процесс разбора завершился с кодом {proc.exitcode}")
    if "error" in result:
        raise MailError(result["error"])
    return result


class _JobObject:
    """A Windows Job Object limiting the committed memory of one process.

    Over the limit, allocations in the child fail and Python raises MemoryError.
    KILL_ON_JOB_CLOSE also ends the child if the server dies without cleaning up.
    """

    _EXTENDED_LIMIT_INFORMATION = 9
    _LIMIT_PROCESS_MEMORY = 0x100
    _LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    def __init__(self, process_handle: int, limit: int):
        import ctypes

        u64, u32, size_t = ctypes.c_uint64, ctypes.c_uint32, ctypes.c_size_t

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", u32),
                ("MinimumWorkingSetSize", size_t),
                ("MaximumWorkingSetSize", size_t),
                ("ActiveProcessLimit", u32),
                ("Affinity", size_t),
                ("PriorityClass", u32),
                ("SchedulingClass", u32),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic),
                ("IoInfo", u64 * 6),
                ("ProcessMemoryLimit", size_t),
                ("JobMemoryLimit", size_t),
                ("PeakProcessMemoryUsed", size_t),
                ("PeakJobMemoryUsed", size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle_t = ctypes.c_void_p
        k32.CreateJobObjectW.restype = handle_t
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.SetInformationJobObject.argtypes = [handle_t, ctypes.c_int, ctypes.c_void_p, u32]
        k32.AssignProcessToJobObject.argtypes = [handle_t, handle_t]
        k32.CloseHandle.argtypes = [handle_t]
        self._k32 = k32
        self._handle = k32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = Extended()
            info.BasicLimitInformation.LimitFlags = (
                self._LIMIT_PROCESS_MEMORY | self._LIMIT_KILL_ON_JOB_CLOSE
            )
            info.ProcessMemoryLimit = limit
            set_info = k32.SetInformationJobObject(
                self._handle,
                self._EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not set_info or not k32.AssignProcessToJobObject(self._handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._handle:
            self._k32.CloseHandle(self._handle)
            self._handle = None


# --- message cache ---------------------------------------------------------------------


class MessageCache:
    """The last two fully downloaded messages, for 5 minutes, in memory only.

    So get_attachment_text after get_email does not download the message again.
    Key: (account, folder, uidvalidity, uid). Messages over 25 MB are not kept.
    """

    def __init__(
        self,
        size: int = 2,
        ttl: float = 300.0,
        max_bytes: int = 25 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.size, self.ttl, self.max_bytes, self.clock = size, ttl, max_bytes, clock
        self._items: OrderedDict[tuple, tuple[float, MailMessage]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> MailMessage | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            if self.clock() >= item[0]:
                del self._items[key]
                return None
            return item[1]

    def put(self, key: tuple, msg: MailMessage, size: int) -> None:
        if size > self.max_bytes:
            return
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = (self.clock() + self.ttl, msg)
            while len(self._items) > self.size:
                self._items.popitem(last=False)
