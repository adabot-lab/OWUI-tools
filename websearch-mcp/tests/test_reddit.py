"""Unit tests for the Reddit .rss backend (no network access needed)."""

import asyncio
import json

import pytest

import httpx

import reddit_backend as rb
import server


# ── parse_reddit_url ──────────────────────────────────────────────────────────

def test_parse_thread_with_slug_and_query():
    parsed = rb.parse_reddit_url(
        "https://www.reddit.com/r/LocalLLaMA/comments/1pwh0q9/best_local_llms_2025/?utm_source=x")
    assert parsed == {"mode": "thread", "sub": "LocalLLaMA", "post_id": "1pwh0q9",
                      "slug": "best_local_llms_2025"}

def test_parse_thread_no_slug():
    assert rb.parse_reddit_url("https://www.reddit.com/r/x/comments/abc123/") == {
        "mode": "thread", "sub": "x", "post_id": "abc123", "slug": None}

def test_parse_thread_old_reddit_t_suffix_is_not_a_slug():
    # old.reddit anchors comments with a bare "t" segment; it is not the slug
    parsed = rb.parse_reddit_url("https://old.reddit.com/r/x/comments/abc/t/")
    assert parsed == {"mode": "thread", "sub": "x", "post_id": "abc", "slug": None}

def test_parse_thread_underscore_and_fullname_segments_are_not_slugs():
    for url in ("https://www.reddit.com/r/x/comments/abc/_/",
                "https://www.reddit.com/r/x/comments/abc/t1_cdef/"):
        assert rb.parse_reddit_url(url)["slug"] is None

def test_parse_thread_comment_permalink_keeps_post_slug():
    # a comment permalink carries the post slug before the comment id
    parsed = rb.parse_reddit_url(
        "https://www.reddit.com/r/x/comments/abc/the_post_slug/def456/?context=3")
    assert parsed["post_id"] == "abc"
    assert parsed["slug"] == "the_post_slug"

def test_parse_listing_plain():
    assert rb.parse_reddit_url("https://www.reddit.com/r/announcements/") == {
        "mode": "listing", "sub": "announcements", "sort": "hot", "time": None}

def test_parse_listing_sort_and_t():
    assert rb.parse_reddit_url("https://reddit.com/r/foo/top/?t=week") == {
        "mode": "listing", "sub": "foo", "sort": "top", "time": "week"}

def test_parse_listing_invalid_t_dropped():
    assert rb.parse_reddit_url("https://www.reddit.com/r/foo/?t=bogus")["time"] is None

def test_parse_user_and_u_shortform():
    assert rb.parse_reddit_url("https://www.reddit.com/user/spez/") == {
        "mode": "user", "name": "spez"}
    assert rb.parse_reddit_url("https://old.reddit.com/u/spez") == {
        "mode": "user", "name": "spez"}

def test_parse_old_reddit_thread():
    assert rb.parse_reddit_url("https://old.reddit.com/r/x/comments/abc/t/")["mode"] == "thread"


def test_parse_non_reddit_and_unknown_paths():
    assert rb.parse_reddit_url("https://example.com/r/x/comments/abc/") is None
    assert rb.parse_reddit_url("https://www.reddit.com/r/x/wiki/index") is None
    assert rb.parse_reddit_url("https://www.reddit.com/search?q=x") is None


# ── atom parsing + rendering ─────────────────────────────────────────────────

_ATOM_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>x : announcements</title>
  <entry>
    <title>Post title</title>
    <link href="https://www.reddit.com/r/x/comments/abc/post/"/>
    <updated>2026-09-01T10:00:00+00:00</updated>
    <author><name>/u/poster</name></author>
    <content type="html">&lt;table&gt;&lt;tr&gt;&lt;td&gt;submitted by
      &lt;a href="/u/poster"&gt;/u/poster&lt;/a&gt; [link] [comments]&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;
      &lt;div&gt;Post &amp;amp; body text&lt;/div&gt;</content>
  </entry>
  <entry>
    <title>/u/commenter on Post title</title>
    <link href="https://www.reddit.com/r/x/comments/abc/post/h1/"/>
    <updated>2026-09-01T11:00:00+00:00</updated>
    <author><name>/u/commenter</name></author>
    <content type="html">&lt;div&gt;First comment&lt;/div&gt;</content>
  </entry>
