"""Tests for the SQLite legal database layer."""
import os
import tempfile
import pytest
from db import LegalDatabase


@pytest.fixture
def db(tmp_path):
    """Create a temporary database for testing."""
    db_path = tmp_path / "test_legal.db"
    return LegalDatabase(str(db_path))


def test_database_initialization_creates_tables(db):
    """Database should create laws and paragraphs tables on init."""
    tables = db.list_tables()
    assert "laws" in tables
    assert "paragraphs" in tables
    assert "paragraphs_fts" in tables  # FTS5 virtual table


def test_insert_and_retrieve_law(db):
    """Should insert a law and retrieve it."""
    law_id = db.insert_law(
        name="Verordnung über die Vergabe öffentlicher Aufträge",
        abbreviation="VgV",
        stand_date="2024-02-07",
        source_file="input/VgV.md"
    )
    law = db.get_law_by_id(law_id)
    assert law is not None
    assert law["abbreviation"] == "VgV"
    assert law["name"] == "Verordnung über die Vergabe öffentlicher Aufträge"


def test_insert_and_retrieve_paragraph(db):
    """Should insert a paragraph and retrieve it by law + section number."""
    law_id = db.insert_law("Test Law", "TL", "2024-01-01", "test.md")
    db.insert_paragraph(
        law_id=law_id,
        section_number="1",
        section_type="paragraph",  # "paragraph" for § or "article" for Artikel
        title="Gegenstand",
        content="§ 1 Gegenstand und Anwendungsbereich (1) Dies ist ein Test."
    )

    result = db.get_paragraph("TL", "1")
    assert result is not None
    assert result["section_number"] == "1"
    assert "Test" in result["content"]
    assert result["law_abbreviation"] == "TL"


def test_get_paragraph_by_full_name(db):
    """Should find paragraph using the full law name, not just abbreviation."""
    law_id = db.insert_law("Test Gesetz Vollname", "TG", "2024-01-01", "test.md")
    db.insert_paragraph(law_id, "5", "paragraph", "", "§ 5 Inhalt")

    result = db.get_paragraph("Test Gesetz Vollname", "5")
    assert result is not None
    assert result["section_number"] == "5"


def test_get_paragraph_not_found(db):
    """Should return None for non-existent paragraph."""
    result = db.get_paragraph("Nonexistent", "999")
    assert result is None


def test_fts_search(db):
    """FTS5 search should find paragraphs by content keyword."""
    law_id = db.insert_law("Test Law", "TL", "2024-01-01", "test.md")
    db.insert_paragraph(law_id, "1", "paragraph", "",
                        "§ 1 Diese Regelung betrifft die Vergabe von Bauaufträgen.")
    db.insert_paragraph(law_id, "2", "paragraph", "",
                        "§ 2 Hier geht es um something completely different.")

    results = db.search_paragraphs("Vergabe")
    assert len(results) >= 1
    assert results[0]["section_number"] == "1"


def test_list_laws(db):
    """Should list all laws with their paragraph counts."""
    law_id_1 = db.insert_law("Law A", "LA", "2024-01-01", "a.md")
    law_id_2 = db.insert_law("Law B", "LB", "2024-01-01", "b.md")
    db.insert_paragraph(law_id_1, "1", "paragraph", "", "§ 1 content")
    db.insert_paragraph(law_id_1, "2", "paragraph", "", "§ 2 content")
    db.insert_paragraph(law_id_2, "1", "paragraph", "", "§ 1 content")

    laws = db.list_laws()
    assert len(laws) == 2
    law_a = next(l for l in laws if l["abbreviation"] == "LA")
    assert law_a["section_count"] == 2


def test_replace_law_data(db):
    """Should clear and replace all paragraphs for a law (re-extraction scenario)."""
    law_id = db.insert_law("Test Law", "TL", "2024-01-01", "test.md")
    db.insert_paragraph(law_id, "1", "paragraph", "", "old content")

    db.replace_law_paragraphs(law_id, [
        {"section_number": "1", "section_type": "paragraph", "title": "", "content": "new"},
        {"section_number": "2", "section_type": "paragraph", "title": "", "content": "new2"},
    ])

    result = db.get_paragraph("TL", "1")
    assert result["content"] == "new"
    result2 = db.get_paragraph("TL", "2")
    assert result2 is not None


def test_reinsert_law_preserves_id(db):
    """Re-inserting a law (re-extraction) must return the SAME law id.

    Regression test: previously used cursor.lastrowid which returned a stale
    value on ON CONFLICT UPDATE, causing paragraphs to be written under the
    wrong law_id on re-extraction.
    """
    law_id_1 = db.insert_law("Test Law", "TL", "2024-01-01", "test.md")
    # Simulate re-extraction (same abbreviation triggers ON CONFLICT UPDATE)
    law_id_2 = db.insert_law("Test Law", "TL", "2024-06-01", "test.md")
    assert law_id_1 == law_id_2, "Re-inserting same abbreviation must return same id"

    # Paragraphs written with the returned id must be retrievable
    db.replace_law_paragraphs(law_id_2, [
        {"section_number": "1", "section_type": "paragraph", "title": "", "content": "re-extracted"},
    ])
    result = db.get_paragraph("TL", "1")
    assert result is not None
    assert result["content"] == "re-extracted"


