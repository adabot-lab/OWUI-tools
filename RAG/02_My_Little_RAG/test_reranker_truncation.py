"""Tests for the reranker latency fix (RERANK_MAX_CHARS scoring truncation).

Run standalone (no pytest required): python3 test_reranker_truncation.py
Covers: truncation in both wire payloads, disabled truncation, config
malformed-value degradation, and unchanged fail-open/rank contract.
"""

import importlib.util
import json
import os
import sys
import urllib.request
from unittest.mock import patch


def load_reranker(env: dict):
    """Import reranker.py fresh with a controlled env (all RERANK_* cleared)."""
    for k in list(os.environ):
        if k.startswith("RERANK_"):
            os.environ.pop(k, None)
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location(
        "reranker_under_test",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "reranker.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_results(n, text="x" * 2000):
    return [
        {"rank": i + 1, "score": 0.5, "content": text, "file_name": f"f{i}"}
        for i in range(n)
    ]


results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f" — {detail}" if detail and not cond else ""))


# --- 1. rerank payload truncation applies ---
m = load_reranker({
    "RERANK_ENABLED": "true", "RERANK_BASE_URL": "http://x:9292",
    "RERANK_MODEL": "m", "RERANK_MODE": "rerank", "RERANK_MAX_CHARS": "500",
})
p = m._build_rerank_payload("q", make_results(3))
check("rerank payload truncates docs to RERANK_MAX_CHARS",
      all(len(d) == 500 for d in p["documents"]))
check("rerank payload keeps doc count", len(p["documents"]) == 3)

# --- 2. listwise payload truncation applies ---
m = load_reranker({
    "RERANK_ENABLED": "true", "RERANK_BASE_URL": "http://x:9292",
    "RERANK_MODEL": "m", "RERANK_MODE": "listwise", "RERANK_MAX_CHARS": "500",
})
p = m._build_listwise_payload("q", make_results(3))
check("listwise payload truncates passages to RERANK_MAX_CHARS",
      all(len(psg["text"]) == 500 for psg in p["state"]["passages"]))

# --- 3. zero/unset = no truncation (backward compat) ---
m = load_reranker({
    "RERANK_ENABLED": "true", "RERANK_BASE_URL": "http://x:9292",
    "RERANK_MODEL": "m", "RERANK_MODE": "rerank",
})
p = m._build_rerank_payload("q", make_results(2))
check("unset RERANK_MAX_CHARS keeps full doc length", len(p["documents"][0]) == 2000)

m = load_reranker({
    "RERANK_ENABLED": "true", "RERANK_BASE_URL": "http://x:9292",
    "RERANK_MODEL": "m", "RERANK_MODE": "rerank", "RERANK_MAX_CHARS": "0",
})
p = m._build_rerank_payload("q", make_results(2))
check("RERANK_MAX_CHARS=0 disables truncation", len(p["documents"][0]) == 2000)

# --- 4. malformed value degrades to no truncation (never breaks startup) ---
m = load_reranker({
    "RERANK_ENABLED": "true", "RERANK_BASE_URL": "httpx://x",
    "RERANK_MODEL": "m", "RERANK_MODE": "rerank", "RERANK_MAX_CHARS": "garbage",
})
p = m._build_rerank_payload("q", make_results(2))
check("malformed RERANK_MAX_CHARS degrades to no truncation",
      len(p["documents"][0]) == 2000)

# --- 5. served content untouched by truncation (end-to-end via fake http) ---
class FakeResp:
    def raise_for_status(self):
        pass
    def json(self):
        return {"results": [{"index": i, "relevance_score": 1.0 - i / 10}
                            for i in range(3)]}

captured = {}

class FakeClient:
    def __init__(self, timeout):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def post(self, url, json=None):
        captured["payload"] = json
        return FakeResp()

orig_results = make_results(3)  # 2000-char contents
m = load_reranker({
    "RERANK_ENABLED": "true", "RERANK_BASE_URL": "http://x:9292",
    "RERANK_MODEL": "m", "RERANK_MODE": "rerank", "RERANK_TIMEOUT_S": "10",
    "RERANK_MAX_CHARS": "500",
})
with patch.object(m.httpx, "Client", FakeClient):
    out = m.maybe_rerank("q", orig_results)
check("served content stays full-length despite scoring truncation",
      all(len(r["content"]) == 2000 for r in out))
check("scoring payload was truncated",
      all(len(d) == 500 for d in captured["payload"]["documents"]))
check("rerank_score present on all served results",
      all("rerank_score" in r for r in out))
check("rank reassigned 1..n", [r["rank"] for r in out] == [1, 2, 3])
check("scores sorted desc", out[0]["rerank_score"] >= out[1]["rerank_score"]
      >= out[2]["rerank_score"])
check("original retrieval score preserved", all(r["score"] == 0.5 for r in out))
check("original list object not mutated",
      all("rerank_score" not in r for r in orig_results))

# --- 6. fail-open contract unchanged (timeout -> unranked original list) ---
class SlowClient(FakeClient):
    def post(self, url, json=None):
        raise TimeoutError("timed out")

m2 = load_reranker({
    "RERANK_ENABLED": "true", "RERANK_BASE_URL": "http://x:9292",
    "RERANK_MODEL": "m", "RERANK_MODE": "rerank", "RERANK_TIMEOUT_S": "10",
    "RERANK_MAX_CHARS": "500",
})
with patch.object(m2.httpx, "Client", SlowClient):
    out2 = m2.maybe_rerank("q", orig_results)
check("timeout fail-open returns original list object", out2 is orig_results)
check("fail-open output carries no rerank_score",
      all("rerank_score" not in r for r in out2))

# --- 7. short docs pass through untruncated ---
m3 = load_reranker({
    "RERANK_ENABLED": "true", "RERANK_BASE_URL": "http://x:9292",
    "RERANK_MODEL": "m", "RERANK_MODE": "rerank", "RERANK_MAX_CHARS": "500",
})
p = m3._build_rerank_payload("q", make_results(2, text="short doc"))
check("docs shorter than limit pass through unmodified",
      p["documents"] == ["short doc", "short doc"])

# --- 8. gate contract unchanged ---
m4 = load_reranker({"RERANK_ENABLED": "false"})
lst = make_results(3)
check("disabled reranker returns same list object", m4.maybe_rerank("q", lst) is lst)

# --- 9. parse contract unchanged (relevance-order -> index mapping) ---
m5 = load_reranker({
    "RERANK_ENABLED": "true", "RERANK_BASE_URL": "http://x:9292",
    "RERANK_MODEL": "m", "RERANK_MODE": "rerank",
})
scores = m5._parse_rerank_scores(
    {"results": [{"index": 2, "relevance_score": 0.9},
                 {"index": 0, "relevance_score": 0.1},
                 {"index": 1, "relevance_score": 0.5}]}, 3)
check("parse maps relevance-order entries back through index",
      scores == [0.1, 0.5, 0.9])

print()
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
