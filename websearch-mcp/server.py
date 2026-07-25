"""
Web Search MCP Server — Streamable HTTP Transport
===================================================
MCP server providing web search (via SearXNG) and page fetching
with content extraction (trafilatura + readability fallback).

Uses MCP Streamable HTTP transport (protocol version 2025-03-26) instead of
the deprecated HTTP+SSE or stdio transports.
"""

import os
import urllib.parse
from typing import Optional

import httpx
from starlette.applications import Starlette
from starlette.routing import Mount

try:
    import trafilatura
except ImportError:
    trafilatura = None

try:
    from readability import Document as ReadabilityDocument
except ImportError:
    ReadabilityDocument = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

from mcp.server.fastmcp import FastMCP

# ── Configuration ────────────────────────────────────────────────────────────

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8040")
SEARXNG_TIMEOUT = int(os.getenv("SEARXNG_TIMEOUT", "15"))
USE_SEARXNG_SEARCH = os.getenv("USE_SEARXNG_SEARCH", "yes").lower() == "yes"

TIKA_SERVER_URL = os.getenv("TIKA_SERVER_URL", "")
USE_TIKA = bool(TIKA_SERVER_URL)

PAGE_FETCH_TIMEOUT = int(os.getenv("PAGE_FETCH_TIMEOUT", "15"))
PAGE_MAX_CONTENT_LENGTH = int(os.getenv("PAGE_MAX_CONTENT_LENGTH", "50000"))

MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "9200"))

# Common browser headers for fetching
_HEADERS = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    },
]


# ── Search: SearXNG ──────────────────────────────────────────────────────────

async def _search_searxng(query: str, num_results: int) -> list[dict]:
    """Search via SearXNG JSON API."""
    if not SEARXNG_URL:
        return []

    params = {
        "q": query,
        "format": "json",
        "language": "auto",
        "safesearch": "0",
        "categories": "general",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=SEARXNG_TIMEOUT) as client:
        try:
            resp = await client.get(f"{SEARXNG_URL}/search", params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("results", []):
                title = item.get("title", "")
                url = item.get("url", "")
                snippet = item.get("content", "")
                if title and url:
                    results.append({
                        "title": title,
                        "url": url,
                        "snippet": snippet,
                        "engine": "searxng",
                    })
            return results[:num_results]
        except Exception as e:
            print(f"SearXNG search error: {e}", flush=True)
            return []


# ── Page Fetching ────────────────────────────────────────────────────────────

def _is_pdf_url(url: str) -> bool:
    return url.lower().endswith(".pdf") or ".pdf?" in url.lower()


async def _extract_pdf_with_tika(pdf_url: str) -> str:
    """Extract text from a PDF via Apache Tika (optional)."""
    if not USE_TIKA:
        return f"[PDF detected at {pdf_url} — Tika server not configured, cannot extract text]"

    async with httpx.AsyncClient(timeout=PAGE_FETCH_TIMEOUT, follow_redirects=True) as dl:
        resp = await dl.get(pdf_url, headers=_HEADERS[0])
        resp.raise_for_status()
        pdf_bytes = resp.content

    async with httpx.AsyncClient(timeout=PAGE_FETCH_TIMEOUT) as tika:
        resp = await tika.put(
            f"{TIKA_SERVER_URL}/tika",
            headers={"Accept": "text/plain", "Content-Type": "application/pdf"},
            content=pdf_bytes,
        )
        resp.raise_for_status()
        return resp.text


def _clean_html(html: str, url: str) -> str:
    """Extract main text content from HTML using trafilatura → readability → BS4 fallback."""
    text = None

    # 1. trafilatura (best quality)
    if trafilatura is not None:
        text = trafilatura.extract(html)

    # 2. readability + BS4
    if (not text or len(text.strip()) < 100) and ReadabilityDocument is not None:
        try:
            doc = ReadabilityDocument(html)
            summary_html = doc.summary()
            soup = BeautifulSoup(summary_html, "html.parser")
            text = soup.get_text(separator=" ")
        except Exception:
            pass

    # 3. Plain BS4 fallback
    if (not text or len(text.strip()) < 50) and BeautifulSoup is not None:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator=" ", strip=True)

    if not text or len(text.strip()) < 50:
        return f"Page content could not be extracted from {url}. The page may contain mostly images, videos, or JavaScript."

    return text.strip()


