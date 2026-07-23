# Legal Paragraph Retrieval Service

MCP server for German procurement law (Vergaberecht): VgV, GWB, VOB, and EU Directive 2014/24/EU.

## What is this?

A pipeline that converts raw law PDFs (converted to markdown) into a searchable SQLite database, exposed via MCP (Model Context Protocol) streamable HTTP.

**Architecture:**

```
PDF → pymupdf4llm → raw .md → LLM extractor → structured JSON → SQLite (FTS5) → MCP tools
```

The LLM handles footnote removal, paragraph boundary detection, and formatting cleanup — replacing fragile bash scripts and regex parsers.

**Three MCP tools:**

| Tool | Description |
|------|-------------|
| `retrieve_paragraph(law_name, section_number)` | Get exact text of a specific paragraph by law + section number |
| `search_paragraphs(query)` | Full-text search across all paragraphs (FTS5) |
| `list_laws()` | List all laws in the collection with section counts |

**Laws included:**
- **VgV** — Verordnung über die Vergabe öffentlicher Aufträge
- **GWB** — Gesetz gegen Wettbewerbsbeschränkungen
- **VOB** — Vergabe- und Vertragsordnung für Bauleistungen
- **EU-2014/24/EU** — Richtlinie 2014/24/EU

## How to Run

### 1. Prerequisites

- Docker + Docker Compose
- A LiteLLM proxy (or any OpenAI-compatible endpoint) for the extraction step
- Raw `.md` files in `input/` (generated from PDFs via `pymupdf4llm`)

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your settings
```

Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_HOST` | `0.0.0.0` | Server bind address |
| `MCP_PORT` | `8000` | Server port |
| `MCP_ALLOWED_HOSTS` | _(empty)_ | Comma-separated host:port allowlist for DNS-rebinding protection. Empty = protection disabled (trusted networks) |
| `LEGAL_DOCUMENTS_DIR` | `input` | Directory containing raw law `.md` files |
| `LEGAL_DB_PATH` | `data/legal.db` | SQLite database path |
| `LLM_BASE_URL` | `http://localhost:4000/v1` | LLM endpoint for extraction pipeline (not needed at runtime) |
| `LLM_EXTRACT_MODEL` | `zai-glm-4.7` | Model name for extraction |

### 3. Populate the database (extraction pipeline)

The extraction pipeline sends raw markdown to an LLM, gets structured paragraphs back, validates, and stores in SQLite.

```bash
# Set LLM credentials
export LLM_BASE_URL=http://your-litellm-proxy:4000/v1
export LLM_API_KEY=sk-your-key

# Run extraction on all .md files in input/
docker compose run --rm legal-paragraph-api python -m extract.run_extraction

# Or extract a specific file
docker compose run --rm legal-paragraph-api python -m extract.run_extraction input/VgV.md

# Or dry-run (extract without writing to DB)
docker compose run --rm legal-paragraph-api python -m extract.run_extraction --dry-run

# With expected section counts for validation
docker compose run --rm legal-paragraph-api python -m extract.run_extraction --expected VgV:63
```

### 4. Start the MCP server

```bash
docker compose up -d
```

The MCP streamable HTTP endpoint is at `http://<host>:8000/mcp`.

### 5. Connect from an MCP client

Add to your MCP client config (e.g., Hermes, Claude Desktop):

```json
{
  "mcpServers": {
    "legal-paragraphs": {
      "url": "http://<host>:8000/mcp"
    }
  }
}
```

## Development

### Local setup (without Docker)

```bash
cd 03_My_Litte_RAG_Laws
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest  # for tests
```

### Run tests

```bash
.venv/bin/python -m pytest tests/ -v
```

### Project structure

```
03_My_Litte_RAG_Laws/
├── main.py                  # MCP streamable HTTP entrypoint
├── db.py                    # SQLite database layer with FTS5
├── legal_engine.py          # Query layer (retrieve, search, list)
├── extract/
│   ├── chunker.py           # Splits raw markdown into LLM-sized chunks
│   ├── extractor.py         # LLM-based paragraph extraction
│   ├── validator.py         # Validates extraction results
│   └── run_extraction.py    # End-to-end CLI pipeline
├── input/                   # Raw .md files (from Convert_to_MD)
├── data/                    # Generated SQLite DB (gitignored)
├── tests/                   # 33 tests across all modules
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## Adding a new law

```bash
# 1. Convert PDF to markdown (pymupdf4llm)
# 2. Add header to the .md file:
#    # Gesetz: Full Law Name
#    # Abkürzung: ABBR
#    # Stand: DD.MM.YYYY
# 3. Place in input/
# 4. Run extraction
python -m extract.run_extraction input/new_law.md
# 5. Restart the server — law is immediately available
```
