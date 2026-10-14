"""AI Job Agent — two agents over an OpenAI-compatible endpoint.

Agent 1  ``_estimate_required_years``: estimate the required years of
                                         experience for one posting.
Agent 2  ``assess_suitability``       : score how well a resume fits a job.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional
import yaml
_RATE_LIMIT_CONFIGURED = False

from .ai_client import AIClient
from .schema import ExperienceYearsOutput, SuitabilityOutput

logger = logging.getLogger(__name__)

def configure_rate_limit() -> None:
    """Apply AI_RATE_LIMIT_PER_MIN to the shared limiter, at most once.

    Only applies while the limiter still sits at its module default: the
    pipeline overrides the limit at runtime from dashboard settings
    (agent_params.ai_rate_limit_per_min), and re-applying the env value on
    every AIJobAgent() init would clobber that override."""
    global _RATE_LIMIT_CONFIGURED
    if _RATE_LIMIT_CONFIGURED:
        return
    _RATE_LIMIT_CONFIGURED = True
    try:
        from ..config import AI_RATE_LIMIT_PER_MIN
        from .rate_limiter import DEFAULT_RATE_LIMIT, NVIDIA_RATE_LIMITER
        if (
            AI_RATE_LIMIT_PER_MIN > 0
            and NVIDIA_RATE_LIMITER.max_calls == DEFAULT_RATE_LIMIT
        ):
            NVIDIA_RATE_LIMITER.max_calls = AI_RATE_LIMIT_PER_MIN
            logger.info("AI rate limit set to %d calls/minute", AI_RATE_LIMIT_PER_MIN)
    except Exception:  # noqa: BLE001 - config is optional in tests/tools
        logger.debug("Could not read AI_RATE_LIMIT_PER_MIN; keeping default", exc_info=True)

class AIJobAgent:
    """Filters jobs by experience and assesses resume suitability."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.prompt_path = Path(__file__).resolve().parent / "Prompt" / "v1.yaml"
        self.config = config or {}
        self.client = AIClient(config=self.config)
        self.ai_model = self.client.ai_model
        self._prompts: Optional[Dict[str, str]] = None
        configure_rate_limit()
    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _load_prompts(self) -> Dict[str, str]:
        if self._prompts is None:
            with self.prompt_path.open(encoding="utf-8") as handle:
                self._prompts = yaml.safe_load(handle)
        return self._prompts

    # ------------------------------------------------------------------ #
    # Agent 1 — experience-years estimate
    # ------------------------------------------------------------------ #
    def _estimate_required_years(self, job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        prompt = self._load_prompts()["experience_years_prompt"].format(
            title=job.get("title", ""),
            description=job.get("description", ""),
        )
        try:
            data = self.client.parse(
                messages=[{"role": "user", "content": prompt}],
                text_format=ExperienceYearsOutput,
                label=f"experience[{job.get('title', '?')} @{job.get('company', '?')}]",
            )
        except Exception:
            # Propagate: the pipeline records the failure and retries the job
            # later. Returning None here (the old behavior) made the window
            # check classify EVERY job as outside-window, which an
            # auto-reject rule would turn into a mass rejection on any
            # transient AI outage.
            logger.exception(
                "Experience estimation failed for job id=%s model=%s",
                job.get("id"),
                self.ai_model,
            )
            raise
        return {"min": data.min_years, "max": data.max_years}

    @staticmethod
    def _experience_matches(
        required: Optional[Dict[str, Any]],
        min_years: Optional[int],
        max_years: Optional[int],
    ) -> bool:
        if min_years is None and max_years is None:
            return True
        if required is None:
            return False
        user_min = min_years or 0
        user_max = max_years
        job_min, job_max = required["min"], required["max"]
        below = user_max is not None and job_min > user_max
        above = job_max is not None and job_max < user_min
        return not (below or above)

    # ------------------------------------------------------------------ #
    # Agent 2 — resume suitability assessment
    # ------------------------------------------------------------------ #
    def assess_suitability(self, job: Dict[str, Any], resume_text: str) -> Dict[str, Any]:
        """Assess how well resume_text fits job. Returns score, verdict, skills, reasoning."""
        prompt = self._load_prompts()["screen_prompt"].format(
            resume=resume_text,
            title=job.get("title", ""),
            company=job.get("company", ""),
            description=job.get("description", ""),
        )
        try:
            data = self.client.parse(
                messages=[{"role": "user", "content": prompt}],
                text_format=SuitabilityOutput,
                temperature=0.2,
                label=f"fit[{job.get('title', '?')} @{job.get('company', '?')}]",
            )
        except Exception:
            # Propagate: the pipeline records the failure and retries the job
            # later. Returning the fake "unknown" verdict here (the old
            # behavior) persisted the job as scored — so it was never
            # retried, and the real failure was invisible in the UI.
            logger.exception(
                "Suitability parsing failed for job id=%s model=%s",
                job.get("id"),
                self.ai_model,
            )
            raise
        return data.model_dump()
