"""Attachment text extraction, run in a child process per call (D12).

This module imports only the standard library, pypdf and defusedxml: never the
server, mcp or the IMAP layer. The parent (attachments.py) starts `main` with the
spawn method, puts the process under a memory limit and only then sends the job,
so no parsing happens before the limit is in place. The limit exists on Windows
(Job Object) and Linux (RLIMIT_AS); macOS does not enforce RLIMIT_AS, so there
only the 15 MB input limit and the 30 s timeout apply (spec attachment-text).

decode_text() and bom() are plain functions: the parent decodes text attachments
itself, without a child process.
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from collections.abc import Callable, Iterator

from defusedxml import DefusedXmlException
from defusedxml import ElementTree as SafeET
from pypdf import PasswordType, PdfReader

PDF_MAX_PAGES = 50
DOCX_XML_MAX = 50 * 1024 * 1024  # unpacked bytes of the XML parts read
LINUX_ADDRESS_SPACE = 2 * 1024**3  # RLIMIT_AS counts mapped libraries and stacks too
ERROR_MAX = 300

NO_TEXT_LAYER = "no text layer, OCR not supported"
OWNER_PASSWORD = "owner-password protected"

_BOMS = (  # UTF-32 first: its little-endian BOM starts with the UTF-16 one
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


class ExtractError(Exception):
    """An expected refusal; its text is the tool error and starts with the reason."""


# --- the child process --------------------------------------------------------------


def _silence() -> None:
    """Send stdout and stderr to devnull, the descriptors 1 and 2 included.

    On Linux the child inherits the parent's descriptors, and the parent's stdout
    is the MCP stream: a stray print from a library would corrupt it.
    """
    devnull = open(os.devnull, "w")  # noqa: SIM115 -- lives as long as the process
    for fd in (1, 2):
        os.dup2(devnull.fileno(), fd)
    sys.stdout = sys.stderr = devnull


def _limit_memory() -> None:
    # Windows: the parent assigns a Job Object. macOS does not enforce RLIMIT_AS.
    if not sys.platform.startswith("linux"):
        return
    import resource

    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = LINUX_ADDRESS_SPACE
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)  # raising the hard limit needs privileges
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (OSError, ValueError) as e:
        raise ExtractError(f"isolation error: не удалось ограничить память: {e}") from None


def serve(conn, handler: Callable[[dict], dict]) -> None:
    """Child entry: silence output, wait for the job, limit memory, answer once.

    The job itself is the go signal: the parent sends it after the Job Object is
    assigned. RLIMIT_AS is set after the job arrives, still before any parsing,
    so a failure to set it reaches the parent as an answer. Every failure becomes
    {"error": text}, so the parent never has to tell a refusal from a crash by
    the exit code.
    """
    _silence()
    try:
        job = conn.recv()
        try:
            _limit_memory()
            result = handler(job)
        except ExtractError as e:
            result = {"error": str(e)}
        except MemoryError:
            result = {"error": "memory limit: разбор превысил лимит памяти"}
        except Exception as e:  # noqa: BLE001 -- a broken file must not look like a crash
            result = {"error": f"parse error: {type(e).__name__}: {e}"[:ERROR_MAX]}
        conn.send(result)
    finally:
        conn.close()


def main(conn) -> None:
    serve(conn, extract)


def extract(job: dict) -> dict:
    kind, data = job["kind"], job["data"]
    if kind == "pdf":
        return pdf_text(data)
    if kind == "docx":
        return docx_text(data)
    raise ExtractError(f"unsupported type: {kind}")


# --- PDF -------------------------------------------------------------------------------


def pdf_text(data: bytes) -> dict:
    """Text of the first 50 pages; a PDF locked only by an owner password is read."""
    reader = PdfReader(io.BytesIO(data))
    notes = []
    if reader.is_encrypted:
        if reader.decrypt("") == PasswordType.NOT_DECRYPTED:
            raise ExtractError("encrypted: PDF открывается только с паролем")
        notes.append(OWNER_PASSWORD)
    total = len(reader.pages)
    read = min(total, PDF_MAX_PAGES)
    pages, broken, first_error = [], [], None
    for i in range(read):
        try:
            pages.append((reader.pages[i].extract_text() or "").strip())
        except MemoryError:
            raise
        except Exception as e:  # noqa: BLE001 -- one damaged page must not hide the rest
            broken.append(str(i + 1))
            first_error = first_error or e
    if read and len(broken) == read:
        raise ExtractError(f"parse error: {type(first_error).__name__}: {first_error}"[:ERROR_MAX])
    text = "\n\n".join(p for p in pages if p)
    if broken:
        notes.append(f"unreadable pages: {', '.join(broken)}")
    elif not text:
        notes.append(NO_TEXT_LAYER)
    return {"text": text, "pages_total": total, "pages_read": read, "note": "; ".join(notes)}


# --- DOCX ------------------------------------------------------------------------------


def _read_limited(zf: zipfile.ZipFile, name: str, budget: int) -> bytes:
    """Unpack `name`, counting the bytes actually produced, not the header's claim."""
    chunks, total = [], 0
    with zf.open(name) as f:
        while chunk := f.read(1 << 20):
            total += len(chunk)
            if total > budget:
                raise ExtractError(
                    f"too large: {name} больше {budget // 2**20} МБ после распаковки"
                )
            chunks.append(chunk)
    return b"".join(chunks)


