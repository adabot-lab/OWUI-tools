"""Parser for gesetze-im-internet.de (GII) XML law texts.

GII publishes laws as XML with a <dokumente> root containing <norm> children.
The first <norm> holds law-level metadata; subsequent <norm> elements hold
individual paragraphs (§) with their text content.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from fetch.parsers.base import BaseParser, LawDocument, Paragraph


# Tags whose presence signals a non-paragraph norm that should be skipped.
_SKIP_ENBEZ = {"Inhaltsübersicht"}


def _strip_noindex(element: ET.Element) -> str:
    """Concatenate all text under *element*, unwrapping <noindex> wrappers.

    Uses itertext() so that nested structure (lists, emphasis, etc.) is
    flattened to plain text while still keeping the visible text.
    """
    return "".join(element.itertext()).strip()


def _extract_section_number(enbez: str) -> str:
    """Extract the bare section number from an <enbez> value.

    Examples:
        "§ 1"      -> "1"
        "§ 127a"   -> "127a"
        "§ 12 Abs. 3" -> "12"
    """
    # Match an optional §, then capture digits and any trailing letter suffix.
    match = re.search(r"§\s*(\d+[a-zA-Z]?)", enbez)
    if match:
        return match.group(1)
    # Fallback: first run of alphanumerics if no § present.
    match = re.match(r"\s*(\d+[a-zA-Z]?)", enbez)
    return match.group(1) if match else enbez.strip()


class GiiXmlParser(BaseParser):
    """Parse gesetze-im-internet.de XML into a LawDocument."""

    def parse(self, raw_data: bytes | str, source_url: str = "") -> LawDocument:
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode("utf-8")

        root = ET.fromstring(raw_data)

        law_name = ""
        abbreviation = ""
        stand_date = ""

        paragraphs: list[Paragraph] = []
        metadata_extracted = False

        for norm in root.iter("norm"):
            metadaten = norm.find("metadaten")

            # Extract law-level metadata from the first norm that has it.
            if metadaten is not None and not metadata_extracted:
                amtabk_el = metadaten.find("amtabk")
                jurabk_el = metadaten.find("jurabk")
                if amtbk_text := (amtabk_el.text if amtabk_el is not None else None):
                    abbreviation = amtbk_text.strip()
                elif jurabk_el is not None and jurabk_el.text:
                    abbreviation = jurabk_el.text.strip()

                langue_el = metadaten.find("langue")
                if langue_el is not None and langue_el.text:
                    law_name = langue_el.text.strip()

                date_el = metadaten.find("ausfertigung-datum")
                if date_el is not None and date_el.text:
                    stand_date = date_el.text.strip()

                metadata_extracted = True

            # Determine whether this norm is a paragraph we should extract.
            if metadaten is None:
                continue

            # Skip structure-only norms (part/section headings).
            if metadaten.find("gliederungseinheit") is not None:
                continue

            enbez_el = metadaten.find("enbez")
            if enbez_el is None or enbez_el.text is None:
                continue

            enbez = enbez_el.text.strip()
            if enbez in _SKIP_ENBEZ:
                continue

            section_number = _extract_section_number(enbez)
            if not section_number:
                continue

            # Extract title if present.
            title = ""
            titel_el = metadaten.find("titel")
            if titel_el is not None:
                title = "".join(titel_el.itertext()).strip()

            # Extract content from textdaten/text/Content/P...
            textdaten = norm.find("textdaten")
            if textdaten is None:
                continue

            text_el = textdaten.find("text")
            if text_el is None:
                continue

            content_el = text_el.find("Content")
            if content_el is None:
                # Skip norms that only carry footnotes.
                continue

            p_parts: list[str] = []
            for p in content_el.findall("P"):
                p_text = _strip_noindex(p)
                if p_text:
                    p_parts.append(p_text)

            if not p_parts:
                continue

            content_body = "\n".join(p_parts)

            # Build the "§ N Title" prefix like the old extractor output.
            header = f"§ {section_number}"
            if title:
                header = f"{header} {title}"

            full_content = f"{header}\n{content_body}" if content_body else header

            paragraphs.append(
                Paragraph(
                    section_number=section_number,
                    section_type="paragraph",
                    title=title,
                    content=full_content,
                )
            )

        return LawDocument(
            law_name=law_name,
            abbreviation=abbreviation,
            stand_date=stand_date,
            source_url=source_url,
            paragraphs=paragraphs,
        )
