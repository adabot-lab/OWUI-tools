# himalaya-mcp

Read + draft-only email MCP server via Streamable HTTP transport. Wraps the [himalaya](https://github.com/pimalaya/himalaya) CLI v2.0.0 in a Docker container, exposing 12 tools with **zero send capability, draft-only delete (move-to-Trash)**.

## What it does

- **Read** email: list envelopes, read bodies, export raw MIME, download attachments
- **Draft** email: generate MIME (RFC 5322) skeletons, save replies/forwards to Drafts via IMAP APPEND
- **Flags**: add/remove `\Seen`, `\Flagged`, `\Answered`
- **NO send** — structurally impossible at code, config, and transport levels
- **Peek by default** — all read ops leave messages unread unless explicitly flagged

Built for automated/cron workflows where failed LLM processing must leave mail unread for retry.

## Security model (defense in depth)

Three independent layers ensure no email can ever be sent:

1. **Code-level**: No `message_send`, `template_send`, or `account_configure` tool exists in `server.py`. Not commented out, not behind a flag — absent. The `draft_delete` tool exists but is hardcoded to the Drafts folder (DRAFTS_FOLDER env var) and only moves the draft to the Trash folder (TRASH_FOLDER env var) — it cannot target other folders.

2. **Config-level**: Mounted `config.toml` omits any `smtp.*` section entirely. Himalaya has no SMTP backend configured — it literally cannot send even if a tool tried.

3. **Peek-default**: All read operations default to `peek=true`. The server controls \Seen state explicitly via the `flag_set` tool. This protects cron retry patterns — a failed processing step leaves mail unread.

The `health_check` tool confirms `send_capability: false` at runtime.

## Tools (12 total)

### Read tools

#### `folder_list(account?)`
List all mailboxes/folders for the account.
- `account` (str, optional): Account name. Defaults to `DEFAULT_ACCOUNT`.

#### `envelope_list(folder?, query?, page?, page_size?, account?)`
List message envelopes with optional filter query. When a `query` is given, the tool routes to himalaya `envelope search` under the hood (v2 `envelope list` has no query positional).
- `folder` (str, default `"INBOX"`): Mailbox to list.
- `query` (str, default `""`): Himalaya filter query. Examples: `"not flag seen"`, `"from sender@example.com"`, `"subject invoice"`, `"before 2026-01-01"`.
- `page` (int, default `1`): Page number.
- `page_size` (int, default `20`): Messages per page.
- `account` (str, optional): Account name.

#### `message_read(id, peek?, folder?, account?)`
Read message body text.
- `id` (str): Message ID (from `envelope_list`).
- `peek` (bool, default `true`): If true, does NOT mark as `\Seen`. This is structural in v2 — himalaya `message read` uses IMAP BODY.PEEK internally and never sets `\Seen` on its own. With `peek=false` the server explicitly runs `flag add seen` afterwards.
- `folder` (str, default `"INBOX"`): Mailbox.
- `account` (str, optional): Account name.

#### `message_export(id, peek?, folder?, account?)`
Export raw MIME (for ICS/calendar attachment parsing).
- `id` (str): Message ID.
- `peek` (bool, default `true`): If true, does NOT mark as `\Seen`. This is peek-safe by design — the tool uses himalaya `message read --raw`, which reads via IMAP BODY.PEEK and never sets `\Seen`, so no flag-removal race condition exists. With `peek=false` the server explicitly runs `flag add seen` afterwards.
- `folder` (str, default `"INBOX"`): Mailbox.
- `account` (str, optional): Account name.

Returns JSON with `raw_mime`, `mime_length`. Reads the raw RFC 5322 MIME directly from stdout (no temp dir involved).

#### `attachment_download(id, folder?, account?)`
Download all attachments for a message to the data directory (`/data` volume).
- `id` (str): Message ID.
- `folder` (str, default `"INBOX"`): Mailbox.
- `account` (str, optional): Account name.

### Draft tools

#### `template_write(account?)`
Generate a blank MIME (RFC 5322) skeleton for composing.
- `account` (str, optional): Account name.

#### `template_reply(id, folder?, account?)`
Generate a reply MIME skeleton pre-filled from the given message.
- `id` (str): Message ID to reply to.
- `folder` (str, default `"INBOX"`): Mailbox.
- `account` (str, optional): Account name.

#### `template_forward(id, folder?, account?)`
Generate a forward MIME skeleton pre-filled from the given message.
- `id` (str): Message ID to forward.
- `folder` (str, default `"INBOX"`): Mailbox.
- `account` (str, optional): Account name.

#### `template_save(mml, folder?, account?)`
Save a message to the Drafts folder via IMAP APPEND. Does NOT send via SMTP.
- `mml` (str): The edited MIME from `template_write`/`template_reply`/`template_forward`. Full MIME (containing `MIME-Version:` or `Content-Type:`) passes through unchanged; simple `Key: Value` + blank line + body text is still compiled to MIME in Python for backwards compat.
- `folder` (str, optional): Target folder. Defaults to `DRAFTS_FOLDER`.
- `account` (str, optional): Account name.

#### `draft_delete(id, account?)`
Delete a draft from the Drafts folder by moving it to the Trash folder (recoverable). Drafts folder only — cannot delete messages from other folders. The folders are hardcoded to DRAFTS_FOLDER (source) and TRASH_FOLDER (destination) and not exposed as parameters.
- `id` (str): Message ID of the draft to delete.
- `account` (str, optional): Account name.

### Flag tools

#### `flag_set(id, flag, add?, folder?, account?)`
Add or remove a flag on a message.
- `id` (str): Message ID.
- `flag` (str): Flag name. Common: `"seen"` (`\Seen`), `"flagged"` (`\Flagged`), `"answered"` (`\Answered`).
- `add` (bool, default `true`): `true` to add, `false` to remove.
- `folder` (str, default `"INBOX"`): Mailbox.
- `account` (str, optional): Account name.

### Health tools

#### `health_check()`
Returns server status, config values, registered tool list, and confirms `send_capability: false`. No parameters.

Example output:
```json
{
  "status": "ok",
  "server": "himalaya-mcp",
  "mode": "draft-only (no send capability)",
  "send_capability": false,
  "tools_registered": ["folder_list", "envelope_list", "...12 tools..."]
}
```

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_HOST` | `0.0.0.0` | Server bind address |
| `MCP_PORT` | `9201` | Server port |
| `HIMALAYA_CONFIG_DIR` | `/config` | Himalaya config directory (mounted read-only) |
| `HIMALAYA_CONFIG_FILE` | `<CONFIG_DIR>/config.toml` | Config **file** path (override to point elsewhere). Himalaya's `--config` expects a file, not a directory. |
| `DEFAULT_ACCOUNT` | *(empty)* | Account name for tools without explicit `account` arg. When empty, himalaya auto-selects the account with `default = true` in config.toml. |
| `DRAFTS_FOLDER` | `Drafts` | IMAP folder for saving drafts |
| `TRASH_FOLDER` | `Trash` | Destination folder for `draft_delete`'s move-to-Trash |
| `DATA_DIR` | `/data` | Attachment download directory |

### Config file (`config.toml`)

Mount a `config.toml` at `HIMALAYA_CONFIG_DIR`. See `config/config.example.toml` for the template. Key requirements (himalaya v2 flat `imap.*` notation):

- **IMAP only** — no `smtp.*` section
- Account name must match `DEFAULT_ACCOUNT` (default: `main`)
- Password via `imap.sasl.plain.password.raw` (inline) or `imap.sasl.plain.password.command` (command like `pass show mail/example`)

Example (`config.example.toml`):
```toml
[accounts.main]
default = true
email = "user@example.com"
display-name = "Your Name"
downloads-dir = "/data"
imap.server = "imap.example.com"
imap.sasl.plain.username = "user@example.com"
imap.sasl.plain.password.raw = "YOUR_PASSWORD_HERE"

# NO smtp.* section — SMTP deliberately omitted (send capability
# structurally absent)
```

## Docker setup

### Prerequisites

The `owui-tools` Docker network must exist (shared with other OWUI-tools services):
```bash
docker network create owui-tools  # only if it doesn't exist yet
```

### Build and run

```bash
cd himalaya-mcp/

# 1. Create config and data directories
mkdir -p config data

# 2. Copy and edit config
cp config.example.toml config/config.toml
# Edit config/config.toml with your IMAP credentials

# 3. Create .env (optional — defaults work for standard setup)
cp .env.example .env

# 4. Build and start
docker compose up -d

# 5. Check logs
docker compose logs -f
```

### Verify

```bash
# Test MCP initialize handshake
curl -X POST http://127.0.0.1:9201/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

# Check himalaya binary inside container
docker exec himalaya-mcp himalaya --version
```

## Integration

### Open WebUI

Admin → Settings → Integrations → Tool Servers → Add:
- **Type**: MCP
- **URL**: `http://himalaya-mcp:9201` (container name on `owui-tools` network)
- **Auth**: none

### Hermes Agent

Register as MCP server in config.yaml:
```yaml
mcp_servers:
  himalaya:
    url: "http://himalaya-mcp:9201/mcp"
    transport: "streamable_http"
```

## Architecture

```
┌─────────────────────────────────────────┐
│  Docker Container (python:3.13-slim)     │
│                                         │
│  ┌─────────────┐   ┌─────────────────┐  │
│  │  uvicorn     │   │  himalaya v2.0.0 │  │
│  │  :9201       │──▶│  (static binary) │  │
│  │  FastMCP     │   │  IMAP only       │  │
│  │  12 tools    │   │  No SMTP config   │  │
│  └─────────────┘   └────────┬────────┘  │
│                             │            │
│  ┌─────────────┐   ┌────────▼────────┐  │
│  │ /data        │   │ /config          │  │
│  │ (attachments)│   │ (config.toml:ro) │  │
│  └─────────────┘   └─────────────────┘  │
└─────────────────────────────────────────┘
         │                        │
    owui-tools network      host mount (read-only)
```

- **Transport**: MCP Streamable HTTP (protocol 2025-03-26)
- **Port**: 9201 (bound to `127.0.0.1` on host)
- **Network**: `owui-tools` (external, shared with other OWUI-tools)
- **Config**: mounted read-only from `./config/`
- **Data**: `./data/` for attachment downloads

## File structure

```
himalaya-mcp/
├── server.py              # FastMCP server — 12 tools
├── Dockerfile             # python:3.13-slim + himalaya v2.0.0 static binary
├── docker-compose.yml     # port 9201, owui-tools network, config/data volumes
├── .env.example           # environment variable template
├── requirements.txt       # mcp>=1.9.0, uvicorn[standard], starlette
├── config.example.toml    # IMAP-only config template (no SMTP)
├── .gitignore             # ignores config/, data/, .env
└── README.md              # this file
```

## Deliberately absent tools

These do NOT exist in the codebase — not registered, not commented out, not behind a flag:

- `message_send` / `template_send` (SMTP delivery)
- Any way to move messages between arbitrary folders — `draft_delete` only moves Drafts → Trash
- `folder_delete` / `folder_purge` / `folder_expunge`
- `account_configure`
