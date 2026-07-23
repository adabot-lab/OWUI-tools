"""Local file cache for downloaded law texts.

Cache layout:
    data/cache/
        manifest.json          # maps filename → {url, source_type}
        vgv_2016.xml           # extracted from GII zip
        gwb.xml
        vob_a.htm              # raw VV HTML
        eurlex_02014L0024.xhtml

The cache stores post-extraction files (GII XML unzipped, VV/EUR-Lex raw).
manifest.json records which parser to use for each file.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CACHE_DIR = Path("data/cache")
MANIFEST_FILE = "manifest.json"

# File extensions per source type
_EXTENSIONS = {
    "gii_xml": ".xml",
    "vv_html": ".htm",
    "eurlex_html": ".xhtml",
}


def _slugify(url: str, source_type: str) -> str:
    """Derive a human-readable filename from a URL.

    Examples:
        https://www.gesetze-im-internet.de/vgv_2016/xml.zip → "vgv_2016"
        https://www.verwaltungsvorschriften-im-internet.de/bsvwvbund_123.htm → "bsvwvbund_123"
        https://eur-lex.europa.eu/...?uri=CELEX:02014L0024-20260101 → "eurlex_02014L0024"
    """
    from urllib.parse import urlparse, parse_qs

    if source_type == "eurlex_html":
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        celex = ""
        for v in qs.get("uri", []):
            if ":" in v:
                celex = v.split(":")[-1]
            else:
                celex = v
            break
        # Strip date suffix: 02014L0024-20260101 → 02014L0024
        if "-" in celex:
            celex = celex.split("-")[0]
        return f"eurlex_{celex}" if celex else "eurlex_unknown"

    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return "unknown"
    last = parts[-1]
    # GII: /vgv_2016/xml.zip → last is "xml.zip" (filename), use parent dir
    # VV: /bsvwvbund_123.htm → last IS the slug
    if "." in last:
        stem = last.rsplit(".", 1)[0]
        # GII: stem is "xml" (generic), use parent dir instead
        if stem in ("xml", "index", "html"):
            return parts[-2] if len(parts) >= 2 else stem
        return stem
    return last


def save_to_cache(
    source_type: str,
    url: str,
    raw_data: bytes,
    cache_dir: Path | None = None,
) -> Path:
    """Write a single source's raw data to cache, updating the manifest.

    Args:
        source_type: One of gii_xml, vv_html, eurlex_html.
        url: Original source URL.
        raw_data: Post-extraction bytes (GII XML unzipped, others raw).
        cache_dir: Cache directory (default: data/cache).

    Returns:
        Path to the cached file.
    """
    cache_dir = cache_dir or CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    slug = _slugify(url, source_type)
    ext = _EXTENSIONS.get(source_type, ".bin")
    filename = f"{slug}{ext}"
    filepath = cache_dir / filename

    filepath.write_bytes(raw_data)

    # Update manifest
    manifest = read_manifest(cache_dir)
    manifest[filename] = {"url": url, "source_type": source_type}
    write_manifest(manifest, cache_dir)

    return filepath


def read_manifest(cache_dir: Path | None = None) -> dict:
    """Read the cache manifest, returning {} if absent."""
    cache_dir = cache_dir or CACHE_DIR
    manifest_path = cache_dir / MANIFEST_FILE
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def write_manifest(manifest: dict, cache_dir: Path | None = None):
    """Write the cache manifest."""
    cache_dir = cache_dir or CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / MANIFEST_FILE
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def list_cached(cache_dir: Path | None = None) -> list[dict]:
    """List all cached sources from the manifest.

    Returns:
        List of dicts with keys: filename, url, source_type, path.
    """
    cache_dir = cache_dir or CACHE_DIR
    manifest = read_manifest(cache_dir)
    result = []
    for filename, info in sorted(manifest.items()):
        result.append({
            "filename": filename,
            "url": info["url"],
            "source_type": info["source_type"],
            "path": str(cache_dir / filename),
        })
    return result


def clear_cache(cache_dir: Path | None = None) -> int:
    """Delete the entire cache directory (manifest + all files).

    Returns:
        Number of files deleted.
    """
    import shutil

    cache_dir = cache_dir or CACHE_DIR
    if not cache_dir.exists():
        return 0

    manifest = read_manifest(cache_dir)
    file_count = len(manifest)
    shutil.rmtree(cache_dir)
    return file_count
