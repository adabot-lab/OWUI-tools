#!/usr/bin/env python3
"""Docker entrypoint: orchestrates data loading mode then starts MCP server.

Modes (via FETCH_MODE env var):
    serve  (default) — if DB exists, just serve;
                       if no DB + cache available, populate from cache then serve;
                       if no DB + no cache, warn and serve empty
    fetch            — refresh: if DB exists, nuke all + re-download;
                       if no DB, use cache if available, else download fresh
    local            — nuke DB, parse from data/cache/, populate, then serve
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

FETCH_MODE = os.getenv("FETCH_MODE", "serve")
DB_PATH = os.getenv("LEGAL_DB_PATH", "data/legal.db")


def _nuke_db():
    """Delete the SQLite database files."""
    db = Path(DB_PATH)
    for suffix in ("", "-wal", "-shm"):
        p = db.with_suffix(db.suffix + suffix) if suffix else db
        if p.exists():
            p.unlink()
            print(f"  Deleted: {p}")


def _nuke_cache():
    """Delete the cache directory."""
    from fetch.cache import clear_cache
    count = clear_cache()
    if count:
        print(f"  Cleared cache ({count} files)")


def _cache_has_files() -> bool:
    """Check if cache directory has any cached source files."""
    from fetch.cache import list_cached
    return len(list_cached()) > 0


def _serve_with_incremental_fetch():
    """Serve mode with incremental source diff.

    When a DB already exists, diff sources.txt against URLs already stored.
    Fetch only new URLs incrementally; existing laws are untouched (append-only).
    If sources.txt is absent, skip the diff (backward compatible).
    """
    sources_file = os.getenv("LEGAL_SOURCES_FILE", "input/sources.txt")

    if not Path(sources_file).exists():
        print("[entrypoint] Mode: serve — DB exists, no sources file, "
              "starting server")
        return

    from fetch.fetcher import read_sources_file, fetch_one
    from fetch.run_fetch import store_result
    from fetch import cache as cache_mod
    from db import LegalDatabase

    urls = read_sources_file(sources_file)
    db = LegalDatabase(DB_PATH)
    existing = set(db.list_source_urls())
    # Dedupe URLs while preserving order (dict.fromkeys in py3.7+)
    new_urls = list(dict.fromkeys(u for u in urls if u not in existing))

    if not new_urls:
        print("[entrypoint] Mode: serve — DB exists, no new sources, "
              "starting server")
        return

    print(f"[entrypoint] serve — {len(new_urls)} new source(s) found, "
          f"fetching incrementally...")
    for url in new_urls:
        result = fetch_one(url, cache_dir=cache_mod.CACHE_DIR)
        summary = store_result(result, db)
        if summary.get("error"):
            print(f"  FAILED: {url} — {summary['error']}")
    print("[entrypoint] serve — incremental fetch complete, starting server")


def main():
    mode = FETCH_MODE.lower().strip()

    if mode == "serve":
        db_exists = Path(DB_PATH).exists()

        if db_exists:
            _serve_with_incremental_fetch()
            # Fall through to MCP server

        elif _cache_has_files():
            # No DB but cache has files: populate from cache, then serve
            print("[entrypoint] Mode: serve — no DB, populating from cache, then serving")
            from fetch.run_fetch import run_pipeline
            run_pipeline(from_cache=True, dry_run=False)

        else:
            # No DB and no cache: start empty server (don't crash)
            print("[entrypoint] Mode: serve — no DB, no cache")
            print("[entrypoint] WARNING: No data available — server will start with an empty database")

    elif mode == "fetch":
        from fetch.run_fetch import run_pipeline

        db_exists = Path(DB_PATH).exists()

        if db_exists:
            # DB exists: nuke all, download fresh
            print("[entrypoint] Mode: fetch — DB exists, nuke all, download fresh, populate")
            _nuke_db()
            _nuke_cache()
            summaries = run_pipeline(from_cache=False, save_cache=True, dry_run=False)

        elif _cache_has_files():
            # No DB but cache has files: parse from cache (local mode)
            print("[entrypoint] Mode: fetch — no DB, using cache, populate")
            _nuke_db()
            summaries = run_pipeline(from_cache=True, dry_run=False)

        else:
            # No DB and no cache: download fresh
            print("[entrypoint] Mode: fetch — no DB, no cache, download fresh, populate")
            _nuke_db()
            summaries = run_pipeline(from_cache=False, save_cache=True, dry_run=False)

        if any(s.get("error") for s in summaries):
            print("[entrypoint] Fetch completed with errors — see above")
            sys.exit(1)

    elif mode == "local":
        print("[entrypoint] Mode: local — nuke DB, parse from cache, populate")
        _nuke_db()
        from fetch.run_fetch import run_pipeline
        summaries = run_pipeline(from_cache=True, dry_run=False)
        if any(s.get("error") for s in summaries):
            print("[entrypoint] Cache load completed with errors — see above")
            sys.exit(1)

    else:
        print(f"[entrypoint] Unknown FETCH_MODE: {mode!r}")
        print(f"  Valid values: serve, fetch, local")
        sys.exit(1)

    # Start MCP server
    print(f"[entrypoint] Starting MCP server...")
    import uvicorn
    from main import app, HOST, PORT
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
