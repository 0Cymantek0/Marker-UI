"""Custom Anthropic LLM service for marker.

marker's built-in ``marker.services.claude.ClaudeService`` constructs its
``anthropic.Anthropic`` client without an explicit ``base_url``, so the
Anthropic SDK falls back to the ``ANTHROPIC_BASE_URL`` environment variable.
The old code worked around that by mutating ``os.environ`` per job, which is
process-global and unsafe: two concurrent jobs using different custom
Anthropic providers would bleed their base URL into each other.

This subclass pins ``base_url`` on the instance and passes it to the client
explicitly, so provider routing is per-object and never touches the global
environment. ``llm_service`` is set to this module path instead of the stock
Claude service when a custom Anthropic provider is selected.
"""

from __future__ import annotations

from typing import Annotated, Optional

import anthropic
from marker.schema.blocks import Block
from marker.services.claude import ClaudeService


class CustomAnthropicService(ClaudeService):
    """Claude-compatible service with an explicit, per-instance base URL."""

    base_url: Annotated[
        str,
        "Base URL for the Anthropic-compatible API endpoint.",
    ] = "https://api.anthropic.com/v1"

    def get_client(self) -> "anthropic.Anthropic":
        return anthropic.Anthropic(
            api_key=self.claude_api_key,
            base_url=self.base_url,
        )

    def __call__(  # noqa: D401 - signature mirrors the parent contract
        self,
        prompt: str,
        image,
        block: Optional[Block],
        response_schema,
        max_retries: Optional[int] = None,
        timeout: Optional[int] = None,
    ):
        # Delegate to the parent implementation so prompt formatting, retries,
        # and schema handling stay in sync with upstream marker. Only client
        # construction differs (see get_client).
        return super().__call__(
            prompt,
            image,
            block,
            response_schema,
            max_retries=max_retries,
            timeout=timeout,
        )


__all__ = ["CustomAnthropicService"]
