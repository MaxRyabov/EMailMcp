"""Attachments (spec attachment-text): type by signature, extraction in a child
process, limits, the message cache and the get_attachment_text tool."""

import multiprocessing
import os
import sys
import time

import pytest
import workers
from docfixtures import document_xml, make_docx, make_pdf, zip_bomb
from fakemail import make_message
from imap_tools import MailMessage
from mcp.server.mcpserver.exceptions import ToolError

from imap_mcp import _extract_worker, attachments, server
from imap_mcp.attachments import DOCX, PDF, TEXT, MessageCache
from imap_mcp.pool import MailError

OCTET = "application/octet-stream"


def attachment(name, ctype, data, charset=None):
    params = [{"charset": charset}] if charset else []
    raw = make_message(attachments=[(name, ctype, data, False, *params)])
    return MailMessage.from_bytes(raw).attachments[0]


def deliver(mail, *atts, size=None):
    """Put a message with `atts` in alpha's INBOX; return its id from list_emails."""
    mail["alpha"].add(make_message(attachments=[(*a[:3], False, *a[3:]) for a in atts]), size=size)
    return server.list_emails(account="alpha")["rows"][0]["id"]


def full_fetches(mail):
    return sum(b"BODY.PEEK[]" in c for c in mail["alpha"].fetches)


@pytest.fixture
def no_child(monkeypatch):
    """Fail the test if anything tries to start a parsing process."""

    def refuse(*args, **kwargs):
        raise AssertionError("a child process was started")

    monkeypatch.setattr(attachments, "run", refuse)


@pytest.fixture(scope="session")
def bomb():
    return zip_bomb()


# --- 5.2 type by signature -----------------------------------------------------------


def test_pdf_sent_as_octet_stream_is_pdf():
    att = attachment("act.pdf", OCTET, make_pdf(["Act"]))
    assert attachments.check(att) == attachments.Check(PDF)


def test_pdf_header_after_300_bytes_of_junk_is_pdf():
    data = b"\x07" * 300 + make_pdf(["Act"])
    assert attachments.detect(data, OCTET, "scan.bin") == PDF


def test_pdf_header_beyond_1024_bytes_is_not_pdf():
    data = b"\x07" * 1100 + make_pdf(["Act"])
    assert attachments.detect(data, OCTET, "scan.bin") is None


@pytest.mark.parametrize(
    "declared", [OCTET, "application/x-download", "application/force-download"]
)
def test_generic_declared_types_do_not_matter(declared):
    assert attachments.check(attachment("d.docx", declared, make_docx(["x"]))).reason is None
    assert attachments.check(attachment("logo.png", declared, make_pdf(["x"]))).reason is None


def test_zip_declared_for_docx_agrees():
    assert (
        attachments.check(attachment("d.docx", "application/zip", make_docx(["x"]))).reason is None
    )


def test_concrete_declared_type_against_signature_is_a_mismatch():
    verdict = attachments.check(attachment("logo.png", "image/png", make_pdf(["x"])))
    assert verdict.reason == "type mismatch"
    assert "image/png" in verdict.error and PDF in verdict.error