</feed>"""


def test_parse_atom_entries():
    entries = rb._parse_atom(_ATOM_FIXTURE)
    assert len(entries) == 2
    post = entries[0]
    assert post["title"] == "Post title"
    assert post["author"] == "poster"          # /u/ prefix stripped
    assert post["url"].endswith("/abc/post/")
    assert "Post & body text" in post["body"]  # html-escaped, footer stripped
    assert "[link]" not in post["body"]


def test_render_thread():
    entries = rb._parse_atom(_ATOM_FIXTURE)
    out = rb.render_thread(entries, "https://www.reddit.com/r/x/comments/abc/", 5000)
    assert out["title"] == "Post title"
    assert out["via"] == "reddit-rss"
    assert out["truncated"] is False
    assert "# Post title" in out["content"]
    assert "- u/commenter: First comment" in out["content"]
    assert "Comments (1" in out["content"]


def test_render_thread_truncates():
    entries = rb._parse_atom(_ATOM_FIXTURE)
    out = rb.render_thread(entries, "u", 50)
    assert out["truncated"] is True
    assert out["content"].endswith("... [content truncated]")


def test_render_listing():
    entries = rb._parse_atom(_ATOM_FIXTURE)
    out = rb.render_listing("r/x (hot)", entries, "https://www.reddit.com/r/x/", 5000)
    assert "r/x (hot)" in out["content"]
    assert "Post title" in out["content"]


def test_feed_urls():
    # thread feeds must carry the post slug; dropping it was the reported 404
    assert rb._feed_url({"mode": "thread", "sub": "x", "post_id": "abc",
                         "slug": "the_post_title"}, 50) == \
        "https://www.reddit.com/r/x/comments/abc/the_post_title/.rss?limit=50"
    # slug-less input still yields a usable feed URL (Reddit tolerates it upstream)
    assert rb._feed_url({"mode": "thread", "sub": "x", "post_id": "abc"}, 50) == \
        "https://www.reddit.com/r/x/comments/abc/.rss?limit=50"
    assert rb._feed_url({"mode": "listing", "sub": "foo", "sort": "top", "time": "week"}, 25) == \
        "https://www.reddit.com/r/foo/top.rss?limit=25&t=week"
    assert rb._feed_url({"mode": "user", "name": "spez"}, 10) == \
        "https://www.reddit.com/user/spez/.rss?limit=10"
    url = rb._search_feed_url("q x", "foo", "new", "all", 15)
    assert url.startswith("https://www.reddit.com/r/foo/search.rss?")
    assert "restrict_sr=1" in url and "sort=new" in url and "limit=15" in url
    assert rb._search_feed_url("q", None, "relevance", "all", 15).startswith(
        "https://www.reddit.com/search.rss?")


def test_feed_url_end_to_end_keeps_slug_from_real_url():
    """Regression: the reported URL turned into a slug-less feed URL -> 404.

    The whole chain (parse -> _feed_url) must preserve the slug, so a future
    refactor cannot silently drop it again.
    """
    original = ("https://www.reddit.com/r/hermesagent/comments/1y4dzt3/"
                "i_read_3_weeks_of_rhermesagent_and_matched_it_to/")
    parsed = rb.parse_reddit_url(original)
    feed_url = rb._feed_url(parsed, 100)
    assert "i_read_3_weeks_of_rhermesagent_and_matched_it_to" in feed_url
    assert feed_url == ("https://www.reddit.com/r/hermesagent/comments/1y4dzt3/"
                        "i_read_3_weeks_of_rhermesagent_and_matched_it_to/.rss?limit=100")
    # the slug-less form that was actually requested is never built from this URL
    assert "/comments/1y4dzt3/.rss" not in feed_url


# ── gate: rate limit + cache (injected clock/fetch, no network) ──────────────


class FakeClock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def _mk_gate():
    clock = FakeClock()
    # pass the injected clock into the gate so its own bookkeeping
    # (feeds_gone_until, surfaced via gate_status) uses the same time domain
    gate = rb.RedditGate(min_interval=65.0, cache_ttl=600.0, clock=clock)
    calls = []

    async def fetch(url):
        calls.append(url)
        return _ATOM_FIXTURE, {}

    return gate, clock, fetch, calls


def test_gate_first_call_fetches_and_caches():
    gate, clock, fetch, calls = _mk_gate()
    entries, err = asyncio.run(gate.get("https://x/.rss", fetch, clock))
    assert err is None and len(entries) == 2 and len(calls) == 1
    # immediate second call: cache hit, no fetch, no rate error
    entries, err = asyncio.run(gate.get("https://x/.rss", fetch, clock))
    assert err is None and len(calls) == 1


def test_gate_second_url_within_window_errors_with_retry_after():
    gate, clock, fetch, calls = _mk_gate()
    asyncio.run(gate.get("https://x/.rss", fetch, clock))
    entries, err = asyncio.run(gate.get("https://y/.rss", fetch, clock))
    assert entries is None and err is not None
    assert err["retry_after"] == 66         # next_allowed - now, +1s boundary buffer
    assert len(calls) == 1                   # second url NOT fetched


def test_gate_waits_short_remaining_interval():
    gate, clock, fetch, calls = _mk_gate()
    asyncio.run(gate.get("https://x/.rss", fetch, clock))
    clock.t += 62.0                          # 3 s left in window < WAIT_BUDGET
    entries, err = asyncio.run(gate.get("https://y/.rss", fetch, clock))
    assert err is None and len(calls) == 2   # waited, then fetched


def test_gate_429_pushes_window_out():
    gate, clock, _default_fetch, calls = _mk_gate()
    gate._next_allowed = 0.0

    async def fetch_429(url):
        calls.append(url)
        raise rb._RateLimited(90)

    entries, err = asyncio.run(gate.get("https://x/.rss", fetch_429, clock))
    assert "429" in err["error"] and err["retry_after"] == 90
    # next caller immediately: window now 90 s out > WAIT_BUDGET -> honest error
    _, err2 = asyncio.run(gate.get("https://y/.rss", fetch_429, clock))
    assert "retry" in err2["error"]


def test_gate_fetch_failure_generic_error():
    async def fetch_boom(url):
        raise httpx.ConnectError("nope")

    gate, clock, _, _ = _mk_gate()
    entries, err = asyncio.run(gate.get("https://x/.rss", fetch_boom, clock))
    assert entries is None and "failed" in err["error"]


def test_gate_status_before_and_after_gate_init(monkeypatch):
    # uninitialized singleton: backend reported off, no crash (regression:
    # live health_check silently swallowed AttributeError -> enabled:false)
    monkeypatch.setattr(rb, "_gate", None)
    assert rb.gate_status() == {"enabled": False, "cached_feeds": 0}

    # initialized + one cached feed: enabled with real cache size (regression:
    # gate_status read _gate.cache, raised AttributeError -> tool call failed)
    gate, clock, fetch, _ = _mk_gate()
    entries, err = asyncio.run(gate.get("https://x/.rss", fetch, clock))
    assert err is None and entries is not None and len(entries) == 2
    monkeypatch.setattr(rb, "_gate", gate)
    status = rb.gate_status()
    assert status["enabled"] is True
    assert status["cached_feeds"] == 1
    assert status["min_interval_s"] == 65.0
    assert status["cache_ttl_s"] == 600.0
    assert status["last_success"] is not None


# ── upstream retirement: honest degradation, no raw 404 ─────────────────────


def test_feed_404_raises_feed_gone_not_httpx_error():
    """A 404 must become _FeedGone, not a generic httpx message."""
    def handler(request):
        return httpx.Response(404, content=b"<feed><title>Not Found</title></feed>")

    async def run():
        transport = httpx.MockTransport(handler)
        orig = httpx.AsyncClient
        class Patched(orig):
            def __init__(self, *a, **k):
                k["transport"] = transport
                super().__init__(*a, **k)
        httpx.AsyncClient = Patched
        try:
            await rb._fetch_feed("https://www.reddit.com/r/x/.rss")
        finally:
            httpx.AsyncClient = orig

    with pytest.raises(rb._FeedGone):
        asyncio.run(run())


def test_gate_404_reports_backend_unavailable_not_fetch_failed():
    """Regression: the reported error was a bare httpx 404 path string."""
    gate, clock, _calls_fetch, calls = _mk_gate()

    async def fetch_gone(url):
        calls.append(url)
        raise rb._FeedGone(url)

    entries, err = asyncio.run(gate.get("https://x/.rss", fetch_gone, clock))
    assert entries is None
    assert err["backend_unavailable"] is True
    assert "retired" in err["error"]
    # the opaque httpx phrasing must be gone
    assert "Client error" not in err["error"]
    assert "404 Not Found' for url" not in err["error"]
    assert "reddit feed fetch failed" not in err["error"]


def test_gate_remembers_gone_state_and_stops_probing():
    """After a 404 the gate must not burn the 1/min window on every call."""
    gate, clock, _f, calls = _mk_gate()

    async def fetch_gone(url):
        calls.append(url)
        raise rb._FeedGone(url)

    asyncio.run(gate.get("https://x/.rss", fetch_gone, clock))
    assert len(calls) == 1

    # a different URL, still inside the gone window: no new outbound request
    entries, err = asyncio.run(gate.get("https://y/.rss", fetch_gone, clock))
    assert entries is None and err["backend_unavailable"] is True
    assert len(calls) == 1

    # once the window elapses the gate probes upstream again
    clock.t += rb.FEED_GONE_TTL + 1
    entries, err = asyncio.run(gate.get("https://z/.rss", fetch_gone, clock))
    assert len(calls) == 2


def test_gate_gone_state_visible_in_status(monkeypatch):
    gate, clock, _f, _c = _mk_gate()

    async def fetch_gone(url):
        raise rb._FeedGone(url)

    asyncio.run(gate.get("https://x/.rss", fetch_gone))
    monkeypatch.setattr(rb, "_gate", gate)
    status = rb.gate_status()
    assert status["upstream"] == "retired"
    assert "retired" in status["upstream_note"]

    # the flag clears once the gone window elapses
    clock.t += rb.FEED_GONE_TTL + 1
    assert "upstream" not in rb.gate_status()


def test_cache_wins_over_gone_window():
    """Cached entries stay served even after upstream goes dark."""
    gate, clock, fetch, calls = _mk_gate()
    asyncio.run(gate.get("https://x/.rss", fetch, clock))       # populate cache
    gate.feeds_gone_until = clock() + rb.FEED_GONE_TTL          # upstream dies
    entries, err = asyncio.run(gate.get("https://x/.rss", fetch, clock))
    assert err is None and entries is not None and len(entries) == 2
    assert len(calls) == 1                                      # no new request


def test_fetch_reddit_degrades_to_honest_error(monkeypatch):
    """End-to-end: a real 404 through fetch_reddit must not surface a 404 path.

    Drives the whole chain — parse_reddit_url -> _feed_url -> gate -> _fetch_feed
    (MockTransport returning Reddit's actual 404 Atom body) -> fetch_reddit.
    """
    def handler(request):
        assert request.url.path.endswith(".rss")
        return httpx.Response(
            404, content=b"<feed><title>Not Found</title></feed>",
            headers={"content-type": "application/atom+xml; charset=UTF-8"})

    orig = httpx.AsyncClient

    class Patched(orig):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", Patched)
    gate = rb.RedditGate(min_interval=65.0, cache_ttl=600.0, clock=FakeClock())
    monkeypatch.setattr(rb, "_gate", gate)

    url = ("https://www.reddit.com/r/hermesagent/comments/1y4dzt3/"
           "i_read_3_weeks_of_rhermesagent_and_matched_it_to/")
    parsed = rb.parse_reddit_url(url)
    assert parsed is not None

    out = asyncio.run(rb.fetch_reddit(parsed, url, 5000, 65.0, 600.0))

    assert out["content_length"] == 0
    assert out["backend_unavailable"] is True
    assert "retired" in out["error"]
    # the reported failure mode: an opaque httpx 404 path string
    assert "Client error" not in out["error"]
    assert "404 Not Found' for url" not in out["error"]
    assert "reddit feed fetch failed" not in out["error"]


def test_search_feeds_degrades_to_honest_error(monkeypatch):
    """search_reddit must degrade the same way (same gate, same 404s)."""
    def handler(request):
        return httpx.Response(404, content=b"<feed><title>Not Found</title></feed>")

    orig = httpx.AsyncClient

    class Patched(orig):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", Patched)
    monkeypatch.setattr(rb, "_gate",
                        rb.RedditGate(min_interval=65.0, cache_ttl=600.0,
                                      clock=FakeClock()))

    entries, err = asyncio.run(rb.search_feeds("q", "x", "new", "all", 10, 65.0, 600.0))
    assert entries is None
    assert err is not None and err["backend_unavailable"] is True
    assert "retired" in err["error"]
    assert "404 Not Found' for url" not in err["error"]


# ── server integration ───────────────────────────────────────────────────────


def test_fetch_page_intercepts_reddit_thread(monkeypatch):
    sentinel = {"url": "x", "title": "t", "content": "c", "content_length": 1,
                "truncated": False, "via": "reddit-rss"}
    async def fake_fetch(parsed, url, max_length, mi, ttl):
        assert parsed["mode"] == "thread"
        return sentinel
    monkeypatch.setattr(server, "reddit_fetch", fake_fetch)
    out = asyncio.run(server._fetch_page(
        "https://www.reddit.com/r/x/comments/abc/t/", 5000))
    assert out is sentinel


def test_fetch_page_leaves_non_reddit_alone(monkeypatch):
    async def fail_fetch(*a, **k):
        raise AssertionError("reddit backend must not be called")
    monkeypatch.setattr(server, "reddit_fetch", fail_fetch)
    out = asyncio.run(server._fetch_page("http://127.0.0.1:1/", 5000))
    assert "Failed to fetch" in out["content"]


def test_fetch_page_reddit_disabled(monkeypatch):
    async def fail_fetch(*a, **k):
        raise AssertionError("reddit backend must not be called when disabled")
    monkeypatch.setattr(server, "reddit_fetch", fail_fetch)
    monkeypatch.setattr(server, "USE_REDDIT_BACKEND", False)
    out = asyncio.run(server._fetch_page("http://127.0.0.1:1/", 5000))
    assert "Failed to fetch" in out["content"]


def test_search_reddit_tool(monkeypatch):
    async def fake_search(query, subreddit, sort, tr, limit, mi, ttl):
        assert (query, subreddit, sort) == ("ollama", "LocalLLaMA", "new")
        return rb._parse_atom(_ATOM_FIXTURE), None
    monkeypatch.setattr(server, "search_feeds", fake_search)
    out = asyncio.run(server.search_reddit("ollama", subreddit="LocalLLaMA", sort="new"))
    parsed = json.loads(out)
    assert parsed["total_results"] == 2
    assert parsed["results"][0]["title"] == "Post title"


def test_search_reddit_rejects_bad_sort(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must validate before fetching")
    monkeypatch.setattr(server, "search_feeds", boom)
    out = asyncio.run(server.search_reddit("q", sort="bogus"))
    assert "error" in json.loads(out)


def test_search_reddit_rate_limited_passthrough(monkeypatch):
    async def limited(*a, **k):
        return None, {"error": "reddit rate limited (429); retry in 90s", "retry_after": 90}
    monkeypatch.setattr(server, "search_feeds", limited)
    parsed = json.loads(asyncio.run(server.search_reddit("q")))
    assert parsed["retry_after"] == 90
