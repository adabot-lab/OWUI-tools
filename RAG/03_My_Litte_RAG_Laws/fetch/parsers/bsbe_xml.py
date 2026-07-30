"""Parser for gesetze.berlin.de (BSBE) XML law texts.

BSBE publishes laws as XML in the GII norm DTD structure but differs from
GII in that <textdaten> contains inline HTML (h4, p, dl, dt, dd) rather
than GII's <text><Content><P> elements. Metadata extraction (norm-level
metadaten) is identical to GiiXmlParser.

Source type: bsbe_xml
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from fetch.parsers.base import BaseParser, LawDocument, Paragraph, extract_section_number


class BsbeXmlParser(BaseParser):
    """Parse gesetze.berlin.de GII norm DTD XML into a LawDocument."""

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
            section_number = extract_section_number(enbez)
            if not section_number:
                continue

            # Extract textdaten inner HTML using BeautifulSoup
            textdaten = norm.find("textdaten")
            if textdaten is None:
                continue

            # Serialize textdaten inner HTML back to string for BeautifulSoup
            inner_html = "".join(
                ET.tostring(child, encoding="unicode") for child in textdaten
            )
            soup = BeautifulSoup(inner_html, "html.parser")

            # Remove <div class="jnhtml"> (footnotes / standangaben)
            for jnhtml in soup.find_all("div", class_="jnhtml"):
                jnhtml.decompose()

            # Extract title from <h4>
            title = ""
            h4 = soup.find("h4")
            if h4 is not None:
                # The h4 contains "§ N\nTitle" — extract just the title part
                h4_text = h4.get_text(separator=" ", strip=True)
                # Remove the "§ N" or "§ N/" prefix
                # e.g. "§ 1 Zweck des Gesetzes" -> "Zweck des Gesetzes"
                import re
                title = re.sub(r"^\s*§\s*\d+[a-zA-Z]?\s*/?\s*", "", h4_text).strip()

            # Extract content from <p> tags (each p is one Absatz)
            # Skip <p> tags inside <dl> (those are rendered as list items)
            p_parts: list[str] = []
            for p_tag in soup.find_all("p"):
                if p_tag.find_parent("dl") is not None:
                    continue
                p_text = p_tag.get_text(separator=" ", strip=True)
                if p_text:
                    p_parts.append(p_text)

            # Also include <dl> content for numbered lists
            dl = soup.find("dl")
            if dl is not None:
                dl_text = dl.get_text(separator=" ", strip=True)
                if dl_text:
                    p_parts.append(dl_text)

            if not p_parts:
                continue

            content_body = "\n".join(p_parts)

            # Build the "§ N Title" prefix
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
