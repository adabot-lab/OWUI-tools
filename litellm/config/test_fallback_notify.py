import asyncio
import logging
import os
import sys
import time
import types

if "pytest" not in sys.modules:
    raise RuntimeError("This file is a pytest module; run it via pytest, not directly.")

try:
    import litellm  # noqa: F401
except ImportError:
    # Stub only the CustomLogger base when litellm is not installed
    # (same pattern as test_strip_thinking.py) — the state machine under
    # test does not touch litellm beyond subclassing it.
    _cl = types.ModuleType("litellm.integrations.custom_logger")
    _cl.CustomLogger = object
    sys.modules.setdefault("litellm", types.ModuleType("litellm"))
    sys.modules.setdefault("litellm.integrations", types.ModuleType("litellm.integrations"))
    sys.modules.setdefault("litellm.integrations.custom_logger", _cl)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fallback_notify


class FakeResponse:
    status_code = 200


class FakeAsyncClient:
    """Records every post() call; usable as an async context manager."""

    posts = []
    fail = False  # when True, post() raises instead of recording

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, timeout=None):
        if FakeAsyncClient.fail:
            raise ConnectionError("gotify unreachable")
        FakeAsyncClient.posts.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()


class FakeSyncClient:
    """Sync twin of FakeAsyncClient for the log_success_event path."""

    posts = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, timeout=None):
        FakeSyncClient.posts.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse()


