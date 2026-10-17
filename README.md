# imap-mcp

All your mailboxes, one MCP server, read-only.

Point it at any number of IMAP accounts (Gmail, Fastmail, iCloud, Migadu,
self-hosted, anything that speaks IMAP) and your MCP client can list, search,
and read across every one of them in a single call. Ask "what's unread across
all my accounts?" or "find the invoice from Hetzner" and get answers that span
providers, not one inbox at a time.

**Strictly read-only.** No send, no delete, no flag changes, no folder moves.
Every fetch uses `BODY.PEEK`, so reading a message never marks it as read.
Your mailboxes look exactly the same after a session as before it.

**No secrets on disk.** The config file names an environment variable per
account; passwords only ever exist in the server process environment. Bring
your own secret manager, or just export a variable.

## Why IMAP

Assistant mail integrations usually connect one Gmail or one Outlook account.
IMAP is the protocol nearly every provider already speaks, so one small server
covers your whole mail footprint with a single read-only code path, no OAuth
apps to register, and nothing granted write access.

## Quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/LeeFlannery/imap-mcp
cd imap-mcp
cp accounts.example.toml accounts.toml
$EDITOR accounts.toml           # add your accounts
export PERSONAL_IMAP_PASSWORD='...'
uv run imap-mcp                 # starts the MCP server on stdio
```

That's it. Register it with your MCP client (below) and start asking about
your mail.

## Configure accounts

`accounts.toml` is gitignored and holds no secrets:

```toml
[[account]]
key = "personal"                        # short id used in tool calls
label = "you@example.com (Fastmail)"    # optional, defaults to email
email = "you@example.com"               # IMAP login
host = "imap.fastmail.com"
port = 993                              # optional, defaults to 993
password_env = "PERSONAL_IMAP_PASSWORD" # env var holding the password
enabled = true                          # optional, defaults to true
```

Add one `[[account]]` block per mailbox. `enabled = false` keeps an account
listed but never logs into it. Set `IMAP_MCP_ACCOUNTS=/path/to/accounts.toml`
to keep the config outside the repo.

### Provider cheat sheet

| Provider | Host | Password |
|----------|------|----------|
| Gmail / Google Workspace | `imap.gmail.com` | [App password](https://myaccount.google.com/apppasswords) (requires 2FA; the spaces Google shows are stripped automatically) |
| Fastmail | `imap.fastmail.com` | [App password](https://www.fastmail.help/hc/en-us/articles/360058752854) |
| iCloud | `imap.mail.me.com` | [App-specific password](https://support.apple.com/102654) |
| Yahoo | `imap.mail.yahoo.com` | [App password](https://help.yahoo.com/kb/SLN15241.html) |
| Migadu | `imap.migadu.com` | Mailbox password |
| Outlook.com / Microsoft 365 | not supported | Microsoft has retired IMAP basic auth; OAuth is not implemented here |

All standard providers use port 993 (implicit TLS), the default.

### Passwords

Each account's `password_env` names an environment variable that must be set
when the server runs. Any of these work:

```sh
# plain export in the shell that launches your MCP client
export PERSONAL_IMAP_PASSWORD='...'

# direnv, sops, pass, whatever you already use

