from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.proxy_server import UserAPIKeyAuth, DualCache
from typing import Literal, Optional


class StripHermesHandler(CustomLogger):
    """
    Rewrite "Hermes Agent" -> "the agent" in the system prompt
    before the request is forwarded to Z.AI (glm) models.
    """

    TARGETS = ("Hermes Agent", "Hermes",)   # add more if needed
    REPLACEMENT = "the agent"

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: Literal[
            "completion", "text_completion", "embeddings",
            "image_generation", "moderation", "audio_transcription",
        ],
    ) -> Optional[dict]:
        # Only touch the models you care about
        model = (data.get("model") or "").lower()
        if "zai" not in model and "glm" not in model:
            return data

        messages = data.get("messages") or []
        changed = False
        for msg in messages:
            if msg.get("role") != "system":
                continue
            content = msg.get("content")
            # content can be a string OR a list of content blocks (vision/multimodal)
            if isinstance(content, str):
                new = content
                for t in self.TARGETS:
                    new = new.replace(t, self.REPLACEMENT)
                if new != content:
                    msg["content"] = new
                    changed = True
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        new = block["text"]
                        for t in self.TARGETS:
                            new = new.replace(t, self.REPLACEMENT)
                        if new != block["text"]:
                            block["text"] = new
                            changed = True

        if changed:
            print("[strip_hermes] rewrote system prompt for", model)
        return data   # MUST return data, even if unchanged


# Module-level instance — this is what config.yaml references
strip_hermes_handler = StripHermesHandler()
