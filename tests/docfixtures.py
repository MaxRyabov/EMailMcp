"""Attachment fixtures built at test time: PDF (pypdf), DOCX, a ZIP bomb."""

from __future__ import annotations

import io
import struct
import zipfile
import zlib
from xml.sax.saxutils import escape

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _name(value: str) -> NameObject:
    return NameObject(value)


def _image(writer: PdfWriter):
    """A 2x2 grey image XObject: a page of a scan carries pixels, not text."""
    img = DecodedStreamObject()
    img.set_data(b"\x80" * 4)
    img.update(
        {
            _name("/Type"): _name("/XObject"),
            _name("/Subtype"): _name("/Image"),
            _name("/Width"): NumberObject(2),
            _name("/Height"): NumberObject(2),
            _name("/ColorSpace"): _name("/DeviceGray"),
            _name("/BitsPerComponent"): NumberObject(8),
        }
    )
    return writer._add_object(img)


def make_pdf(
    pages: list[str | None],
    user_password: str | None = None,
    owner_password: str | None = None,
) -> bytes:
    """One page per item: ASCII text in Helvetica, or a scanned image for None.

    With an owner password and an empty user password the PDF opens without a
    password but is encrypted (AES-128), like a bank statement that forbids printing.
    """
    writer = PdfWriter()
    font = DictionaryObject(
        {
            _name("/Type"): _name("/Font"),
            _name("/Subtype"): _name("/Type1"),
            _name("/BaseFont"): _name("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    for text in pages:
        page = writer.add_blank_page(612, 792)
        content = DecodedStreamObject()
        if text is None:
            content.set_data(b"q 500 0 0 700 50 50 cm /Im1 Do Q")
            resources = {_name("/XObject"): DictionaryObject({_name("/Im1"): _image(writer)})}
        else:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
            resources = {_name("/Font"): DictionaryObject({_name("/F1"): font_ref})}
        page[_name("/Contents")] = writer._add_object(content)
        page[_name("/Resources")] = DictionaryObject(resources)
        page[_name("/MediaBox")] = ArrayObject([NumberObject(n) for n in (0, 0, 612, 792)])
    if user_password is not None or owner_password is not None:
        writer.encrypt(
            user_password=user_password or "",
            owner_password=owner_password,
            algorithm="AES-128",
        )
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _paragraph(text: str) -> str:
    return f'<w:p><w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'  # noqa: E501


def _table(rows: list[list[str]]) -> str:
    cells = "".join(
        "<w:tr>" + "".join(f"<w:tc>{_paragraph(c)}</w:tc>" for c in row) + "</w:tr>" for row in rows
    )
    return f"<w:tbl>{cells}</w:tbl>"


def document_xml(body: str, prolog: str = "") -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>{prolog}'
        f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'
    ).encode()


def make_docx(blocks: list[str | list[list[str]]] | None = None, xml: bytes | None = None) -> bytes:
    """A minimal DOCX: paragraphs (str) and tables (list of rows) in order."""
    if xml is None:
        body = "".join(_table(b) if isinstance(b, list) else _paragraph(b) for b in blocks or [])
        xml = document_xml(body)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        zf.writestr("word/document.xml", xml)
    return out.getvalue()


def zip_bomb(unpacked: int = 2 * 1024**3, name: str = "word/document.xml") -> bytes:
    """A ZIP whose `name` unpacks to `unpacked` zero bytes (2 GB from about 2 MB).

    zipfile would spend seconds deflating 2 GB, so one 16 MB block is compressed
    once and repeated: after Z_FULL_FLUSH no block refers to the one before it.
    """
    chunk = b"\0" * (1 << 24)
    count = unpacked // len(chunk)
    deflater = zlib.compressobj(9, zlib.DEFLATED, -15)
    block = deflater.compress(chunk) + deflater.flush(zlib.Z_FULL_FLUSH)
    data = block * count + zlib.compressobj(9, zlib.DEFLATED, -15).flush()  # final block
    crc = 0
    for _ in range(count):
        crc = zlib.crc32(chunk, crc)
    raw_name = name.encode()
    size = count * len(chunk)
    local = struct.pack(
        "<4sHHHHHIIIHH", b"PK\x03\x04", 20, 0, 8, 0, 0, crc, len(data), size, len(raw_name), 0
    )
    central = struct.pack(
        "<4sHHHHHHIIIHHHHHII",
        b"PK\x01\x02", 20, 20, 0, 8, 0, 0, crc, len(data), size, len(raw_name),
        0, 0, 0, 0, 0, 0,
    )  # fmt: skip
    head = local + raw_name + data
    directory = central + raw_name
    end = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 1, 1, len(directory), len(head), 0)
    return head + directory + end
