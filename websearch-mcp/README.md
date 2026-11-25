# websearch-mcp

Web search and page fetch MCP server for Open WebUI via **Streamable HTTP** transport.

Uses the MCP Streamable HTTP protocol (spec version 2025-03-26) — the current standard, replacing the deprecated HTTP+SSE transport.

## What it does

Exposes three MCP tools that Open WebUI can call directly:

- **web_search** — queries SearXNG, returns ranked results with title, URL, and snippet
- **search_reddit** — native Reddit search via public feeds (anonymous, no API key) — **retired upstream, returns an explicit error (see Reddit)**
- **fetch_page** — retrieves a URL, strips boilerplate, returns clean text content
- **health_check** — reports backend availability (which search engines are configured)

## Differences from `websearch/` (OpenAPI version)

| | websearch (OpenAPI) | websearch-mcp (Streamable HTTP) |
|---|---|---|
| Protocol | OpenAPI / REST | MCP Streamable HTTP |
| Transport | HTTP JSON endpoints | MCP tool calls |
| Google PSE | Supported | Not supported |
| Container port | 8000 | 9200 |
| Python | 3.11 | 3.13 |

The MCP version drops Google PSE in favor of a simpler, self-hosted-only setup (SearXNG). No API keys required.

## Search engines

| Engine | Needs API key | Notes |
|---|---|---|
| **SearXNG** | No | Self-hosted metasearch, aggregates Google/Bing/Brave/etc. |

Enabled by default. Disable via `USE_SEARXNG_SEARCH=no`.

## Page content extraction

Same three-stage fallback as the OpenAPI version:

1. **trafilatura** — best for news/articles
2. **readability** — Mozilla's Readability algorithm
3. **BeautifulSoup** — last resort, strips tags

Optional **Apache Tika** integration for PDF/Office documents (set `TIKA_SERVER_URL`).

## Reddit

> **RETIRED UPSTREAM (verified 2026-11-25).** Reddit ended RSS support on
> 2026-11-13 and the public Atom feeds stopped serving content around
> 2026-11-17. Every feed URL — subreddit listing, thread (with or without the
> post slug), user, search — now answers HTTP 404 with an Atom body
> `<title>Not Found</title>`. Reddit's own announcement states there is no
> replacement for non-moderators. `fetch_page` on a reddit.com URL therefore
> returns an explicit `backend_unavailable` error instead of content, and
> `search_reddit` returns the same error. Both degrade honestly: the backend
> does not retry doomed requests (it remembers the retirement for
> `FEED_GONE_TTL`, 15 min) and never reports an opaque `404 Not Found for url
> …` string as if the caller's URL were malformed. `health_check` surfaces
> `reddit_rss.upstream: "retired"` while that window is open. Set
> `USE_REDDIT_BACKEND=no` to skip the backend entirely.

Historical behaviour (kept in the code so a restoration is a one-line change):
`fetch_page` served reddit.com thread/subreddit/user URLs from Reddit's public
Atom feeds (anonymous — no API key, no login), returning the post plus up to
~100 comments as markdown, marked with `"via": "reddit-rss"`. Thread feed URLs
must carry the post slug; `_feed_url` builds `/r/<sub>/comments/<id>/<slug>/.rss`
from the permalink rather than dropping the slug.

Limits (if feeds ever return): ~1 request/minute/IP shared across all reddit
calls, served through a TTL cache. On a rate limit you get
`{"error": ..., "retry_after": N}` — wait N seconds and retry. No scores or
comment nesting (anonymous feeds never carried them).

## Configuration

Copy `.env.example` to `.env` and adjust. Key variables:

| Variable | Default | Description |
|---|---|---|
| `SEARXNG_URL` | `http://searxng:8080` | SearXNG instance URL |
| `USE_SEARXNG_SEARCH` | `yes` | Enable SearXNG |
| `TIKA_SERVER_URL` | `http://tika:9998` | Tika server URL (empty = disabled) |
| `PAGE_FETCH_TIMEOUT` | `15` | Fetch timeout in seconds |
| `PAGE_MAX_CONTENT_LENGTH` | `50000` | Max extracted text length |
| `SEARXNG_TIMEOUT` | `15` | SearXNG query timeout |
| `MCP_HOST` | `0.0.0.0` | Server bind address |
| `MCP_PORT` | `9200` | Server bind port |
| `USE_REDDIT_BACKEND` | `yes` | Serve reddit URLs via .rss feeds |
| `REDDIT_MIN_INTERVAL` | `65` | Seconds between outbound reddit feed requests |
| `REDDIT_CACHE_TTL` | `600` | Feed cache TTL in seconds |

## Open WebUI setup

Go to **Admin Panel → Settings → Integrations → Manage Tool Servers → +**.

- **Type:** MCP
- **Name:** `websearch-mcp` (or any name you like)
- **URL:** `http://websearch-mcp:9200`
- **Auth:** none

Click the reload button next to the URL, make sure the tool is active (green), save.

## Port

Container internal: `9200`, bound to `127.0.0.1:9200` by default.
