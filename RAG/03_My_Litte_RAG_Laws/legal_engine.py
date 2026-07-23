"""Query layer over the SQLite legal database. Replaces the old in-memory engine."""
from typing import Optional
from db import LegalDatabase


class LegalEngine:
    """Thin query interface over SQLite, preserving the old engine's API."""

    def __init__(self, db: LegalDatabase = None):
        self.db = db or LegalDatabase()

    def retrieve_paragraph(self, law_identifier: str, section_number: str) -> Optional[dict]:
        """Look up a specific paragraph by law name/abbreviation + section number.

        Args:
            law_identifier: Law abbreviation (e.g. "VgV") or full name.
            section_number: Section number, with or without § prefix.

        Returns:
            Dict with law_name, law_abbreviation, section_number, content, title.
            None if not found.
        """
        return self.db.get_paragraph(law_identifier, section_number)

    def search_paragraphs(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across all paragraphs.

        Uses SQLite FTS5. Returns ranked results with snippets.
        """
        return self.db.search_paragraphs(query, limit)

    def list_laws(self) -> list[dict]:
        """List all available laws with section counts."""
        return self.db.list_laws()
