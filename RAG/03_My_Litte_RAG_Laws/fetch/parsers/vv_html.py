"""Parser for VV (verwaltungsvorschriften-im-internet.de) HTML law texts.

Handles documents like VOB/A where the text is served as ISO-8859-1 encoded
HTML with section headers (&#167; N [suffix]) inside bold <p> tags.

The document is split into multiple Abschnitte (parts): Basisparagrafen, EU,
VS. Sections from different Abschnitte share the same number but are
distinguished by an "EU" or "VS" suffix (e.g. "1", "1 EU", "1 VS").
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from fetch.parsers.base import BaseParser, LawDocument, Paragraph

# Matches a section marker line: § + number + optional letter + optional suffix
# e.g. "§ 1", "§ 3a", "§ 1 EU", "§ 1 VS"
_SECTION_RE = re.compile(
    r"^§\s*(\d+[a-z]?(?:\s*(?:EU|VS))?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Matches an abbreviation in parentheses, e.g. "(VOB/A)" or "(VgV)"
_ABBR_RE = re.compile(r"\(([A-Z]{2,}/[A-Z]{1,})\)")

# Matches "Vom DD. Month YYYY" or "Vom DD. Month YYYY DD" date forms
_DATE_RE = re.compile(
    r"Vom\s+\d{1,2}\.\s+\w+\s+\d{4}",
    re.IGNORECASE,
)


class VVHtmlParser(BaseParser):
    """Parse HTML law texts from verwaltungsvorschriften-im-internet.de."""

    def parse(self, raw_data: bytes | str, source_url: str = "") -> LawDocument:
        # Decode bytes if needed; VV pages are ISO-8859-1
        if isinstance(raw_data, bytes):
            text_source = raw_data.decode("iso-8859-1", errors="replace")
        else:
            text_source = raw_data

        soup = BeautifulSoup(text_source, "lxml")

        # --- Metadata extraction -------------------------------------------------
        law_name = self._extract_law_name(soup)
        abbreviation = self._extract_abbreviation(law_name, soup)
        full_text = soup.get_text("\n", strip=True)
        stand_date = self._extract_stand_date(full_text)

        # --- Section extraction --------------------------------------------------
        paragraphs = self._extract_paragraphs(full_text)

        return LawDocument(
            law_name=law_name,
            abbreviation=abbreviation,
            stand_date=stand_date,
            source_url=source_url,
            paragraphs=paragraphs,
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _extract_law_name(soup: BeautifulSoup) -> str:
        """Extract law name from <h1> or <title>, stripping boilerplate."""
        for tag_name in ("h1", "title"):
            tag = soup.find(tag_name)
            if tag:
                name = tag.get_text(" ", strip=True)
                if name:
                    return name
        # Fallback: use the <strong> headline if present
        return ""

    @staticmethod
    def _extract_abbreviation(law_name: str, soup: BeautifulSoup) -> str:
        """Find abbreviation like 'VOB/A' from the title or body text."""
        # Look in the law name first
        if law_name:
            m = _ABBR_RE.search(law_name)
            if m:
                return m.group(1)

        # Search the whole document text as a fallback
        full_text = soup.get_text(" ", strip=True)
        m = _ABBR_RE.search(full_text)
        if m:
            return m.group(1)

        # Last resort: derive from first significant word
        if law_name:
            return law_name.split()[0] if law_name.split() else ""
        return ""

    @staticmethod
    def _extract_stand_date(full_text: str) -> str:
        """Find a 'Vom DD. Month YYYY' date before the first § marker."""
        first_section = _SECTION_RE.search(full_text)
        search_region = (
            full_text[: first_section.start()] if first_section else full_text
        )
        m = _DATE_RE.search(search_region)
        return m.group(0) if m else ""

    @staticmethod
    def _extract_paragraphs(full_text: str) -> list[Paragraph]:
        """Split the full text into Paragraph objects at §-markers."""
        lines = full_text.split("\n")

        # Find section boundary line indices (lines matching the § marker)
        section_starts: list[tuple[int, str, str]] = []  # (line_idx, number, title)
        for idx, line in enumerate(lines):
            stripped = line.strip()
            m = _SECTION_RE.match(stripped)
            if m:
                raw_number = m.group(1).strip()
                # Normalize suffix spacing: "1 EU" -> "1 EU" (single space)
                number = re.sub(r"\s+", " ", raw_number) if raw_number else raw_number
                # The title is the following non-empty line
                title = ""
                for ahead in range(idx + 1, len(lines)):
                    candidate = lines[ahead].strip()
                    if candidate:
                        title = candidate
                        break
                section_starts.append((idx, number, title))

        if not section_starts:
            return []

        paragraphs: list[Paragraph] = []
        for i, (idx, number, title) in enumerate(section_starts):
            # Content lines: from after the title line up to next section marker
            # First, find the index of the title line (first non-empty after idx)
            title_line_idx = idx + 1
            for ahead in range(idx + 1, len(lines)):
                if lines[ahead].strip():
                    title_line_idx = ahead
                    break

            if i + 1 < len(section_starts):
                end_idx = section_starts[i + 1][0]
            else:
                end_idx = len(lines)

            content_lines = lines[title_line_idx + 1 : end_idx]
            body = "\n".join(
                ln for ln in (l.strip() for l in content_lines) if ln
            ).strip()

            # Strip trailing navigation boilerplate (only on the last section)
            if i == len(section_starts) - 1:
                body = _strip_trailing_boilerplate(body)

            # Prepend the §-header so content is self-describing
            content = f"§ {number} {title}".strip()
            if body:
                content = f"{content}\n{body}"

            paragraphs.append(
                Paragraph(
                    section_number=number,
                    section_type="paragraph",
                    title=title,
                    content=content,
                )
            )

        return paragraphs


def _strip_trailing_boilerplate(body: str) -> str:
    """Remove trailing navigation/boilerplate lines after the last section."""
    # Cut at common boilerplate markers
    for marker in (
        "Zurueck zur Teilliste",
        "Zurück zur Teilliste",
        "navigation",
        "Navigation",
    ):
        pos = body.find(marker)
        if pos != -1:
            body = body[:pos].strip()
    return body.strip()