def _extract_links(html: str, base_url: str, max_links: int = 200) -> list[dict]:
    """Extract navigational anchor links from raw HTML.

    Returns a list of {"text": ..., "href": ...} dicts with absolute URLs,
    deduplicated by (href, text) and capped at max_links.
    """
    if not html or BeautifulSoup is None:
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    seen = set()
    links: list[dict] = []

    for a in soup.find_all("a", href=True):
        if len(links) >= max_links:
            break

        href = a["href"].strip()
        if not href:
            continue
        # Skip non-navigational hrefs
        lower = href.lower()
        if lower.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        # Resolve relative → absolute
        absolute = urllib.parse.urljoin(base_url, href)
        if not absolute:
            continue

        text = a.get_text(strip=True)

        key = (absolute, text)
        if key in seen:
            continue
        seen.add(key)

        links.append({"text": text, "href": absolute})

    return links


async def _fetch_page(url: str, max_length: int = PAGE_MAX_CONTENT_LENGTH, include_links: bool = False) -> dict:
    """Fetch a URL and return its cleaned text content."""
    if _is_pdf_url(url):
        content = await _extract_pdf_with_tika(url)
        truncated_content = content[:max_length]
        return {
            "url": url,
            "title": os.path.basename(urllib.parse.urlparse(url).path) or url,
            "content": truncated_content,
            "content_length": len(truncated_content),
            "truncated": len(content) > max_length,
        }

    html = None
    last_error = None
    fatal_error = False

    for headers in _HEADERS:
        for timeout_extra in (0, 5, 10):
            try:
                async with httpx.AsyncClient(
                    timeout=PAGE_FETCH_TIMEOUT + timeout_extra,
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(url, headers=headers)
                    resp.raise_for_status()
                    html = resp.text
                    break
            except httpx.TimeoutException:
                continue
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (403, 429, 401):
                    continue
                last_error = f"HTTP {e.response.status_code}"
                fatal_error = True
                break
            except httpx.RequestError:
                continue
        if html or fatal_error:
            break

    if html is None:
        return {
            "url": url,
            "title": "",
            "content": f"Failed to fetch page: {last_error or 'all attempts failed'}.",
            "content_length": 0,
            "truncated": False,
        }

    if BeautifulSoup is None:
        return {
            "url": url,
            "title": "",
            "content": "Failed to parse HTML: BeautifulSoup not available",
            "content_length": 0,
            "truncated": False,
        }

    # Extract title
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    if soup.title:
        title = soup.title.get_text(strip=True)
    elif soup.find("h1"):
        title = soup.find("h1").get_text(strip=True)

    # Extract content
    cleaned = _clean_html(html, url)
    truncated = len(cleaned) > max_length
    if truncated:
        cleaned = cleaned[:max_length] + "... [content truncated]"

    result = {
        "url": url,
        "title": title,
        "content": cleaned,
        "content_length": len(cleaned),
        "truncated": truncated,
    }

    if include_links:
        result["links"] = _extract_links(html, url)

    return result


# ── MCP Server Definition ────────────────────────────────────────────────────

mcp = FastMCP(
    "websearch",
    json_response=True,  # Use JSON responses instead of SSE where possible
)


@mcp.tool()
async def web_search(
    query: str,
    num_results: int = 10,
    engines: Optional[list[str]] = None,
) -> str:
    """Search the web using SearXNG. Returns a list of results
    with title, URL, and snippet for each. Use this tool to find information on
    the internet. Prefer specific queries for better results.

    Args:
        query: The search query — be specific for better results.
        num_results: Number of results to return (1-20, default: 10).
        engines: Which engines to use. Default: all available. Currently only
                 "searxng" is supported. Options: ["searxng"].
    """
    import asyncio
    import json

    num_results = max(1, min(20, num_results))

    if engines is None:
        engines = []
        if USE_SEARXNG_SEARCH and SEARXNG_URL:
            engines.append("searxng")

    if not engines:
        return json.dumps({
            "error": "No search engines available. Configure SEARXNG_URL."
        })

    # Run searches concurrently
    tasks = []
    for engine in engines:
        if engine == "searxng" and USE_SEARXNG_SEARCH:
            tasks.append(_search_searxng(query, num_results))

    results_raw = await asyncio.gather(*tasks, return_exceptions=True)

    all_results = []
    for i, res in enumerate(results_raw):
        if isinstance(res, Exception):
            engine_name = engines[i] if i < len(engines) else "unknown"
            print(f"Engine {engine_name} error: {res}", flush=True)
            continue
        if res:
            all_results.extend(res)

    # Deduplicate by URL
    seen_urls = set()
    unique = []
    for r in all_results:
        if r["url"] not in seen_urls:
            seen_urls.add(r["url"])
            unique.append(r)

    limited = unique[:num_results]

    output = {
        "query": query,
        "total_results": len(limited),
        "results": limited,
    }
    return json.dumps(output, ensure_ascii=False, indent=2)


@mcp.tool()
async def fetch_page(
    url: str,
    max_length: int = 50000,
    include_links: bool = False,
) -> str:
    """Fetch a web page and extract its main text content, removing navigation,
    ads, and boilerplate. Use this to read the full content of a URL found via
    web_search or any web page. Returns the page title and cleaned text.

    Args:
        url: Full URL to fetch (must start with http:// or https://).
        max_length: Maximum characters to extract (default: 50000, max: 100000).
        include_links: If true, extract anchor links from the page HTML and return
            them as a separate "links" array of {text, href} objects with absolute URLs.
            Default: false (backward compatible — no links field in output).
    """
    import json

    if not url or not url.startswith(("http://", "https://")):
        return json.dumps({"error": "url is required and must start with http:// or https://."})

    max_length = max(1, min(100000, max_length))

    try:
        result = await _fetch_page(url, max_length, include_links=include_links)
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)


