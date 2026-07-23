#!/usr/bin/env python3
"""End-to-end extraction pipeline.

Usage:
    python -m extract.run_extraction                    # process all .md in input/
    python -m extract.run_extraction input/VgV.md       # process specific file
    python -m extract.run_extraction --dry-run          # extract without writing to DB
    python -m extract.run_extraction --expected VgV:127 # with expected paragraph counts

Environment variables:
    LLM_BASE_URL:     LLM API endpoint (default: http://localhost:4000/v1)
    LLM_EXTRACT_MODEL: Model name (default: zai-glm-4.7)
    LEGAL_DB_PATH:    SQLite path (default: data/legal.db)
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from extract.chunker import extract_header, chunk_markdown
from extract.extractor import LLMExtractor
from extract.validator import validate_extraction
from db import LegalDatabase

INPUT_DIR = Path(os.getenv("LEGAL_DOCUMENTS_DIR", "input"))
OUTPUT_JSON = Path("data/laws.json")
DB_PATH = os.getenv("LEGAL_DB_PATH", "data/legal.db")


def process_file(
    md_path: Path,
    extractor: LLMExtractor,
    db: LegalDatabase | None = None,
    expected_count: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Process a single markdown file: chunk, extract, validate, store.

    Returns a summary dict with counts and any issues.
    """
    print(f"\n{'='*60}")
    print(f"Processing: {md_path.name}")
    print(f"{'='*60}")

    content = md_path.read_text(encoding="utf-8")
    header = extract_header(content)

    if not header["name"]:
        print(f"  WARNING: Could not extract law name from header")
        header["name"] = md_path.stem

    if not header["abbreviation"]:
        print(f"  WARNING: Could not extract abbreviation from header")
        header["abbreviation"] = md_path.stem

    print(f"  Law: {header['name']}")
    print(f"  Abbr: {header['abbreviation']}")
    print(f"  Stand: {header['stand_date']}")

    # Chunk the markdown
    chunks = chunk_markdown(content, max_chunk_chars=12000)
    print(f"  Chunks: {len(chunks)}")

    # Extract from each chunk
    all_paragraphs = []
    for i, chunk in enumerate(chunks):
        print(f"  Extracting chunk {i+1}/{len(chunks)}...", end=" ", flush=True)
        start = time.time()
        paragraphs = extractor.extract(chunk)
        elapsed = time.time() - start
        print(f"{len(paragraphs)} sections ({elapsed:.1f}s)")
        all_paragraphs.extend(paragraphs)

        # Rate limit: small delay between chunks
        if i < len(chunks) - 1:
            time.sleep(0.5)

    # Deduplicate across chunks (a paragraph might span a chunk boundary)
    seen = set()
    deduped = []
    for p in all_paragraphs:
        if p["section_number"] not in seen:
            seen.add(p["section_number"])
            deduped.append(p)
    all_paragraphs = deduped

    print(f"  Total unique sections: {len(all_paragraphs)}")

    # Validate
    report = validate_extraction(all_paragraphs, expected_count)
    if report.issues:
        print(f"  VALIDATION ISSUES:")
        for issue in report.issues:
            print(f"    - {issue}")
    else:
        print(f"  Validation: OK (no issues)")

    # Write to database
    if not dry_run and db:
        law_id = db.insert_law(
            name=header["name"],
            abbreviation=header["abbreviation"],
            stand_date=header["stand_date"],
            source_file=str(md_path),
        )
        db.replace_law_paragraphs(law_id, all_paragraphs)
        print(f"  Written to database: {db.db_path}")

    return {
        "file": str(md_path),
        "law_name": header["name"],
        "abbreviation": header["abbreviation"],
        "section_count": len(all_paragraphs),
        "issues": report.issues,
    }


def main():
    parser = argparse.ArgumentParser(description="Extract legal paragraphs from markdown")
    parser.add_argument("files", nargs="*", help="Specific .md files to process (default: all in input/)")
    parser.add_argument("--dry-run", action="store_true", help="Extract without writing to DB")
    parser.add_argument("--expected", action="append", default=[],
                        help="Expected section counts: --expected VgV:127 --expected GWB:200")
    parser.add_argument("--input-dir", default=str(INPUT_DIR), help="Input directory")
    args = parser.parse_args()

    # Resolve files
    input_dir = Path(args.input_dir)
    if args.files:
        md_files = [Path(f) for f in args.files]
    else:
        md_files = sorted(input_dir.glob("*.md"))

    if not md_files:
        print(f"No .md files found in {input_dir}")
        sys.exit(1)

    # Parse expected counts
    expected_map = {}
    for item in args.expected:
        abbr, _, count = item.partition(":")
        expected_map[abbr.strip()] = int(count)

    # Initialize components
    extractor = LLMExtractor()
    db = None if args.dry_run else LegalDatabase(DB_PATH)

    # Process each file
    summaries = []
    for md_path in md_files:
        expected = expected_map.get(md_path.stem)
        summary = process_file(md_path, extractor, db, expected, args.dry_run)
        summaries.append(summary)

    # Write JSON summary
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")

    # Final report
    print(f"\n{'='*60}")
    print(f"EXTRACTION COMPLETE")
    print(f"{'='*60}")
    total_sections = sum(s["section_count"] for s in summaries)
    total_issues = sum(len(s["issues"]) for s in summaries)
    print(f"  Files processed: {len(summaries)}")
    print(f"  Total sections:  {total_sections}")
    print(f"  Total issues:    {total_issues}")

    if total_issues > 0:
        print(f"\n  Issues require manual review. See data/laws.json for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()
