"""Tests for the EUR-Lex ELI XHTML parser."""
from __future__ import annotations

from pathlib import Path

from fetch.parsers.eurlex_html import EurlexHtmlParser

FIXTURE = Path(__file__).resolve().parent / "testdata" / "eurlex_sample.xhtml"


def _parse():
    raw = FIXTURE.read_text(encoding="utf-8")
    return EurlexHtmlParser().parse(raw, source_url="https://eur-lex.example/32014L0024")


def test_law_name_contains_directive():
    doc = _parse()
    assert "RICHTLINIE 2014/24/EU" in doc.law_name


def test_abbreviation_extracted():
    doc = _parse()
    assert doc.abbreviation == "2014/24/EU"


def test_paragraph_count():
    doc = _parse()
    assert len(doc.paragraphs) == 3


def test_first_paragraph_number_and_type():
    doc = _parse()
    p = doc.paragraphs[0]
    assert p.section_number == "1"
    assert p.section_type == "article"


def test_first_paragraph_title():
    doc = _parse()
    assert "Gegenstand" in doc.paragraphs[0].title


def test_first_article_content_starts_with_heading():
    doc = _parse()
    assert doc.paragraphs[0].content.startswith("Artikel 1")


def test_all_paragraphs_have_content():
    doc = _parse()
    for p in doc.paragraphs:
        assert p.content.strip(), f"paragraph {p.section_number} has empty content"


def test_no_duplicate_section_numbers():
    doc = _parse()
    numbers = [p.section_number for p in doc.paragraphs]
    assert len(numbers) == len(set(numbers))


def test_second_and_third_articles_parsed():
    doc = _parse()
    assert doc.paragraphs[1].section_number == "2"
    assert doc.paragraphs[2].section_number == "3"
    assert "Begriffsbestimmungen" in doc.paragraphs[1].title


def test_modref_markers_stripped():
    """Modification-reference symbols (▼B / ▼C2) must not leak into content."""
    doc = _parse()
    for p in doc.paragraphs:
        assert "▼" not in p.content
        assert "modref" not in p.content.lower()
