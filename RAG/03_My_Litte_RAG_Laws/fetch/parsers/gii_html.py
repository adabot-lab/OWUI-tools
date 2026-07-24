"""Parser for gesetze-im-internet.de HTML law texts.

GII publishes some laws (e.g. the Grundgesetz GG) as HTML pages rather than
XML zip archives.  These pages use consistent CSS classes:

  .jnnorm[title="Rahmen"]      — law-level metadata container
  .jnnorm[title="Einzelnorm"]   — individual article/paragraph
  .jnnorm[title="Gliederung"]   — structure-only heading (skip)
  .jnlangue                     — law long name
  .jnenbez                      — section identifier (Art 1, § 1)
  .jurAbsatz                    — paragraph text within a norm
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from fetch.parsers.base import BaseParser, LawDocument, Paragraph

# Match the date in "Ausfertigungsdatum: 23.05.1949"
_DATE_RE = re.compile(r"Ausfertigungsdatum:\s*(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})")


class GiiHtmlParser(BaseParser):
    """Parse gesetze-im-internet.de HTML pages into a LawDocument."""

    def parse(self, raw_data: bytes | str, source_url: str = "") -> LawDocument:
        # GII HTML declares charset=iso-8859-1 — decode with latin-1, NOT UTF-8
        if isinstance(raw_data, bytes):
            text_source = raw_data.decode("latin-1")
        else:
            text_source = raw_data

        soup = BeautifulSoup(text_source, "lxml")

        law_name = ""
        abbreviation = ""
        stand_date = ""

        # --- Law-level metadata from the Rahmen norm ------------------------
        rahmen = soup.select_one('.jnnorm[title="Rahmen"]')
        if rahmen:
            law_name = self._extract_law_name(rahmen)
            abbreviation = self._extract_abbreviation(rahmen)
            stand_date = self._extract_stand_date(rahmen)

        # --- Individual norms (articles / paragraphs) ----------------------
        paragraphs: list[Paragraph] = []
        for norm in soup.select('.jnnorm[title="Einzelnorm"]'):
            para = self._parse_norm(norm)
            if para:
                paragraphs.append(para)

        return LawDocument(
            law_name=law_name,
            abbreviation=abbreviation,
            stand_date=stand_date,
            source_url=source_url,
            paragraphs=paragraphs,
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _extract_law_name(rahmen: Tag) -> str:
        """Extract law name from the jnlangue span in the Rahmen norm."""
        tag = rahmen.select_one(".jnlangue")
        if tag:
            return tag.get_text(" ", strip=True)
        return ""

    @staticmethod
    def _extract_abbreviation(rahmen: Tag) -> str:
        """Extract abbreviation (e.g. 'GG') from the Rahmen norm.

        In GII HTML the abbreviation appears in a <p> tag, typically as plain
        text like 'GG' or 'BGB'.
        """
        # The abbreviation is usually in a <p> right after the h1/langue.
        # Look for a short uppercase <p> text.
        for p in rahmen.find_all("p"):
            text = p.get_text(strip=True)
            # Abbreviation is typically 2-10 uppercase chars (possibly with /)
            if text and re.fullmatch(r"[A-Z][A-Z/a-z]{0,15}", text):
                return text
        return ""

    @staticmethod
    def _extract_stand_date(rahmen: Tag) -> str:
        """Extract the Ausfertigungsdatum from the Rahmen norm."""
        full_text = rahmen.get_text(" ", strip=True)
        m = _DATE_RE.search(full_text)
        return m.group(1) if m else ""

    @staticmethod
    def _parse_norm(norm: Tag) -> Paragraph | None:
        """Parse a single Einzelnorm div into a Paragraph.

        Returns None for norms that have no extractable section number.
        """
        enbez_tag = norm.select_one(".jnenbez")
        if enbez_tag is None:
            return None

        enbez = enbez_tag.get_text(strip=True)
        if not enbez:
            return None

        # Determine section type and number
        if enbez.startswith("Art"):
            section_type = "article"
            section_number = enbez[3:].strip()  # strip "Art" prefix
        elif enbez.startswith("§"):
            section_type = "paragraph"
            section_number = enbez.lstrip("§").strip()
        else:
            # Unknown format — use raw enbez as number, default to paragraph
            section_type = "paragraph"
            section_number = enbez

        if not section_number:
            return None

        # Extract title (jnentitel), often empty
        title = ""
        titel_tag = norm.select_one(".jnentitel")
        if titel_tag:
            title = titel_tag.get_text(" ", strip=True)

        # Extract content from jurAbsatz divs
        absatz_parts: list[str] = []
        for absatz in norm.select(".jurAbsatz"):
            text = absatz.get_text(" ", strip=True)
            if text:
                absatz_parts.append(text)

        # Build content: "Art N Title" prefix + concatenated absaetze
        marker = f"Art {section_number}" if section_type == "article" else f"§ {section_number}"
        header = marker
        if title:
            header = f"{header} {title}"

        body = "\n".join(absatz_parts)
        full_content = f"{header}\n{body}" if body else header

        return Paragraph(
            section_number=section_number,
            section_type=section_type,
            title=title,
            content=full_content,
        )
