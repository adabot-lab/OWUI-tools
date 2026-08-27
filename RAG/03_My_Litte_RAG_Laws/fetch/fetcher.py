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

from pathlib import Path

from fetch.parsers.base import BaseParser, LawDocument
from fetch.parsers.gii_xml import GiiXmlParser
from fetch.parsers.gii_html import GiiHtmlParser
from fetch.parsers.vv_html import VVHtmlParser
from fetch.parsers.eurlex_html import EurlexHtmlParser
from fetch.parsers.bsbe_xml import BsbeXmlParser
from fetch import cache as cache_mod


# --- Source types ---
GII = "gii_xml"
GII_HTML = "gii_html"
VV = "vv_html"
EURLEX = "eurlex_html"
BSBE = "bsbe_xml"


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
        elif source_type == GII_HTML:
            _PARSERS[source_type] = GiiHtmlParser()
        elif source_type == VV:
            _PARSERS[source_type] = VVHtmlParser()
        elif source_type == EURLEX:
            _PARSERS[source_type] = EurlexHtmlParser()
        elif source_type == BSBE:
            _PARSERS[source_type] = BsbeXmlParser()
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

    if "gesetze-im-internet.de" in host and url.endswith(".html"):
        return GII_HTML

    if "verwaltungsvorschriften-im-internet.de" in host:
        return VV

    if "eur-lex.europa.eu" in host:
        return EURLEX

    if "gesetze.berlin.de" in host:
        return BSBE

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


def _fetch_bsbe(url: str) -> bytes:
    """Download a gesetze.berlin.de AIZ ZIP and return the content.xml bytes.

    The BSBE portal requires:
      1. Resolve perma redirect -> docId
      2. POST /init with r3autologin cookie + CSRF headers -> JSESSIONID
      3. GET AIZ ZIP -> unzip content.xml

    Each call creates its own httpx.Client(http2=True) — HTTP/2 is required
    (HTTP/1.1 gets security_wrongDomain).

    Returns:
        Raw bytes of the extracted content.xml

    Raises:
        ValueError: If the ZIP cannot be found or extracted.
    """
    import datetime as _dt
    import time as _time

    client = httpx.Client(
        http2=True,
        cookies={"r3autologin": "bsbe"},
        timeout=60.0,
        follow_redirects=True,
    )

    try:
        # Step 1: Resolve permalink -> extract docId
        resp = client.get(url)
        resp.raise_for_status()
        doc_id_match = re.search(r"docId=([a-zA-Z0-9-]+)", str(resp.url))
        if not doc_id_match:
            raise ValueError(f"Could not extract docId from final URL: {resp.url}")
        doc_id = doc_id_match.group(1)

        # Step 2: Init session (establishes JSESSIONID cookie)
        init_resp = client.post(
            "https://gesetze.berlin.de/jportal/wsrest/recherche3/init?portalId=bsbe",
            headers={
                "X-CSRF-TOKEN": "r3autologin",
                "JURIS-PORTALID": "bsbe",
                "Content-Type": "application/json;charset=UTF-8",
            },
            json={
                "portalId": "bsbe",
                "clientID": "bsbe",
                "clientVersion": "bsbe - V08_33_01",
                "r3ID": "bsbe",
            },
        )
        init_resp.raise_for_status()
        init_data = init_resp.json()
        aiz_slug = init_data.get("aizSlug", "bsbeAizDownload")

        # Step 3: Extract j= value from perma URL for slug construction
        parsed_url = urlparse(url)
        qs = parse_qs(parsed_url.query)
        j_value = (qs.get("j") or [""])[0]

        # Try candidate slugs for the AIZ download.
        # Primary pattern: <j_value>_<year>.zip for recent years.
        # Fallback: search API if all years fail.
        current_year = _dt.date.today().year
        candidate_years = [2020, 2024, 2025, current_year, current_year - 1, 2023, 2022, 2021]
        candidate_years = list(dict.fromkeys(candidate_years))  # dedupe preserving order

        zip_bytes = None
        for year in candidate_years:
            slug = f"{j_value}_{year}.zip"
            zip_url = (
                f"https://gesetze.berlin.de/jportal/{aiz_slug}/{slug}"
                f"?doc.id={doc_id}&doc.part=X"
            )
            zip_resp = client.get(zip_url)
            if zip_resp.status_code == 200:
                zip_bytes = zip_resp.content
                break
            # Brief pause to avoid hammering on 404s
            _time.sleep(0.1)

        # Fallback: search API for aizZipUrl
        if zip_bytes is None:
            search_resp = client.post(
                "https://gesetze.berlin.de/jportal/wsrest/recherche3/search?portalId=bsbe",
                headers={
                    "X-CSRF-TOKEN": "r3autologin",
                    "JURIS-PORTALID": "bsbe",
                    "Content-Type": "application/json;charset=UTF-8",
                },
                json={
                    "portalId": "bsbe",
                    "docId": doc_id,
                    "searchMask": "aktuelleNorm",
                    "searchMode": "ADVANCED",
                    "r3ID": "bsbe",
                },
            )
            search_resp.raise_for_status()
            search_data = search_resp.json()
            other_rep = search_data.get("otherRepresentations", {})
            if isinstance(other_rep, dict):
                aiz_zip_url = other_rep.get("aizZipUrl", "")
            elif isinstance(other_rep, list):
                aiz_zip_url = other_rep[0].get("aizZipUrl", "") if other_rep else ""
            else:
                aiz_zip_url = ""
            if aiz_zip_url:
                zip_resp = client.get(aiz_zip_url)
                zip_resp.raise_for_status()
                zip_bytes = zip_resp.content

        if zip_bytes is None:
            raise ValueError(
                f"Could not download AIZ ZIP for {j_value} (docId={doc_id})"
            )

        # Step 4: Extract content.xml from ZIP
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            return zf.read("content.xml")

    finally:
        client.close()


