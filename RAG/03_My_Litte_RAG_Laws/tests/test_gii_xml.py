"""Tests for the GII (gesetze-im-internet.de) XML parser."""
from __future__ import annotations

from pathlib import Path

import pytest

from fetch.parsers.base import LawDocument, Paragraph
from fetch.parsers.gii_xml import GiiXmlParser

FIXTURE = Path(__file__).parent / "testdata" / "vgv_sample.xml"


@pytest.fixture
def sample_xml() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def parsed(sample_xml: str) -> LawDocument:
    return GiiXmlParser().parse(sample_xml, source_url="https://example.com/vgv.xml")


class TestMetadataExtraction:
    def test_law_name(self, parsed: LawDocument) -> None:
        assert parsed.law_name == "Verordnung über die Vergabe öffentlicher Aufträge"

    def test_abbreviation_uses_amtabk(self, parsed: LawDocument) -> None:
        # amtabk "VgV" preferred over jurabk "VgV 2016"
        assert parsed.abbreviation == "VgV"

    def test_stand_date(self, parsed: LawDocument) -> None:
        assert parsed.stand_date == "2016-04-12"

    def test_source_url_preserved(self, parsed: LawDocument) -> None:
        assert parsed.source_url == "https://example.com/vgv.xml"


class TestParagraphExtraction:
    def test_exactly_three_paragraphs(self, parsed: LawDocument) -> None:
        assert len(parsed.paragraphs) == 3

    def test_first_paragraph_number(self, parsed: LawDocument) -> None:
        assert parsed.paragraphs[0].section_number == "1"

    def test_first_paragraph_title(self, parsed: LawDocument) -> None:
        assert parsed.paragraphs[0].title == "Gegenstand und Anwendungsbereich"

    def test_first_paragraph_section_type(self, parsed: LawDocument) -> None:
        assert parsed.paragraphs[0].section_type == "paragraph"

    def test_first_paragraph_content_starts_with_section(self, parsed: LawDocument) -> None:
        assert parsed.paragraphs[0].content.startswith("§ 1")

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
        """First line should be '§ N Title' prefix like old extractor output."""
        first_line = parsed.paragraphs[0].content.splitlines()[0]
        assert first_line == "§ 1 Gegenstand und Anwendungsbereich"

    def test_content_includes_body_text(self, parsed: LawDocument) -> None:
        # The noindex wrapper text should be kept (stripped of the tag).
        # § 1 body mentions "Teil 4 des Gesetzes gegen Wettbewerbsbeschränkungen"
        assert "Wettbewerbsbeschränkungen" in parsed.paragraphs[0].content

    def test_noindex_text_preserved(self, parsed: LawDocument) -> None:
        """First norm in the fixture has a noindex wrapper; its text must survive."""
        # The fixture's first paragraph norm (§ 1) body contains normal text.
        # Confirm nested DL/DT/DD list text is flattened correctly.
        assert "Sektorenauftraggeber" in parsed.paragraphs[0].content


class TestEdgeCases:
    def test_parses_bytes_input(self) -> None:
        raw = FIXTURE.read_bytes()
        doc = GiiXmlParser().parse(raw)
        assert doc.abbreviation == "VgV"
        assert len(doc.paragraphs) == 3

    def test_fussnoten_only_norm_skipped(self) -> None:
        """A norm with <fussnoten> but no <Content> must be skipped."""
        xml = """<?xml version="1.0"?>
        <dokumente>
          <norm>
            <metadaten>
              <jurabk>TestG</jurabk>
              <langue>Testgesetz</langue>
              <ausfertigung-datum>2020-01-01</ausfertigung-datum>
            </metadaten>
          </norm>
          <norm>
            <metadaten>
              <jurabk>TestG</jurabk>
              <enbez>§ 1</enbez>
              <titel>Erster Paragraph</titel>
            </metadaten>
            <textdaten>
              <text format="XML">
                <Content><P>Real content here.</P></Content>
              </text>
            </textdaten>
          </norm>
          <norm>
            <metadaten>
              <jurabk>TestG</jurabk>
              <enbez>§ 2</enbez>
            </metadaten>
            <textdaten>
              <text format="XML">
                <fussnoten />
              </text>
            </textdaten>
          </norm>
        </dokumente>"""
        doc = GiiXmlParser().parse(xml)
        assert len(doc.paragraphs) == 1
        assert doc.paragraphs[0].section_number == "1"

    def test_skip_inhaltsuebersicht(self) -> None:
        """TOC norms with enbez 'Inhaltsübersicht' must be skipped."""
        xml = """<?xml version="1.0"?>
        <dokumente>
          <norm>
            <metadaten>
              <jurabk>TG</jurabk>
              <langue>Test G</langue>
            </metadaten>
          </norm>
          <norm>
            <metadaten>
              <jurabk>TG</jurabk>
              <enbez>Inhaltsübersicht</enbez>
            </metadaten>
            <textdaten><text format="XML"><Content><P>Some TOC text</P></Content></text></textdaten>
          </norm>
          <norm>
            <metadaten>
              <jurabk>TG</jurabk>
              <enbez>§ 1</enbez>
            </metadaten>
            <textdaten><text format="XML"><Content><P>Body</P></Content></text></textdaten>
          </norm>
        </dokumente>"""
        doc = GiiXmlParser().parse(xml)
        assert len(doc.paragraphs) == 1
        assert doc.paragraphs[0].section_number == "1"

    def test_skip_gliederungseinheit(self) -> None:
        """Structure-only norms (with <gliederungseinheit>) must be skipped."""
        xml = """<?xml version="1.0"?>
        <dokumente>
          <norm>
            <metadaten>
              <jurabk>TG</jurabk>
              <langue>Test G</langue>
            </metadaten>
          </norm>
          <norm>
            <metadaten>
              <jurabk>TG</jurabk>
              <gliederungseinheit>
                <gliederungsbez>Abschnitt 1</gliederungsbez>
              </gliederungseinheit>
            </metadaten>
            <textdaten><text format="XML"><Content><P>Heading text</P></Content></text></textdaten>
          </norm>
          <norm>
            <metadaten>
              <jurabk>TG</jurabk>
              <enbez>§ 1</enbez>
            </metadaten>
            <textdaten><text format="XML"><Content><P>Body</P></Content></text></textdaten>
          </norm>
        </dokumente>"""
        doc = GiiXmlParser().parse(xml)
        assert len(doc.paragraphs) == 1


class TestParagraphDictConversion:
    def test_to_paragraph_dicts_roundtrip(self, parsed: LawDocument) -> None:
        dicts = parsed.to_paragraph_dicts()
        assert len(dicts) == 3
        assert dicts[0]["section_number"] == "1"
        assert dicts[0]["section_type"] == "paragraph"
        assert dicts[0]["title"] == "Gegenstand und Anwendungsbereich"
        assert "§ 1" in dicts[0]["content"]
