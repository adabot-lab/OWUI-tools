#!/usr/bin/env python3
"""CLI entrypoint: read sources.txt, fetch, parse, validate, store in SQLite.

Usage:
    python -m fetch.run_fetch                        # process all in sources.txt
    python -m fetch.run_fetch --dry-run              # parse + validate, no DB write
    python -m fetch.run_fetch --sources custom.txt   # custom sources file

Environment variables:
    LEGAL_SOURCES_FILE: Path to source URLs file (default: input/sources.txt)
    LEGAL_DB_PATH:      SQLite path (default: data/legal.db)
"""
import argparse
import os
import sys
from pathlib import Path

from fetch.fetcher import fetch_one, fetch_all, read_sources_file
from fetch.validator import validate_extraction
from db import LegalDatabase

SOURCES_FILE = os.getenv("LEGAL_SOURCES_FILE", "input/sources.txt")
DB_PATH = os.getenv("LEGAL_DB_PATH", "data/legal.db")


def process_source(url: str, db: LegalDatabase | None = None,
                   dry_run: bool = False) -> dict:
    """Fetch, parse, and optionally store a single source URL.

    Returns a summary dict with counts, validation issues, and status.
    """
    print(f"\n{'='*60}")
    print(f"Fetching: {url}")
    print(f"{'='*60}")

    result = fetch_one(url)

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


def main():
    parser = argparse.ArgumentParser(
        description="Fetch and parse law texts from official sources"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and validate without writing to DB"
    )
    parser.add_argument(
        "--sources", default=SOURCES_FILE,
        help=f"Path to sources file (default: {SOURCES_FILE})"
    )
    args = parser.parse_args()

    # Read URLs
    sources_path = args.sources
    if not Path(sources_path).exists():
        print(f"Sources file not found: {sources_path}")
        sys.exit(1)

    urls = read_sources_file(sources_path)
    if not urls:
        print(f"No URLs found in {sources_path}")
        sys.exit(1)

    print(f"Found {len(urls)} source URL(s) in {sources_path}")

    # Initialize DB (unless dry-run)
    db = None if args.dry_run else LegalDatabase(DB_PATH)

    # Process each URL
    summaries = []
    for url in urls:
        summary = process_source(url, db, args.dry_run)
        summaries.append(summary)

    # Final report
    print(f"\n{'='*60}")
    print(f"FETCH COMPLETE")
    print(f"{'='*60}")
    total_sections = sum(s.get("section_count", 0) for s in summaries)
    total_errors = sum(1 for s in summaries if s.get("error"))
    total_issues = sum(len(s.get("issues", [])) for s in summaries)
    print(f"  Sources processed: {len(summaries)}")
    print(f"  Total sections:    {total_sections}")
    print(f"  Errors:            {total_errors}")
    print(f"  Validation issues: {total_issues}")

    if total_errors > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
