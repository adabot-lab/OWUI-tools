"""Tests for the LLM extractor. Uses httpx mock — no real API calls."""
import json
import pytest
from unittest.mock import patch, MagicMock
from extract.extractor import extract_paragraphs_from_chunk, LLMExtractor


MOCK_LLM_RESPONSE = {
    "paragraphs": [
        {
            "section_number": "1",
            "section_type": "paragraph",
            "title": "Gegenstand",
            "content": "§ 1 Gegenstand und Anwendungsbereich (1) Dies ist der Inhalt."
        },
        {
            "section_number": "2",
            "section_type": "paragraph",
            "title": "",
            "content": "§ 2 (1) Ein weiterer Absatz."
        }
    ]
}


@pytest.fixture
def extractor():
    return LLMExtractor(
        base_url="http://localhost:4000/v1",
        model="zai-glm-4.7",
        api_key="test-key"
    )


def test_extract_paragraphs_from_chunk_success(extractor):
    """Should parse LLM response into list of paragraph dicts."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{
            "message": {
                "content": json.dumps(MOCK_LLM_RESPONSE)
            }
        }]
    }

    with patch.object(extractor.client, "post", return_value=mock_response):
        result = extractor.extract("§ 1 Test content")

    assert len(result) == 2
    assert result[0]["section_number"] == "1"
    assert result[0]["section_type"] == "paragraph"
    assert "Gegenstand" in result[0]["title"]


def test_extract_handles_json_in_code_block(extractor):
    """LLMs sometimes wrap JSON in ```json blocks. Should strip them."""
    wrapped_response = f"```json\n{json.dumps(MOCK_LLM_RESPONSE)}\n```"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": wrapped_response}}]
    }

    with patch.object(extractor.client, "post", return_value=mock_response):
        result = extractor.extract("§ 1 Test")

    assert len(result) == 2


def test_extract_returns_empty_on_api_error(extractor):
    """Should return empty list if LLM returns an error."""
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.json.return_value = {"error": "server error"}

    with patch.object(extractor.client, "post", return_value=mock_response):
        result = extractor.extract("§ 1 Test")

    assert result == []


def test_extract_returns_empty_on_malformed_json(extractor):
    """Should return empty list if LLM returns non-JSON content."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "This is not JSON"}}]
    }

    with patch.object(extractor.client, "post", return_value=mock_response):
        result = extractor.extract("§ 1 Test")

    assert result == []


def test_extract_returns_empty_on_missing_paragraphs_key(extractor):
    """Should return empty list if JSON doesn't have 'paragraphs' key."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"items": []})}}]
    }

    with patch.object(extractor.client, "post", return_value=mock_response):
        result = extractor.extract("§ 1 Test")

    assert result == []


def test_extract_dedupes_duplicate_section_numbers(extractor):
    """Should not return duplicate section numbers."""
    dup_response = {
        "paragraphs": [
            {"section_number": "1", "section_type": "paragraph", "title": "", "content": "first"},
            {"section_number": "1", "section_type": "paragraph", "title": "", "content": "second"},
            {"section_number": "2", "section_type": "paragraph", "title": "", "content": "third"},
        ]
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps(dup_response)}}]
    }

    with patch.object(extractor.client, "post", return_value=mock_response):
        result = extractor.extract("§ 1 Test")

    assert len(result) == 2  # deduped
    section_numbers = [p["section_number"] for p in result]
    assert section_numbers == ["1", "2"]