# 1Password: put op:// references in an env file and wrap the command
op run --env-file=op.env -- uv run imap-mcp
```

## Register with an MCP client

### Claude Code

```sh
claude mcp add imap -- uv run --directory /path/to/imap-mcp imap-mcp
```

Or in `.mcp.json` / your user MCP config:

```json
{
  "mcpServers": {
    "imap": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/imap-mcp", "imap-mcp"]
    }
  }
}
```

### Claude Desktop and other clients

Same command and args in the client's MCP server config
(`claude_desktop_config.json`, etc.). Any stdio MCP client works.

Either way, the password env vars must be visible to the spawned process:
export them in the environment the client launches from, or wrap the command
in your secret injector, e.g. `"command": "op"`,
`"args": ["run", "--env-file=/path/to/op.env", "--", "uv", "run", ...]`.

## Tools

**`list_accounts()`** configured accounts with live status. Run this first;
it tells you which mailboxes are queryable and what to pass as `account`.

```json
{"account": "personal", "email": "you@example.com", "enabled": true, "status": "ok"}
```

**`list_folders(account?)`** folders with special-use flags (`\Sent`, `\Junk`,
`\Trash`, `\Drafts`, `\Archive`, `\All`), `selectable`, and message/unseen
counts. Names are decoded (Cyrillic included) and accepted as `folder` elsewhere.

**`list_emails(account?, folder?, since?, unread_only?, limit?)`** recent mail,
newest first. Omit `account` to merge all enabled accounts into one timeline.
`folder` defaults to `INBOX`; a special-use alias such as `\Junk` is resolved
per account, so one call reads every account's spam folder.

**`search_emails(query, account?, folder?, since?, unread_only?, limit?)`**
search over sender, subject, and body, in any language. Also cross-account
unless you name one.

Both return an object; one row per message:

```json
{"rows": [{"account": "personal", "folder": "INBOX",
           "id": "v1.personal.SU5CT1g.1712345678.4711",
           "from": "billing@hetzner.com", "to": ["you@example.com"],
           "subject": "Invoice 2026-07", "date": "2026-07-03T09:12:44+00:00",
           "unread": true, "size": 18234, "snippet": "Your invoice for July..."}],
 "errors": [], "limit_capped": false, "truncated": false}
```

`limit` is per account, 25 by default, at most 100 (`limit_capped` says it was
cut). Messages over 128 KB get `snippet: null` and are not downloaded. The
answer stays under 45 000 characters; the oldest rows are dropped first
(`truncated`). If one account is down, it lands in `errors` and the other
accounts still answer.

**`get_email(id, offset?, max_chars?)`** one message by the `id` from a row:
headers, attachment metadata and the body as text (plain text preferred, HTML
converted, hidden elements removed and marked). Long bodies come in windows of
`max_chars` (20 000 by default, at most 40 000): call again with
`offset=next_offset` while `truncated` is true. Reading does not mark it read.
Each attachment comes with `index`, `filename`, `content_type`, `size`, `inline`
and `extractable` (with a `reason` when false); contents are never included.

**`get_attachment_text(id, index, offset?, max_chars?)`** the text of one
attachment: PDF (the first 50 pages, `pages_total` and `pages_read`), DOCX
(paragraphs and tables in order), TXT and CSV (BOM, then the MIME charset, then
UTF-8, then cp1251). The type is detected from the content, so a PDF sent as
`application/octet-stream` is read, and an `image/png` that is really a PDF is
refused as `type mismatch`. Windows like `get_email`. A PDF locked only by an
owner password is read with `note: "owner-password protected"`; a scan without a
text layer gives an empty `text` and a note, there is no OCR. A damaged page is
skipped and listed in `note` as `unreadable pages`. Errors: `unsupported
type`, `too large` (over 15 MB), `encrypted`, `unsafe xml`, `timeout`, `memory
limit`.

PDF and DOCX are parsed in a separate process per call, killed after 30 seconds.
The process gets a memory limit: a Job Object of 1 GB on Windows, `RLIMIT_AS` of
2 GB on Linux. **macOS has no memory limit**: its kernel does not enforce
`RLIMIT_AS`, so only the size and time limits apply there. DOCX parts are capped
at 50 MB unpacked and parsed without DTDs or entities. The last two downloaded
messages stay in memory for 5 minutes (not those over 25 MB), so reading several
attachments of one message downloads it once.

Dates are ISO; `since` is `YYYY-MM-DD`. Message content is written by third
parties and is data, not instructions; the tool descriptions say so to the model.

## Troubleshooting

- **`status: "no-credential"`**: the account's `password_env` variable is not
  set in the server's environment. Remember the server inherits its env from
  whatever launched it (your MCP client), not from your interactive shell.
- **`status: "unreachable: MailboxLoginError"`** on Gmail/iCloud/Yahoo: you're
  using the account password; these providers require an app password (see
  cheat sheet).
- **Config not found**: the server looks for `accounts.toml` next to
  `pyproject.toml`, or wherever `IMAP_MCP_ACCOUNTS` points.

## Development

```sh
uv run pytest
```

31 tests, no network: the IMAP layer is faked. The suite locks in the
read-only contract (`mark_seen=False` on every fetch), per-account error
isolation, and config handling.

## License

MIT
