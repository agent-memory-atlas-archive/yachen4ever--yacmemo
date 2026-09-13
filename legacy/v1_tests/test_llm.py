"""Tests for llm.py: LLMClient with mocked httpx responses."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from yacmemo.llm import LLMClient, LLMError, LLMJSONError


def _mock_response(content: str, status_code: int = 200) -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


class TestChatJson:
    def test_valid_json(self):
        llm = LLMClient("http://localhost:11234/v1", "sk-test", "model")
        json_content = json.dumps({"files": [], "entities": []})
        with patch("httpx.post", return_value=_mock_response(json_content)):
            result = llm.chat_json("system", "user", 100)
            assert result == {"files": [], "entities": []}

    def test_invalid_json_raises(self):
        llm = LLMClient("http://localhost:11234/v1", "sk-test", "model")
        with (
            patch("httpx.post", return_value=_mock_response("not json at all")),
            pytest.raises(LLMJSONError),
        ):
            llm.chat_json("system", "user", 100)

    def test_sends_thinking_disabled(self):
        llm = LLMClient("http://localhost:11234/v1", "sk-test", "model",
                        enable_thinking=False)
        with patch("httpx.post", return_value=_mock_response("{}")) as mock_post:
            llm.chat_json("s", "u", 10)
            call_kwargs = mock_post.call_args.kwargs
            payload = call_kwargs["json"]
            assert payload["enable_thinking"] is False

    def test_sends_response_format(self):
        llm = LLMClient("http://localhost:11234/v1", "sk-test", "model")
        with patch("httpx.post", return_value=_mock_response("{}")) as mock_post:
            llm.chat_json("s", "u", 10)
            call_kwargs = mock_post.call_args.kwargs
            payload = call_kwargs["json"]
            assert payload["response_format"] == {"type": "json_object"}

    def test_custom_timeout(self):
        llm = LLMClient("http://localhost:11234/v1", "sk-test", "model", timeout=30)
        with patch("httpx.post", return_value=_mock_response("{}")) as mock_post:
            llm.chat_json("s", "u", 10, timeout=120)
            call_kwargs = mock_post.call_args.kwargs
            assert call_kwargs["timeout"] == 120


class TestChatText:
    def test_returns_raw_text(self):
        llm = LLMClient("http://localhost:11234/v1", "sk-test", "model")
        with patch("httpx.post", return_value=_mock_response("hello world")):
            result = llm.chat_text("system", "user", 100)
            assert result == "hello world"

    def test_no_response_format(self):
        llm = LLMClient("http://localhost:11234/v1", "sk-test", "model",
                        response_format="")
        with patch("httpx.post", return_value=_mock_response("text")) as mock_post:
            llm.chat_text("s", "u", 10)
            payload = mock_post.call_args.kwargs["json"]
            assert "response_format" not in payload


class TestRetry:
    def test_retries_on_timeout(self):
        llm = LLMClient("http://localhost:11234/v1", "sk-test", "model")
        with patch("httpx.post", side_effect=[
            httpx.TimeoutException("timeout"),
            _mock_response("{}"),
        ]), patch("time.sleep"):  # don't actually sleep
            result = llm.chat_json("s", "u", 10)
            assert result == {}

    def test_fails_after_3_timeouts(self):
        llm = LLMClient("http://localhost:11234/v1", "sk-test", "model")
        with (
            patch("httpx.post", side_effect=httpx.TimeoutException("timeout")),
            patch("time.sleep"),
            pytest.raises(LLMError, match="timeout after 3 attempts"),
        ):
            llm.chat_json("s", "u", 10)


class TestBaseUrlHandling:
    def test_trailing_slash_stripped(self):
        llm = LLMClient("http://localhost:11234/v1/", "sk-test", "model")
        assert llm.base_url == "http://localhost:11234/v1"
