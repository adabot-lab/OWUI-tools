"""Tests for the VV (verwaltungsvorschriften-im-internet.de) HTML parser.

Uses the real VOB/A sample fixture at tests/testdata/vob_sample.htm.
"""
from pathlib import Path

from fetch.parsers.base import LawDocument
from fetch.parsers.vv_html import VVHtmlParser

FIXTURE = Path(__file__).parent / "testdata" / "vob_sample.htm"


def _load_fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _parse() -> LawDocument:
    parser = VVHtmlParser()
    return parser.parse(_load_fixture_bytes(), source_url="https://example/vob-a")


# --- Tests -----------------------------------------------------------------


def test_law_name_contains_voba():
    """Law name should contain 'VOB/A'."""
    doc = _parse()
    assert "VOB/A" in doc.law_name


def test_abbreviation_is_voba():
    """abbreviation should be 'VOB/A'."""
    doc = _parse()
    assert doc.abbreviation == "VOB/A"


def test_extracts_exactly_five_paragraphs():
    """The fixture has 5 sections: §1, §2, §3, §1 EU, §1 VS."""
    doc = _parse()
    assert len(doc.paragraphs) == 5


def test_section_numbers_include_suffixes():
    """section_numbers should be exactly: '1', '2', '3', '1 EU', '1 VS'."""
    doc = _parse()
    numbers = [p.section_number for p in doc.paragraphs]
    assert "1" in numbers
    assert "2" in numbers
    assert "3" in numbers
    assert "1 EU" in numbers
    assert "1 VS" in numbers


def test_section_numbers_are_unique():
    """The plain '1' and the '1 EU' / '1 VS' variants must be distinct."""
    doc = _parse()
    numbers = [p.section_number for p in doc.paragraphs]
    assert len(numbers) == len(set(numbers)), f"duplicates in {numbers}"


def test_first_section_content_starts_with_marker_and_contains_bauleistungen():
    """Content of § 1 should start with '§ 1' and mention 'Bauleistungen'."""
    doc = _parse()
    first = doc.paragraphs[0]
    assert first.content.startswith("§ 1")
    assert "Bauleistungen" in first.content


def test_all_paragraphs_have_non_empty_content():
    """Every extracted paragraph must have non-empty content."""
    doc = _parse()
    assert doc.paragraphs, "no paragraphs extracted"
    for p in doc.paragraphs:
        assert p.content.strip(), f"empty content for § {p.section_number}"
        assert p.title, f"empty title for § {p.section_number}"


def test_section_type_is_paragraph():
    """All VOB/A sections use § -> section_type 'paragraph'."""
    doc = _parse()
    for p in doc.paragraphs:
        assert p.section_type == "paragraph"


def test_source_url_propagated():
    """source_url passed to parse() should be stored on the document."""
    doc = _parse()
    assert doc.source_url == "https://example/vob-a"


def test_accepts_decoded_string_input():
    """Parser should also accept a pre-decoded ISO-8859-1 string."""
    text = _load_fixture_bytes().decode("iso-8859-1")
    doc = VVHtmlParser().parse(text)
    assert len(doc.paragraphs) == 5
    assert doc.abbreviation == "VOB/A"


def test_letter_suffix_section_keeps_lowercase():
    """§ 6a must be stored as '6a', not uppercased to '6A'.

    Regression test: section numbers used to be .upper()-ed, which corrupted
    letter-suffix sections like § 6a.
    """
    mini_html = """<html><head><title>Testvergabe (TVg)</title></head><body>
<p>Vom 1. Januar 2020</p>
<p><b>&#167; 6a</b></p>
<p>Titel von sechs a</p>
<p>Inhalt des Paragraphen sechs a.</p>
</body></html>"""
    doc = VVHtmlParser().parse(mini_html)
    assert len(doc.paragraphs) == 1
    assert doc.paragraphs[0].section_number == "6a"
    assert doc.paragraphs[0].content.startswith("§ 6a")
