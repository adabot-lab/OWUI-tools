# websearch

Web search and page fetch tool for Open WebUI via **OpenAPI** (FastAPI).

## What it does

Exposes three endpoints that any Open WebUI model can call via native function calling:

- **web search** — queries multiple search engines, returns ranked results with title, URL, and snippet
- **fetch page** — retrieves a URL, strips boilerplate/navigation, returns clean text
- **fetch page for RAG** — fetches + chunks a URL for retrieval-augmented generation

## Search engines

| Engine | Needs API key | Notes |
|---|---|---|
| **SearXNG** | No | Self-hosted metasearch, aggregates Google/Bing/Brave/etc. |
| **DuckDuckGo** | No | Direct HTML scrape, no key required |
| **Google PSE** | Yes | Programmable Search Engine, requires API key + CX |

Engines are enabled/disabled individually via env vars. SearXNG + DuckDuckGo work out of the box.

## Page content extraction

Three-stage fallback pipeline:

1. **trafilatura** — best quality for news/articles
2. **readability** — Mozilla's Readability algorithm
3. **BeautifulSoup** — last resort, strips tags

Optional **Apache Tika** integration for PDF/Office document parsing (set `TIKA_SERVER_URL`).

## Configuration

Copy `.env.example` to `.env` and adjust. Key variables:

| Variable | Default | Description |
|---|---|---|
| `SEARXNG_URL` | `http://searxng:8080` | SearXNG instance URL |
| `USE_SEARXNG_SEARCH` | `yes` | Enable SearXNG |
| `USE_DUCKDUCKGO_SEARCH` | `no` | Enable DuckDuckGo |
| `USE_GOOGLE_SEARCH` | `yes` | Enable Google PSE |
| `GOOGLE_PSE_API_KEY` | — | Google API key |
| `GOOGLE_PSE_CX` | — | Google custom search CX |
| `TIKA_SERVER_URL` | `http://tika:9998` | Tika server URL (empty = disabled) |
| `PAGE_FETCH_TIMEOUT` | `10` | Fetch timeout in seconds |
| `PAGE_MAX_CONTENT_LENGTH` | `50000` | Max extracted text length |
| `SEARXNG_TIMEOUT` | `30` | SearXNG query timeout |

## Open WebUI setup

Go to **Admin Panel → Settings → Integrations → Manage Tool Servers → +**.

- **Type:** OpenAPI
- **Name:** `websearch` (or any name you like)
- **URL:** `http://websearch-fastapi:8000`
- **Auth:** none

Click the reload button next to the URL, make sure the tool is active (green), save.

## Port

Container internal: `8000` (map externally as needed, e.g. `8011:8000`).
