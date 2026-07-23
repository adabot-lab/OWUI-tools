"""Tests for the fetcher: source-type detection, URL rewriting, ZIP handling."""
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from fetch.fetcher import (
    detect_source_type,
    _rewrite_eurlex_url,
    _extract_zip_xml,
    fetch_one,
    read_sources_file,
    FetchResult,
    GII, VV, EURLEX,
)


class TestDetectSourceType:
    def test_gii_zip_url(self):
        url = "https://www.gesetze-im-internet.de/vgv_2016/xml.zip"
        assert detect_source_type(url) == GII

    def test_vv_url(self):
        url = "https://www.verwaltungsvorschriften-im-internet.de/bsvwvbund_123.htm"
        assert detect_source_type(url) == VV

    def test_eurlex_url(self):
        url = "https://eur-lex.europa.eu/legal-content/DE/TXT/HTML/?uri=CELEX:02014L0024-20260101"
        assert detect_source_type(url) == EURLEX

    def test_unknown_url_raises(self):
        with pytest.raises(ValueError, match="Cannot determine source type"):
            detect_source_type("https://example.com/law.txt")

    def test_gii_without_zip_raises(self):
        with pytest.raises(ValueError):
            detect_source_type("https://www.gesetze-im-internet.de/vgv_2017/")


class TestRewriteEurlexUrl:
    def test_rewrites_to_cellar(self):
        url = "https://eur-lex.europa.eu/legal-content/DE/TXT/HTML/?uri=CELEX:02014L0024-20260101"
        fetch_url, headers = _rewrite_eurlex_url(url)
        assert "publications.europa.eu/resource/celex/02014L0024-20260101" in fetch_url
        assert headers["Accept"] == "application/xhtml+xml"
        assert headers["Accept-Language"] == "de"

    def test_strips_celex_prefix(self):
        url = "https://eur-lex.europa.eu/legal-content/DE/TXT/HTML/?uri=CELEX:32014L0024"
        fetch_url, _ = _rewrite_eurlex_url(url)
        assert fetch_url.endswith("/32014L0024")

    def test_missing_uri_raises(self):
        url = "https://eur-lex.europa.eu/legal-content/DE/TXT/HTML/"
        with pytest.raises(ValueError, match="no.*uri"):
            _rewrite_eurlex_url(url)


class TestExtractZipXml:
    def test_extracts_xml_from_zip(self, tmp_path):
        import zipfile
        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("law.xml", "<dokumente><norm/></dokumente>")
        raw = zip_path.read_bytes()
        xml_bytes = _extract_zip_xml(raw)
        assert b"<dokumente>" in xml_bytes

    def test_no_xml_in_zip_raises(self, tmp_path):
        import zipfile
        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("readme.txt", "no xml here")
        raw = zip_path.read_bytes()
        with pytest.raises(ValueError, match="no XML"):
            _extract_zip_xml(raw)


class TestFetchOne:
    def test_fetch_gii_with_mock(self):
        """Mock HTTP to test GII fetch pipeline end-to-end."""
        # Build a minimal GII ZIP
        import zipfile, io
        xml_content = Path("tests/testdata/vgv_sample.xml").read_bytes()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("BJNR062410016.xml", xml_content)
        zip_bytes = buf.getvalue()

        mock_response = MagicMock()
        mock_response.content = zip_bytes
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get = MagicMock(return_value=mock_response)
        mock_client.close = MagicMock()

        result = fetch_one(
            "https://www.gesetze-im-internet.de/vgv_2016/xml.zip",
            client=mock_client,
        )

        assert result.error == ""
        assert result.document is not None
        assert result.document.abbreviation == "VgV"
        assert len(result.document.paragraphs) == 3

    def test_fetch_vv_with_mock(self):
        """Mock HTTP to test VV fetch pipeline."""
        html_bytes = Path("tests/testdata/vob_sample.htm").read_bytes()

        mock_response = MagicMock()
        mock_response.content = html_bytes
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get = MagicMock(return_value=mock_response)

        result = fetch_one(
            "https://www.verwaltungsvorschriften-im-internet.de/bsvwvbund_test.htm",
            client=mock_client,
        )

        assert result.error == ""
        assert result.document is not None
        assert result.document.abbreviation == "VOB/A"
        assert len(result.document.paragraphs) == 5

    def test_fetch_eurlex_with_mock(self):
        """Mock HTTP to test EUR-Lex fetch pipeline."""
        xhtml_bytes = Path("tests/testdata/eurlex_sample.xhtml").read_bytes()

        mock_response = MagicMock()
        mock_response.content = xhtml_bytes
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get = MagicMock(return_value=mock_response)

        result = fetch_one(
            "https://eur-lex.europa.eu/legal-content/DE/TXT/HTML/?uri=CELEX:02014L0024-20260101",
            client=mock_client,
        )

        assert result.error == ""
        assert result.document is not None
        assert result.document.abbreviation == "2014/24/EU"
        assert len(result.document.paragraphs) == 3

    def test_fetch_error_handled(self):
        """Network errors should be caught and returned in FetchResult.error."""
        mock_client = MagicMock()
        mock_client.get = MagicMock(side_effect=ConnectionError("DNS failed"))

        result = fetch_one(
            "https://www.gesetze-im-internet.de/vgv_2016/xml.zip",
            client=mock_client,
        )

        assert result.document is None
        assert "DNS failed" in result.error


class TestReadSourcesFile:
    def test_reads_urls_ignoring_comments(self, tmp_path):
        sources = tmp_path / "sources.txt"
        sources.write_text(
            "# This is a comment\n"
            "https://example.com/law.zip\n"
            "\n"
            "# Another comment\n"
            "https://example.com/other.htm\n"
        )
        urls = read_sources_file(str(sources))
        assert urls == [
            "https://example.com/law.zip",
            "https://example.com/other.htm",
        ]

    def test_empty_file_returns_empty_list(self, tmp_path):
        sources = tmp_path / "empty.txt"
        sources.write_text("# only comments\n\n")
        urls = read_sources_file(str(sources))
        assert urls == []
