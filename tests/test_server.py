import datetime as dt

import pytest

from imap_mcp import accounts as acct
from imap_mcp import imap, server


def test_parse_since():
    assert server._parse_since(None) is None
    assert server._parse_since("2026-07-01") == dt.date(2026, 7, 1)
    with pytest.raises(ValueError):
        server._parse_since("July 1st")


def test_targets(config):
    assert [a.key for a in server._targets(None)] == ["alpha", "gmailish"]
    assert [a.key for a in server._targets("alpha")] == ["alpha"]
    assert server._targets("off") == []  # disabled account resolves to nothing
    with pytest.raises(KeyError):
        server._targets("nope")


def test_list_accounts_statuses(config, monkeypatch):
    class Box:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_box(account):
        if account.key == "gmailish":
            raise ConnectionRefusedError("nope")
        return Box()

    monkeypatch.setenv("ALPHA_PW", "pw")
    monkeypatch.setenv("BETA_PW", "pw")
    monkeypatch.setattr(imap, "open_box", open_box)

    report = server.list_accounts()
    status = {e["account"]: e["status"] for e in report["accounts"]}
    assert status == {
        "alpha": "ok",
        "gmailish": "unreachable: ConnectionRefusedError (imap.gmail.com:993)",
        "off": "disabled",
    }
    assert {e["auth"] for e in report["accounts"]} == {"password"}
    assert report["warnings"] == []

    monkeypatch.delenv("ALPHA_PW")
    status = {e["account"]: e["status"] for e in server.list_accounts()["accounts"]}
    assert status["alpha"] == "no-credential"


def test_list_accounts_reports_config_errors_and_no_enabled_accounts(
    tmp_path, monkeypatch, request
):
    request.addfinalizer(acct.clear_cache)
    path = tmp_path / "accounts.toml"
    path.write_text('[[account]]\nkey = "bad"\nemail = "x@example.com"\n', encoding="utf-8")
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(path))
    acct.clear_cache()
    report = server.list_accounts()
    assert [(e["account"], e["status"]) for e in report["accounts"]] == [
        ("bad", "config-error: missing password_env")
    ]
    assert report["warnings"] == [f"нет включённых аккаунтов, конфиг: {path}"]


def test_list_emails_reports_broken_account_next_to_good_one(config, monkeypatch):
    with config.open("a", encoding="utf-8") as fh:
        fh.write('\n[[account]]\nkey = "typo"\nemail = "t@example.com"\npassword_env = "T"\n')
    acct.clear_cache()
    monkeypatch.setattr(
        imap, "fetch_rows", lambda a, **kw: [{"account": a.key, "id": "1", "date": "2026-07-01"}]
    )
    rows = server.list_emails()
    assert {"account": "typo", "error": "config-error: missing host"} in rows
    assert {r["account"] for r in rows if "id" in r} == {"alpha", "gmailish"}


def test_list_emails_merges_and_sorts(config, monkeypatch):
    def fetch_rows(account, **kwargs):
        return {
            "alpha": [
                {"account": "alpha", "id": "1", "date": "2026-07-01T00:00:00+00:00"},
                {"account": "alpha", "id": "2", "date": "2026-07-03T00:00:00+00:00"},
            ],
            "gmailish": [
                {"account": "gmailish", "id": "9", "date": "2026-07-02T00:00:00+00:00"},
            ],
        }[account.key]

    monkeypatch.setattr(imap, "fetch_rows", fetch_rows)
    rows = server.list_emails()
    assert [(r["account"], r["id"]) for r in rows] == [
        ("alpha", "2"),
        ("gmailish", "9"),
        ("alpha", "1"),
    ]


def test_list_emails_single_account_passes_filters(config, monkeypatch):
    seen = {}

    def fetch_rows(account, **kwargs):
        seen[account.key] = kwargs
        return []

    monkeypatch.setattr(imap, "fetch_rows", fetch_rows)
    server.list_emails(account="alpha", since="2026-07-01", unread_only=True, limit=5)
    assert seen == {
        "alpha": {"since": dt.date(2026, 7, 1), "unread_only": True, "limit": 5},
    }


def test_list_emails_account_failure_is_isolated(config, monkeypatch):
    def fetch_rows(account, **kwargs):
        if account.key == "alpha":
            raise TimeoutError("slow server")
        return [{"account": account.key, "id": "1", "date": "2026-07-01T00:00:00+00:00"}]

    monkeypatch.setattr(imap, "fetch_rows", fetch_rows)
    rows = server.list_emails()
    errors = [r for r in rows if "error" in r]
    assert errors == [{"account": "alpha", "error": "TimeoutError: slow server"}]
    assert [r["account"] for r in rows if "error" not in r] == ["gmailish"]


def test_search_emails_passes_query(config, monkeypatch):
    seen = {}

    def fetch_rows(account, **kwargs):
        seen[account.key] = kwargs
        return []

    monkeypatch.setattr(imap, "fetch_rows", fetch_rows)
    server.search_emails("invoice", since="2026-07-01", limit=10)
    assert set(seen) == {"alpha", "gmailish"}
    assert seen["alpha"] == {"query": "invoice", "since": dt.date(2026, 7, 1), "limit": 10}


def test_get_email_found(config, monkeypatch):
    monkeypatch.setattr(imap, "fetch_one", lambda a, uid: {"account": a.key, "id": uid})
    assert server.get_email("alpha", "42") == {"account": "alpha", "id": "42"}


def test_get_email_not_found(config, monkeypatch):
    monkeypatch.setattr(imap, "fetch_one", lambda a, uid: None)
    out = server.get_email("alpha", "42")
    assert out["error"] == "not found in INBOX"


def test_all_tools_registered():
    import asyncio

    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert tools == {"list_accounts", "list_emails", "search_emails", "get_email"}
