"""Command line: `imap-mcp` without arguments runs the MCP server (D17).

Every line printed here goes through redact(), the --trace output included.
"""

from __future__ import annotations

import argparse
import sys

from . import accounts as acct
from .redact import redact

EXIT_INTERRUPTED = 130


def _out(text: str) -> None:
    print(redact(text), flush=True)


def _err(text: str) -> None:
    print(redact(text), file=sys.stderr, flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imap-mcp", description="Read-only MCP server for IMAP mail."
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server on stdio (the default)")
    auth = sub.add_parser("auth", help="get an OAuth token for an account (device flow)")
    auth.add_argument("account")
    auth.add_argument("--trace", action="store_true", help="print HTTP and IMAP to stderr")
    st = sub.add_parser("status", help="show account statuses")
    st.add_argument("--offline", action="store_true", help="do not touch the network")
    forget = sub.add_parser("forget", help="revoke and delete an account's OAuth tokens")
    forget.add_argument("account")
    forget.add_argument("--trace", action="store_true", help="print HTTP to stderr")
    return parser


def _oauth_account(key: str) -> acct.Account | None:
    try:
        account = acct.get_account(key)
    except (acct.ConfigError, acct.AccountConfigError) as e:
        _err(str(e))
        return None
    except KeyError as e:
        _err(e.args[0])
        return None
    if account.auth != "oauth":
        _err(
            f"аккаунт {key} входит по паролю из переменной {account.password_env}, "
            "OAuth-авторизация для него не нужна"
        )
        return None
    return account


def cmd_auth(key: str, trace: bool) -> int:
    from .auth import oauth

    account = _oauth_account(key)
    if account is None:
        return 1
    try:
        oauth.authorize(account, _out, trace=_err if trace else None)
    except oauth.AuthFailed as e:
        _err(f"ошибка: {e}")
        return 1
    return 0


def cmd_forget(key: str, trace: bool) -> int:
    from .auth import oauth

    account = _oauth_account(key)
    if account is None:
        return 1
    try:
        oauth.forget(account, _out, trace=_err if trace else None)
    except oauth.AuthFailed as e:
        _err(f"ошибка: {e}")
        return 1
    return 0


def cmd_status(offline: bool) -> int:
    from . import status

    try:
        report = status.report(live=not offline)
    except acct.ConfigError as e:
        _err(str(e))
        return 1
    for e in report["accounts"]:
        line = f"{e['account']}: {e['status']}"
        if e.get("expires_at"):
            line += f", токен до {e['expires_at']}"
        if e.get("hint"):
            line += f" — {e['hint']}"
        _out(line)
    for w in report["warnings"]:
        _out(f"предупреждение: {w}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in (None, "serve"):
            from .server import mcp

            mcp.run()
            return 0
        if args.command == "auth":
            return cmd_auth(args.account, args.trace)
        if args.command == "forget":
            return cmd_forget(args.account, args.trace)
        return cmd_status(args.offline)
    except KeyboardInterrupt:
        if args.command == "auth":
            _err("прервано, ничего не сохранено")
        elif args.command == "forget":
            _err("прервано; что осталось в хранилище, покажет `imap-mcp status --offline`")
        return EXIT_INTERRUPTED
