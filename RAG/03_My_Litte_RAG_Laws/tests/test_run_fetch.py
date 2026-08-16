"""Tests for fetch.run_fetch.store_result."""
import pytest

from db import LegalDatabase
from fetch.fetcher import FetchResult
from fetch.parsers.base import LawDocument, Paragraph
from fetch.run_fetch import store_result


def test_store_result_skips_empty_abbreviation(tmp_path):
    """Docs with an empty abbreviation must not be written to the DB.

    Regression test: the laws table has abbreviation TEXT NOT NULL UNIQUE, so
    two empty-abbrev docs in one run would collapse into one row and silently
    delete each other's paragraphs.
    """
    doc = LawDocument(
        law_name="Verordnung (EU) ohne Kurzel",
        abbreviation="",
        stand_date="",
        source_url="https://example.com/x",
        paragraphs=[Paragraph(section_number="1", section_type="paragraph",
                              title="T", content="§ 1 T")],
    )
    result = FetchResult(url="https://example.com/x",
                         source_type="eurlex_html",
                         document=doc)
    db = LegalDatabase(str(tmp_path / "test_legal.db"))

    summary = store_result(result, db=db, dry_run=False)

    assert summary["skipped"] == "empty_abbreviation"
    assert db.list_laws() == []
    assert db.search_paragraphs("Inhalt") == []


def test_store_result_stores_non_empty_abbreviation(tmp_path):
    """Docs with a non-empty abbreviation still get stored normally."""
    doc = LawDocument(
        law_name="Verordnung mit Kurzel",
        abbreviation="VOX",
        stand_date="2020-01-01",
        source_url="https://example.com/x",
        paragraphs=[Paragraph(section_number="1", section_type="paragraph",
                              title="T", content="§ 1 T")],
    )
    result = FetchResult(url="https://example.com/x",
                         source_type="eurlex_html",
                         document=doc)
    db = LegalDatabase(str(tmp_path / "test_legal.db"))

    summary = store_result(result, db=db, dry_run=False)

    assert "skipped" not in summary
    laws = db.list_laws()
    assert len(laws) == 1
    assert laws[0]["abbreviation"] == "VOX"
    assert laws[0]["section_count"] == 1
