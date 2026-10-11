"""Message id v1.<account>.<b64url folder>.<UIDVALIDITY>.<UID> (D8)."""

import pytest
from fakemail import make_message
from mcp.server.mcpserver.exceptions import ToolError

from imap_mcp import ids, server
from imap_mcp.validate import ArgumentError


@pytest.mark.parametrize("folder", ["INBOX", "Счета", "Work/2026", "[Gmail]/Спам", "a.b c"])
def test_round_trip(folder):
    text = ids.make("alpha", folder, 7, 42)
    assert text.startswith("v1.alpha.")
    assert text.count(".") == 4
    assert ids.parse(text) == ids.MessageId("alpha", folder, 7, 42)


@pytest.mark.parametrize(
    "bad",
    [
        "1:*",
        "42",
        "",
        None,
        42,
        "v2.alpha.SU5CT1g.7.42",
        "v1.Alpha.SU5CT1g.7.42",
        "v1.alpha..7.42",
        "v1.alpha.SU5CT1g=.7.42",  # padding is not part of the format
        "v1.alpha.SU5CT1g.7.0",
        "v1.alpha.SU5CT1g.0.42",
        "v1.alpha.SU5CT1g.07.42",  # leading zero: not canonical
        "v1.alpha.SU5CT1g.7.4294967296",
        "v1.alpha.SU5CT1g.7.1:*",
        "v1.alpha.SU5CT1g.7.42.1",
        "v1.alpha._w.7.42",  # not UTF-8
        "v1.alpha.SU5CT1h.7.42",  # stray bits in the last base64 character
        "v1.alpha.YQ0K.7.42",  # folder "a\r\n"
    ],
)
def test_garbage_is_rejected(bad):
    with pytest.raises(ArgumentError, match="^id: неверный формат id"):
        ids.parse(bad)


def test_changed_uidvalidity_means_stale_list(mail):
    mail["alpha"].add(make_message())
    mid = server.list_emails(account="alpha")["rows"][0]["id"]
    mail["alpha"].folders["INBOX"].uidvalidity = 8  # the folder was re-created
    fetches = len(mail["alpha"].fetches)
    with pytest.raises(ToolError, match="список устарел, повторите list_emails/search_emails"):
        server.get_email(mid)
    assert len(mail["alpha"].fetches) == fetches  # the message was not read


def test_unknown_account_in_id(mail):
    with pytest.raises(ToolError, match="аккаунта 'nope' из id нет в конфиге"):
        server.get_email(ids.make("nope", "INBOX", 7, 1))


def test_disabled_account_in_id(mail):
    with pytest.raises(ToolError, match="аккаунт 'off' выключен"):
        server.get_email(ids.make("off", "INBOX", 7, 1))
    assert mail["alpha"].sockets == []


def test_missing_message(mail):
    with pytest.raises(ToolError, match="письмо не найдено"):
        server.get_email(ids.make("alpha", "INBOX", 7, 99))
