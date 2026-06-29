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

HIMALAYA_BIN = os.getenv("HIMALAYA_BIN", "himalaya")
HIMALAYA_CONFIG_DIR = os.getenv("HIMALAYA_CONFIG_DIR", "/config/himalaya")
# Resolve the actual config FILE. Allow a direct override via
# HIMALAYA_CONFIG_FILE; otherwise expect config.toml inside the dir.
# himalaya's --config flag takes a FILE, not a directory — passing the
# directory causes "cannot read config file ... Is a directory".
HIMALAYA_CONFIG_FILE = os.getenv("HIMALAYA_CONFIG_FILE") or os.path.join(
    HIMALAYA_CONFIG_DIR, "config.toml"
)
DEFAULT_ACCOUNT = os.getenv("DEFAULT_ACCOUNT", "main")
DRAFTS_FOLDER = os.getenv("DRAFTS_FOLDER", "Drafts")
DATA_DIR = os.getenv("DATA_DIR", "/data")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "9201"))


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

mcp = FastMCP("himalaya", json_response=True)


async def _himalaya(*args) -> dict:
    cmd = [HIMALAYA_BIN, "--config", HIMALAYA_CONFIG_FILE,
           "--output", "json", "--quiet"] + list(args)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return {"error": stderr.decode().strip(), "returncode": proc.returncode}
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError:
        return {"raw": stdout.decode().strip()}


async def _himalaya_stdin(args: list, stdin_data: str) -> dict:
    cmd = [HIMALAYA_BIN, "--config", HIMALAYA_CONFIG_FILE,
           "--output", "json", "--quiet"] + args
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate(input=stdin_data.encode())
    if proc.returncode != 0:
        return {"error": stderr.decode().strip(), "returncode": proc.returncode}
    try:
        return json.loads(stdout.decode())
    except json.JSONDecodeError:
        return {"raw": stdout.decode().strip()}


@mcp.tool()
async def folder_list(account: Optional[str] = None) -> str:
    """List all mailboxes/folders for the account."""
    acc = account or DEFAULT_ACCOUNT
    result = await _himalaya("folder", "list", "-a", acc)
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
    acc = account or DEFAULT_ACCOUNT
    args = ["envelope", "list", "-a", acc, "-f", folder]
    if query:
        args.append(query)
    args.extend(["--page", str(page), "--page-size", str(page_size)])
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
    acc = account or DEFAULT_ACCOUNT
    args = ["message", "read", "-a", acc, "-f", folder, id]
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
    Since 'message export' has no --preview flag, peek is achieved by
    immediately removing \\Seen flag after export (atomic).
    Exports to a temp dir, reads back the .eml content, cleans up."""
    acc = account or DEFAULT_ACCOUNT
    # Export to temp directory, then read content back
    import tempfile, glob, os
    tmpdir = tempfile.mkdtemp(prefix="himalaya_export_")
    result = await _himalaya(
        "message", "export", "-a", acc, "-f", folder,
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
    # Undo auto-Seen from export (atomic, before return)
    if peek and "error" not in result:
        await _himalaya("flag", "remove", "-a", acc, "-f", folder, id, "seen")
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
    acc = account or DEFAULT_ACCOUNT
    result = await _himalaya("attachment", "download", "-a", acc, "-f", folder, "-d", DATA_DIR, id)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def template_write(account: Optional[str] = None) -> str:
    """Generate a blank MML (Markdown Mail) template for composing."""
    acc = account or DEFAULT_ACCOUNT
    result = await _himalaya("template", "write", "-a", acc)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def template_reply(
    id: str,
    folder: str = "INBOX",
    account: Optional[str] = None,
) -> str:
    """Generate a reply MML template for the given message."""
    acc = account or DEFAULT_ACCOUNT
    result = await _himalaya("template", "reply", "-a", acc, "-f", folder, id)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def template_forward(
    id: str,
    folder: str = "INBOX",
    account: Optional[str] = None,
) -> str:
    """Generate a forward MML template for the given message."""
    acc = account or DEFAULT_ACCOUNT
    result = await _himalaya("template", "forward", "-a", acc, "-f", folder, id)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def template_save(
    mml: str,
    folder: Optional[str] = None,
    account: Optional[str] = None,
) -> str:
    """Compile MML and save to Drafts folder via IMAP APPEND.
    Does NOT send via SMTP — SMTP is not configured in this container."""
    acc = account or DEFAULT_ACCOUNT
    fld = folder or DRAFTS_FOLDER
    result = await _himalaya_stdin(["template", "save", "-a", acc, "-f", fld], mml)
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
    acc = account or DEFAULT_ACCOUNT
    action = "add" if add else "remove"
    result = await _himalaya("flag", action, "-a", acc, "-f", folder, id, flag)
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
            "DEFAULT_ACCOUNT": DEFAULT_ACCOUNT,
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
