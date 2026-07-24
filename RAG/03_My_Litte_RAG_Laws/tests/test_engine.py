"""Tests for the legal query engine."""
import pytest
from db import LegalDatabase
from legal_engine import LegalEngine


@pytest.fixture
def engine(tmp_path):
    """Create engine with a temp database and some test data."""
    db = LegalDatabase(str(tmp_path / "test.db"))
    law_id = db.insert_law("Verordnung über die Vergabe", "VgV", "2024-02-07", "VgV.md")
    db.insert_paragraph(law_id, "1", "paragraph", "Gegenstand",
                        "§ 1 Gegenstand und Anwendungsbereich (1) Dies ist ein Test.")
    db.insert_paragraph(law_id, "2", "paragraph", "", "§ 2 Vergabe von Bauaufträgen.")
    return LegalEngine(db=db)


def test_retrieve_by_abbreviation(engine):
    result = engine.retrieve_paragraph("VgV", "1")
    assert result is not None
    assert result["law_abbreviation"] == "VgV"
    assert "Gegenstand" in result["content"]
    assert "error" not in result


def test_retrieve_by_full_name(engine):
    result = engine.retrieve_paragraph("Verordnung über die Vergabe", "2")
    assert result is not None
    assert result["section_number"] == "2"
    assert "error" not in result


def test_retrieve_not_found(engine):
    result = engine.retrieve_paragraph("VgV", "999")
    assert result["error"] == "section_not_found"
    assert "available_range" in result
    assert result["available_range"]["max"] == 2


def test_retrieve_not_found_law_missing(engine):
    """retrieve_paragraph with nonexistent law should return law_not_found error."""
    result = engine.retrieve_paragraph("NONEXISTENT", "1")
    assert result["error"] == "law_not_found"
    assert "available_laws" in result


def test_retrieve_not_found_section_missing(engine):
    """retrieve_paragraph with existing law but missing section should return section_not_found."""
    result = engine.retrieve_paragraph("VgV", "999")
    assert result["error"] == "section_not_found"
    assert result["available_range"]["max"] == 2


def test_search(engine):
    results = engine.search_paragraphs("Vergabe")
    assert len(results) >= 1
    assert results[0]["section_number"] == "2"


def test_list_laws(engine):
    laws = engine.list_laws()
    assert len(laws) == 1
    assert laws[0]["abbreviation"] == "VgV"
    assert laws[0]["section_count"] == 2


def test_retrieve_strips_section_sign(engine):
    """Should work whether user passes '1' or '§1' or '§ 1'."""
    result = engine.retrieve_paragraph("VgV", "§ 1")
    assert result is not None
    assert result["section_number"] == "1"
