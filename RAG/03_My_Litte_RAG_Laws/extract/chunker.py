"""Split raw OCR markdown into LLM-sized chunks, preserving structure."""
import re

HEADER_PATTERNS = {
    "name": r"^#\s*Gesetz:\s*(.+)$",
    "abbreviation": r"^#\s*Abkürzung:\s*(.+)$",
    "stand_date": r"^#\s*Stand:\s*(.+)$",
}

# Matches start of a § section or Artikel section at beginning of a line
SECTION_START_RE = re.compile(
    r"^(?:\s*§\s*\d+[a-zA-Z]?|\s*Artikel\s+\d+[a-zA-Z]?)",
    re.IGNORECASE | re.MULTILINE
)


def extract_header(markdown: str) -> dict:
    """Extract law metadata from the standardized header lines.

    Expected format:
        # Gesetz: Full Law Name
        # Abkürzung: ABBR
        # Stand: Date
    """
    result = {"name": "", "abbreviation": "", "stand_date": ""}
    for line in markdown.split("\n")[:10]:
        line = line.strip()
        for field, pattern in HEADER_PATTERNS.items():
            match = re.match(pattern, line, re.IGNORECASE)
            if match:
                result[field] = match.group(1).strip()
    return result


def chunk_markdown(markdown: str, max_chunk_chars: int = 8000) -> list[str]:
    """Split markdown into chunks at paragraph (§/Artikel) boundaries.

    Each chunk stays under max_chunk_chars. Splits only at section starts
    to avoid cutting a paragraph in half.
    """
    if len(markdown) <= max_chunk_chars:
        return [markdown]

    # Find all section start positions
    positions = [m.start() for m in SECTION_START_RE.finditer(markdown)]
    if not positions or positions[0] != 0:
        positions.insert(0, 0)

    chunks = []
    current_start = positions[0]

    for i in range(1, len(positions)):
        # If adding this section would exceed the limit, cut here
        section_end = positions[i]
        if section_end - current_start > max_chunk_chars and section_end > current_start:
            chunks.append(markdown[current_start:positions[i - 1] if i > 1 else positions[i]].strip())
            current_start = positions[i - 1] if i > 1 else positions[i]

    # Add remaining content
    if current_start < len(markdown):
        chunks.append(markdown[current_start:].strip())

    # Handle case where a single section is larger than max_chunk_chars
    # (rare but possible for very long paragraphs) — split by char boundary
    final_chunks = []
    for chunk in chunks:
        if len(chunk) <= max_chunk_chars:
            final_chunks.append(chunk)
        else:
            # Hard split for oversized single sections
            for i in range(0, len(chunk), max_chunk_chars):
                final_chunks.append(chunk[i:i + max_chunk_chars])

    return final_chunks if final_chunks else [markdown]
