import asyncio
import logging
import os
import sys

if "pytest" not in sys.modules:
    raise RuntimeError("This file is a pytest module; run it via pytest, not directly.")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fallback_notify


class FakeResponse:
    status_code = 200


class FakeAsyncClient:
    """Records every post() call; usable as an async context manager."""

    posts = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, timeout=None):
        FakeAsyncClient.posts.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()


def _reset(monkeypatch):
    fallback_notify.LAST = 0.0
    fallback_notify._warned = False
    FakeAsyncClient.posts = []
    monkeypatch.setattr(fallback_notify.httpx, "AsyncClient", FakeAsyncClient)


def _run_hook():
    hook = fallback_notify.fallback_notifier.log_success_fallback_event
    asyncio.run(
        hook(
            original_model_group="m1",
            kwargs={"model": "m2"},
            original_exception=ValueError("boom"),
        )
    )


def test_env_unset_noop(monkeypatch, caplog):
    monkeypatch.delenv("GOTIFY_URL", raising=False)
    monkeypatch.delenv("GOTIFY_TOKEN", raising=False)
    _reset(monkeypatch)

    with caplog.at_level(logging.WARNING):
        _run_hook()
        _run_hook()

    assert FakeAsyncClient.posts == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_throttle_one_post(monkeypatch):
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.example.com")
    monkeypatch.setenv("GOTIFY_TOKEN", "tok123")
    _reset(monkeypatch)

    _run_hook()
    _run_hook()

    assert len(FakeAsyncClient.posts) == 1
    post = FakeAsyncClient.posts[0]
    assert "/message?token=tok123" in post["url"]
    assert post["json"]["title"] == "LiteLLM fallback"
    assert post["json"]["priority"] == 8
    assert "m1" in post["json"]["message"]
    assert "ValueError" in post["json"]["message"]
    assert "m2" in post["json"]["message"]
