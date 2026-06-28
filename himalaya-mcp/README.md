# himalaya-mcp

Read + draft-only email MCP server via Streamable HTTP transport. Wraps the [himalaya](https://github.com/pimalaya/himalaya) CLI.

## What it does

- **Read** email: list envelopes, read bodies, export raw MIME, download attachments
- **Draft** email: generate MML templates, save replies/forwards to Drafts via IMAP APPEND
- **NO send capability** — structurally impossible at code, config, and transport levels

## Security model

Three defense-in-depth layers:

1. **Code-level**: No `message_send`, `template_send`, or `message_delete` tool exists in server.py. Not commented out, not behind a flag — absent.
2. **Config-level**: Mounted `config.toml` omits `[message.send]` backend entirely. Himalaya cannot send via SMTP even if a tool tried.
3. **Peek-default**: All read operations default `peek=true` (do not mark as `\Seen`). Explicit `flag_set` with `add=true` + `flag=seen` required to mark as read.

## Tools

### Read tools

| Tool | Description |
|------|-------------|
| `folder_list` | List all mailboxes/folders |
| `envelope_list` | List envelopes with filter queries (`not flag seen`, `from X`, `subject Y`, dates) |
| `message_read` | Read message body (peek by default, no `\Seen`) |
| `message_export` | Export raw MIME for ICS/calendar parsing (peek by default) |
| `attachment_download` | Download all attachments to data directory |

### Draft tools

| Tool | Description |
|------|-------------|
| `template_write` | Generate a blank MML template |
| `template_reply` | Generate a reply MML template |
| `template_forward` | Generate a forward MML template |
| `template_save` | Compile MML and save to Drafts via IMAP APPEND (no SMTP) |

### Flag tools

| Tool | Description |
|------|-------------|
| `flag_set` | Add or remove flags (`seen`, `flagged`, `answered`) |

### Health

| Tool | Description |
|------|-------------|
| `health_check` | Server status, config values, no-send confirmation |

## Configuration

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_HOST` | `0.0.0.0` | Server bind address |
| `MCP_PORT` | `9201` | Server port |
| `HIMALAYA_CONFIG_DIR` | `/config/himalaya` | Himalaya config directory |
| `DEFAULT_ACCOUNT` | `main` | Default himalaya account |
| `DRAFTS_FOLDER` | `Drafts` | IMAP folder for saving drafts |
| `DATA_DIR` | `/data` | Attachment download directory |

### Config file structure

Mount a `config.toml` at the path specified by `HIMALAYA_CONFIG_DIR`. See `config.example.toml` for the full structure. Key requirements:

- IMAP backend only — no SMTP section
- Account name `main` by default (configurable via `DEFAULT_ACCOUNT`)

## Docker setup

```bash
# 1. Create config and data directories
mkdir -p config data

# 2. Copy and edit config
cp config.example.toml config/config.toml
# Edit config/config.toml with your IMAP credentials

# 3. Create password file if using passwd-cmd
echo -n 'your-password' > config/password

# 4. Build and start
docker compose build
docker compose up -d

# 5. Check logs
docker compose logs -f
```

## Open WebUI integration

Register as a Streamable HTTP MCP tool:

- **URL**: `http://himalaya-mcp:9201`
- **Port**: `9201`
- **Protocol**: Streamable HTTP (2025-03-26)

## Port

`9201`