def test_list_source_urls_returns_stored_urls(db):
    """list_source_urls should return all source_file URLs in the laws table."""
    db.insert_law("Law A", "LA", "2024-01-01", "https://example.com/a")
    db.insert_law("Law B", "LB", "2024-01-01", "https://example.com/b")
    db.insert_law("Law C", "LC", "2024-01-01", "")  # empty source_file

    urls = db.list_source_urls()
    assert "https://example.com/a" in urls
    assert "https://example.com/b" in urls
    # Empty source_file should be filtered out
    assert "" not in urls
    assert len(urls) == 2


def test_list_source_urls_empty_db(db):
    """list_source_urls on an empty DB should return an empty list."""
    urls = db.list_source_urls()
    assert urls == []


def test_get_paragraph_strips_artikel_prefix(db):
    """Should strip 'Artikel ' prefix for EU directives, not just §."""
    law_id = db.insert_law("EU Directive", "EU-2014/24/EU", "2014-03-26", "eu.md")
    db.insert_paragraph(law_id, "1", "article", "Gegenstand", "Artikel 1 Gegenstand")

    # User passes "Artikel 1" — should find it
    result = db.get_paragraph("EU-2014/24/EU", "Artikel 1")
    assert result is not None
    assert result["section_type"] == "article"

    # Case-insensitive
    result2 = db.get_paragraph("EU-2014/24/EU", "artikel 1")
    assert result2 is not None


def test_get_law_section_range(db):
    """get_law_section_range should return min/max/total for numeric sections."""
    law_id = db.insert_law("Test Law", "TL", "2024-01-01", "test.md")
    db.insert_paragraph(law_id, "1", "paragraph", "", "section 1 content")
    db.insert_paragraph(law_id, "2", "paragraph", "", "section 2 content")
    db.insert_paragraph(law_id, "10", "paragraph", "", "section 10 content")

    result = db.get_law_section_range(law_id)
    assert result is not None
    assert result["min"] == 1
    assert result["max"] == 10
    assert result["total"] == 3


def test_get_law_section_range_with_suffix(db):
    """get_law_section_range should handle suffixed section numbers like 2a."""
    law_id = db.insert_law("Test Law", "TL", "2024-01-01", "test.md")
    db.insert_paragraph(law_id, "1", "paragraph", "", "section 1")
    db.insert_paragraph(law_id, "2a", "paragraph", "", "section 2a")
    db.insert_paragraph(law_id, "7", "paragraph", "", "section 7")

    result = db.get_law_section_range(law_id)
    assert result is not None
    assert result["min"] == 1
    assert result["max"] == 7
    assert result["total"] == 3


def test_get_law_section_range_empty_law(db):
    """get_law_section_range should return None for law with no paragraphs."""
    law_id = db.insert_law("Empty Law", "EL", "2024-01-01", "test.md")
    result = db.get_law_section_range(law_id)
    assert result is None


def test_find_law_by_full_name(db):
    """find_law should match by full name, abbreviation, and return None for miss."""
    db.insert_law("Test Gesetz Vollname", "TGV", "2024-01-01", "test.md")

    # Match by full name
    result = db.find_law("Test Gesetz Vollname")
    assert result is not None
    assert result["abbreviation"] == "TGV"

    # Match by abbreviation
    result = db.find_law("TGV")
    assert result is not None
    assert result["abbreviation"] == "TGV"

    # Case-insensitive abbreviation
    result = db.find_law("tgv")
    assert result is not None

    # Nonexistent
    result = db.find_law("NONEXISTENT")
    assert result is None


def test_fts_multi_term_non_adjacent_match(db):
    """FTS5 multi-term query must match terms that are far apart in the text.

    Regression test: the old code wrapped the whole query in double quotes
    (phrase search), so a two-term query only matched adjacent terms and
    returned [] for a sentence where the terms are non-adjacent.
    """
    law_id = db.insert_law("Vergaberecht Test", "VRG", "2024-01-01", "test.md")
    db.insert_paragraph(
        law_id, "1", "paragraph", "",
        "§ 1 Die Vergabe von öffentlichen Aufträgen erfolgt gemäß dem "
        "geltenden Wettbewerbsrecht.",
    )

    results = db.search_paragraphs("Vergabe Wettbewerbsrecht")
    assert len(results) >= 1
    assert results[0]["section_number"] == "1"

    single = db.search_paragraphs("Vergabe")
    assert len(single) >= 1

    assert db.search_paragraphs("   ") == []
