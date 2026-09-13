"""LLM client for mlx-serve (Ling-3.0-tiny) on m2ultra:11234."""

from __future__ import annotations

import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)


class LLMError(Exception):
    pass


class LLMJSONError(LLMError):
    """LLM returned content that's not valid JSON."""

    def __init__(self, message: str, raw: str):
        super().__init__(message)
        self.raw = raw


class LLMClient:
    """Client for mlx-serve /v1/chat/completions with JSON output support."""

    def __init__(self, base_url: str, api_key: str, model: str,
                 enable_thinking: bool = False,
                 response_format: str = "json_object",
                 timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.enable_thinking = enable_thinking
        self.response_format = response_format
        self.timeout = timeout

    def _call(self, system: str, user: str, max_tokens: int,
              response_format: str | None = None, timeout: int | None = None) -> str:
        """Make a chat completion call. Returns the content string."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }

        # Thinking control: top-level enable_thinking is the working method
        # (chat_template_kwargs does NOT work on mlx-serve)
        if not self.enable_thinking:
            payload["enable_thinking"] = False

        # Response format for JSON output
        rf = response_format or self.response_format
        if rf:
            payload["response_format"] = {"type": rf}

        to = timeout or self.timeout

        for attempt in range(3):
            try:
                resp = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    json=payload,
                    timeout=to,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except httpx.TimeoutException:
                logger.warning("LLM call timeout (attempt %d/3)", attempt + 1)
                if attempt == 2:
                    raise LLMError("LLM timeout after 3 attempts") from None
                time.sleep(2 ** (attempt + 1))
            except httpx.HTTPStatusError as e:
                logger.warning("LLM HTTP error (attempt %d/3): %s", attempt + 1, e)
                if attempt == 2:
                    raise LLMError(f"LLM HTTP error: {e}") from e
                time.sleep(2 ** (attempt + 1))

    def chat_json(self, system: str, user: str, max_tokens: int,
                  timeout: int | None = None) -> dict:
        """Call LLM and parse JSON response.

        Returns parsed dict. Raises LLMJSONError if content is not valid JSON.
        """
        raw = self._call(system, user, max_tokens, response_format="json_object",
                         timeout=timeout)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMJSONError(f"LLM returned invalid JSON: {e}", raw) from e

    def chat_text(self, system: str, user: str, max_tokens: int,
                  timeout: int | None = None) -> str:
        """Call LLM and return raw text (no JSON parsing)."""
        return self._call(system, user, max_tokens, response_format=None, timeout=timeout)
