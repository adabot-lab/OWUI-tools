# Legal Paragraph Retrieval Service

MCP server for retrieving German and EU legal texts via full-text search and
exact paragraph lookup. Law texts are fetched deterministically from official
sources — no LLM, no manual data entry.

## Architecture

```
input/sources.txt (URLs)
       │
       ▼
fetch/fetcher.py ── download + classify by domain
       │
       ├── gesetze-im-internet.de/*.zip      → fetch/parsers/gii_xml.py
       ├── verwaltungsvorschriften-.../*.htm  → fetch/parsers/vv_html.py
       ├── eur-lex.europa.eu/...CELEX:...     → fetch/parsers/eurlex_html.py
       │                                          (rewritten to CELLAR API)
       └── gesetze.berlin.de/perma?j=...      → fetch/parsers/bsbe_xml.py
                                                  (AIZ ZIP download, HTTP/2)
       │
       ├── data/cache/ (optional offline snapshot)
       │
       ▼
SQLite + FTS5 (data/legal.db)
       │
       ▼
legal_engine.py → main.py (MCP server, 3 tools)
```

## Data Loading Modes

Controlled via `FETCH_MODE` env var (Docker entrypoint):

| Mode | Behavior |
|------|----------|
| `serve` (default) | Start MCP server with existing DB. No data loading. |
| `fetch` | **Nuke DB + cache**, download fresh from sources.txt, populate DB, save to cache, then serve. |
| `local` | **Nuke DB**, parse from `data/cache/` (no network), populate DB, then serve. |

Typical workflow:
1. First run: `FETCH_MODE=fetch` — downloads everything, populates DB + cache.
2. Subsequent runs: `FETCH_MODE=serve` — uses existing DB.
3. Laws changed upstream: `FETCH_MODE=fetch` again.
4. Offline rebuild: `FETCH_MODE=local` — uses cached files, no network.

### Cache Layout

```
data/cache/
    manifest.json              # maps filename → {url, source_type}
    vgv_2016.xml               # GII XML (unzipped)
    gwb.xml
    bsvwvbund_*.htm            # VV HTML (raw)
    eurlex_02014L0024.xhtml    # EUR-Lex XHTML (raw)
    VergabeG_BE.xml            # BSBE XML (extracted from AIZ ZIP)
```

## Source Types

The fetcher auto-detects the source type from the URL domain:

| Source | URL Pattern | Format |
|--------|-------------|--------|
| **GII** (gesetze-im-internet.de) | `.../<slug>/xml.zip` | ZIP with GII norm DTD XML |
| **VV** (verwaltungsvorschriften-im-internet.de) | `.../<doc>.htm` | Semi-structured HTML |
| **EUR-Lex** (eur-lex.europa.eu) | `...?uri=CELEX:<number>` | ELI-annotated XHTML (via CELLAR API) |
| **BSBE** (gesetze.berlin.de) | `.../perma?j=<law_id>` | GII norm DTD XML via AIZ ZIP (HTTP/2) |

EUR-Lex URLs are automatically rewritten to the CELLAR API at
`publications.europa.eu/resource/celex/{CELEX}` to bypass the AWS WAF block
on the EUR-Lex frontend.

BSBE URLs use the jportal AIZ ZIP endpoint — the fetcher resolves the perma
redirect, establishes a session via POST /init, and downloads the AIZ ZIP
containing the law XML.

## Adding Laws

Edit `input/sources.txt` — one URL per line. Comments (`#`) and empty lines
are ignored. See the file header for URL format examples.

```bash
# Download + store in DB
python -m fetch.run_fetch

# Download + save to cache (for offline use)
python -m fetch.run_fetch --cache

# Parse from cache (no network)
python -m fetch.run_fetch --from-cache

# Dry run (fetch + validate, no DB write)
python -m fetch.run_fetch --dry-run
```

## Configuration

All config via environment variables (`.env` file or docker-compose):

| Variable | Default | Description |
|----------|---------|-------------|
| `FETCH_MODE` | `serve` | Data loading mode: serve/fetch/local |
| `LEGAL_SOURCES_FILE` | `input/sources.txt` | Source URLs file |
| `LEGAL_DB_PATH` | `data/legal.db` | SQLite database path |
| `MCP_HOST` | `0.0.0.0` | MCP server bind host |
| `MCP_PORT` | `8000` | MCP server port |
| `MCP_ALLOWED_HOSTS` | (empty) | Comma-separated allowed Host headers |

## MCP Tools

| Tool | Description |
|------|-------------|
| `retrieve_paragraph(law_name, section_number)` | Exact paragraph lookup by law + section number |
| `search_paragraphs(query, limit=20)` | Full-text search with ranked snippets |
| `list_laws()` | List all laws with section counts |

## Docker

```bash
# Default: serve with existing DB
docker compose up -d --build

# Fresh download + populate
FETCH_MODE=fetch docker compose up -d --build

# Offline rebuild from cache
FETCH_MODE=local docker compose up -d --build
```

The server listens on port 8000 at `/mcp`.

## Development

```bash
# Create venv
python -m venv .venv && source .venv/bin/activate

# Install deps
pip install -r requirements.txt

# Run tests
python -m pytest tests/ -v

# Run fetch pipeline
python -m fetch.run_fetch

# Start MCP server directly (skips entrypoint mode logic)
python main.py
```

## Testing

Test fixtures are trimmed versions of real source documents in
`tests/testdata/`:

- `vgv_sample.xml` — GII XML (VgV, 3 paragraphs)
- `vob_sample.htm` — VV HTML (VOB/A, 5 paragraphs across 3 Abschnitte)
- `eurlex_sample.xhtml` — EUR-Lex XHTML (RL 2014/24/EU, 3 articles)
- `berlavg_sample.xml` — BSBE XML (BerlAVG, 3 paragraphs with HTML textdaten)

```bash
python -m pytest tests/ -v          # full suite
python -m pytest tests/test_gii_xml.py -v  # single parser
```
