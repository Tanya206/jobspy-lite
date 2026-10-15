"""Central configuration for JobSpy Lite.

Every setting can be overridden from the ``.env`` file at the project root (or
plain environment variables). Runtime-editable values (score threshold,
experience window, scrape params, scheduler config) live in the SQLite
``settings`` table instead — this module only supplies the defaults.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


# --------------------------------------------------------------------------- #
# Env parsing helpers
# --------------------------------------------------------------------------- #
def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(raw) if raw is not None and raw.strip() != "" else default
    except ValueError:
        return default


def _env_int_or_none(name: str, default: Optional[int]) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
        return value if value >= 0 else None  # negative = open-ended
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(raw) if raw is not None and raw.strip() != "" else default
    except ValueError:
        return default


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    """Clamp ``value`` into ``[low, high]``; ``default`` when it is not an int."""
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default


def _env_list(name: str, default: List[str]) -> List[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# Scraper defaults (runtime-editable via the settings table)
# --------------------------------------------------------------------------- #
SITES: List[str] = _env_list("SITES", ["linkedin"])
SEARCH_TERMS: List[str] = _env_list("SEARCH_TERMS", ["AI Engineer"])
LOCATION: str = os.environ.get("LOCATION", "Bengaluru")
RESULTS_WANTED: int = _env_int("RESULTS_WANTED", 600)
HOURS_OLD: int = _env_int("HOURS_OLD",2)
COUNTRY_INDEED: str = os.environ.get("COUNTRY_INDEED", "india")
LINKEDIN_FETCH_DESCRIPTION: bool = _env_bool("LINKEDIN_FETCH_DESCRIPTION", True)
IS_REMOTE: bool = _env_bool("IS_REMOTE", True)
SCRAPE_COOLDOWN_MINUTES: float = _env_float("SCRAPE_COOLDOWN_MINUTES", 0.5)

# Optional proxy list, comma-separated, e.g. http://user:pass@host:port
PROXIES: List[str] = _env_list("PROXIES", [])

# --------------------------------------------------------------------------- #
# Screening defaults (runtime-editable via the settings table)
# --------------------------------------------------------------------------- #
EXPERIENCE_MIN_YEARS: int = _env_int("EXPERIENCE_MIN_YEARS", 0)
# Negative or empty = open-ended ("min and up").
EXPERIENCE_MAX_YEARS: Optional[int] = _env_int_or_none("EXPERIENCE_MAX_YEARS", 2)
SCORE_THRESHOLD: int = _env_int("SCORE_THRESHOLD", 55)

# --------------------------------------------------------------------------- #
# AI provider (OpenAI-compatible; default NVIDIA NIM)
# --------------------------------------------------------------------------- #
AI_PROVIDER: str = "nvidia"
AI_MODEL: str = os.environ.get("AI_MODEL", "nvidia/nemotron-3-super-120b-a12b")
AI_BASE_URL: str = os.environ.get("AI_BASE_URL", "https://integrate.api.nvidia.com/v1")
# API key is read from NVIDIA_API_KEY by the client (never stored here).

# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
DATA_DIR: Path = Path(os.environ.get("JOBSPY_DATA_DIR", str(PROJECT_ROOT / "data")))
DB_PATH: Path = DATA_DIR / "jobspy-lite.db"
EXCEL_PATH: Path = DATA_DIR / "jobs.xlsx"

# --------------------------------------------------------------------------- #
# Notifications (Telegram)
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

# AI client tuning
AI_RATE_LIMIT_PER_MIN: int = _env_int("AI_RATE_LIMIT_PER_MIN", 30)

# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
MAX_WORKERS: int = _env_int("MAX_WORKERS", 4)


def agent_defaults() -> Dict[str, Any]:
    """The AI agent parameter subset, as stored/served for the settings UI."""
    return {
        "ai_model": AI_MODEL,
        "ai_base_url": AI_BASE_URL,
        "ai_rate_limit_per_min": AI_RATE_LIMIT_PER_MIN,
        "rewrite_model": "",
        "rewrite_base_url": "",
        "rewrite_max_tries": 3,
    }


def scrape_defaults() -> Dict[str, Any]:
    """The scrape parameter subset, as stored/served for the settings UI."""
    return {
        "sites": list(SITES),
        "search_terms": list(SEARCH_TERMS),
        "location": LOCATION,
        "results_wanted": RESULTS_WANTED,
        "hours_old": HOURS_OLD,
        "country_indeed": COUNTRY_INDEED,
        "linkedin_fetch_description": LINKEDIN_FETCH_DESCRIPTION,
        "is_remote": IS_REMOTE,
        "scrape_cooldown_minutes": SCRAPE_COOLDOWN_MINUTES,
    }


def load_config() -> Dict[str, Any]:
    """Everything the scraper and the AI agent need, as one dict."""
    return {
        # scraper
        **scrape_defaults(),
        **agent_defaults(),
        "proxies": list(PROXIES),
        # experience filter
        "experience_min_years": EXPERIENCE_MIN_YEARS,
        "experience_max_years": EXPERIENCE_MAX_YEARS,
        # ai
        "ai_provider": AI_PROVIDER,
        # pipeline
        "max_workers": MAX_WORKERS,
    }
