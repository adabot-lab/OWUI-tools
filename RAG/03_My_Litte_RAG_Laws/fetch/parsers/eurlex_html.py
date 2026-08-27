"""Parser for EUR-Lex XHTML from the CELLAR API.

Handles the XHTML format published at publications.europa.eu for EU directives
and regulations in two flavours:

- ELI format (consolidated docs): each article lives in a
  ``<div class="eli-subdivision" id="art_N">`` and carries a title via
  ``<p class="title-article-norm">`` / ``<p class="stitle-article-norm">``.
- OJ format (original OJ publications): law title from ``p.oj-doc-ti`` inside
  ``div.eli-main-title``, article heading/subtitle from ``p.oj-ti-art`` /
  ``p.oj-sti-art``, body from ``p.oj-normal`` (incl. table cells). No
  ``p.reference`` element is present.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from .base import BaseParser, LawDocument, Paragraph

# CELEX codes look like 02014L0024 (sector 0, year 2014, type L, number 0024).
# We convert the leading "0{year}L{number}" into "{year}/{number}/EU".
_CELEX_RE = re.compile(r"0(\d{4})L(\d+)")
# Reference line format: "... — DE — DD.MM.YYYY" (any language code).
_DATE_RE = re.compile(r"—\s*[A-Z]{2}\s*—\s*(\d{2}\.\d{2}\.\d{4})")
# Fallback: extract directive number from the law name, e.g. "2014/24/EU".
_DIRECTIVE_RE = re.compile(r"(\d{4})/(\d+)/EU")
# Extract the trailing integer from "Artikel 1" / "Article 94".
_ARTICLE_NUM_RE = re.compile(r"\d+")
# German month names used in OJ "vom <day>. <Month> <year>" date lines.
_GERMAN_MONTHS = {
    "Januar": "01", "Februar": "02", "März": "03", "April": "04",
    "Mai": "05", "Juni": "06", "Juli": "07", "August": "08",
    "September": "09", "Oktober": "10", "November": "11", "Dezember": "12",
}
# Date line in OJ docs, e.g. "vom 14. März 2012".
_GERMAN_DATE_RE = re.compile(r"\bvom\s+(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s+(\d{4})")

# CSS selectors for elements that should be stripped before text extraction.
_STRIP_SELECTORS = ("p.modref", "a.modref", ".footnote", ".note")


def _parse_german_date(text: str) -> str:
    """Normalize a German date string like "vom 14. März 2012" to ISO.

    Returns ``""`` when no parseable date is found.
    """
    m = _GERMAN_DATE_RE.search(text)
    if m is None:
        return ""
    month = _GERMAN_MONTHS.get(m.group(2))
    if month is None:
        return ""
    return f"{m.group(3)}-{month}-{int(m.group(1)):02d}"


class EurlexHtmlParser(BaseParser):
    """Parse EUR-Lex ELI/OJ XHTML into a :class:`LawDocument`."""

    def parse(self, raw_data: bytes | str, source_url: str = "") -> LawDocument:
        markup = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else raw_data
        soup = BeautifulSoup(markup, "lxml")

        # OJ format iff a law title appears inside div.eli-main-title.
        oj_format = soup.select_one("div.eli-main-title p.oj-doc-ti") is not None

        law_name = self._extract_law_name(soup, oj_format)
        abbreviation, stand_date = self._extract_reference(soup, oj_format)

        paragraphs: list[Paragraph] = []
        seen_numbers: set[str] = set()
        for art_div in soup.select("div.eli-subdivision"):
            art_id = str(art_div.get("id") or "")
            # Skip chapter/section containers (ids starting with "enc_").
            if not art_id.startswith("art_"):
                continue
            paragraph = self._parse_article(art_div, oj_format)
            if paragraph is None:
                continue
            if paragraph.section_number in seen_numbers:
                continue
            seen_numbers.add(paragraph.section_number)
            paragraphs.append(paragraph)

        return LawDocument(
            law_name=law_name,
            abbreviation=abbreviation,
            stand_date=stand_date,
            source_url=source_url,
            paragraphs=paragraphs,
        )

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_law_name(soup: BeautifulSoup, oj_format: bool = False) -> str:
        if oj_format:
            nodes = soup.select("div.eli-main-title p.oj-doc-ti")
            parts = [n.get_text(" ", strip=True) for n in nodes]
            return " ".join(p for p in parts if p)
        node = soup.select_one("p.title-doc-first")
        return node.get_text(strip=True) if node is not None else ""

    @staticmethod
    def _extract_reference(
        soup: BeautifulSoup, oj_format: bool = False
    ) -> tuple[str, str]:
        """Return ``(abbreviation, stand_date)`` from the reference metadata.

        For OJ docs there is no ``<p class="reference">`` element; the
        stand_date is taken from the "vom <date>" line of the law title and
        the abbreviation stays empty. Otherwise the abbreviation is derived
        from the CELEX reference line (or the law name when the reference
        element is absent, e.g. in trimmed fixtures).
        """
        abbreviation = ""
        stand_date = ""

        if oj_format:
            for node in soup.select("div.eli-main-title p.oj-doc-ti"):
                text = node.get_text(" ", strip=True)
                if text.startswith("vom "):
                    stand_date = _parse_german_date(text)
                    break
            return abbreviation, stand_date

        ref_node = soup.select_one("p.reference")
        if ref_node is not None:
            ref_text = ref_node.get_text(" ", strip=True)

            celex_match = _CELEX_RE.search(ref_text)
            if celex_match:
                year, number = celex_match.group(1), celex_match.group(2)
                abbreviation = f"{year}/{int(number)}/EU"

            date_match = _DATE_RE.search(ref_text)
            if date_match:
                stand_date = date_match.group(1).replace(".", "-")

        if not abbreviation:
            name_node = soup.select_one("p.title-doc-first")
            if name_node is not None:
                name_text = name_node.get_text(" ", strip=True)
                name_match = _DIRECTIVE_RE.search(name_text)
                if name_match:
                    abbreviation = f"{name_match.group(1)}/{name_match.group(2)}/EU"

        return abbreviation, stand_date

    # ------------------------------------------------------------------
    # Article extraction
    # ------------------------------------------------------------------
    def _parse_article(
        self, art_div: Tag, oj_format: bool = False
    ) -> Paragraph | None:
        section_number = self._extract_article_number(art_div, oj_format)
        title = self._extract_article_title(art_div, oj_format)

        # Strip modification references and footnotes in-place; each art_div
        # is processed exactly once so mutation is safe. For OJ docs these
        # selectors match nothing (no modref/footnotes exist there).
        for selector in _STRIP_SELECTORS:
            for el in art_div.select(selector):
                el.decompose()

        content = self._extract_article_content(art_div, oj_format)
        if not content.strip():
            return None

        return Paragraph(
            section_number=section_number,
            section_type="article",
            title=title,
            content=content.strip(),
        )

    @staticmethod
    def _extract_article_number(art_div: Tag, oj_format: bool = False) -> str:
        node = art_div.select_one("p.title-article-norm")
        if oj_format:
            node = art_div.select_one("p.oj-ti-art")
        if node is not None:
            m = _ARTICLE_NUM_RE.search(node.get_text(strip=True))
            if m:
                return m.group(0)
        # Fallback: extract from the id ("art_1" → "1").
        m = _ARTICLE_NUM_RE.search(str(art_div.get("id") or ""))
        return m.group(0) if m else ""

    @staticmethod
    def _extract_article_title(art_div: Tag, oj_format: bool = False) -> str:
        node = art_div.select_one("p.stitle-article-norm")
        if oj_format:
            node = art_div.select_one("p.oj-sti-art")
        return node.get_text(strip=True) if node is not None else ""

    @staticmethod
    def _extract_article_content(
        art_div: Tag, oj_format: bool = False
    ) -> str:
        """Build cleaned article text: heading + subtitle + body paragraphs.

        For OJ docs the body is the concatenation of all ``p.oj-normal``
        elements (also inside table cells) in document order. For ELI docs
        the body text is collected from the *outermost*
        ``<div|p class="norm">`` elements — i.e. those not nested inside
        another ``norm`` element — so that wrapper/child overlap does not
        duplicate text.
        """
        parts: list[str] = []

        if oj_format:
            heading = art_div.select_one("p.oj-ti-art")
            if heading is not None:
                parts.append(heading.get_text(strip=True))

            subtitle = art_div.select_one("p.oj-sti-art")
            if subtitle is not None:
                parts.append(subtitle.get_text(strip=True))

            body_texts = [
                p.get_text(" ", strip=True)
                for p in art_div.select("p.oj-normal")
                if p.get_text(" ", strip=True)
            ]
            if body_texts:
                parts.append("\n".join(body_texts))

            return "\n".join(parts)

        heading = art_div.select_one("p.title-article-norm")
        if heading is not None:
            parts.append(heading.get_text(strip=True))

        subtitle = art_div.select_one("p.stitle-article-norm")
        if subtitle is not None:
            parts.append(subtitle.get_text(strip=True))

        body_texts: list[str] = []
        for descendant in art_div.descendants:
            if not isinstance(descendant, Tag):
                continue
            if descendant.name not in ("div", "p"):
                continue
            classes = descendant.get("class") or []
            if "norm" not in classes:
                continue
            # Skip norm blocks nested inside another norm block.
            if _has_norm_ancestor(descendant, art_div):
                continue
            text = descendant.get_text(" ", strip=True)
            if text:
                body_texts.append(text)

        if body_texts:
            parts.append("\n".join(body_texts))

        return "\n".join(parts)


def _has_norm_ancestor(tag: Tag, container: Tag) -> bool:
    """Return True if *tag* has a ``norm``-classed div/p ancestor below *container*."""
    parent = tag.parent
    while parent is not None and parent is not container:
        if isinstance(parent, Tag) and parent.name in ("div", "p"):
            classes = parent.get("class") or []
            if "norm" in classes:
                return True
        parent = parent.parent
    return False
