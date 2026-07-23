#!/usr/bin/env python3
"""Docker entrypoint: orchestrates data loading mode then starts MCP server.

Modes (via FETCH_MODE env var):
    serve  (default) — start MCP server with existing DB, no data loading
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


def main():
    mode = FETCH_MODE.lower().strip()

    if mode == "serve":
        print("[entrypoint] Mode: serve — starting with existing DB")
        # Nothing to do, just fall through to MCP server

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