def fetch_one(
    url: str,
    client: httpx.Client | None = None,
    cache_dir: Path | None = None,
) -> FetchResult:
    """Download and parse a single source URL.

    Args:
        url: The source URL (GII .zip, VV .htm, or EUR-Lex CELEX URL).
        client: Optional httpx client (one is created if not provided).
        cache_dir: If set, save the downloaded raw data to this cache directory.

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

        # BSBE: uses own HTTP/2 client with session setup — skip shared client GET
        if source_type == BSBE:
            raw_data = _fetch_bsbe(url)
        else:
            response = client.get(fetch_url, headers=headers)
            response.raise_for_status()
            raw_data = response.content

            # GII: unzip to get XML
            if source_type == GII:
                raw_data = _extract_zip_xml(raw_data)

        # Save to cache if requested (after unzip, before parse)
        if cache_dir is not None:
            cache_mod.save_to_cache(source_type, url, raw_data, cache_dir)

        parser = _get_parser(source_type)
        document = parser.parse(raw_data, source_url=url)

        return FetchResult(url=url, source_type=source_type, document=document)

    except Exception as e:
        return FetchResult(url=url, source_type="", document=None, error=str(e))

    finally:
        if own_client:
            client.close()


def fetch_one_from_cache(
    filename: str,
    source_type: str,
    original_url: str = "",
    cache_dir: Path | None = None,
) -> FetchResult:
    """Parse a single source from the local cache (no network).

    Args:
        filename: Cache filename (e.g. "vgv_2016.xml").
        source_type: Parser to use (gii_xml, vv_html, eurlex_html).
        original_url: Original source URL (for LawDocument metadata).
        cache_dir: Cache directory (default: data/cache).

    Returns:
        FetchResult with the parsed LawDocument or an error message.
    """
    cache_dir = cache_dir or cache_mod.CACHE_DIR
    filepath = cache_dir / filename

    try:
        raw_data = filepath.read_bytes()
        parser = _get_parser(source_type)
        document = parser.parse(raw_data, source_url=original_url)
        return FetchResult(url=original_url, source_type=source_type, document=document)

    except Exception as e:
        return FetchResult(
            url=original_url, source_type=source_type, document=None, error=str(e)
        )


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
    text = Path(path).read_text(encoding="utf-8")
    urls = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            urls.append(stripped)
    return urls


def fetch_all_from_cache(
    cache_dir: Path | None = None,
) -> list[FetchResult]:
    """Load and parse all cached sources (no network).

    Args:
        cache_dir: Cache directory (default: data/cache).

    Returns:
        List of FetchResult, one per cached file.
    """
    cached = cache_mod.list_cached(cache_dir)
    return [
        fetch_one_from_cache(
            entry["filename"],
            entry["source_type"],
            entry["url"],
            cache_dir,
        )
        for entry in cached
    ]
