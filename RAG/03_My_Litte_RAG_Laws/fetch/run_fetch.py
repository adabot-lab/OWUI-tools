#!/usr/bin/env python3
"""CLI entrypoint: fetch/parse law texts, validate, store in SQLite.

Usage:
    python -m fetch.run_fetch                        # download + store
    python -m fetch.run_fetch --dry-run              # download + validate, no DB
    python -m fetch.run_fetch --from-cache           # parse from data/cache/, no network
    python -m fetch.run_fetch --cache                # download + save to data/cache/ + store
    python -m fetch.run_fetch --sources custom.txt   # custom sources file

Environment variables:
    LEGAL_SOURCES_FILE: Path to source URLs file (default: input/sources.txt)
    LEGAL_DB_PATH:      SQLite path (default: data/legal.db)
"""
import argparse
import os
import sys
from pathlib import Path

from fetch.fetcher import (
    FetchResult,
    fetch_one,
    fetch_all_from_cache,
    read_sources_file,
)
from fetch import cache as cache_mod
from fetch.validator import validate_extraction
from db import LegalDatabase

SOURCES_FILE = os.getenv("LEGAL_SOURCES_FILE", "input/sources.txt")
DB_PATH = os.getenv("LEGAL_DB_PATH", "data/legal.db")


def store_result(
    result: FetchResult,
    db: LegalDatabase | None = None,
    dry_run: bool = False,
) -> dict:
    """Process a FetchResult: validate, optionally store in DB.

    Returns a summary dict with counts, validation issues, and status.
    """
    url = result.url

    if result.error or result.document is None:
        error_msg = result.error or "No document returned"
        print(f"  ERROR: {error_msg}")
        return {"url": url, "error": error_msg, "section_count": 0, "issues": []}

    doc = result.document
    print(f"  Law: {doc.law_name}")
    print(f"  Abbr: {doc.abbreviation}")
    print(f"  Stand: {doc.stand_date}")
    print(f"  Source type: {result.source_type}")
    print(f"  Sections: {len(doc.paragraphs)}")

    # Validate
    report = validate_extraction(doc.paragraphs)
    if report.issues:
        print(f"  VALIDATION ISSUES:")
        for issue in report.issues:
            print(f"    - {issue}")
    else:
        print(f"  Validation: OK (no issues)")

    # Skip documents with empty abbreviation: the laws table has
    # abbreviation TEXT NOT NULL UNIQUE, so two empty-abbrev docs in one run
    # would collapse into one row and silently delete each other's paragraphs.
    if not dry_run and db and not doc.abbreviation.strip():
        print("  WARNING: empty abbreviation — skipping DB write "
              "(would collide with other laws via UNIQUE constraint)")
        return {
            "url": url,
            "law_name": doc.law_name,
            "abbreviation": doc.abbreviation,
            "source_type": result.source_type,
            "section_count": len(doc.paragraphs),
            "issues": report.issues,
            "skipped": "empty_abbreviation",
        }

    # Store in database
    if not dry_run and db:
        law_id = db.insert_law(
            name=doc.law_name,
            abbreviation=doc.abbreviation,
            stand_date=doc.stand_date,
            source_file=url,
        )
        db.replace_law_paragraphs(law_id, doc.to_paragraph_dicts())
        print(f"  Written to database: {db.db_path}")

    return {
        "url": url,
        "law_name": doc.law_name,
        "abbreviation": doc.abbreviation,
        "source_type": result.source_type,
        "section_count": len(doc.paragraphs),
        "issues": report.issues,
    }


def run_pipeline(
    from_cache: bool = False,
    save_cache: bool = False,
    dry_run: bool = False,
    sources_path: str = SOURCES_FILE,
    db_path: str = DB_PATH,
) -> list[dict]:
    """Run the fetch/parse/validate/store pipeline.

    Args:
        from_cache: If True, read from data/cache/ instead of downloading.
        save_cache: If True, save downloaded files to data/cache/.
        dry_run: If True, validate but don't write to DB.
        sources_path: Path to sources file (ignored if from_cache).
        db_path: SQLite database path.

    Returns:
        List of summary dicts.
    """
    # Collect FetchResults
    if from_cache:
        cached = cache_mod.list_cached()
        if not cached:
            print(f"Cache is empty. Run with --cache first to populate it.")
            sys.exit(1)
        print(f"Loading {len(cached)} file(s) from cache: {cache_mod.CACHE_DIR}")
        results = fetch_all_from_cache()
    else:
        urls = read_sources_file(sources_path)
        if not urls:
            print(f"No URLs found in {sources_path}")
            sys.exit(1)
        print(f"Found {len(urls)} source URL(s) in {sources_path}")
        cache_dir = cache_mod.CACHE_DIR if save_cache else None
        results = [
            fetch_one(url, cache_dir=cache_dir)
            for url in urls
        ]

    # Initialize DB (unless dry-run)
    db = None if dry_run else LegalDatabase(db_path)

    # Process each result
    summaries = []
    for i, result in enumerate(results):
        print(f"\n{'='*60}")
        label = "Caching" if from_cache else "Fetching"
        print(f"{label}: {result.url or '(cached file)'}")
        print(f"{'='*60}")
        summary = store_result(result, db, dry_run)
        summaries.append(summary)

    # Final report
    print(f"\n{'='*60}")
    mode = "CACHE" if from_cache else "FETCH"
    print(f"{mode} COMPLETE")
    print(f"{'='*60}")
    total_sections = sum(s.get("section_count", 0) for s in summaries)
    total_errors = sum(1 for s in summaries if s.get("error"))
    total_issues = sum(len(s.get("issues", [])) for s in summaries)
    print(f"  Sources processed: {len(summaries)}")
    print(f"  Total sections:    {total_sections}")
    print(f"  Errors:            {total_errors}")
    print(f"  Validation issues: {total_issues}")

    return summaries


def main():
    parser = argparse.ArgumentParser(
        description="Fetch and parse law texts from official sources"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and validate without writing to DB"
    )
    parser.add_argument(
        "--from-cache", action="store_true",
        help="Parse from data/cache/ instead of downloading"
    )
    parser.add_argument(
        "--cache", action="store_true",
        help="Download and save raw files to data/cache/ (for offline use)"
    )
    parser.add_argument(
        "--sources", default=SOURCES_FILE,
        help=f"Path to sources file (default: {SOURCES_FILE})"
    )
    args = parser.parse_args()

    summaries = run_pipeline(
        from_cache=args.from_cache,
        save_cache=args.cache,
        dry_run=args.dry_run,
        sources_path=args.sources,
    )

    if any(s.get("error") for s in summaries):
        sys.exit(1)


if __name__ == "__main__":
    main()
