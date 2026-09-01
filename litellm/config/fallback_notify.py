import os
import time
import logging

import httpx
from litellm.integrations.custom_logger import CustomLogger

# Throttle state: unix timestamp of the last notification sent
LAST = 0.0
THROTTLE_S = 600          # at most one gotify push per 10 minutes
_warned = False           # warn about missing GOTIFY_* env only once

logger = logging.getLogger(__name__)


class FallbackNotifier(CustomLogger):
    """
    Push a gotify notification whenever ANY proxy-level fallback succeeds
    (e.g. plan-token exhaustion causing a failover to another deployment).

    Deliberately does NO model filtering — it fires on every fallback.

    Notifications are throttled to one per THROTTLE_S seconds; the window
    is consumed even if the POST fails, so a flaky gotify server cannot
    cause a notification storm.
    """

    async def log_success_fallback_event(
        self,
        original_model_group: str,
        kwargs: dict,
        original_exception: Exception,
    ):
        global LAST, _warned

        # Throttle first: silently drop repeats inside the window
        if time.time() - LAST < THROTTLE_S:
            return
        # Consume the window BEFORE sending, so failed POSTs also count
        LAST = time.time()

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


# Module-level instance — this is what config.yaml references
fallback_notifier = FallbackNotifier()
