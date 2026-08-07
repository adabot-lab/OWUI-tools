"""
Himalaya MCP Server — Streamable HTTP Transport (Draft-Only)
=============================================================
Read + draft email via himalaya CLI. NO send capability.
SMTP deliberately absent from config — structural enforcement.
"""

import asyncio
import json
import os
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

HIMALAYA_BIN = os.getenv("HIMALAYA_BIN", "himalaya")
HIMALAYA_CONFIG_DIR = os.getenv("HIMALAYA_CONFIG_DIR", "/config")
# Resolve the actual config FILE. Allow a direct override via
# HIMALAYA_CONFIG_FILE; otherwise expect config.toml inside the dir.
# himalaya's --config flag takes a FILE, not a directory — passing the
# directory causes "cannot read config file ... Is a directory".
HIMALAYA_CONFIG_FILE = os.getenv("HIMALAYA_CONFIG_FILE") or os.path.join(
    HIMALAYA_CONFIG_DIR, "config.toml"
)
# When set, forces a specific account for all tools that don't receive an
# explicit `account` argument. When empty (default), no `-a` flag is passed
# and himalaya resolves the account itself (the one with `default = true`).
DEFAULT_ACCOUNT = os.getenv("DEFAULT_ACCOUNT", "")
DRAFTS_FOLDER = os.getenv("DRAFTS_FOLDER", "Drafts")
DATA_DIR = os.getenv("DATA_DIR", "/data")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "9201"))

# Allowed Host header values for FastMCP's DNS-rebinding protection
# (Starlette TrustedHostMiddleware-equivalent). "*" disables protection and
# allows any host; otherwise a comma-separated list of allowed host values.
# Each entry may use a ":*" suffix as a port wildcard, e.g. "192.168.0.209:*".
_allowed_hosts_raw = os.getenv("ALLOWED_HOSTS", "*").strip()
if _allowed_hosts_raw == "*":
    _transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
else:
    _allowed_hosts = [h.strip() for h in _allowed_hosts_raw.split(",") if h.strip()]
    _transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=_allowed_hosts)


def _acc(account: Optional[str]) -> list:
    """Build the -a flag args for himalaya.

    Priority: explicit caller arg > DEFAULT_ACCOUNT env > omit (let
    himalaya pick the account marked `default = true` in config.toml).

    Passing a wrong account name (e.g. hardcoded "main" when the config
    defines "assistant") causes: 'cannot find configuration for account'.
    Omitting -a entirely lets himalaya resolve it correctly.
    """
    acc = account or DEFAULT_ACCOUNT
    return ["-a", acc] if acc else []


