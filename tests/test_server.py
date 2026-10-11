from imap_mcp import accounts as acct
from imap_mcp import imap, server


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


def test_targets(config):
    targets, errors = server._targets(None)
    assert [a.key for a in targets] == ["alpha", "gmailish"] and errors == []
    targets, errors = server._targets("alpha")
    assert [a.key for a in targets] == ["alpha"] and errors == []
    assert server._targets("off") == (
        [],
        [{"account": "off", "error": "аккаунт выключен (enabled = false)"}],
    )
    targets, errors = server._targets("nope")
    assert targets == []
    assert (
        errors[0]["error"] == "account: неизвестный аккаунт 'nope'; известные: alpha, gmailish, off"
    )