def _reset(monkeypatch):
    fallback_notify.LAST_FAILOVER = 0.0
    fallback_notify.LAST_RECOVERY = 0.0
    fallback_notify._warned = False
    fallback_notify._fallback_active.clear()
    FakeAsyncClient.posts = []
    FakeAsyncClient.fail = False
    FakeSyncClient.posts = []
    monkeypatch.setattr(fallback_notify.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(fallback_notify.httpx, "Client", FakeSyncClient)


def _run_fallback_hook(group="m1", served="m2"):
    hook = fallback_notify.fallback_notifier.log_success_fallback_event
    asyncio.run(
        hook(
            original_model_group=group,
            kwargs={"model": served},
            original_exception=ValueError("boom"),
        )
    )


def _success_kwargs(group="m1", fallback_depth=None):
    kwargs = {"litellm_params": {"metadata": {"model_group": group}}}
    if fallback_depth is not None:
        kwargs["fallback_depth"] = fallback_depth
    return kwargs


def _run_success_hook(group="m1", fallback_depth=None):
    hook = fallback_notify.fallback_notifier.async_log_success_event
    asyncio.run(hook(_success_kwargs(group, fallback_depth), None, None, None))


def _run_sync_success_hook(group="m1", fallback_depth=None):
    hook = fallback_notify.fallback_notifier.log_success_event
    hook(_success_kwargs(group, fallback_depth), None, None, None)


def test_env_unset_noop(monkeypatch, caplog):
    monkeypatch.delenv("GOTIFY_URL", raising=False)
    monkeypatch.delenv("GOTIFY_TOKEN", raising=False)
    _reset(monkeypatch)

    with caplog.at_level(logging.WARNING):
        _run_fallback_hook()
        _run_fallback_hook()

    assert FakeAsyncClient.posts == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_throttle_one_post(monkeypatch):
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.example.com")
    monkeypatch.setenv("GOTIFY_TOKEN", "tok123")
    _reset(monkeypatch)

    _run_fallback_hook()
    _run_fallback_hook()

    assert len(FakeAsyncClient.posts) == 1
    post = FakeAsyncClient.posts[0]
    assert "/message?token=tok123" in post["url"]
    assert post["json"]["title"] == "LiteLLM fallback"
    assert post["json"]["priority"] == 8
    assert "m1" in post["json"]["message"]
    assert "ValueError" in post["json"]["message"]
    assert "m2" in post["json"]["message"]


# --- recovery state machine (back on primary) ---------------------------


def test_fallback_event_sets_flag(monkeypatch):
    """(a) every fallback event sets the fallback-active flag."""
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.example.com")
    monkeypatch.setenv("GOTIFY_TOKEN", "tok123")
    _reset(monkeypatch)

    assert "m1" not in fallback_notify._fallback_active
    _run_fallback_hook()
    assert "m1" in fallback_notify._fallback_active
    assert fallback_notify._fallback_active["m1"] > 0


def test_flag_set_even_when_throttled(monkeypatch):
    """Flag tracks serving state: throttled fallback events still set it."""
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.example.com")
    monkeypatch.setenv("GOTIFY_TOKEN", "tok123")
    _reset(monkeypatch)

    fallback_notify.LAST_FAILOVER = time.time()  # failover ping fully throttled
    _run_fallback_hook()

    assert FakeAsyncClient.posts == []
    assert "m1" in fallback_notify._fallback_active


def test_recovery_first_success_one_ping_and_clears_flag(monkeypatch):
    """(b) first direct-primary success: exactly one recovery ping, flag cleared."""
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.example.com")
    monkeypatch.setenv("GOTIFY_TOKEN", "tok123")
    _reset(monkeypatch)

    fallback_notify._fallback_active["m1"] = time.time() - 125  # 2m ago

    _run_success_hook(group="m1")
    _run_success_hook(group="m1")

    assert len(FakeAsyncClient.posts) == 1
    post = FakeAsyncClient.posts[0]
    assert "/message?token=tok123" in post["url"]
    assert post["json"]["title"] == "LiteLLM recovery"
    assert post["json"]["priority"] == 8
    assert "m1" in post["json"]["message"]
    assert "back on primary" in post["json"]["message"]
    assert "(2m on fallback)" in post["json"]["message"]
    assert "m1" not in fallback_notify._fallback_active


def test_fallback_served_success_keeps_flag(monkeypatch):
    """(c) fallback_depth >= 1 is fallback traffic: no ping, flag kept."""
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.example.com")
    monkeypatch.setenv("GOTIFY_TOKEN", "tok123")
    _reset(monkeypatch)

    fallback_notify._fallback_active["m1"] = time.time() - 300

    _run_success_hook(group="m2", fallback_depth=1)  # served BY the fallback
    _run_success_hook(group="m1", fallback_depth=2)

    assert FakeAsyncClient.posts == []
    assert "m1" in fallback_notify._fallback_active


def test_second_recovery_throttled_but_flag_cleared(monkeypatch):
    """(d) second recovery inside 600 s: no ping, flag still cleared."""
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.example.com")
    monkeypatch.setenv("GOTIFY_TOKEN", "tok123")
    _reset(monkeypatch)

    fallback_notify._fallback_active["m1"] = time.time() - 60
    _run_success_hook(group="m1")  # recovery #1 -> ping, window consumed
    assert len(FakeAsyncClient.posts) == 1

    # New outage + recovery inside the throttle window
    fallback_notify._fallback_active["m1"] = time.time() - 30
    _run_success_hook(group="m1")  # recovery #2 -> throttled

    assert len(FakeAsyncClient.posts) == 1
    assert "m1" not in fallback_notify._fallback_active


def test_failed_post_consumes_throttle_window(monkeypatch, caplog):
    """(e) a failed gotify POST still consumes the recovery window."""
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.example.com")
    monkeypatch.setenv("GOTIFY_TOKEN", "tok123")
    _reset(monkeypatch)
    FakeAsyncClient.fail = True

    fallback_notify._fallback_active["m1"] = time.time() - 60
    with caplog.at_level(logging.ERROR):
        _run_success_hook(group="m1")
    assert "m1" not in fallback_notify._fallback_active

    # Window was consumed: a fresh recovery attempt sends nothing
    FakeAsyncClient.fail = False
    fallback_notify._fallback_active["m1"] = time.time() - 60
    _run_success_hook(group="m1")

    assert FakeAsyncClient.posts == []
    assert "m1" not in fallback_notify._fallback_active
    assert any("failed to send" in r.message for r in caplog.records)


def test_recovery_via_sync_hook(monkeypatch):
    """The sync log_success_event path sends the same single ping."""
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.example.com")
    monkeypatch.setenv("GOTIFY_TOKEN", "tok123")
    _reset(monkeypatch)

    fallback_notify._fallback_active["m1"] = time.time() - 125
    _run_sync_success_hook(group="m1")

    assert len(FakeSyncClient.posts) == 1
    post = FakeSyncClient.posts[0]
    assert post["json"]["title"] == "LiteLLM recovery"
    assert "(2m on fallback)" in post["json"]["message"]
    assert FakeAsyncClient.posts == []
    assert "m1" not in fallback_notify._fallback_active


def test_no_model_group_noop(monkeypatch):
    """Successes without a model_group never touch the state machine."""
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.example.com")
    monkeypatch.setenv("GOTIFY_TOKEN", "tok123")
    _reset(monkeypatch)

    hook = fallback_notify.fallback_notifier.async_log_success_event
    asyncio.run(hook({"litellm_params": {"metadata": {}}}, None, None, None))
    asyncio.run(hook({}, None, None, None))

    assert FakeAsyncClient.posts == []
    assert fallback_notify._fallback_active == {}


def test_recovery_independent_from_failover_throttle(monkeypatch):
    """A recent failover ping cannot swallow the recovery ping."""
    monkeypatch.setenv("GOTIFY_URL", "http://gotify.example.com")
    monkeypatch.setenv("GOTIFY_TOKEN", "tok123")
    _reset(monkeypatch)

    fallback_notify.LAST_FAILOVER = time.time()  # failover ping just sent
    fallback_notify._fallback_active["m1"] = time.time() - 60

    _run_success_hook(group="m1")

    assert len(FakeAsyncClient.posts) == 1
    assert FakeAsyncClient.posts[0]["json"]["title"] == "LiteLLM recovery"
