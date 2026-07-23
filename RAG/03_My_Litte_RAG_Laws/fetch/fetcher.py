"""Download law texts from official sources and dispatch to source-specific parsers.

Source-type detection by URL:
    gesetze-im-internet.de + .zip          → GII XML parser
    verwaltungsvorschriften-im-internet.de → VV HTML parser
    eur-lex.europa.eu + uri=CELEX:...      → rewrite to CELLAR, EUR-Lex parser
"""
from __future__ import annotations

import zipfile
import io
import re
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs, quote

import httpx

from fetch.parsers.base import BaseParser, LawDocument
from fetch.parsers.gii_xml import GiiXmlParser
from fetch.parsers.vv_html import VVHtmlParser
from fetch.parsers.eurlex_html import EurlexHtmlParser


# --- Source types ---
GII = "gii_xml"
VV = "vv_html"
EURLEX = "eurlex_html"


@dataclass
class FetchResult:
    """Result of fetching and parsing a single source URL."""
    url: str
    source_type: str
    document: LawDocument | None
    error: str = ""


# Map source type → parser instance (instantiated lazily / on first use).
_PARSERS: dict[str, BaseParser] = {}


def _get_parser(source_type: str) -> BaseParser:
    """Return the parser instance for a source type."""
    if source_type not in _PARSERS:
        if source_type == GII:
            _PARSERS[source_type] = GiiXmlParser()
        elif source_type == VV:
            _PARSERS[source_type] = VVHtmlParser()
        elif source_type == EURLEX:
            _PARSERS[source_type] = EurlexHtmlParser()
        else:
            raise ValueError(f"Unknown source type: {source_type}")
    return _PARSERS[source_type]


def detect_source_type(url: str) -> str:
    """Classify a URL into one of the three source types.

    Raises:
        ValueError: If the URL doesn't match any known source.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""

    if "gesetze-im-internet.de" in host and url.endswith(".zip"):
        return GII

    if "verwaltungsvorschriften-im-internet.de" in host:
        return VV

    if "eur-lex.europa.eu" in host:
        return EURLEX

    raise ValueError(f"Cannot determine source type for URL: {url}")


def _rewrite_eurlex_url(url: str) -> tuple[str, dict[str, str]]:
    """Rewrite a EUR-Lex frontend URL to the CELLAR API endpoint.

    Returns:
        (fetch_url, headers) — the URL to fetch and the HTTP headers to use.
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    uri_values = qs.get("uri", [])
    if not uri_values:
        raise ValueError(f"EUR-Lex URL has no ?uri= parameter: {url}")

    celex = uri_values[0]
    # Strip "CELEX:" prefix if present
    if celex.upper().startswith("CELEX:"):
        celex = celex[6:]

    fetch_url = f"https://publications.europa.eu/resource/celex/{quote(celex)}"
    headers = {
        "Accept": "application/xhtml+xml",
        "Accept-Language": "de",
    }
    return fetch_url, headers


def _extract_zip_xml(raw_bytes: bytes) -> bytes:
    """Extract the XML content from a GII ZIP archive."""
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
        xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
        if not xml_names:
            raise ValueError("ZIP archive contains no XML file")
        return zf.read(xml_names[0])


def fetch_one(
    url: str,
    client: httpx.Client | None = None,
) -> FetchResult:
    """Download and parse a single source URL.

    Args:
        url: The source URL (GII .zip, VV .htm, or EUR-Lex CELEX URL).
        client: Optional httpx client (one is created if not provided).

    Returns:
        FetchResult with the parsed LawDocument or an error message.
    """
    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=60.0, follow_redirects=True)

    try:
        source_type = detect_source_type(url)

        # EUR-Lex needs URL rewriting + special headers
        if source_type == EURLEX:
            fetch_url, headers = _rewrite_eurlex_url(url)
        else:
            fetch_url = url
            headers = {}

        response = client.get(fetch_url, headers=headers)
        response.raise_for_status()
        raw_data = response.content

        # GII: unzip to get XML
        if source_type == GII:
            raw_data = _extract_zip_xml(raw_data)

        parser = _get_parser(source_type)
        document = parser.parse(raw_data, source_url=url)

        return FetchResult(url=url, source_type=source_type, document=document)

    except Exception as e:
        return FetchResult(url=url, source_type="", document=None, error=str(e))

    finally:
        if own_client:
            client.close()


def fetch_all(urls: list[str]) -> list[FetchResult]:
    """Fetch multiple source URLs, returning results in order.

    Args:
        urls: List of source URLs.

    Returns:
        List of FetchResult, one per URL, in the same order.
    """
    results: list[FetchResult] = []
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        for url in urls:
            results.append(fetch_one(url, client=client))
    return results


def read_sources_file(path: str = "input/sources.txt") -> list[str]:
    """Read a sources.txt file, returning non-comment, non-empty lines.

    Args:
        path: Path to the sources file.

    Returns:
        List of URL strings.
    """
    from pathlib import Path
    text = Path(path).read_text(encoding="utf-8")
    urls = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            urls.append(stripped)
    return urls
