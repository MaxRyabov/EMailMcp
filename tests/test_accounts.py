import os
import textwrap
from types import SimpleNamespace

import pytest

from imap_mcp import accounts


def test_load_reads_fields_and_defaults(config):
    accts = {a.key: a for a in accounts.all_accounts()}
    assert set(accts) == {"alpha", "gmailish", "off"}

    alpha = accts["alpha"]
    assert alpha.email == "alpha@example.com"
    assert alpha.host == "imap.example.com"
    assert alpha.port == 1993
    assert alpha.password_env == "ALPHA_PW"
    assert alpha.enabled

    # defaults: port 993, label falls back to email, enabled true, upstream login
    gmailish = accts["gmailish"]
    assert gmailish.port == 993
    assert gmailish.label == "beta@gmail.com"
    assert gmailish.enabled
    assert gmailish.auth == "password"


def test_enabled_accounts_skips_disabled(config):
    assert [a.key for a in accounts.enabled_accounts()] == ["alpha", "gmailish"]


def test_get_account_unknown_key(config):
    with pytest.raises(KeyError, match="unknown account 'nope'"):
        accounts.get_account("nope")


def test_password_from_env(config, monkeypatch):
    alpha = accounts.get_account("alpha")
    assert alpha.password() is None
    monkeypatch.setenv("ALPHA_PW", "hunter2")
    assert alpha.password() == "hunter2"
    monkeypatch.setenv("ALPHA_PW", "")
    assert alpha.password() is None  # empty counts as unset


def test_missing_config_error_mentions_example(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(tmp_path / "nope.toml"))
    accounts.clear_cache()
    with pytest.raises(accounts.ConfigError, match="accounts.example.toml") as e:
        accounts.all_accounts()
    assert "nope.toml" in str(e.value)


def test_config_path_through_a_file_is_a_config_error(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(blocker / "accounts.toml"))
    accounts.clear_cache()
    with pytest.raises(accounts.ConfigError, match="accounts.toml"):
        accounts.load()


def test_invalid_toml_is_a_config_error_with_path(write_config):
    path = write_config('[[account]]\nkey = "a\n')
    with pytest.raises(accounts.ConfigError, match="not valid TOML") as e:
        accounts.load()
    assert str(path) in str(e.value)


def test_example_config_is_loadable(monkeypatch):
    example = accounts.DEFAULT_CONFIG_PATH.parent / "accounts.example.toml"
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(example))
    accounts.clear_cache()
    try:
        registry = accounts.load()
        assert registry.broken == []
        accts = registry.accounts
        assert accts, "example config should define at least one account"
        yandex = [a for a in accts.values() if a.auth == "oauth"]
        assert [(a.oauth_provider, a.host, a.port) for a in yandex] == [
            ("yandex", "imap.yandex.com", 993)
        ]
        assert all(a.password_env for a in accts.values() if a.auth == "password")
    finally:
        accounts.clear_cache()


# --- per-account validation (account-auth spec) ---------------------------------

UPSTREAM = """
[[account]]
key = "work"
email = "me@example.com"
host = "imap.example.com"
password_env = "WORK_PW"
"""


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    path = tmp_path / "accounts.toml"
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(path))
    accounts.clear_cache()

    def write(text):
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    yield write
    accounts.clear_cache()


def broken(registry):
    return {b.key: b.status for b in registry.broken}


def test_upstream_config_without_auth_is_password(write_config):
    write_config(UPSTREAM)
    work = accounts.get_account("work")
    assert work.auth == "password"
    assert work.oauth_provider is None


def test_oauth_account_gets_provider_defaults(write_config):
    write_config("""
        [[account]]
        key = "yandex"
        email = "me@yandex.ru"
        auth = "oauth"
        oauth_provider = "yandex"
        client_id = "abc123"
    """)
    y = accounts.get_account("yandex")
    assert (y.host, y.port, y.client_id, y.password_env) == ("imap.yandex.com", 993, "abc123", None)


@pytest.mark.parametrize(
    "entry, error",
    [
        ('auth = "oauth"\nclient_id = "c"', "config-error: missing oauth_provider"),
        ('auth = "oauth"\noauth_provider = "yandex"', "config-error: missing client_id"),
        ('auth = "token"', "config-error: invalid auth 'token'"),
        ("", "config-error: missing password_env"),
    ],
)
def test_missing_or_invalid_auth_fields(write_config, entry, error):
    write_config(f'[[account]]\nkey = "y"\nemail = "me@yandex.ru"\nhost = "h"\n{entry}\n')
    assert broken(accounts.load())["y"].startswith(error)


def test_unknown_provider_lists_supported(write_config):
    write_config("""
        [[account]]
        key = "g"
        email = "me@gmail.com"
        auth = "oauth"
        oauth_provider = "gmail"
        client_id = "c"
    """)
    status = broken(accounts.load())["g"]
    assert status.startswith("config-error: unknown oauth_provider 'gmail'")
    assert "supported: yandex" in status


def test_scope_in_config_is_rejected(write_config):
    write_config("""
        [[account]]
        key = "y"
        email = "me@yandex.ru"
        auth = "oauth"
        oauth_provider = "yandex"
        client_id = "c"
        scope = "mail:imap_full"
    """)
    status = broken(accounts.load())["y"]
    assert "scope is not allowed" in status
    assert "provider profile" in status


@pytest.mark.parametrize("key", ["Мой ящик", "a.b", "UPPER", "x" * 33])
def test_invalid_key_shows_format(write_config, key):
    write_config(UPSTREAM.replace('"work"', f'"{key}"'))
    status = broken(accounts.load())[key]
    assert status == f"config-error: invalid key, expected {accounts.KEY_PATTERN.pattern}"


def test_key_with_trailing_newline_is_rejected(write_config):
    # `$` in a regex also matches before a final "\n"; the key goes into message ids.
    write_config(UPSTREAM.replace('"work"', '"work\\n"'))
    assert accounts.load().accounts == {}
    assert broken(accounts.load())["work\n"].startswith("config-error: invalid key")


def test_one_typo_does_not_break_the_other_account(write_config):
    write_config(
        UPSTREAM
        + """
[[account]]
key = "second"
email = "two@example.com"
password_env = "TWO_PW"
"""
    )
    registry = accounts.load()
    assert list(registry.accounts) == ["work"]
    assert broken(registry) == {"second": "config-error: missing host"}
    with pytest.raises(accounts.AccountConfigError, match="missing host"):
        accounts.get_account("second")


def test_duplicate_key_breaks_both(write_config):
    write_config(UPSTREAM + UPSTREAM)
    registry = accounts.load()
    assert registry.accounts == {}
    assert [b.status for b in registry.broken] == ["config-error: duplicate key 'work'"] * 2


def test_config_is_reread_when_mtime_changes(write_config):
    path = write_config(UPSTREAM)
    assert [a.key for a in accounts.all_accounts()] == ["work"]

    write_config(UPSTREAM + UPSTREAM.replace("work", "home"))
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert [a.key for a in accounts.all_accounts()] == ["work", "home"]


def test_unchanged_config_is_cached(write_config, monkeypatch):
    write_config(UPSTREAM)
    first = accounts.load()
    fake = SimpleNamespace(load=lambda fh: pytest.fail("re-read"), TOMLDecodeError=ValueError)
    monkeypatch.setattr(accounts, "tomllib", fake)
    assert accounts.load() is first
