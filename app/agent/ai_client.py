"""Simple OpenAI-compatible client (default: NVIDIA NIM), using native
structured outputs via chat.completions.parse()."""

import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

from .observability import AI_METRICS
from .rate_limiter import NVIDIA_RATE_LIMITER


logger = logging.getLogger(__name__)

MAX_PARSE_ATTEMPTS = 4
INITIAL_RETRY_DELAY_SECONDS = 2.0
MAX_RETRY_DELAY_SECONDS = 120

# NVIDIA returns 429 (quota) and 503 (overloaded) as bursts; both are worth
# waiting out — the shared limiter paces everything else.
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}




class AIClient:
    """Thin wrapper around an OpenAI-compatible chat.completions API."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        project_root = Path(__file__).resolve().parents[2]
        load_dotenv(project_root / ".env")

        config = config or {}
        self.ai_model = config.get("ai_model", "")
        api_key = config.get("api_key") or os.environ.get("NVIDIA_API_KEY")
        base_url = config.get(
            "ai_base_url",
            os.environ.get("AI_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        )

        # max_retries=0: the SDK's internal retries bypass our rate limiter,
        # which let bursts exceed NVIDIA's cap (429s). Our parse() retry loop
        # owns all retrying, so every attempt is rate-limited.
        # timeout: the SDK default is 10 MINUTES — a hung connection (NVIDIA
        # accepts then never answers) stalled scoring threads that long before
        # our retry loop could act. 90s covers slow generation, fails fast on
        # dead connections. connect timeout kept tight at 15s.
        self._client = OpenAI(
            api_key=api_key or "not-used",
            base_url=base_url,
            max_retries=0,
            timeout=httpx.Timeout(90.0, connect=15.0),
        )

    def parse(
        self,
        messages: list,
        text_format: type,
        temperature: float = 0.0,
        label: str = "ai-call",
    ):
        """Chat completion with native structured output, validated against text_format.

        Transient NVIDIA errors (429/503 bursts) are retried with exponential
        backoff + jitter; permanent errors fail fast to the caller. Every
        attempt acquires the shared rate limiter first, so a retry cannot
        burst past the cap. ``label`` (e.g. the job title) is included in
        every log line so retries and rate-limit waits are traceable per job.
        """
        last_error = None

        for attempt in range(1, MAX_PARSE_ATTEMPTS + 1):
            waited = NVIDIA_RATE_LIMITER.acquire()
            if waited > 1.0:
                logger.info(
                    "%s: waited %.1fs for a rate-limit slot (call %d/%d this window)",
                    label, waited, NVIDIA_RATE_LIMITER.recent_count(),
                    NVIDIA_RATE_LIMITER.max_calls,
                )
            started = time.monotonic()
            try:
                response = self._client.beta.chat.completions.parse(
                    model=self.ai_model,
                    messages=messages,
                    response_format=text_format,
                    temperature=temperature,
                )
                AI_METRICS.record_attempt(time.monotonic() - started)
                message = response.choices[0].message
                parsed = message.parsed
                if parsed is None:
                    refusal = getattr(message, "refusal", None)
                    detail = f"AI returned no structured {text_format.__name__} response"
                    if refusal:
                        detail += f": {refusal}"
                    raise ValueError(detail)
                # Validate even when the SDK returns a model instance. This
                # keeps the retry policy responsible for every bad response.
                result = text_format.model_validate(parsed)
                if attempt > 1:
                    logger.info("%s: succeeded on attempt %d/%d", label, attempt, MAX_PARSE_ATTEMPTS)
                AI_METRICS.record_call(True)
                return result
            except Exception as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                AI_METRICS.record_attempt(time.monotonic() - started, status)
                permanent = status is not None and status not in RETRYABLE_STATUS_CODES
                if attempt == MAX_PARSE_ATTEMPTS or permanent:
                    logger.error(
                        "%s: FAILED after %d attempts%s: %s",
                        label, attempt, " (permanent error)" if permanent else "", exc,
                    )
                    AI_METRICS.record_call(False, str(exc))
                    break
                delay = min(
                    INITIAL_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)),
                    MAX_RETRY_DELAY_SECONDS,
                )
                delay += random.uniform(0, delay / 2)  # jitter: spread retries out
                logger.warning(
                    "%s: attempt %d/%d failed (%s); retrying in %.1fs",
                    label, attempt, MAX_PARSE_ATTEMPTS,
                    f"HTTP {status}" if status else exc, delay,
                )
                time.sleep(delay)

        raise last_error
