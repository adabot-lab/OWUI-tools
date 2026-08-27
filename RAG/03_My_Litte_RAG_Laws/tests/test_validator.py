"""Tests for fetch validator (relocated from extract/validator.py)."""
from fetch.validator import validate_extraction, ValidationReport
from fetch.parsers.base import Paragraph


def test_valid_extraction_no_issues():
    """Clean extraction should have no issues."""
    paragraphs = [
        Paragraph(section_number=str(i), section_type="paragraph",
                  title="", content=f"§ {i} content")
        for i in range(1, 11)
    ]
    report = validate_extraction(paragraphs, expected_count=None)
    assert len(report.issues) == 0
    assert report.is_valid


def test_detects_gap_in_section_numbers():
    """Should flag a gap in numbering (e.g., 1,2,4 missing 3)."""
    paragraphs = [
        Paragraph(section_number="1", section_type="paragraph", content="§ 1"),
        Paragraph(section_number="2", section_type="paragraph", content="§ 2"),
        Paragraph(section_number="4", section_type="paragraph", content="§ 4"),
    ]
    report = validate_extraction(paragraphs)
    assert any("gap" in issue.lower() or "missing" in issue.lower() for issue in report.issues)


def test_detects_empty_content():
    """Should flag paragraphs with empty content."""
    paragraphs = [
        Paragraph(section_number="1", section_type="paragraph", content=""),
        Paragraph(section_number="2", section_type="paragraph", content="§ 2 valid"),
    ]
    report = validate_extraction(paragraphs)
    assert any("empty" in issue.lower() for issue in report.issues)


def test_detects_count_mismatch():
    """Should flag when extracted count differs from expected."""
    paragraphs = [
        Paragraph(section_number="1", section_type="paragraph", content="§ 1"),
    ]
    report = validate_extraction(paragraphs, expected_count=10)
    assert any("count" in issue.lower() or "expected" in issue.lower() for issue in report.issues)


def test_detects_duplicate_numbers():
    """Duplicates should be flagged."""
    paragraphs = [
        Paragraph(section_number="1", section_type="paragraph", content="first"),
        Paragraph(section_number="1", section_type="paragraph", content="second"),
    ]
    report = validate_extraction(paragraphs)
    assert any("duplicate" in issue.lower() for issue in report.issues)


def test_accepts_dict_input():
    """Should also accept plain dicts (backward compatibility)."""
    paragraphs = [
        {"section_number": "1", "content": "§ 1"},
        {"section_number": "2", "content": "§ 2"},
    ]
    report = validate_extraction(paragraphs)
    assert report.is_valid
