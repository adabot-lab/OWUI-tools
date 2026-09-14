import os
import time
import logging

import httpx
from litellm.integrations.custom_logger import CustomLogger

# Throttle state: unix timestamp of the last notification sent.
# One per direction so a recent failover ping cannot swallow a recovery
# ping (and vice versa).
LAST_FAILOVER = 0.0       # failover pings (primary -> fallback)
LAST_RECOVERY = 0.0       # recovery pings (back on primary)
THROTTLE_S = 600          # at most one gotify push per 10 minutes

# Serving state: model group -> unix ts of the failover that moved the
# group onto fallback. Set on EVERY fallback event (regardless of the
# notification throttle) and cleared on the first direct-primary success.
# In-memory only — a proxy restart while on fallback loses the flag.
_fallback_active: dict = {}

_warned = False           # warn about missing GOTIFY_* env only once

logger = logging.getLogger(__name__)


def _model_group(kwargs):
    """model_group from litellm kwargs; None when absent or malformed."""
    litellm_params = kwargs.get("litellm_params") or {}
    metadata = litellm_params.get("metadata") or {}
    group = metadata.get("model_group")
    return group if isinstance(group, str) and group else None


def _check_recovery(kwargs):
    """Decide whether a success event is a back-on-primary recovery.

    Returns (endpoint, payload) when a recovery ping must be sent, else
    None. The fallback-active flag is ALWAYS consumed for a direct-primary
    success — even when throttled or the gotify env is missing — because
    the serving state advances regardless of whether we could notify.
    """
    global LAST_RECOVERY, _warned

    group = _model_group(kwargs)
    if not group or group not in _fallback_active:
        return None
    if kwargs.get("fallback_depth", 0) >= 1:
        return None  # fallback-served traffic, not a recovery

    failed_at = _fallback_active.pop(group)

    # Throttle first: silently drop repeats inside the window
    if time.time() - LAST_RECOVERY < THROTTLE_S:
        return None
    # Consume the window BEFORE sending, so failed POSTs also count
    LAST_RECOVERY = time.time()

    gotify_url = os.environ.get("GOTIFY_URL")
    gotify_token = os.environ.get("GOTIFY_TOKEN")
    if not gotify_url or not gotify_token:
        if not _warned:
            _warned = True
            logger.warning(
                "[fallback_notify] GOTIFY_URL / GOTIFY_TOKEN not set; "
                "recovery notifications disabled"
            )
        return None

    minutes = max(0, int((time.time() - failed_at) // 60))
    msg = f"✅ '{group}' back on primary ({minutes}m on fallback)"
    payload = {
        "title": "LiteLLM recovery",
        "message": msg,
        "priority": 8,
    }
    return f"{gotify_url}/message?token={gotify_token}", payload


class FallbackNotifier(CustomLogger):
    """
    Push a gotify notification whenever ANY proxy-level fallback succeeds
    (e.g. plan-token exhaustion causing a failover to another deployment),
    and once more when the model group is back on its primary.

    Deliberately does NO model filtering — it fires on every fallback.

    Notifications are throttled to one per THROTTLE_S seconds PER
    DIRECTION (LAST_FAILOVER / LAST_RECOVERY); the window is consumed
    even if the POST fails, so a flaky gotify server cannot cause a
    notification storm.
    """

    async def log_success_fallback_event(
        self,
        original_model_group: str,
        kwargs: dict,
        original_exception: Exception,
    ) -> None:
        global LAST_FAILOVER, _warned

        # Serving state first: flag EVERY fallback event, even when the
        # notification below is throttled — this tracks serving state,
        # not notification state.
        _fallback_active[original_model_group] = time.time()

        # Throttle first: silently drop repeats inside the window
        if time.time() - LAST_FAILOVER < THROTTLE_S:
            return
        # Consume the window BEFORE sending, so failed POSTs also count
        LAST_FAILOVER = time.time()

        gotify_url = os.environ.get("GOTIFY_URL")
        gotify_token = os.environ.get("GOTIFY_TOKEN")
        if not gotify_url or not gotify_token:
            if not _warned:
                _warned = True
                logger.warning(
                    "[fallback_notify] GOTIFY_URL / GOTIFY_TOKEN not set; "
                    "fallback notifications disabled"
                )
            return

        msg = (
            f"⚠️ '{original_model_group}' failed "
            f"({type(original_exception).__name__}) — now serving on "
            f"'{kwargs.get('model')}'. Wrap up, no new heavy tasks."
        )

        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{gotify_url}/message?token={gotify_token}",
                    json={
                        "title": "LiteLLM fallback",
                        "message": msg,
                        "priority": 8,
                    },
                    timeout=10.0,
                )
        except Exception:
            logger.exception("[fallback_notify] failed to send gotify notification")

    def log_success_event(
        self,
        kwargs: dict,
        response_obj,
        start_time,
        end_time,
    ) -> None:
        """Sync success hook — fires on sync SDK calls (log_success_event)."""
        out = _check_recovery(kwargs)
        if out is None:
            return
        endpoint, payload = out
        try:
            with httpx.Client() as client:
                client.post(endpoint, json=payload, timeout=10.0)
        except Exception:
            logger.exception("[fallback_notify] failed to send gotify notification")

    async def async_log_success_event(
        self,
        kwargs: dict,
        response_obj,
        start_time,
        end_time,
    ) -> None:
        """Async success hook — the path the proxy actually invokes."""
        out = _check_recovery(kwargs)
        if out is None:
            return
        endpoint, payload = out
        try:
            async with httpx.AsyncClient() as client:
                await client.post(endpoint, json=payload, timeout=10.0)
        except Exception:
            logger.exception("[fallback_notify] failed to send gotify notification")


# Module-level instance — this is what config.yaml references
fallback_notifier = FallbackNotifier()