def docx_text(data: bytes) -> dict:
    """Paragraphs and tables of word/document.xml in document order."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ExtractError("parse error: DOCX не является ZIP-архивом") from None
    with zf:
        if "word/document.xml" not in zf.namelist():
            raise ExtractError("parse error: в DOCX нет word/document.xml")
        if zf.getinfo("word/document.xml").flag_bits & 0x1:  # ZIP encryption
            raise ExtractError("encrypted: DOCX защищён паролем")
        xml = _read_limited(zf, "word/document.xml", DOCX_XML_MAX)
    try:
        root = SafeET.fromstring(xml, forbid_dtd=True)
    except DefusedXmlException:
        raise ExtractError("unsafe xml: DTD или сущности в word/document.xml") from None
    except SafeET.ParseError as e:
        raise ExtractError(f"parse error: word/document.xml: {e}") from None
    body = root.find(f"{_W}body")
    return {"text": "\n".join(_blocks(body)) if body is not None else ""}


def _blocks(elem) -> Iterator[str]:
    for child in elem:
        if child.tag == f"{_W}p":
            yield _paragraph(child)
        elif child.tag == f"{_W}tbl":
            yield _table(child)
        elif child.tag != _MC_FALLBACK:  # sdt, customXml, insertions hold paragraphs
            yield from _blocks(child)


def _runs(elem) -> Iterator:
    """Runs in document order, those of text boxes included, without mc:Fallback.

    Word saves a text box twice, as mc:Choice (DrawingML) and mc:Fallback (VML):
    reading both would repeat its text.
    """
    for child in elem:
        if child.tag == f"{_W}r":
            yield child
        if child.tag != _MC_FALLBACK:
            yield from _runs(child)


def _paragraph(p) -> str:
    # Runs only: w:tab inside w:pPr is a tab stop definition, not a character.
    parts = []
    for run in _runs(p):
        for el in run:
            if el.tag == f"{_W}t":
                parts.append(el.text or "")
            elif el.tag == f"{_W}tab":
                parts.append("\t")
            elif el.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
    return "".join(parts)


def _table(tbl) -> str:
    # Direct children only: a nested table comes out inside its cell.
    rows = []
    for tr in tbl.findall(f"{_W}tr"):
        cells = [" ".join(_blocks(tc)).replace("\n", " ") for tc in tr.findall(f"{_W}tc")]
        rows.append("\t".join(cells))
    return "\n".join(rows)


# --- text ------------------------------------------------------------------------------


def bom(data: bytes) -> tuple[bytes, str] | None:
    for mark, encoding in _BOMS:
        if data.startswith(mark):
            return mark, encoding
    return None


def decode_text(data: bytes, charset: str | None = None) -> str:
    """BOM first, then the MIME charset, then UTF-8, then cp1251 with replacement."""
    found = bom(data)
    if found:
        mark, encoding = found
        text = data[len(mark) :].decode(encoding, errors="replace")
    elif charset and _known(charset):
        text = data.decode(charset, errors="replace")
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("cp1251", errors="replace")
    return text.replace("\r\n", "\n")


def _known(charset: str) -> bool:
    # The sender picks the charset: base64, rot13 and other non-text codecs
    # cannot decode bytes to str and fall through to UTF-8 and cp1251.
    try:
        b"a".decode(charset, errors="replace")  # empty bytes skip the codec lookup
    except LookupError:
        return False
    return True
