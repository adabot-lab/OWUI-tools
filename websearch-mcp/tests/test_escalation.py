"""Tests for the tiered fetch escalation helpers in server.py.

Plain pytest layout (no __init__.py); async helpers are exercised via
asyncio.run() inside sync test functions to avoid a pytest-asyncio
dependency. The environment deliberately has NO curl_cffi/playwright
installed, which the lazy-import tests rely on.
"""

import asyncio
import os
import subprocess
import sys

import server

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SCRIPT_5 = "".join("<script>var x = {};</script>".format(i) for i in range(5))
_SCRIPT_4 = "".join("<script>var x = {};</script>".format(i) for i in range(4))
_LONG_TEXT = "word " * 120  # > _EMPTY_EXTRACT_CHARS (500) after strip
_SHORT_TEXT = "Just a moment..."


# ── _should_escalate_tier2 ───────────────────────────────────────────────────

def test_should_escalate_tier2_403():
    assert server._should_escalate_tier2(403, "<html></html>", _LONG_TEXT) is True


def test_should_escalate_tier2_429():
    assert server._should_escalate_tier2(429, None, None) is True


def test_should_escalate_tier2_200_long_cleaned_no():
    assert server._should_escalate_tier2(200, _SCRIPT_5, _LONG_TEXT) is False


def test_should_escalate_tier2_200_short_script_heavy_yes():
    assert server._should_escalate_tier2(200, _SCRIPT_5, _SHORT_TEXT) is True


def test_should_escalate_tier2_200_short_not_script_heavy_no():
    assert server._should_escalate_tier2(200, _SCRIPT_4, _SHORT_TEXT) is False


def test_should_escalate_tier2_200_short_html_none_no():
    assert server._should_escalate_tier2(200, None, _SHORT_TEXT) is False


# ── _is_script_heavy ─────────────────────────────────────────────────────────

def test_is_script_heavy_five_tags_true():
    assert server._is_script_heavy(_SCRIPT_5) is True


def test_is_script_heavy_four_tags_false():
    assert server._is_script_heavy(_SCRIPT_4) is False


def test_is_script_heavy_empty_and_none_false():
    assert server._is_script_heavy("") is False
    assert server._is_script_heavy(None) is False


def test_is_script_heavy_case_insensitive():
    upper = "".join("<SCRIPT src='a{}.js'></SCRIPT>".format(i) for i in range(5))
    assert server._is_script_heavy(upper) is True


# ── _normalize_cookies ───────────────────────────────────────────────────────

def _pw_cookie(name, value, domain):
    """Build a playwright-style cookie dict with extra ignored keys."""
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": 1893456000,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


def test_normalize_cookies_basic():
    cookies = [_pw_cookie("b", "2", ".example.com"), _pw_cookie("a", "1", ".example.com")]
    assert server._normalize_cookies(cookies) == [
        ("a", "1", "example.com"),
        ("b", "2", "example.com"),
    ]


def test_normalize_cookies_last_wins_on_duplicate_name_domain():
    cookies = [
        _pw_cookie("sid", "first", ".example.com"),
        _pw_cookie("sid", "second", "example.com"),
    ]
    # Same dedupe key (name, domain) modulo leading dot — LAST value wins.
    result = [t for t in server._normalize_cookies(cookies) if t[0] == "sid"]
    assert len(result) == 1
    assert result[0] == ("sid", "second", "example.com")


def test_normalize_cookies_leading_dot_stripped_and_sorted():
    cookies = [
        _pw_cookie("z", "9", ".zzz.io"),
        _pw_cookie("a", "1", ".aaa.io"),
        _pw_cookie("m", "5", "mmm.io"),
    ]
    assert server._normalize_cookies(cookies) == [
        ("a", "1", "aaa.io"),
        ("m", "5", "mmm.io"),
        ("z", "9", "zzz.io"),
    ]


# ── _cookies_for_request ─────────────────────────────────────────────────────

def test_cookies_for_request_dot_domain_matches_subdomain():
    jar = server._cookies_for_request([("id", "x", ".example.com")], "https://www.example.com/x")
    assert jar == {"id": "x"}


def test_cookies_for_request_exact_domain_match():
    jar = server._cookies_for_request([("id", "x", "example.com")], "https://example.com/")
    assert jar == {"id": "x"}


def test_cookies_for_request_other_domain_excluded():
    jar = server._cookies_for_request([("id", "x", "other.com")], "https://www.example.com/")
    assert jar == {}


def test_cookies_for_request_suffix_boundary():
    # "ample.com" is NOT a parent of www.example.com (suffix must align on ".").
    jar = server._cookies_for_request([("id", "x", "ample.com")], "https://www.example.com/")
    assert jar == {}


def test_cookies_for_request_same_name_different_domains_both_kept():
    tuples = [("sid", "A", "example.com"), ("sid", "B", "other.com")]
    # Normalization keeps both (dedupe key includes the domain)...
    assert len(server._normalize_cookies([{"name": "sid", "value": v, "domain": d} for v, d in (("A", "example.com"), ("B", "other.com"))])) == 2
    # ...and request filtering picks exactly the one matching the URL host.
    jar = server._cookies_for_request(tuples, "https://example.com/")
    assert jar == {"sid": "A"}
    jar_b = server._cookies_for_request(tuples, "https://other.com/")
    assert jar_b == {"sid": "B"}


# ── lazy-import discipline ───────────────────────────────────────────────────

def test_stealth_fetch_without_curl_cffi():
    # This venv deliberately has no curl_cffi installed.
    result = asyncio.run(server._stealth_fetch("http://x/", {}))
    assert result == (None, None)


def test_lazy_import_probe():
    # THE guard: importing server.py must not pull curl_cffi or playwright
    # into sys.modules. Verified in a fresh interpreter via subprocess.
    code = (
        "import sys; "
        "sys.path.insert(0, {!r}); ".format(_SERVER_DIR)
        + "import server; "
        "print(sys.modules.get('curl_cffi') is not None, sys.modules.get('playwright') is not None)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_SERVER_DIR,
    )
    assert proc.returncode == 0, proc.stderr
    last_line = proc.stdout.strip().splitlines()[-1]
    assert last_line == "False False"
