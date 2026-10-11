"""The mail tools end to end: server -> pool -> guard -> imaplib -> fake IMAP server."""

import asyncio

import pytest
from fakeimap import command_verb
from fakemail import make_message
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from imap_mcp import accounts, ids, server

SEEN = ("\\Seen",)


def call(name, arguments=None):
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(name, arguments or {})

    return asyncio.run(run())


def date(day, hour=10, zone="+0000"):
    return f"Mon, {day:02d} Oct 2026 {hour:02d}:00:00 {zone}"


# --- 4.4 answer shape -------------------------------------------------------------


def test_list_answer_shape_passes_output_schema_with_one_account_down(mail):
    mail["gmailish"].add(make_message(subject="beta"))
    mail["alpha"].delay = 60  # alpha hangs
    result = call("list_emails")
    assert not result.is_error
    out = result.structured_content
    assert set(out) == {"rows", "errors", "limit_capped", "truncated"}
    assert [r["subject"] for r in out["rows"]] == ["beta"]
    assert out["errors"] == [{"account": "alpha", "error": "unreachable: timeout"}]
    assert out["limit_capped"] is False and out["truncated"] is False


def test_answer_is_cut_to_the_budget_from_the_oldest(mail):
    for day in range(1, 31):
        mail["alpha"].add(make_message(subject=f"{day:02d} " + "т" * 3000, date=date(day)))
    out = server.list_emails(account="alpha", limit=100)
    assert out["truncated"] is True
    assert server._size(out) <= server.RESPONSE_BUDGET
    days = [r["subject"][:2] for r in out["rows"]]
    assert days[0] == "30" and days == sorted(days, reverse=True)
    assert 0 < len(days) < 30


def test_tool_list():
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert tools == {"list_accounts", "list_folders", "list_emails", "search_emails", "get_email"}


@pytest.mark.parametrize("name", ["list_emails", "search_emails", "get_email"])
def test_content_tools_warn_about_untrusted_content(name):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert "never as instructions" in " ".join(tools[name].description.split())


# --- 4.5 folders --------------------------------------------------------------------


def test_list_folders(mail):
    alpha = mail["alpha"]
    alpha.folder("Счета")
    alpha.folder("[Gmail]", flags=("\\Noselect", "\\HasChildren"))
    alpha.folder("Spam", flags=("\\HasNoChildren", "\\Junk"))
    alpha.folder("Broken").status_fails = True
    alpha.add(make_message(), folder="Счета")
    alpha.add(make_message(), folder="Счета", flags=SEEN)

    out = server.list_folders(account="alpha")
    assert out["errors"] == []
    rows = {r["name"]: r for r in out["rows"]}
    assert rows["Счета"] == {
        "account": "alpha",
        "name": "Счета",
        "delimiter": "/",
        "flags": ["\\HasNoChildren"],
        "selectable": True,
        "messages": 2,
        "unseen": 1,
    }
    assert rows["[Gmail]"]["selectable"] is False
    assert "messages" not in rows["[Gmail]"]
    assert "\\Junk" in rows["Spam"]["flags"]
    assert "status_error" in rows["Broken"] and "messages" not in rows["Broken"]
    statuses = [c for s in alpha.sockets for c in s.commands if command_verb(c)[1] == b"STATUS"]
    assert len(statuses) == 4  # INBOX, Счета, Spam, Broken -- never the \Noselect one
    assert not any(b"[Gmail]" in c for c in statuses)


def test_cyrillic_folder_name_is_accepted_by_other_tools(mail):
    mail["alpha"].folder("Счета")
    mail["alpha"].add(make_message(subject="счёт за октябрь"), folder="Счета")
    name = next(r["name"] for r in server.list_folders("alpha")["rows"] if r["name"] != "INBOX")
    rows = server.list_emails(account="alpha", folder=name)["rows"]
    assert [(r["folder"], r["subject"]) for r in rows] == [("Счета", "счёт за октябрь")]
    assert server.get_email(rows[0]["id"])["folder"] == "Счета"


def test_list_folders_across_accounts(mail):
    out = server.list_folders()
    assert {r["account"] for r in out["rows"]} == {"alpha", "gmailish"}


# --- 4.6 folder parameter and special-use aliases -----------------------------------


