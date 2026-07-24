"""Query layer over the SQLite legal database. Replaces the old in-memory engine."""
import re
from typing import Optional
from db import LegalDatabase


class LegalEngine:
    """Thin query interface over SQLite, preserving the old engine's API."""

    def __init__(self, db: LegalDatabase = None):
        self.db = db or LegalDatabase()

    def retrieve_paragraph(self, law_identifier: str, section_number: str) -> dict:
        """Look up a specific paragraph by law name/abbreviation + section number.

        Returns:
            Dict with paragraph content on success.
            Dict with 'error' key and context on failure:
              - law_not_found: lists available laws
              - section_not_found: lists section range for the law
        """
        law = self.db.find_law(law_identifier)

        if law is None:
            return {
                "error": "law_not_found",
                "message": f"No law matching '{law_identifier}' found.",
                "available_laws": [
                    l["abbreviation"] for l in self.db.list_laws()
                ],
            }

        # Strip section sign prefix and Artikel prefix, same as db.get_paragraph
        section_number = section_number.replace("\u00a7", "").strip()
        section_number = re.sub(r"^Artikel\s+", "", section_number, flags=re.IGNORECASE)

        result = self.db.get_paragraph(law_identifier, section_number)
        if result is not None:
            return result

        # Section not found - provide range context
        range_info = self.db.get_law_section_range(law["id"])
        if range_info:
            return {
                "error": "section_not_found",
                "message": (
                    f"Law '{law['abbreviation']}' exists but \u00a7 {section_number} "
                    f"is not in the database. "
                    f"Available sections: {range_info['min']} to {range_info['max']} "
                    f"({range_info['total']} total sections)."
                ),
                "law": law["abbreviation"],
                "requested_section": section_number,
                "available_range": {
                    "min": range_info["min"],
                    "max": range_info["max"],
                    "total": range_info["total"],
                },
            }
        else:
            return {
                "error": "section_not_found",
                "message": (
                    f"Law '{law['abbreviation']}' exists but has no sections "
                    f"in the database."
                ),
                "law": law["abbreviation"],
                "requested_section": section_number,
            }

    def search_paragraphs(self, query: str, limit: int = 20) -> list[dict]:
        """Full-text search across all paragraphs.

        Uses SQLite FTS5. Returns ranked results with snippets.
        """
        return self.db.search_paragraphs(query, limit)

    def list_laws(self) -> list[dict]:
        """List all available laws with section counts."""
        return self.db.list_laws()
