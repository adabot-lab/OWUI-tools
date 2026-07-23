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
       └── eur-lex.europa.eu/...CELEX:...     → fetch/parsers/eurlex_html.py
                                                   (rewritten to CELLAR API)
       │
       ▼
SQLite + FTS5 (data/legal.db)
       │
       ▼
legal_engine.py → main.py (MCP server, 3 tools)
```

## Source Types

The fetcher auto-detects the source type from the URL domain:

| Source | URL Pattern | Format |
|--------|-------------|--------|
| **GII** (gesetze-im-internet.de) | `.../<slug>/xml.zip` | ZIP with GII norm DTD XML |
| **VV** (verwaltungsvorschriften-im-internet.de) | `.../<doc>.htm` | Semi-structured HTML |
| **EUR-Lex** (eur-lex.europa.eu) | `...?uri=CELEX:<number>` | ELI-annotated XHTML (via CELLAR API) |

EUR-Lex URLs are automatically rewritten to the CELLAR API at
`publications.europa.eu/resource/celex/{CELEX}` to bypass the AWS WAF block
on the EUR-Lex frontend.

## Adding Laws

Edit `input/sources.txt` — one URL per line. Comments (`#`) and empty lines
are ignored. See the file header for URL format examples.

```bash
# Re-fetch all sources
python -m fetch.run_fetch

# Dry run (fetch + validate, no DB write)
python -m fetch.run_fetch --dry-run
```

## Configuration

All config via environment variables (`.env` file or docker-compose):

| Variable | Default | Description |
|----------|---------|-------------|
| `LEGAL_SOURCES_FILE` | `input/sources.txt` | Source URLs file |
| `LEGAL_DB_PATH` | `data/legal.db` | SQLite database path |
| `MCP_HOST` | `0.0.0.0` | MCP server bind host |
| `MCP_PORT` | `8000` | MCP server port |
| `MCP_ALLOWED_HOSTS` | (empty) | Comma-separated allowed Host headers |

## MCP Tools

| Tool | Description |
|------|-------------|
| `retrieve_paragraph(law_name, section_number)` | Exact paragraph lookup by law + §/Artikel number |
| `search_paragraphs(query, limit=20)` | Full-text search with ranked snippets |
| `list_laws()` | List all laws with section counts |

## Docker

```bash
docker compose up -d --build
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

# Start MCP server
python main.py
```

## Testing

Test fixtures are trimmed versions of real source documents in
`tests/testdata/`:

- `vgv_sample.xml` — GII XML (VgV, 3 paragraphs)
- `vob_sample.htm` — VV HTML (VOB/A, 5 paragraphs across 3 Abschnitte)
- `eurlex_sample.xhtml` — EUR-Lex XHTML (RL 2014/24/EU, 3 articles)

```bash
python -m pytest tests/ -v          # full suite
python -m pytest tests/test_gii_xml.py -v  # single parser
```
