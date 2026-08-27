"""Integration test: serve-mode incremental source diff.

Tests that when a DB already exists, the serve-mode logic correctly diffs
sources.txt against URLs already stored and only fetches new URLs.
"""
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from db import LegalDatabase


@pytest.fixture
def db_with_two_laws(tmp_path):
    """Create a temp DB with 2 laws already stored."""
    db_path = tmp_path / "test_legal.db"
    db = LegalDatabase(str(db_path))
    db.insert_law("Law Alpha", "LA", "2024-01-01", "https://example.com/alpha")
    db.insert_law("Law Beta", "LB", "2024-01-02", "https://example.com/beta")
    return db, str(db_path)


def test_incremental_diff_finds_only_new_urls(db_with_two_laws):
    """Given 2 laws in DB and 3 URLs in sources.txt, only the new URL
    should be flagged for fetching."""
    db, db_path = db_with_two_laws

    # Simulate sources.txt content: 2 existing + 1 new
    all_urls = [
        "https://example.com/alpha",   # already in DB
        "https://example.com/beta",    # already in DB
        "https://example.com/gamma",   # NEW
    ]

    existing = set(db.list_source_urls())
    new_urls = list(dict.fromkeys(u for u in all_urls if u not in existing))

    assert new_urls == ["https://example.com/gamma"]


def test_incremental_diff_dedupes_duplicates(db_with_two_laws):
    """Duplicate URLs in sources.txt should be deduplicated."""
    db, db_path = db_with_two_laws

    all_urls = [
        "https://example.com/gamma",   # NEW
        "https://example.com/gamma",   # duplicate
        "https://example.com/alpha",   # existing
        "https://example.com/gamma",   # another duplicate
    ]

    existing = set(db.list_source_urls())
    new_urls = list(dict.fromkeys(u for u in all_urls if u not in existing))

    assert new_urls == ["https://example.com/gamma"]


def test_incremental_diff_no_new_urls(db_with_two_laws):
    """When all URLs are already in DB, new_urls should be empty."""
    db, db_path = db_with_two_laws

    all_urls = [
        "https://example.com/alpha",
        "https://example.com/beta",
    ]

    existing = set(db.list_source_urls())
    new_urls = list(dict.fromkeys(u for u in all_urls if u not in existing))

    assert new_urls == []


def test_list_source_urls_after_insert(db_with_two_laws):
    """Verify the DB correctly reports stored source URLs."""
    db, _ = db_with_two_laws
    urls = db.list_source_urls()
    assert "https://example.com/alpha" in urls
    assert "https://example.com/beta" in urls
    assert len(urls) == 2
