"""Parser for EUR-Lex ELI-annotated XHTML from the CELLAR API.

Handles the XHTML format published at publications.europa.eu for EU directives
(e.g. 2014/24/EU). The document is ELI-annotated: each article lives in a
``<div class="eli-subdivision" id="art_N">`` and carries a title via
``<p class="title-article-norm">`` / ``<p class="stitle-article-norm">``.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag

from .base import BaseParser, LawDocument, Paragraph

# CELEX codes look like 02014L0024 (sector 0, year 2014, type L, number 0024).
# We convert the leading "0{year}L{number}" into "{year}/{number}/EU".
_CELEX_RE = re.compile(r"0(\d{4})L(\d+)")
# Reference line format: "... — DE — DD.MM.YYYY" (any language code).
_DATE_RE = re.compile(r"—\s*[A-Z]{2}\s*—\s*(\d{2}\.\d{2}\.\d{4})")
# "Artikel 1" / "Artikel 94" / "Article 1".
_ARTICLE_NUM_RE = re.compile(r"\d+")


class EurlexHtmlParser(BaseParser):
    """Parse EUR-Lex ELI XHTML into a :class:`LawDocument`."""

    def parse(self, raw_data: bytes | str, source_url: str = "") -> LawDocument:
        markup = raw_data.decode("utf-8") if isinstance(raw_data, bytes) else raw_data
        soup = BeautifulSoup(markup, "lxml")

        law_name = self._extract_law_name(soup)
        abbreviation, stand_date = self._extract_reference(soup)

        paragraphs: list[Paragraph] = []
        seen_numbers: set[str] = set()
        for art_div in soup.select("div.eli-subdivision"):
            art_id = art_div.get("id", "")
            # Skip chapter/section containers (ids starting with "enc_").
            if not art_id.startswith("art_"):
                continue
            paragraph = self._parse_article(art_div)
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
    def _extract_law_name(soup: BeautifulSoup) -> str:
        node = soup.select_one("p.title-doc-first")
        if node is None:
            return ""
        return node.get_text(strip=True)

    @staticmethod
    def _extract_reference(soup: BeautifulSoup) -> tuple[str, str]:
        """Return ``(abbreviation, stand_date)`` parsed from the CELEX reference."""
        abbreviation = ""
        stand_date = ""

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

        return abbreviation, stand_date

    # ------------------------------------------------------------------
    # Article extraction
    # ------------------------------------------------------------------
    def _parse_article(self, art_div: Tag) -> Paragraph | None:
        section_number = self._extract_article_number(art_div)
        title = self._extract_article_title(art_div)
        content = self._extract_article_content(art_div)

        if not content.strip():
            return None

        return Paragraph(
            section_number=section_number,
            section_type="article",
            title=title,
            content=content.strip(),
        )

    @staticmethod
    def _extract_article_number(art_div: Tag) -> str:
        node = art_div.select_one("p.title-article-norm")
        if node is None:
            # Fall back to the id ("art_1" → "1").
            art_id = art_div.get("id", "")
            m = _ARTICLE_NUM_RE.search(art_id)
            return m.group(0) if m else ""
        text = node.get_text(strip=True)
        m = _ARTICLE_NUM_RE.search(text)
        return m.group(0) if m else ""

    @staticmethod
    def _extract_article_title(art_div: Tag) -> str:
        node = art_div.select_one("p.stitle-article-norm")
        if node is None:
            return ""
        return node.get_text(strip=True)

    @staticmethod
    def _extract_article_content(art_div: Tag) -> str:
        """Build cleaned article text: "Artikel N" + title + body.

        Modification markers (``<p class="modref">`` / ``<a class="modref">``)
        and footnote elements are stripped before text extraction.
        """
        art_div = art_div.__copy__()

        # Remove modification references and footnote markers wholesale.
        for selector in ("p.modref", "a.modref", ".footnote", ".note"):
            for el in art_div.select(selector):
                el.decompose()

        parts: list[str] = []
        title_node = art_div.select_one("p.title-article-norm")
        if title_node is not None:
            parts.append(title_node.get_text(strip=True))

        subtitle_node = art_div.select_one("p.stitle-article-norm")
        if subtitle_node is not None:
            parts.append(subtitle_node.get_text(strip=True))

        # Body: walk every <div class="norm"> (top-level and nested) plus any
        # loose <p class="norm">, in document order, skipping nodes already
        # consumed as title/subtitle.
        body_chunks: list[str] = []
        seen: set[int] = set()
        for node in title_node, subtitle_node:
            if node is not None:
                seen.add(id(node))
            if node is not None and node.parent is not None:
                seen.add(id(node.parent))

        # Walk descendants in document order and collect norm blocks.
        for descendant in art_div.descendants:
            if not isinstance(descendant, Tag):
                continue
            classes = descendant.get("class", []) or []
            is_norm_div = descendant.name == "div" and "norm" in classes
            is_norm_p = descendant.name == "p" and "norm" in classes
            if not (is_norm_div or is_norm_p):
                continue
            # Skip a norm div if one of its ancestors already contributed text
            # (we only want the most specific block, not the wrapper plus its
            # children duplicated).
            text = descendant.get_text(" ", strip=True)
            if not text:
                continue
            # Deduplicate identical consecutive chunks to avoid wrapper/child
            # overlap, while still preserving paragraph ordering.
            if not body_chunks or body_chunks[-1] != text:
                body_chunks.append(text)

        if body_chunks:
            parts.append("\n".join(body_chunks))

        return "\n".join(part for part in parts if part)
