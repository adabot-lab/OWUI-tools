"""Tests for the GII (gesetze-im-internet.de) HTML parser.

Uses a small Grundgesetz (GG) sample at tests/testdata/gg_sample.html.
"""
from pathlib import Path

from fetch.parsers.base import LawDocument
from fetch.parsers.gii_html import GiiHtmlParser

FIXTURE = Path(__file__).parent / "testdata" / "gg_sample.html"


def _load_fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _parse() -> LawDocument:
    parser = GiiHtmlParser()
    return parser.parse(_load_fixture_bytes(),
                        source_url="https://www.gesetze-im-internet.de/gg/BJNR000010949.html")


# --- Tests -----------------------------------------------------------------


def test_abbreviation_is_gg():
    """abbreviation should be 'GG'."""
    doc = _parse()
    assert doc.abbreviation == "GG"


def test_law_name_starts_with_grundgesetz():
    """law_name should start with 'Grundgesetz'."""
    doc = _parse()
    assert doc.law_name.startswith("Grundgesetz")


def test_stand_date_extracted():
    """stand_date should contain the Ausfertigungsdatum."""
    doc = _parse()
    assert "23.05.1949" in doc.stand_date


def test_articles_extracted():
    """Should extract 3 articles (Art 1, Art 2, Art 3)."""
    doc = _parse()
    assert len(doc.paragraphs) == 3


def test_article_numbers_correct():
    """Section numbers should be '1', '2', '3'."""
    doc = _parse()
    numbers = [p.section_number for p in doc.paragraphs]
    assert numbers == ["1", "2", "3"]


def test_section_type_is_article():
    """All GG sections use Art -> section_type 'article'."""
    doc = _parse()
    for p in doc.paragraphs:
        assert p.section_type == "article"


def test_article_1_has_three_absatz():
    """Article 1 should have 3 Absatz (jurAbsatz divs)."""
    doc = _parse()
    art1 = doc.paragraphs[0]
    # Content should contain 3 numbered paragraphs
    assert "(1)" in art1.content
    assert "(2)" in art1.content
    assert "(3)" in art1.content


def test_article_1_content_has_menschenwuerde():
    """Article 1 should mention 'Würde des Menschen'."""
    doc = _parse()
    art1 = doc.paragraphs[0]
    assert "Würde des Menschen" in art1.content


def test_source_url_propagated():
    """source_url passed to parse() should be stored on the document."""
    doc = _parse()
    assert doc.source_url == "https://www.gesetze-im-internet.de/gg/BJNR000010949.html"


def test_all_paragraphs_have_non_empty_content():
    """Every extracted paragraph must have non-empty content."""
    doc = _parse()
    assert doc.paragraphs, "no paragraphs extracted"
    for p in doc.paragraphs:
        assert p.content.strip(), f"empty content for Art {p.section_number}"


def test_content_starts_with_article_marker():
    """Content should start with 'Art N' marker."""
    doc = _parse()
    for p in doc.paragraphs:
        assert p.content.startswith(f"Art {p.section_number}")


def test_accepts_decoded_string_input():
    """Parser should also accept a pre-decoded latin-1 string."""
    text = _load_fixture_bytes().decode("latin-1")
    doc = GiiHtmlParser().parse(text)
    assert len(doc.paragraphs) == 3
    assert doc.abbreviation == "GG"


def test_glirung_norms_skipped():
    """Norms with title='Gliederung' should be skipped (structure-only)."""
    doc = _parse()
    # We should only have 3 Einzelnorm articles, not the Gliederung heading
    assert len(doc.paragraphs) == 3
    # The Gliederung heading 'Erster Abschnitt' should NOT appear as a paragraph
    numbers = [p.section_number for p in doc.paragraphs]
    assert "Abschnitt" not in numbers