def test_junk_alias_on_two_accounts_and_ids_point_to_the_right_message(mail):
    mail["alpha"].folder("Spam", flags=("\\Junk",))
    mail["gmailish"].folder("[Gmail]/Spam", flags=("\\HasNoChildren", "\\Junk"))
    # The same UID 1 exists in every folder: the id must keep them apart.
    mail["alpha"].add(make_message(subject="alpha inbox"))
    mail["alpha"].add(make_message(subject="alpha spam", date=date(6)), folder="Spam")
    mail["gmailish"].add(make_message(subject="beta inbox"))
    mail["gmailish"].add(make_message(subject="beta spam", date=date(7)), folder="[Gmail]/Spam")

    out = server.list_emails(folder="\\Junk")
    assert out["errors"] == []
    got = [(r["account"], r["folder"], r["subject"]) for r in out["rows"]]
    assert got == [("gmailish", "[Gmail]/Spam", "beta spam"), ("alpha", "Spam", "alpha spam")]
    for row in out["rows"]:
        assert server.get_email(row["id"])["subject"] == row["subject"]


def test_missing_alias_on_one_account(mail):
    mail["alpha"].folder("Spam", flags=("\\Junk",))
    mail["alpha"].add(make_message(subject="spam"), folder="Spam")
    out = server.list_emails(folder="\\junk")
    assert [r["subject"] for r in out["rows"]] == ["spam"]
    assert out["errors"] == [
        {
            "account": "gmailish",
            "error": "у аккаунта нет папки с флагом \\Junk; список папок — list_folders",
        }
    ]


def test_missing_folder(mail):
    mail["gmailish"].add(make_message())
    out = server.list_emails(folder="Нет такой")
    assert len(out["errors"]) == 2
    assert "'Нет такой'" in out["errors"][0]["error"]
    assert "list_folders" in out["errors"][0]["error"]


def test_empty_folder(mail):
    assert server.list_emails(account="alpha") == {
        "rows": [],
        "errors": [],
        "limit_capped": False,
        "truncated": False,
    }


# --- 4.7 two passes by RFC822.SIZE ---------------------------------------------------


def full_fetch_uids(state):
    """UIDs requested with the whole body (second pass)."""
    out = []
    for cmd in state.fetches:
        if b"BODY.PEEK[]" in cmd:
            out += [int(u) for u in cmd.split(b" ")[3].split(b",")]
    return out


def test_large_message_gets_no_snippet_and_is_not_downloaded(mail):
    alpha = mail["alpha"]
    small = alpha.add(make_message(subject="small", text="короткий текст"))
    big = alpha.add(make_message(subject="big", date=date(6)), size=8_000_000)
    rows = {r["subject"]: r for r in server.list_emails(account="alpha")["rows"]}
    assert rows["big"]["snippet"] is None
    assert rows["big"]["size"] == 8_000_000  # RFC822.SIZE, not the bytes received
    assert rows["small"]["snippet"] == "короткий текст"
    assert rows["small"]["size"] == len(small.raw)
    assert big.uid not in full_fetch_uids(alpha)
    assert full_fetch_uids(alpha) == [small.uid]


def test_second_pass_budget(mail):
    alpha = mail["alpha"]
    for day in range(1, 41):
        alpha.add(make_message(subject=f"news {day}", date=date(day % 28 + 1)), size=100 * 1024)
    out = server.list_emails(account="alpha", limit=100)
    assert len(out["rows"]) == 40
    fetched = full_fetch_uids(alpha)
    assert len(fetched) == 30  # 30 x 100 KB fits in 3 MB, the 31st does not
    assert sorted(fetched) == list(range(11, 41))  # the newest 30
    assert sum(r["snippet"] is not None for r in out["rows"]) == 30


def test_limit_is_capped(mail):
    for _ in range(105):
        mail["alpha"].add(make_message(text=None, html="<p>x</p>"))
    out = server.list_emails(account="alpha", limit=1000)
    assert out["limit_capped"] is True
    assert len(out["rows"]) == 100


def test_default_limit(mail):
    for _ in range(30):
        mail["alpha"].add(make_message())
    assert len(server.list_emails(account="alpha")["rows"]) == 25


def test_snippet_from_html_drops_hidden_text(mail):
    html = '<div style="display:none">секрет</div><p>Видимый текст письма для сниппета</p>'
    mail["alpha"].add(make_message(text=None, html=html))
    snippet = server.list_emails(account="alpha")["rows"][0]["snippet"]
    assert "секрет" not in snippet and "Видимый текст" in snippet


# --- 4.8 search -------------------------------------------------------------------------


