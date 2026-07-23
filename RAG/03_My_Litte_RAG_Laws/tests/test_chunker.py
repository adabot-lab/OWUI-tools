"""Tests for the markdown chunker."""
from extract.chunker import chunk_markdown, extract_header


def test_extract_header_vgv():
    """Should extract law name and abbreviation from header."""
    md = "# Gesetz: Verordnung über die Vergabe öffentlicher Aufträge\n# Abkürzung: VgV\n# Stand: 7. Februar 2024\n\n§ 1 Test"
    header = extract_header(md)
    assert header["name"] == "Verordnung über die Vergabe öffentlicher Aufträge"
    assert header["abbreviation"] == "VgV"
    assert header["stand_date"] == "7. Februar 2024"


def test_extract_header_eu_directive():
    """Should handle EU directive headers."""
    md = "# Gesetz: Richtlinie 2014/24/EU\n# Abkürzung: EU-2014/24/EU\n# Stand: 26.03.2014"
    header = extract_header(md)
    assert header["name"] == "Richtlinie 2014/24/EU"
    assert header["abbreviation"] == "EU-2014/24/EU"


def test_extract_header_missing_fields():
    """Should return empty strings for missing fields."""
    md = "# Gesetz: Some Law\n\n§ 1 Test"
    header = extract_header(md)
    assert header["name"] == "Some Law"
    assert header["abbreviation"] == ""


def test_chunk_markdown_small_file():
    """Small files should produce a single chunk."""
    md = "# Gesetz: Test\n# Abkürzung: T\n\n§ 1 Inhalt\n\n§ 2 Inhalt"
    chunks = chunk_markdown(md, max_chunk_chars=10000)
    assert len(chunks) == 1


def test_chunk_markdown_splits_on_paragraph_boundary():
    """Large files should split at paragraph boundaries."""
    padding = "Das ist ein sehr langer Absatz mit viel Text. " * 10
    md = "# Gesetz: Test\n# Abkürzung: T\n\n"
    for i in range(1, 21):
        md += f"§ {i} {padding}\n\n"

    chunks = chunk_markdown(md, max_chunk_chars=2000)
    assert len(chunks) > 1
    # Each chunk should contain a § or header (sections split at boundaries)
    for chunk in chunks:
        assert "§" in chunk or "# Gesetz" in chunk


def test_chunk_markdown_preserves_all_sections():
    """No section should be lost during chunking."""
    md = "# Gesetz: Test\n# Abkürzung: T\n\n"
    for i in range(1, 11):
        md += f"§ {i} Absatz Nummer {i}.\n\n"

    chunks = chunk_markdown(md, max_chunk_chars=500)
    rejoined = " ".join(chunks)
    for i in range(1, 11):
        assert f"§ {i}" in rejoined