def _validate_config_path() -> None:
    """Fail fast on the classic misconfiguration: resolved path is a directory.
    A merely missing file is only a warning, so health_check and the MCP
    handshake still work before live IMAP creds are mounted."""
    if os.path.isdir(HIMALAYA_CONFIG_FILE):
        print(
            "FATAL: HIMALAYA_CONFIG_FILE "
            f"'{HIMALAYA_CONFIG_FILE}' is a directory, but himalaya "
            "--config expects a FILE. Point HIMALAYA_CONFIG_DIR at the dir "
            "containing config.toml, or set HIMALAYA_CONFIG_FILE to the "
            ".toml file directly.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if not os.path.isfile(HIMALAYA_CONFIG_FILE):
        print(
            f"WARNING: config file not found at '{HIMALAYA_CONFIG_FILE}' "
            f"(dir '{HIMALAYA_CONFIG_DIR}'). IMAP tools will fail until a "
            "valid config.toml is mounted.",
            file=sys.stderr,
        )


_validate_config_path()

mcp = FastMCP("himalaya", json_response=True, transport_security=_transport_security)


async def _himalaya(*args) -> dict:
    cmd = [HIMALAYA_BIN, "--config", HIMALAYA_CONFIG_FILE,
           "--output", "json", "--quiet"] + list(args)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": "himalaya command timed out after 30 seconds", "returncode": -1}
    if proc.returncode != 0:
        return {"error": stderr.decode().strip(), "returncode": proc.returncode}
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError:
        return {"raw": stdout.decode().strip()}


@mcp.tool()
async def folder_list(account: Optional[str] = None) -> str:
    """List all mailboxes/folders for the account."""
    result = await _himalaya("folder", "list", *_acc(account))
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def envelope_list(
    folder: str = "INBOX",
    query: str = "",
    page: int = 1,
    page_size: int = 20,
    account: Optional[str] = None,
) -> str:
    """List message envelopes with optional filter query.
    Query examples: 'not flag seen', 'from sender@example.com',
    'subject invoice', 'before 2026-01-01'."""
    args = ["envelope", "list", *_acc(account), "-f", folder]
    args.extend(["--page", str(page), "--page-size", str(page_size)])
    if query:
        args.append(query)
    result = await _himalaya(*args)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def message_read(
    id: str,
    peek: bool = True,
    folder: str = "INBOX",
    account: Optional[str] = None,
) -> str:
    """Read message body. Defaults to peek=true (does NOT mark as read).
    Set peek=false to mark as read after reading."""
    args = ["message", "read", *_acc(account), "-f", folder, id]
    if peek:
        args.append("--preview")
    result = await _himalaya(*args)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def message_export(
    id: str,
    peek: bool = True,
    folder: str = "INBOX",
    account: Optional[str] = None,
) -> str:
    """Export raw MIME (for ICS/calendar parsing). Defaults to peek=true.
    Since message export has no --preview flag, peek is achieved by immediately removing \\Seen flag after export.
    NOTE: This is NOT atomic — between export (which marks \\Seen) and flag removal, the message briefly appears as read. This is a known race condition with no better workaround in the himalaya CLI.
    Exports to a temp dir, reads back the .eml content, cleans up."""
    import tempfile, glob, os
    tmpdir = tempfile.mkdtemp(prefix="himalaya_export_")
    result = await _himalaya(
        "message", "export", *_acc(account), "-f", folder,
        "-d", tmpdir, id, "--full")
    # Find the exported .eml file
    eml_files = glob.glob(os.path.join(tmpdir, "*.eml"))
    raw_mime = ""
    if eml_files:
        with open(eml_files[0], "r", errors="replace") as f:
            raw_mime = f.read()
        # Clean up temp files
        for fp in eml_files:
            try:
                os.remove(fp)
            except OSError:
                pass
    try:
        os.rmdir(tmpdir)
    except OSError:
        pass
    # Undo auto-Seen from export (before return)
    if peek and "error" not in result:
        await _himalaya("flag", "remove", *_acc(account), "-f", folder, id, "seen")
    output = result if "error" in result else {
        "id": id, "folder": folder,
        "raw_mime": raw_mime, "mime_length": len(raw_mime),
    }
    return json.dumps(output, ensure_ascii=False, indent=2)


@mcp.tool()
async def attachment_download(
    id: str,
    folder: str = "INBOX",
    account: Optional[str] = None,
) -> str:
    """Download all attachments for a message to the data directory."""
    result = await _himalaya("attachment", "download", *_acc(account), "-f", folder, "-d", DATA_DIR, id)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def template_write(account: Optional[str] = None) -> str:
    """Generate a blank MML (Markdown Mail) template for composing."""
    result = await _himalaya("template", "write", *_acc(account))
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def template_reply(
    id: str,
    folder: str = "INBOX",
    account: Optional[str] = None,
) -> str:
    """Generate a reply MML template for the given message."""
    result = await _himalaya("template", "reply", *_acc(account), "-f", folder, id)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def template_forward(
    id: str,
    folder: str = "INBOX",
    account: Optional[str] = None,
) -> str:
    """Generate a forward MML template for the given message."""
    result = await _himalaya("template", "forward", *_acc(account), "-f", folder, id)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def template_save(
    mml: str,
    folder: Optional[str] = None,
    account: Optional[str] = None,
) -> str:
    """Compile MML and save to Drafts folder via IMAP APPEND.
    Does NOT send via SMTP — SMTP is not configured in this container.

    Implementation note: himalaya v1.2.0's `template save` chooses its input
    source via `if is_tty || is_json` (save.rs:68-79). Because this server
    always runs himalaya with `--output json`, is_json is true and himalaya
    reads the MML from the trailing positional `TEMPLATE` argument — NOT from
    stdin. Piping MML to stdin (the previous _himalaya_stdin approach) is
    silently ignored, an empty template is compiled, and the compiler errors
    with "cannot parse template". The MML MUST be passed as a positional arg.
    """
    fld = folder or DRAFTS_FOLDER
    result = await _himalaya("template", "save", *_acc(account), "-f", fld, mml)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def flag_set(
    id: str,
    flag: str,
    add: bool = True,
    folder: str = "INBOX",
    account: Optional[str] = None,
) -> str:
    """Add or remove a flag on a message.
    Common flags: 'seen' (\\Seen), 'flagged' (\\Flagged), 'answered' (\\Answered).
    Use add=false to remove the flag."""
    action = "add" if add else "remove"
    result = await _himalaya("flag", action, *_acc(account), "-f", folder, id, flag)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def health_check() -> str:
    """Check server health and confirm no send capability exists."""
    status = {
        "status": "ok",
        "server": "himalaya-mcp",
        "mode": "draft-only (no send capability)",
        "config": {
            "HIMALAYA_BIN": HIMALAYA_BIN,
            "HIMALAYA_CONFIG_DIR": HIMALAYA_CONFIG_DIR,
            "HIMALAYA_CONFIG_FILE": HIMALAYA_CONFIG_FILE,
            "DEFAULT_ACCOUNT": DEFAULT_ACCOUNT or "(himalaya default)",
            "DRAFTS_FOLDER": DRAFTS_FOLDER,
            "DATA_DIR": DATA_DIR,
        },
        "tools_registered": [
            "folder_list", "envelope_list", "message_read",
            "message_export", "attachment_download", "template_write",
            "template_reply", "template_forward", "template_save",
            "flag_set", "health_check",
        ],
        "send_capability": False,
    }
    return json.dumps(status, indent=2)


app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