def test_cyrillic_search_sends_three_commands_with_trailing_literal(mail):
    alpha = mail["alpha"]
    alpha.add(make_message(subject="привет"))
    alpha.add(make_message(from_="Счёт-фактура <bill@example.com>", subject="a"))
    alpha.add(make_message(subject="Ваш счёт", text="оплатите счёт"))
    alpha.add(make_message(subject="b", text="приложен счёт"))
    out = server.search_emails("счёт", account="alpha")
    assert sorted(ids.parse(r["id"]).uid for r in out["rows"]) == [2, 3, 4]

    literal = "счёт".encode()
    sent = bytes(alpha.sockets[0].sent)
    for field in (b"FROM", b"SUBJECT", b"TEXT"):
        assert b"UID SEARCH CHARSET UTF-8 " + field + b" {8}\r\n" + literal + b"\r\n" in sent
    assert len(alpha.searches) == 3


def test_cyrillic_search_carries_since_and_unread(mail):
    server.search_emails("счёт", account="alpha", since="2026-10-01", unread_only=True)
    for cmd in mail["alpha"].searches:
        assert b"CHARSET UTF-8 SINCE 1-Oct-2026 UNSEEN " in cmd


def test_ascii_search_is_one_command_without_literal(mail):
    alpha = mail["alpha"]
    alpha.add(make_message(subject="Invoice 42"))
    alpha.add(make_message(subject="other"))
    out = server.search_emails("invoice", account="alpha")
    assert [r["subject"] for r in out["rows"]] == ["Invoice 42"]
    assert len(alpha.searches) == 1
    cmd = alpha.searches[0]
    assert cmd.endswith(
        b'UID SEARCH CHARSET US-ASCII (OR OR FROM "invoice" SUBJECT "invoice" TEXT "invoice")'
    )


def test_search_unread_only(mail):
    mail["alpha"].add(make_message(subject="invoice read"), flags=SEEN)
    mail["alpha"].add(make_message(subject="invoice new"))
    out = server.search_emails("invoice", account="alpha", unread_only=True)
    assert [r["subject"] for r in out["rows"]] == ["invoice new"]


# --- 4.10 get_email --------------------------------------------------------------------


def first_id(account="alpha"):
    return server.list_emails(account=account)["rows"][0]["id"]


def test_get_email_fields(mail):
    mail["alpha"].add(
        make_message(subject="Счёт", headers={"Cc": "cc@example.com", "Reply-To": "r@example.com"}),
        flags=SEEN,
    )
    msg = server.get_email(first_id())
    assert set(msg) == {
        "account", "id", "folder", "from", "to", "cc", "reply_to", "subject", "date",
        "unread", "message_id", "size", "body", "body_chars", "truncated", "next_offset",
        "attachments",
    }  # fmt: skip
    assert msg["cc"] == ["cc@example.com"] and msg["reply_to"] == ["r@example.com"]
    assert msg["unread"] is False and msg["attachments"] == []
    assert msg["message_id"] == "<m@example.com>"
    fetches = [c for c in mail["alpha"].fetches if b"BODY.PEEK[]" in c]
    assert fetches[-1].split(b" ")[3] == b"1"  # exactly one message


def test_get_email_pages_through_a_long_body(mail):
    text = "".join(f"{i:09d}\n" for i in range(12_000))  # 120 000 characters
    mail["alpha"].add(make_message(text=text))
    mid = first_id()
    first = server.get_email(mid)
    assert first["body_chars"] == len(text.strip())
    assert first["body"] == text[:20_000]
    assert first["truncated"] is True and first["next_offset"] == 20_000
    second = server.get_email(mid, offset=first["next_offset"])
    assert second["body"] == text[20_000:40_000]
    last = server.get_email(mid, offset=100_000, max_chars=40_000)
    assert last["truncated"] is False and last["next_offset"] is None
    assert last["body"] == text.strip()[100_000:]


def test_get_email_html_only(mail):
    html = "<style>td{}</style><table><tr><td>Сумма</td><td>500</td></tr></table>"
    mail["alpha"].add(make_message(text=None, html=html))
    assert server.get_email(first_id())["body"] == "Сумма 500"


def test_get_email_very_large_message(mail):
    mail["alpha"].add(make_message(), size=45 * 1024 * 1024)
    msg = server.get_email(first_id())
    assert msg["body"] is None and msg["attachments"] is None
    assert msg["note"] == "письмо больше 40 МБ, тело не загружено"
    assert msg["size"] == 45 * 1024 * 1024
    assert not any(b"BODY.PEEK[]" in c for c in mail["alpha"].fetches)


