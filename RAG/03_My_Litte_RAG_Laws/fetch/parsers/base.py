"""Base parser interface and shared data classes for law source parsers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Paragraph:
    """A single legal paragraph or article."""
    section_number: str
    section_type: str  # "paragraph" for §, "article" for Artikel
    title: str = ""
    content: str = ""


@dataclass
class LawDocument:
    """Parsed law document ready for database insertion."""
    law_name: str
    abbreviation: str
    stand_date: str = ""
    source_url: str = ""
    paragraphs: list[Paragraph] = field(default_factory=list)

    def to_paragraph_dicts(self) -> list[dict]:
        """Convert paragraphs to the dict format expected by db.replace_law_paragraphs()."""
        return [
            {
                "section_number": p.section_number,
                "section_type": p.section_type,
                "title": p.title,
                "content": p.content,
            }
            for p in self.paragraphs
        ]


class BaseParser(ABC):
    """Abstract base for source-specific law parsers."""

    @abstractmethod
    def parse(self, raw_data: bytes | str, source_url: str = "") -> LawDocument:
        """Parse raw downloaded data into a LawDocument.

        Args:
            raw_data: Raw bytes or string from the HTTP response.
            source_url: The original source URL (for metadata).

        Returns:
            LawDocument with extracted paragraphs.
        """
        ...
