"""Argument checks before IMAP (D7): errors name the parameter, no socket is opened."""

import asyncio
import datetime as dt

import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import TextContent

from imap_mcp import server, validate
from imap_mcp.validate import ArgumentError


def call(name, arguments):
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(name, arguments)

    result = asyncio.run(run())
    return result, " ".join(c.text for c in result.content if isinstance(c, TextContent))


def test_uid_range_instead_of_id_is_rejected_without_imap(mail):
    with pytest.raises(ToolError, match="неверный формат id"):
        server.get_email(id="1:*")
    assert mail["alpha"].sockets == [] and mail["gmailish"].sockets == []


def test_control_characters_in_query_are_rejected_without_imap(mail):
    with pytest.raises(ToolError, match="query: недопустимые управляющие символы"):
        server.search_emails(query="счёт\r\nDELETE INBOX")
    assert mail["alpha"].sockets == [] and mail["gmailish"].sockets == []


def test_limit_of_wrong_type_is_our_error_not_a_schema_error(mail):
    result, text = call("list_emails", {"limit": "много"})
    assert result.is_error
    assert "limit: ожидается целое число не меньше 1" in text
    assert "validation error" not in text
    assert mail["alpha"].sockets == [] and mail["gmailish"].sockets == []


def test_integer_parameters_keep_integer_schema():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert tools["list_emails"].input_schema["properties"]["limit"]["type"] == "integer"
    assert tools["get_email"].input_schema["properties"]["max_chars"]["type"] == "integer"
    assert tools["get_email"].input_schema["properties"]["offset"]["type"] == "integer"


@pytest.mark.parametrize("value", [0, -1, True, 2.5, "25", None])
def test_limit_bounds(value):
    with pytest.raises(ArgumentError, match="^limit: "):
        validate.limit(value)


def test_limit_is_capped_not_rejected():
    assert validate.limit(25) == (25, False)
    assert validate.limit(100) == (100, False)
    assert validate.limit(1000) == (100, True)


@pytest.mark.parametrize(
    "param, value, lo, hi",
    [("max_chars", 0, 1, 40_000), ("max_chars", 40_001, 1, 40_000), ("offset", -1, 0, 10)],
)
def test_integer_bounds_are_named(param, value, lo, hi):
    with pytest.raises(ArgumentError, match=f"^{param}: .*от {lo} до {hi}"):
        validate.integer(param, value, lo, hi)


def test_since():
    assert validate.since(None) is None
    assert validate.since("2026-07-01") == dt.date(2026, 7, 1)
    bad_dates = ("2026-13-01", "July 1st", "2026-7-1", 20260701, "2026-07-01T00:00", "2025-W15-3")
    for bad in bad_dates:
        with pytest.raises(ArgumentError, match="^since: ожидается дата YYYY-MM-DD"):
            validate.since(bad)


def test_blank_query_is_named_as_blank():
    with pytest.raises(ArgumentError, match="^query: ожидается непустая строка"):
        validate.query("   ")


@pytest.mark.parametrize("bad", ["", "   ", "x" * 501, "a\tb", "a\nb", "a\x00b", "a\u2028b", 5])
def test_query_rules(bad):
    with pytest.raises(ArgumentError, match="^query: "):
        validate.query(bad)


def test_query_accepts_cyrillic_and_max_length():
    assert validate.query("счёт") == "счёт"
    assert validate.query("x" * 500) == "x" * 500


@pytest.mark.parametrize("alias", validate.SPECIAL_USE)
def test_special_use_aliases_are_canonical(alias):
    assert validate.folder(alias) == alias
    assert validate.folder(alias.lower()) == alias
    assert validate.folder(alias.upper()) == alias


def test_folder_rules():
    assert validate.folder("Счета") == "Счета"
    for bad in ("", "x" * 501, "a\rb", "\\Spam"):
        with pytest.raises(ArgumentError, match="^folder: "):
            validate.folder(bad)


def test_account_format():
    assert validate.account(None) is None
    assert validate.account("alpha") == "alpha"
    for bad in ("Alpha", "a.b", "x" * 33, 7):
        with pytest.raises(ArgumentError, match="^account: "):
            validate.account(bad)