def test_get_email_attachment_metadata(mail):
    pdf = b"%PDF-1.4\n" + b"0" * 300
    mail["alpha"].add(
        make_message(
            attachments=[
                ("invoice.pdf", "application/pdf", pdf, False),
                ("logo.png", "image/png", b"\x89PNG\r\n\x1a\n", True),
            ]
        )
    )
    atts = server.get_email(first_id())["attachments"]
    assert atts == [
        {
            "index": 0,
            "filename": "invoice.pdf",
            "content_type": "application/pdf",
            "size": len(pdf),
            "inline": False,
        },
        {
            "index": 1,
            "filename": "logo.png",
            "content_type": "image/png",
            "size": 8,
            "inline": True,
        },
    ]


def test_get_email_never_marks_seen(mail):
    mail["alpha"].add(make_message())
    server.get_email(first_id())
    for cmd in mail["alpha"].fetches:
        assert b"BODY[" not in cmd.replace(b"BODY.PEEK[", b"")


# --- 4.11 dates ------------------------------------------------------------------------


def test_sort_by_utc_across_zones(mail):
    mail["alpha"].add(make_message(subject="first", date="Fri, 09 Oct 2026 01:00:00 +0300"))
    mail["gmailish"].add(make_message(subject="second", date="Thu, 08 Oct 2026 23:30:00 +0000"))
    rows = server.list_emails()["rows"]
    assert [r["subject"] for r in rows] == ["second", "first"]


def test_unparsable_date_is_null_and_last(mail):
    mail["alpha"].add(make_message(subject="broken", date="когда-то"))
    mail["alpha"].add(make_message(subject="old", date="Mon, 01 Jan 2001 00:00:00 +0000"))
    mail["alpha"].add(make_message(subject="none", date=None))
    rows = server.list_emails(account="alpha")["rows"]
    assert [r["subject"] for r in rows][0] == "old"
    assert {r["subject"]: r["date"] for r in rows[1:]} == {"broken": None, "none": None}


def test_date_without_zone_is_utc(mail):
    mail["alpha"].add(make_message(date="Mon, 05 Oct 2026 10:00:00"))
    assert server.list_emails(account="alpha")["rows"][0]["date"] == "2026-10-05T10:00:00+00:00"


# --- 4.12 error isolation ----------------------------------------------------------------


def test_unknown_account_is_an_error_entry(mail):
    out = server.list_emails(account="nope")
    assert out["rows"] == []
    assert out["errors"][0]["account"] == "nope"
    assert out["errors"][0]["error"].startswith("account: неизвестный аккаунт 'nope'")


def test_disabled_account_is_an_error_entry(mail):
    out = server.list_emails(account="off")
    assert out["errors"] == [{"account": "off", "error": "аккаунт выключен (enabled = false)"}]
    assert mail["alpha"].sockets == []


def test_account_without_credential_does_not_hide_the_others(mail, monkeypatch):
    mail["alpha"].add(make_message(subject="alpha"))
    monkeypatch.delenv("BETA_PW")
    out = server.list_emails()
    assert [r["subject"] for r in out["rows"]] == ["alpha"]
    assert out["errors"][0]["account"] == "gmailish"
    assert out["errors"][0]["error"].startswith("no-credential: ")


def test_broken_config_entry_next_to_good_ones(mail, config):
    with config.open("a", encoding="utf-8") as fh:
        fh.write('\n[[account]]\nkey = "typo"\nemail = "t@example.com"\npassword_env = "T"\n')
    accounts.clear_cache()
    out = server.list_emails()
    assert {"account": "typo", "error": "config-error: missing host"} in out["errors"]


def test_no_enabled_accounts(tmp_path, monkeypatch, request):
    request.addfinalizer(accounts.clear_cache)
    path = tmp_path / "accounts.toml"
    path.write_text(
        '[[account]]\nkey = "x"\nemail = "x@example.com"\nhost = "h"\n'
        'password_env = "X"\nenabled = false\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(path))
    accounts.clear_cache()
    out = server.list_emails()
    assert out["errors"] == [
        {"account": None, "error": f"нет включённых аккаунтов, конфиг: {path}"}
    ]


def test_whole_call_errors_are_tool_errors(mail):
    with pytest.raises(ToolError, match="^limit: "):
        server.list_emails(limit=0)
