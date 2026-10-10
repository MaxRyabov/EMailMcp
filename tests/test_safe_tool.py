"""@safe_tool and redact() on a real MCPServer, through the in-memory MCP client."""

import asyncio
import base64
from typing import TypedDict

import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent

from imap_mcp import accounts, imap, server, status
from imap_mcp.redact import MASK, redact, register

TOKEN = "y0_AgAAAABkZXZpY2UtdG9rZW4tZm9yLXRlc3RzLW9ubHk"
XOAUTH2 = base64.b64encode(f"user=me@yandex.ru\1auth=Bearer {TOKEN}\1\1".encode()).decode()


def call(mcp, name, arguments=None):
    async def run():
        async with Client(mcp) as client:
            return await client.call_tool(name, arguments or {})

    return asyncio.run(run())


def text_of(result):
    return " ".join(c.text for c in result.content if isinstance(c, TextContent))


# --- 3.1: what mcp 2.0.0 does with CallToolResult(isError=True) --------------------


class Out(TypedDict):
    rows: list[dict]


def test_mcp_validates_returned_call_tool_result_against_output_schema():
    """Pins the finding of task 3.1 (RUN.md): returning CallToolResult(isError=True)
    from a tool with an output schema does not bypass validation in mcp 2.0.0, so
    @safe_tool raises ToolError instead. If this starts to fail, mcp changed."""
    mcp = MCPServer("probe")

    @mcp.tool()
    def returns_error_result() -> Out:
        return CallToolResult(content=[TextContent(type="text", text="ours")], is_error=True)

    result = call(mcp, "returns_error_result")
    assert result.is_error
    assert "ours" not in text_of(result)
    assert "validation error" in text_of(result)


def test_safe_tool_error_reaches_client_as_is_error_with_our_text():
    mcp = MCPServer("probe")

    @mcp.tool()
    @server.safe_tool
    def boom() -> Out:
        raise RuntimeError(f"login failed for {TOKEN}")

    register(TOKEN)

    result = call(mcp, "boom")
    assert result.is_error
    assert text_of(result) == f"Error executing tool boom: RuntimeError: login failed for {MASK}"


def test_safe_tool_keeps_tool_schema():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools["list_emails"].input_schema["properties"]) == {
        "account",
        "since",
        "unread_only",
        "limit",
    }
    assert set(tools["list_accounts"].output_schema["properties"]) == {"accounts", "warnings"}


# --- redact() -------------------------------------------------------------------


def test_redact_registered_token_and_its_xoauth2_form():
    register(TOKEN)
    text = f"AUTHENTICATE failed: token={TOKEN} sasl={XOAUTH2}"
    out = redact(text)
    assert TOKEN not in out
    assert XOAUTH2 not in out
    assert out.count(MASK) == 2


def test_redact_patterns_without_registration():
    unknown = "y0_NeverRegisteredTokenValue1234567890"
    b64 = base64.b64encode(f"user=a@b.c\1auth=Bearer {unknown}\1\1".encode()).decode()
    for text in (
        f"Authorization: Bearer {unknown}",
        f'{{"access_token": "{unknown}", "token_type": "bearer"}}',
        f"client_secret={unknown}&code=1234567",
        f"IMAP > A001 AUTHENTICATE XOAUTH2 {b64}",
        f"IMAP > {b64}",
    ):
        assert unknown not in redact(text)
        assert b64 not in redact(text)


def test_registered_secret_equal_to_anchor_does_not_unmask_value():
    register("password")
    assert redact("password=unregistered-value-123") == f"{MASK}={MASK}"


def test_quoted_value_with_space_is_masked_whole():
    register("abc def ghi")
    assert redact('{"password": "abc def ghi", "user": "me"}') == (
        f'{{"password": "{MASK}", "user": "me"}}'
    )


def test_redact_decodes_bytes_before_matching():
    register("pa'ss\\word")
    assert redact(b"LOGIN me pa'ss\\word") == f"LOGIN me {MASK}"


def test_redact_leaves_ordinary_text_and_base64():
    plain = base64.b64encode(b"just some attachment bytes, nothing secret").decode()
    text = f"user_code 1234567, verification_url https://oauth.yandex.ru/device, {plain}"
    assert redact(text) == text


# --- every registered tool --------------------------------------------------------

TOOL_ARGS = {
    "list_accounts": {},
    "list_emails": {},
    "search_emails": {"query": "invoice"},
    "get_email": {"account": "alpha", "id": "1"},
}


def test_tool_args_cover_every_tool():
    assert set(TOOL_ARGS) == {t.name for t in asyncio.run(server.mcp.list_tools())}


@pytest.mark.parametrize("name", sorted(TOOL_ARGS))
def test_every_tool_turns_exception_with_secret_into_redacted_error(name, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError(f"AUTHENTICATE XOAUTH2 {XOAUTH2} for {TOKEN}")

    register(TOKEN)
    monkeypatch.setattr(accounts, "load", explode)
    monkeypatch.setattr(status.acct, "load", explode)

    result = call(server.mcp, name, TOOL_ARGS[name])
    assert result.is_error
    text = text_of(result)
    assert TOKEN not in text
    assert XOAUTH2 not in text
    assert MASK in text
    assert "validation error" not in text


def test_per_account_error_rows_are_redacted(config, monkeypatch):
    register(TOKEN)

    def fetch_rows(account, **kwargs):
        raise RuntimeError(f"rejected {TOKEN}")

    monkeypatch.setattr(imap, "fetch_rows", fetch_rows)
    rows = server.list_emails()
    assert [r["error"] for r in rows] == [f"RuntimeError: rejected {MASK}"] * 2


def test_missing_config_is_a_tool_error_with_path(tmp_path, monkeypatch):
    missing = tmp_path / "nowhere" / "accounts.toml"
    monkeypatch.setenv("IMAP_MCP_ACCOUNTS", str(missing))
    accounts.clear_cache()
    result = call(server.mcp, "list_accounts")
    assert result.is_error
    assert str(missing) in text_of(result)
    accounts.clear_cache()