@pytest.mark.parametrize(
    ("name", "declared", "data", "detected"),
    [
        ("table.xlsx", OCTET, b"PK\x03\x04....xl/workbook.xml....", attachments.XLSX),
        ("logo.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"\0" * 20, "image/png"),
        ("data.bin", OCTET, b"\x01\x02\x03", None),
        (
            "old.doc",
            "application/msword",
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 8,
            "application/x-ole-storage",
        ),  # noqa: E501
    ],
)
def test_unsupported_types(name, declared, data, detected):
    verdict = attachments.check(attachment(name, declared, data))
    assert verdict.reason == "unsupported type"
    assert verdict.detected == detected


def test_text_needs_text_type_or_name():
    assert attachments.detect(b"plain words", OCTET, "notes.txt") == TEXT
    assert attachments.detect(b"plain words", "text/csv", None) == TEXT
    assert attachments.detect(b"plain words", OCTET, "notes.bin") is None


def test_text_with_nul_bytes_is_binary_unless_it_has_a_bom():
    utf16 = "a;b\r\n".encode("utf-16-le")
    assert attachments.detect(utf16, "text/csv", "t.csv") is None
    assert attachments.detect(b"\xff\xfe" + utf16, "text/csv", "t.csv") == TEXT
    assert attachments.detect(b"x" * 9000 + b"\0", "text/plain", "late.txt") == TEXT


def test_text_with_a_binary_signature_is_not_text():
    assert attachments.detect(b"\x89PNG\r\n\x1a\nzz", "text/plain", "x.txt") == "image/png"


def test_too_large_is_refused_by_size(no_child):
    att = attachment("big.pdf", PDF, b"%PDF-1.4\n" + b"0" * (15 * 1024 * 1024))
    verdict = attachments.check(att)
    assert verdict.reason == "too large"
    with pytest.raises(MailError, match="^too large"):
        attachments.extract(att)


# --- 5.3 the worker's parsers, in this process -----------------------------------------


def test_pdf_text_and_page_counts():
    result = _extract_worker.pdf_text(make_pdf(["Invoice 42", "Total 500"]))
    assert result["text"] == "Invoice 42\n\nTotal 500"
    assert (result["pages_total"], result["pages_read"]) == (2, 2)
    assert result["note"] == ""


def test_pdf_reads_at_most_50_pages():
    result = _extract_worker.pdf_text(make_pdf([f"page {i}" for i in range(52)]))
    assert (result["pages_total"], result["pages_read"]) == (52, 50)
    assert "page 49" in result["text"] and "page 50" not in result["text"]


def test_scan_without_text_layer():
    result = _extract_worker.pdf_text(make_pdf([None, None]))
    assert result["text"] == ""
    assert result["note"] == "no text layer, OCR not supported"


def test_owner_password_aes_pdf_is_read_with_a_note():
    data = make_pdf(["Statement 7"], owner_password="bank")
    assert b"/AESV2" in data  # AES-128
    result = _extract_worker.pdf_text(data)
    assert result["text"] == "Statement 7"
    assert result["note"] == "owner-password protected"


def test_user_password_pdf_is_encrypted():
    with pytest.raises(_extract_worker.ExtractError, match="^encrypted"):
        _extract_worker.pdf_text(make_pdf(["Secret"], user_password="open", owner_password="x"))


def test_docx_paragraphs_and_tables_in_document_order():
    data = make_docx(["Договор №5", [["Товар", "Цена"], ["Стол", "500"]], "Подписи"])
    text = _extract_worker.docx_text(data)["text"]
    assert text == "Договор №5\nТовар\tЦена\nСтол\t500\nПодписи"


def test_docx_with_external_entity_is_unsafe_xml():
    xml = document_xml(
        "<w:p><w:r><w:t>&xxe;</w:t></w:r></w:p>",
        '<!DOCTYPE d [<!ENTITY xxe SYSTEM "http://attacker.invalid/x">]>',
    )
    with pytest.raises(_extract_worker.ExtractError, match="^unsafe xml"):
        _extract_worker.docx_text(make_docx(xml=xml))


def test_zip_bomb_stops_at_the_unpacked_limit(bomb):
    assert len(bomb) < 3 * 1024 * 1024
    started = time.monotonic()
    with pytest.raises(_extract_worker.ExtractError, match="^too large"):
        _extract_worker.docx_text(bomb)
    assert time.monotonic() - started < 10


def test_cp1251_text_without_charset():
    assert _extract_worker.decode_text("Счёт на оплату".encode("cp1251")) == "Счёт на оплату"


def test_utf8_is_tried_before_cp1251():
    assert _extract_worker.decode_text("Счёт".encode()) == "Счёт"


def test_bom_wins_over_charset():
    data = b"\xff\xfe" + "Сумма;500\r\n".encode("utf-16-le")
    assert _extract_worker.decode_text(data, "koi8-r") == "Сумма;500\n"


def test_charset_from_mime_part_is_used():
    assert _extract_worker.decode_text("Привет".encode("koi8-r"), "koi8-r") == "Привет"
    assert _extract_worker.decode_text("Привет".encode("cp1251"), "x-unknown") == "Привет"


@pytest.mark.parametrize("charset", ["base64", "rot13", "hex", "zlib"])
def test_non_text_codec_as_charset_falls_back(charset):
    assert _extract_worker.decode_text("Привет".encode("cp1251"), charset) == "Привет"


@pytest.mark.parametrize("encoding", ["utf-32-le", "utf-32-be"])
def test_utf32_bom_is_not_taken_for_utf16(encoding):
    data = "﻿Сумма;500".encode(encoding)
    assert _extract_worker.decode_text(data) == "Сумма;500"


# --- 5.4 the child process -----------------------------------------------------------


def test_pdf_and_docx_are_parsed_by_run(monkeypatch):
    jobs = []
    real_run = attachments.run

    def spy(job, **kwargs):
        jobs.append(job["kind"])
        return real_run(job, **kwargs)

    monkeypatch.setattr(attachments, "run", spy)
    assert attachments.extract(attachment("a.pdf", PDF, make_pdf(["A"])))["text"] == "A"
    assert attachments.extract(attachment("b.docx", DOCX, make_docx(["B"])))["text"] == "B"
    assert jobs == ["pdf", "docx"]


def test_run_executes_in_another_process():
    child = int(attachments.run({}, target=workers.pid)["text"])
    assert child != os.getpid()
    assert attachments.run({"kind": "pdf", "data": make_pdf(["Invoice 42"])})["text"] == (
        "Invoice 42"
    )


def test_worker_does_not_import_server_mcp_or_imap():
    loaded = set(attachments.run({}, target=workers.modules)["text"].split("\n"))
    assert "imap_mcp._extract_worker" in loaded
    forbidden = {
        "mcp",
        "imap_tools",
        "imaplib",
        "imap_mcp.server",
        "imap_mcp.imap",
        "imap_mcp.pool",
    }
    assert not loaded & forbidden


def test_timeout_kills_the_child_and_the_next_call_runs_at_once():
    started = time.monotonic()
    with pytest.raises(MailError, match="^timeout"):
        attachments.run({"seconds": 60}, target=workers.sleepy, timeout=2)
    assert time.monotonic() - started < 10
    assert multiprocessing.active_children() == []
    started = time.monotonic()
    assert attachments.run({"kind": "pdf", "data": make_pdf(["next"])})["text"] == "next"
    assert time.monotonic() - started < 10


def test_timeout_holds_when_the_child_never_reads_a_large_job():
    started = time.monotonic()
    with pytest.raises(MailError, match="^timeout"):
        attachments.run({"data": b"x" * (15 * 1024 * 1024)}, target=workers.deaf, timeout=2)
    assert time.monotonic() - started < 10
    assert multiprocessing.active_children() == []


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object exists only on Windows")
def test_job_object_failure_is_a_tool_error(monkeypatch):
    def refuse(*args):
        raise OSError("access denied")

    monkeypatch.setattr(attachments, "_JobObject", refuse)
    with pytest.raises(MailError, match="^isolation error: .*access denied"):
        attachments.run({}, target=workers.pid)
    assert multiprocessing.active_children() == []


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS does not enforce RLIMIT_AS")
def test_memory_limit():
    over = 3 * 1024**3 if sys.platform.startswith("linux") else 3 * 1024**3 // 2
    with pytest.raises(MailError, match="^memory limit"):
        attachments.run({"bytes": over}, target=workers.hungry)
    within = 64 * 1024 * 1024
    assert attachments.run({"bytes": within}, target=workers.hungry)["text"] == str(within)


def test_worker_output_never_reaches_the_parent_streams(capfd):
    assert attachments.run({}, target=workers.noisy)["text"] == "printed"
    out, err = capfd.readouterr()
    assert out == "" and err == ""


def test_child_dying_without_answer_is_an_error():
    with pytest.raises(MailError, match="^parse error: .*3"):
        attachments.run({}, target=workers.crashing)


def test_broken_pdf_is_an_error_not_a_crash():
    with pytest.raises(MailError, match="^parse error"):
        attachments.run({"kind": "pdf", "data": b"%PDF-1.4\nnot really"})


# --- 5.5 cache -----------------------------------------------------------------------


def test_cache_keeps_two_messages_for_five_minutes():
    now = [0.0]
    cache = MessageCache(clock=lambda: now[0])
    for n in (1, 2, 3):
        cache.put(("a", "INBOX", 7, n), f"m{n}", 100)
    assert cache.get(("a", "INBOX", 7, 1)) is None
    assert cache.get(("a", "INBOX", 7, 3)) == "m3"
    now[0] = 299.0
    assert cache.get(("a", "INBOX", 7, 2)) == "m2"
    now[0] = 300.0
    assert cache.get(("a", "INBOX", 7, 2)) is None


def test_cache_skips_messages_over_25_mb():
    cache = MessageCache()
    cache.put(("a", "INBOX", 7, 1), "big", 25 * 1024 * 1024 + 1)
    assert cache.get(("a", "INBOX", 7, 1)) is None


# --- 5.6 get_attachment_text ---------------------------------------------------------


def test_text_of_a_pdf_invoice(mail):
    mid = deliver(mail, ("invoice.pdf", PDF, make_pdf(["Invoice 42", "Total 500 RUB"])))
    out = server.get_attachment_text(mid, 0)
    assert out["text"] == "Invoice 42\n\nTotal 500 RUB"
    assert out["pages_total"] == 2 and out["pages_read"] == 2
    assert out["content_type"] == PDF and out["detected_type"] == PDF
    assert out["truncated"] is False and out["next_offset"] is None
    assert "note" not in out


def test_docx_contract_is_read_in_parts(mail):
    lines = [f"{i:08d} " + "д" * 81 for i in range(1000)]  # about 90 000 characters
    text = "\n".join(lines)
    assert len(text) == 90_999
    mid = deliver(mail, ("contract.docx", DOCX, make_docx(lines)))
    first = server.get_attachment_text(mid, 0)
    assert first["text"] == text[:20_000] and first["text_chars"] == len(text)
    assert first["truncated"] is True and first["next_offset"] == 20_000
    second = server.get_attachment_text(mid, 0, offset=first["next_offset"])
    assert second["text"] == text[20_000:40_000] and second["next_offset"] == 40_000


def test_utf16_csv_from_excel(mail, no_child):
    data = b"\xff\xfe" + "Имя\tСумма\r\nИван\t500\r\n".encode("utf-16-le")
    mid = deliver(mail, ("export.csv", "text/csv", data))
    out = server.get_attachment_text(mid, 0)
    assert out["text"] == "Имя\tСумма\nИван\t500\n"
    assert out["detected_type"] == TEXT


def test_cp1251_txt_without_charset(mail, no_child):
    mid = deliver(mail, ("письмо.txt", OCTET, "Акт сверки за октябрь".encode("cp1251")))
    assert server.get_attachment_text(mid, 0)["text"] == "Акт сверки за октябрь"


def test_txt_charset_from_the_mime_part(mail, no_child):
    mid = deliver(
        mail, ("note.txt", "text/plain", "Привет".encode("koi8-r"), {"charset": "koi8-r"})
    )
    assert server.get_attachment_text(mid, 0)["text"] == "Привет"


def test_png_with_pdf_content_is_a_type_mismatch(mail, no_child):
    mid = deliver(mail, ("photo.png", "image/png", make_pdf(["x"])))
    with pytest.raises(ToolError, match="type mismatch") as e:
        server.get_attachment_text(mid, 0)
    assert "image/png" in str(e.value) and PDF in str(e.value)


@pytest.mark.parametrize(
    ("name", "ctype", "data"),
    [
        ("table.xlsx", attachments.XLSX, b"PK\x03\x04....xl/workbook.xml...."),
        ("logo.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"\0" * 20),
    ],
)
def test_unsupported_type_is_not_parsed(mail, no_child, name, ctype, data):
    mid = deliver(mail, (name, ctype, data))
    with pytest.raises(ToolError, match="unsupported type") as e:
        server.get_attachment_text(mid, 0)
    assert ctype in str(e.value)


def test_zip_bomb_docx_is_too_large_and_the_server_keeps_working(mail, bomb):
    mid = deliver(mail, ("contract.docx", DOCX, bomb), ("note.txt", "text/plain", b"ok"))
    with pytest.raises(ToolError, match="too large"):
        server.get_attachment_text(mid, 0)
    assert server.get_attachment_text(mid, 1)["text"] == "ok"


def test_index_out_of_range_names_the_count(mail):
    mid = deliver(mail, ("a.txt", "text/plain", b"a"), ("b.txt", "text/plain", b"b"))
    with pytest.raises(ToolError, match="вложений в письме 2"):
        server.get_attachment_text(mid, 2)


def test_message_without_attachments(mail):
    mail["alpha"].add(make_message())
    mid = server.list_emails(account="alpha")["rows"][0]["id"]
    with pytest.raises(ToolError, match="вложений в письме 0"):
        server.get_attachment_text(mid, 0)


def test_negative_index_is_an_argument_error(mail):
    mid = deliver(mail, ("a.txt", "text/plain", b"a"))
    with pytest.raises(ToolError, match="index"):
        server.get_attachment_text(mid, -1)


def test_three_attachments_one_download(mail):
    mid = deliver(
        mail,
        ("a.txt", "text/plain", b"first"),
        ("b.pdf", PDF, make_pdf(["second"])),
        ("c.txt", "text/plain", b"third"),
    )
    before = full_fetches(mail)  # list_emails downloads small messages for snippets
    server.get_email(mid)
    texts = [server.get_attachment_text(mid, i)["text"] for i in range(3)]
    assert texts == ["first", "second", "third"]
    assert full_fetches(mail) - before == 1


def test_message_is_downloaded_again_after_five_minutes(mail):
    mid = deliver(mail, ("a.txt", "text/plain", b"first"))
    before = full_fetches(mail)
    server.get_attachment_text(mid, 0)
    server.get_attachment_text(mid, 0)
    assert full_fetches(mail) - before == 1
    mail.clock.advance(301)
    server.get_attachment_text(mid, 0)
    assert full_fetches(mail) - before == 2


def test_message_over_40_mb_has_no_attachment_text(mail):
    mid = deliver(mail, ("a.txt", "text/plain", b"a"), size=45 * 1024 * 1024)
    with pytest.raises(ToolError, match="больше 40 МБ"):
        server.get_attachment_text(mid, 0)
    assert full_fetches(mail) == 0
