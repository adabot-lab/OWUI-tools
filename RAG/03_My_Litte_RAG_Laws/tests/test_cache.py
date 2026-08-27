"""Tests for the cache layer: save, read, list, clear, slugify."""
import json
from pathlib import Path

import pytest

from fetch.cache import (
    save_to_cache,
    read_manifest,
    write_manifest,
    list_cached,
    clear_cache,
    _slugify,
    CACHE_DIR,
)


class TestSlugify:
    def test_gii_url(self):
        url = "https://www.gesetze-im-internet.de/vgv_2016/xml.zip"
        assert _slugify(url, "gii_xml") == "vgv_2016"

    def test_vv_url(self):
        url = "https://www.verwaltungsvorschriften-im-internet.de/bsvwvbund_123.htm"
        assert _slugify(url, "vv_html") == "bsvwvbund_123"

    def test_eurlex_url(self):
        url = "https://eur-lex.europa.eu/legal-content/DE/TXT/HTML/?uri=CELEX:02014L0024-20260101"
        assert _slugify(url, "eurlex_html") == "eurlex_02014L0024"

    def test_eurlex_url_without_prefix(self):
        url = "https://eur-lex.europa.eu/legal-content/DE/TXT/HTML/?uri=32014L0024"
        assert _slugify(url, "eurlex_html") == "eurlex_32014L0024"


class TestSaveAndReadCache:
    def test_save_creates_file_and_manifest(self, tmp_path):
        url = "https://www.gesetze-im-internet.de/vgv_2016/xml.zip"
        data = b"<dokumente/>"

        path = save_to_cache("gii_xml", url, data, cache_dir=tmp_path)

        assert path.exists()
        assert path.read_bytes() == data
        assert path.name == "vgv_2016.xml"

        manifest = read_manifest(tmp_path)
        assert "vgv_2016.xml" in manifest
        assert manifest["vgv_2016.xml"]["url"] == url
        assert manifest["vgv_2016.xml"]["source_type"] == "gii_xml"

    def test_save_multiple_sources(self, tmp_path):
        save_to_cache("gii_xml",
                      "https://www.gesetze-im-internet.de/vgv_2016/xml.zip",
                      b"<doc1/>", tmp_path)
        save_to_cache("eurlex_html",
                      "https://eur-lex.europa.eu/legal-content/DE/TXT/HTML/?uri=CELEX:02014L0024-20260101",
                      b"<html/>", tmp_path)

        manifest = read_manifest(tmp_path)
        assert len(manifest) == 2
        assert "vgv_2016.xml" in manifest
        assert "eurlex_02014L0024.xhtml" in manifest

    def test_overwrite_existing_file(self, tmp_path):
        url = "https://www.gesetze-im-internet.de/vgv_2016/xml.zip"
        save_to_cache("gii_xml", url, b"old", tmp_path)
        save_to_cache("gii_xml", url, b"new", tmp_path)

        manifest = read_manifest(tmp_path)
        assert len(manifest) == 1  # no duplicate manifest entries
        assert (tmp_path / "vgv_2016.xml").read_bytes() == b"new"


class TestListCached:
    def test_list_returns_all_entries(self, tmp_path):
        save_to_cache("gii_xml",
                      "https://www.gesetze-im-internet.de/vgv_2016/xml.zip",
                      b"x", tmp_path)
        save_to_cache("vv_html",
                      "https://www.verwaltungsvorschriften-im-internet.de/doc.htm",
                      b"y", tmp_path)

        entries = list_cached(tmp_path)
        assert len(entries) == 2
        filenames = [e["filename"] for e in entries]
        assert "vgv_2016.xml" in filenames
        assert "doc.htm" in filenames
        for e in entries:
            assert "url" in e
            assert "source_type" in e
            assert "path" in e

    def test_list_empty_cache(self, tmp_path):
        entries = list_cached(tmp_path)
        assert entries == []


class TestClearCache:
    def test_clear_removes_everything(self, tmp_path):
        save_to_cache("gii_xml",
                      "https://www.gesetze-im-internet.de/vgv_2016/xml.zip",
                      b"x", tmp_path)

        count = clear_cache(tmp_path)
        assert count == 1
        assert not tmp_path.exists()

    def test_clear_empty_returns_zero(self, tmp_path):
        count = clear_cache(tmp_path)
        assert count == 0
