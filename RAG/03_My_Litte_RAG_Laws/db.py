"""SQLite database layer for legal texts with FTS5 full-text search."""
import sqlite3
import os
import re
from typing import Optional
from contextlib import contextmanager

DB_PATH = os.getenv("LEGAL_DB_PATH", "data/legal.db")


class LegalDatabase:
    """Manages the SQLite database for legal paragraphs and laws."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS laws (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT NOT NULL,
                    abbreviation TEXT NOT NULL UNIQUE,
                    stand_date  TEXT,
                    source_file TEXT,
                    created_at  TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS paragraphs (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    law_id         INTEGER NOT NULL REFERENCES laws(id),
                    section_number TEXT NOT NULL,
                    section_type   TEXT NOT NULL DEFAULT 'paragraph',
                    title          TEXT DEFAULT '',
                    content        TEXT NOT NULL,
                    UNIQUE(law_id, section_number)
                );

                CREATE INDEX IF NOT EXISTS idx_paragraphs_law
                    ON paragraphs(law_id);

                CREATE VIRTUAL TABLE IF NOT EXISTS paragraphs_fts
                    USING fts5(content, content='paragraphs', content_rowid='id',
                               tokenize = 'unicode61 remove_diacritics 2');
            """)

            # FTS5 triggers to keep index in sync
            conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS paragraphs_ai AFTER INSERT ON paragraphs
                BEGIN
                    INSERT INTO paragraphs_fts(rowid, content)
                    VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS paragraphs_ad AFTER DELETE ON paragraphs
                BEGIN
                    INSERT INTO paragraphs_fts(paragraphs_fts, rowid, content)
                    VALUES('delete', old.id, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS paragraphs_au AFTER UPDATE ON paragraphs
                BEGIN
                    INSERT INTO paragraphs_fts(paragraphs_fts, rowid, content)
                    VALUES('delete', old.id, old.content);
                    INSERT INTO paragraphs_fts(rowid, content)
                    VALUES (new.id, new.content);
                END;
            """)

    def list_tables(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            return [r["name"] for r in rows]

    def insert_law(self, name: str, abbreviation: str,
                   stand_date: str = "", source_file: str = "") -> int:
        # NOTE: SQLite cursor.lastrowid returns a stale/foreign value when
        # ON CONFLICT triggers an UPDATE instead of INSERT. Use RETURNING to
        # reliably get the actual row id in both cases.
        with self._conn() as conn:
            row = conn.execute(
                """INSERT INTO laws (name, abbreviation, stand_date, source_file)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(abbreviation) DO UPDATE SET
                       name=excluded.name,
                       stand_date=excluded.stand_date,
                       source_file=excluded.source_file
                   RETURNING id""",
                (name, abbreviation, stand_date, source_file)
            ).fetchone()
            return row["id"]

    def get_law_by_id(self, law_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM laws WHERE id = ?", (law_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_law_by_abbreviation(self, abbreviation: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM laws WHERE abbreviation = ? COLLATE NOCASE",
                (abbreviation,)
            ).fetchone()
            return dict(row) if row else None

    def find_law(self, identifier: str) -> Optional[dict]:
        """Look up a law by abbreviation OR full name (case-insensitive)."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM laws
                   WHERE abbreviation = ? COLLATE NOCASE
                      OR name = ? COLLATE NOCASE""",
                (identifier, identifier)
            ).fetchone()
            return dict(row) if row else None

    def get_law_section_range(self, law_id: int) -> Optional[dict]:
        """Return min/max numeric section numbers for a law.

        Returns None if the law has no paragraphs.
        Only considers section_numbers starting with a digit (1, 127a, 305).
        Non-numeric (e.g. annex letters) are ignored.
        """
        with self._conn() as conn:
            row = conn.execute(
                """SELECT
                       MIN(CAST(
                           CASE WHEN section_number GLOB '[0-9]*'
                                THEN section_number ELSE NULL END AS INTEGER
                       )) as min_section,
                       MAX(CAST(
                           CASE WHEN section_number GLOB '[0-9]*'
                                THEN section_number ELSE NULL END AS INTEGER
                       )) as max_section,
                       COUNT(*) as total_sections
                   FROM paragraphs
                   WHERE law_id = ?""",
                (law_id,)
            ).fetchone()
        if not row or row["total_sections"] == 0:
            return None
        return {
            "min": row["min_section"],
            "max": row["max_section"],
            "total": row["total_sections"],
        }

    def insert_paragraph(self, law_id: int, section_number: str,
                         section_type: str, title: str, content: str):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO paragraphs
                       (law_id, section_number, section_type, title, content)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(law_id, section_number) DO UPDATE SET
                       section_type=excluded.section_type,
                       title=excluded.title,
                       content=excluded.content""",
                (law_id, section_number, section_type, title, content)
            )

    def replace_law_paragraphs(self, law_id: int, paragraphs: list[dict]):
        """Delete all paragraphs for a law, then insert new ones."""
        with self._conn() as conn:
            conn.execute("DELETE FROM paragraphs WHERE law_id = ?", (law_id,))
            conn.executemany(
                """INSERT INTO paragraphs
                       (law_id, section_number, section_type, title, content)
                   VALUES (?, ?, ?, ?, ?)""",
                [(law_id, p["section_number"], p.get("section_type", "paragraph"),
                  p.get("title", ""), p["content"])
                 for p in paragraphs]
            )

    def get_paragraph(self, law_identifier: str, section_number: str) -> Optional[dict]:
        """Look up a paragraph by law abbreviation OR full name + section number."""
        # Strip § prefix and "Artikel " prefix (EU directives)
        section_number = section_number.replace("§", "").strip()
        section_number = re.sub(r"^Artikel\s+", "", section_number, flags=re.IGNORECASE)
        with self._conn() as conn:
            row = conn.execute(
                """SELECT p.*, l.name as law_name, l.abbreviation as law_abbreviation
                   FROM paragraphs p
                   JOIN laws l ON p.law_id = l.id
                   WHERE (l.abbreviation = ? COLLATE NOCASE
                          OR l.name = ? COLLATE NOCASE)
                     AND p.section_number = ?""",
                (law_identifier, law_identifier, section_number)
            ).fetchone()
            return dict(row) if row else None

    def search_paragraphs(self, query: str, limit: int = 20) -> list[dict]:
        # Sanitize FTS5 query: wrap in double quotes for phrase search to
        # prevent FTS5 operators (AND, OR, NEAR, *, column:) from breaking.
        safe_query = '"{}"'.format(query.replace('"', '""'))
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT p.*, l.name as law_name, l.abbreviation as law_abbreviation,
                          snippet(paragraphs_fts, 0, '<mark>', '</mark>', '...', 30) as snippet
                   FROM paragraphs_fts
                   JOIN paragraphs p ON p.id = paragraphs_fts.rowid
                   JOIN laws l ON p.law_id = l.id
                   WHERE paragraphs_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (safe_query, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    def list_source_urls(self) -> list[str]:
        """Return all source_file URLs currently stored in the laws table."""
        with self._conn() as conn:
            rows = conn.execute("SELECT source_file FROM laws").fetchall()
        return [r["source_file"] for r in rows if r["source_file"]]

    def list_laws(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT l.id, l.name, l.abbreviation, l.stand_date,
                          COUNT(p.id) as section_count
                   FROM laws l
                   LEFT JOIN paragraphs p ON p.law_id = l.id
                   GROUP BY l.id
                   ORDER BY l.abbreviation"""
            ).fetchall()
            return [dict(r) for r in rows]
