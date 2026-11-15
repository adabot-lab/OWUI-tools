"""health_check must survive reddit gate-state drift.

Regression: server.py called reddit_gate_status() unguarded — any drift in
the gate object (historical: e8e13f5 _gate.cache typo, fixed in 157e2cf)
raised inside health_check and killed the whole tool call.
"""

import asyncio
import json

import server


def test_health_check_degrades_reddit_rss_on_gate_drift(monkeypatch):
    def _boom():
        raise AttributeError("'_RedditRateGate' object has no attribute 'cache'")

    monkeypatch.setattr(server, "USE_REDDIT_BACKEND", True)
    monkeypatch.setattr(server, "reddit_gate_status", _boom)
    monkeypatch.setattr(server, "USE_SEARXNG_SEARCH", False)  # skip live ping

    result = json.loads(asyncio.run(server.health_check()))

    assert result["status"] == "ok"            # whole tool still succeeds
    assert result["reddit_rss"]["enabled"] is False
    assert "error" in result["reddit_rss"]     # degraded, not silently off


def test_health_check_reports_off_when_reddit_disabled(monkeypatch):
    monkeypatch.setattr(server, "USE_REDDIT_BACKEND", False)
    monkeypatch.setattr(server, "USE_SEARXNG_SEARCH", False)

    result = json.loads(asyncio.run(server.health_check()))

    assert result["status"] == "ok"
    assert result["reddit_rss"] == {"enabled": False}
