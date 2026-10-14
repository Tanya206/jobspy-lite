"""Retry-policy tests for AIClient.parse — no network involved."""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.agent.ai_client import MAX_PARSE_ATTEMPTS, AIClient


class Out(BaseModel):
    score: int = 0


class _FakeHTTPError(Exception):
    """Mimics openai.APIStatusError: carries .response.status_code."""

    def __init__(self, status: int):
        super().__init__(f"http {status}")
        self.response = SimpleNamespace(status_code=status)


def _ok_response():
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(parsed=Out(score=7), refusal=None))
        ]
    )


def _client() -> AIClient:
    return AIClient(
        config={
            "api_key": "test-key",
            "ai_model": "test-model",
            "ai_base_url": "http://localhost:9/v1",
        }
    )


def test_parse_retries_transient_503_then_succeeds(monkeypatch):
    client = _client()
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeHTTPError(503)
        return _ok_response()

    monkeypatch.setattr(client._client.beta.chat.completions, "parse", flaky)
    monkeypatch.setattr("app.agent.ai_client.time.sleep", lambda _s: None)

    out = client.parse(messages=[{"role": "user", "content": "hi"}], text_format=Out)

    assert out.score == 7
    assert calls["n"] == 3


def test_parse_fails_fast_on_permanent_error(monkeypatch):
    client = _client()
    calls = {"n": 0}

    def forbidden(**kwargs):
        calls["n"] += 1
        raise _FakeHTTPError(401)

    monkeypatch.setattr(client._client.beta.chat.completions, "parse", forbidden)
    monkeypatch.setattr("app.agent.ai_client.time.sleep", lambda _s: None)

    with pytest.raises(Exception):
        client.parse(messages=[{"role": "user", "content": "hi"}], text_format=Out)

    assert calls["n"] == 1  # no retry on a permanent auth error


def test_parse_gives_up_after_max_attempts(monkeypatch):
    client = _client()
    calls = {"n": 0}

    def always_503(**kwargs):
        calls["n"] += 1
        raise _FakeHTTPError(503)

    monkeypatch.setattr(client._client.beta.chat.completions, "parse", always_503)
    monkeypatch.setattr("app.agent.ai_client.time.sleep", lambda _s: None)

    with pytest.raises(Exception):
        client.parse(messages=[{"role": "user", "content": "hi"}], text_format=Out)

    assert calls["n"] == MAX_PARSE_ATTEMPTS


def test_parse_acquires_a_rate_limit_slot_on_every_attempt(monkeypatch):
    """Retries must not skip the shared limiter (that is what caused 429 bursts)."""
    client = _client()
    acquires = {"n": 0}

    def acquire():
        acquires["n"] += 1
        return 0.0

    def flaky(**kwargs):
        if acquires["n"] < 2:
            raise _FakeHTTPError(503)
        return _ok_response()

    monkeypatch.setattr("app.agent.ai_client.NVIDIA_RATE_LIMITER.acquire", acquire)
    monkeypatch.setattr(client._client.beta.chat.completions, "parse", flaky)
    monkeypatch.setattr("app.agent.ai_client.time.sleep", lambda _s: None)

    out = client.parse(messages=[{"role": "user", "content": "hi"}], text_format=Out)

    assert out.score == 7
    assert acquires["n"] == 2
