"""Tests for the BSBE (gesetze.berlin.de) XML parser."""
from __future__ import annotations

from pathlib import Path

import pytest

from fetch.parsers.base import LawDocument, Paragraph
from fetch.parsers.bsbe_xml import BsbeXmlParser

FIXTURE = Path(__file__).parent / "testdata" / "berlavg_sample.xml"


@pytest.fixture
def sample_xml() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def parsed(sample_xml: str) -> LawDocument:
    return BsbeXmlParser().parse(sample_xml, source_url="https://gesetze.berlin.de/perma?j=VergabeG_BE")


class TestMetadataExtraction:
    def test_law_name(self, parsed: LawDocument) -> None:
        assert parsed.law_name == "Berliner Ausschreibungs- und Vergabegesetz (BerlAVG) vom 22. April 2020"

    def test_abbreviation(self, parsed: LawDocument) -> None:
        assert parsed.abbreviation == "BerlAVG"

    def test_stand_date(self, parsed: LawDocument) -> None:
        assert parsed.stand_date == "22.04.2020"

    def test_source_url_preserved(self, parsed: LawDocument) -> None:
        assert parsed.source_url == "https://gesetze.berlin.de/perma?j=VergabeG_BE"


class TestParagraphExtraction:
    def test_exactly_three_paragraphs(self, parsed: LawDocument) -> None:
        assert len(parsed.paragraphs) == 3

    def test_first_paragraph_number(self, parsed: LawDocument) -> None:
        assert parsed.paragraphs[0].section_number == "1"

    def test_first_paragraph_title(self, parsed: LawDocument) -> None:
        assert parsed.paragraphs[0].title == "Zweck des Gesetzes"

    def test_first_paragraph_section_type(self, parsed: LawDocument) -> None:
        assert parsed.paragraphs[0].section_type == "paragraph"

    def test_first_paragraph_content_starts_with_section(self, parsed: LawDocument) -> None:
        assert parsed.paragraphs[0].content.startswith("§ 1")

    def test_second_paragraph_title(self, parsed: LawDocument) -> None:
        assert parsed.paragraphs[1].title == "Persönlicher Anwendungsbereich"

    def test_third_paragraph_title(self, parsed: LawDocument) -> None:
        assert parsed.paragraphs[2].title == "Sachlicher Anwendungsbereich"

    def test_all_paragraphs_non_empty(self, parsed: LawDocument) -> None:
        for p in parsed.paragraphs:
            assert p.content.strip(), f"Empty content for § {p.section_number}"
            assert p.section_number, "Missing section_number"

    def test_no_duplicate_section_numbers(self, parsed: LawDocument) -> None:
        numbers = [p.section_number for p in parsed.paragraphs]
        assert len(numbers) == len(set(numbers)), f"Duplicate section numbers: {numbers}"

    def test_section_numbers_are_1_2_3(self, parsed: LawDocument) -> None:
        assert [p.section_number for p in parsed.paragraphs] == ["1", "2", "3"]


class TestContentFormat:
    def test_content_has_header_with_title(self, parsed: LawDocument) -> None:
        """First line should be '§ N Title' prefix."""
        first_line = parsed.paragraphs[0].content.splitlines()[0]
        assert first_line == "§ 1 Zweck des Gesetzes"

    def test_content_includes_keywords(self, parsed: LawDocument) -> None:
        """§ 1 body mentions mittelständische Unternehmen."""
        assert "mittelständische" in parsed.paragraphs[0].content

    def test_dl_list_content(self, parsed: LawDocument) -> None:
        """§ 3 has a dl list with numbered items about exemptions."""
        assert "vergaberechtsfreie" in parsed.paragraphs[2].content

    def test_multi_absatz_content(self, parsed: LawDocument) -> None:
        """§ 1 has two paragraphs (Absätze)."""
        assert "(1)" in parsed.paragraphs[0].content
        assert "(2)" in parsed.paragraphs[0].content


class TestEdgeCases:
    def test_parses_bytes_input(self) -> None:
        raw = FIXTURE.read_bytes()
        doc = BsbeXmlParser().parse(raw)
        assert doc.abbreviation == "BerlAVG"
        assert len(doc.paragraphs) == 3


class TestParagraphDictConversion:
    def test_to_paragraph_dicts_roundtrip(self, parsed: LawDocument) -> None:
        dicts = parsed.to_paragraph_dicts()
        assert len(dicts) == 3
        assert dicts[0]["section_number"] == "1"
        assert dicts[0]["section_type"] == "paragraph"
        assert dicts[0]["title"] == "Zweck des Gesetzes"
        assert "§ 1" in dicts[0]["content"]
