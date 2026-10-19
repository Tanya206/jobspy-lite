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


def test_openai_base_url_uses_openai_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-key")
    client = AIClient(
        config={
            "ai_model": "gpt-4.1-mini",
            "ai_base_url": "https://api.openai.com/v1",
        }
    )
    assert client._client.api_key == "sk-openai"
    assert str(client._client.base_url).rstrip("/") == "https://api.openai.com/v1"


def test_nvidia_base_url_keeps_nvidia_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-key")
    client = AIClient(
        config={
            "ai_model": "nvidia/nemotron-3-super-120b-a12b",
            "ai_base_url": "https://integrate.api.nvidia.com/v1",
        }
    )
    assert client._client.api_key == "nv-key"


def test_explicit_api_key_wins(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    client = AIClient(
        config={
            "api_key": "explicit-key",
            "ai_model": "gpt-4.1-mini",
            "ai_base_url": "https://api.openai.com/v1",
        }
    )
    assert client._client.api_key == "explicit-key"


def test_blank_model_and_url_fall_back_to_preset():
    client = AIClient(config={"api_key": "test-key", "ai_model": "  ", "ai_base_url": ""})
    assert client.ai_model
    assert client.base_url.startswith("http")


def _capture_kwargs(monkeypatch, client):
    seen = {}

    def parse(**kwargs):
        seen.update(kwargs)
        return _ok_response()

    monkeypatch.setattr(client._client.beta.chat.completions, "parse", parse)
    return seen


@pytest.mark.parametrize("model", ["gpt-5-nano", "gpt-5-mini", "openai/o4-mini"])
def test_reasoning_models_get_effort_not_temperature(monkeypatch, model):
    client = AIClient(
        config={
            "api_key": "k",
            "ai_model": model,
            "ai_base_url": "http://localhost:9/v1",
            "reasoning_effort": "Minimal",
        }
    )
    seen = _capture_kwargs(monkeypatch, client)
    client.parse(messages=[], text_format=Out, temperature=0.2)
    assert "temperature" not in seen
    assert seen["reasoning_effort"] == "minimal"


def test_blank_effort_sends_neither(monkeypatch):
    client = AIClient(
        config={
            "api_key": "k",
            "ai_model": "gpt-5-nano",
            "ai_base_url": "http://localhost:9/v1",
            "reasoning_effort": "",
        }
    )
    seen = _capture_kwargs(monkeypatch, client)
    client.parse(messages=[], text_format=Out)
    assert "temperature" not in seen
    assert "reasoning_effort" not in seen


@pytest.mark.parametrize("model", ["gpt-4.1-mini", "gpt-5-chat-latest", "nvidia/nemotron-3-super-120b-a12b"])
def test_other_models_keep_temperature(monkeypatch, model):
    client = AIClient(
        config={
            "api_key": "k",
            "ai_model": model,
            "ai_base_url": "http://localhost:9/v1",
            "reasoning_effort": "low",
        }
    )
    seen = _capture_kwargs(monkeypatch, client)
    client.parse(messages=[], text_format=Out, temperature=0.2)
    assert seen["temperature"] == 0.2
    assert "reasoning_effort" not in seen


def test_rewrite_client_uses_mini_with_its_own_effort(monkeypatch):
    from app.agent import resume_rewrite

    monkeypatch.setattr("app.config.REWRITE_MODEL", "gpt-5-mini")
    monkeypatch.setattr("app.config.REWRITE_BASE_URL", "")
    monkeypatch.setattr("app.config.AI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr("app.config.REWRITE_REASONING_EFFORT", "medium")
    client = resume_rewrite.make_client("", "", {"rewrite_model": "", "rewrite_base_url": ""})
    assert client.ai_model == "gpt-5-mini"
    assert client.reasoning_effort == "medium"


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