@mcp.tool()
async def health_check() -> str:
    """Check if the web search MCP server is healthy and which backends are available."""
    import json

    available = []
    if USE_SEARXNG_SEARCH and SEARXNG_URL:
        available.append("searxng")

    # Quick SearXNG ping
    searxng_ok = False
    if USE_SEARXNG_SEARCH and SEARXNG_URL:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{SEARXNG_URL}/healthz")
                searxng_ok = resp.status_code == 200
        except Exception:
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(
                        f"{SEARXNG_URL}/search",
                        params={"q": "test", "format": "json"},
                        headers={"Accept": "application/json"},
                    )
                    searxng_ok = resp.status_code == 200
            except Exception:
                pass

    status = {
        "status": "ok",
        "available_engines": available,
        "searxng_reachable": searxng_ok,
        "tika_enabled": USE_TIKA,
        "config": {
            "SEARXNG_URL": SEARXNG_URL,
            "USE_SEARXNG_SEARCH": USE_SEARXNG_SEARCH,
            "PAGE_FETCH_TIMEOUT": PAGE_FETCH_TIMEOUT,
            "PAGE_MAX_CONTENT_LENGTH": PAGE_MAX_CONTENT_LENGTH,
        },
    }
    return json.dumps(status, indent=2)


# ── ASGI App (Streamable HTTP) ───────────────────────────────────────────────
# streamable_http_app() returns a complete Starlette app with routes at /mcp
# No additional Mount or wrapping needed — use it directly as the ASGI app.
app = mcp.streamable_http_app()


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
