"""Legal Paragraph Retrieval Service — MCP streamable HTTP entrypoint.

Exposes three MCP tools: retrieve_paragraph, search_paragraphs, list_laws.
Served via streamable HTTP at http://host:port/mcp (default port 8000).
"""
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from legal_engine import LegalEngine

# --- Configuration via environment ---
HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_PORT", "8000"))

# Comma-separated list of allowed Host headers (DNS-rebinding protection).
# In Docker, set this to the hostname/port clients will use to reach the
# container, e.g. MCP_ALLOWED_HOSTS=my-lit.le.rag:8000
_ALLOWED_HOSTS_RAW = os.getenv("MCP_ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _ALLOWED_HOSTS_RAW.split(",") if h.strip()]

# Initialize engine (creates/opens SQLite DB)
engine = LegalEngine()

# --- Transport security ---
# streamable_http_app() defaults to localhost-only (rejects non-localhost
# with 421). For Docker or remote access, pass explicit allowed_hosts.
if ALLOWED_HOSTS:
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=ALLOWED_HOSTS,
        allowed_origins=[f"http://{h}" for h in ALLOWED_HOSTS],
    )
else:
    # No allowlist configured — disable DNS-rebinding protection so the
    # server is reachable from any host. Suitable for trusted local/Docker
    # networks. For public exposure, always set MCP_ALLOWED_HOSTS.
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )

mcp = FastMCP(
    "Legal Paragraph Retrieval Service",
    host=HOST,
    port=PORT,
    transport_security=transport_security,
)


@mcp.tool()
def retrieve_paragraph(law_name: str, section_number: str) -> dict:
    """
    Retrieve a specific legal paragraph by law name and section number.
    Returns the exact paragraph content for a given law and section.

    Args:
        law_name: Law abbreviation (e.g., "VgV", "GWB") or full name.
        section_number: Section number (e.g., "97" for § 97).
    """
    return engine.retrieve_paragraph(law_name, section_number)


@mcp.tool()
def search_paragraphs(query: str, limit: int = 20) -> list[dict]:
    """
    Full-text search across all legal paragraphs.
    Returns ranked results with text snippets.

    Args:
        query: Search terms in German.
        limit: Maximum results (default 20, max 100).
    """
    return engine.search_paragraphs(query, limit)


@mcp.tool()
def list_laws() -> list[dict]:
    """List all available laws in the collection with section counts."""
    return engine.list_laws()


# Build the ASGI app
app = mcp.streamable_http_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
