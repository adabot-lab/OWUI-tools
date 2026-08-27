"""Validate parsed law documents for completeness and quality.

Relocated from extract/validator.py — source-agnostic, logic unchanged.
Works with LawDocument.paragraphs (list[Paragraph]) or list[dict].
"""
import re
from dataclasses import dataclass, field


@dataclass
class ValidationReport:
    issues: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.issues) == 0

    def add(self, msg: str):
        self.issues.append(msg)


def _extract_numeric(section_number: str) -> int | None:
    """Extract leading integer from section number like '127a' -> 127."""
    match = re.match(r"(\d+)", section_number)
    return int(match.group(1)) if match else None


def validate_extraction(
    paragraphs: list,
    expected_count: int | None = None,
) -> ValidationReport:
    """Check extraction results for common issues.

    Args:
        paragraphs: List of paragraph dicts or Paragraph dataclasses.
        expected_count: If known, the expected number of sections.

    Returns:
        ValidationReport with a list of human-readable issues.
    """
    report = ValidationReport()

    if not paragraphs:
        report.add("No paragraphs extracted at all")
        return report

    # Normalize to dicts (support both dict and dataclass inputs)
    def get_field(p, name):
        if isinstance(p, dict):
            return p.get(name, "")
        return getattr(p, name, "")

    # Check for duplicates
    numbers = [get_field(p, "section_number") for p in paragraphs]
    seen = set()
    for n in numbers:
        if n in seen:
            report.add(f"Duplicate section number: § {n}")
        seen.add(n)

    # Check for empty content
    for p in paragraphs:
        if not str(get_field(p, "content", )).strip():
            report.add(f"Empty content for § {get_field(p, 'section_number', )}")

    # Check for gaps in sequential numbering
    numeric_values = []
    for p in paragraphs:
        val = _extract_numeric(str(get_field(p, "section_number")))
        if val is not None:
            numeric_values.append(val)

    if numeric_values:
        numeric_values.sort()
        for i in range(len(numeric_values) - 1):
            if numeric_values[i + 1] - numeric_values[i] > 1:
                report.add(
                    f"Gap in numbering: jumps from § {numeric_values[i]} "
                    f"to § {numeric_values[i + 1]}"
                )

    # Check against expected count
    if expected_count is not None:
        actual = len(paragraphs)
        if actual != expected_count:
            report.add(
                f"Count mismatch: expected {expected_count} sections, got {actual}"
            )

    return report
