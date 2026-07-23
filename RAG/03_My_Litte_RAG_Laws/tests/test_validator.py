"""Tests for extraction validation."""
from extract.validator import validate_extraction, ValidationReport


def test_valid_extraction_no_issues():
    """Clean extraction should have no issues."""
    paragraphs = [
        {"section_number": str(i), "section_type": "paragraph", "title": "", "content": f"§ {i} content"}
        for i in range(1, 11)
    ]
    report = validate_extraction(paragraphs, expected_count=None)
    assert len(report.issues) == 0
    assert report.is_valid


def test_detects_gap_in_section_numbers():
    """Should flag a gap in numbering (e.g., 1,2,4 missing 3)."""
    paragraphs = [
        {"section_number": "1", "content": "§ 1"},
        {"section_number": "2", "content": "§ 2"},
        {"section_number": "4", "content": "§ 4"},
    ]
    report = validate_extraction(paragraphs)
    assert any("gap" in issue.lower() or "missing" in issue.lower() for issue in report.issues)


def test_detects_empty_content():
    """Should flag paragraphs with empty content."""
    paragraphs = [
        {"section_number": "1", "content": ""},
        {"section_number": "2", "content": "§ 2 valid"},
    ]
    report = validate_extraction(paragraphs)
    assert any("empty" in issue.lower() for issue in report.issues)


def test_detects_count_mismatch():
    """Should flag when extracted count differs from expected."""
    paragraphs = [{"section_number": "1", "content": "§ 1"}]
    report = validate_extraction(paragraphs, expected_count=10)
    assert any("count" in issue.lower() or "expected" in issue.lower() for issue in report.issues)


def test_detects_duplicate_numbers():
    """Duplicates should already be removed by extractor, but validator catches them too."""
    paragraphs = [
        {"section_number": "1", "content": "first"},
        {"section_number": "1", "content": "second"},
    ]
    report = validate_extraction(paragraphs)
    assert any("duplicate" in issue.lower() for issue in report.issues)
