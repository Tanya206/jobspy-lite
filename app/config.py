"""Settings for JobSpy Lite.

Non-secret values come from ``config.yaml`` at the project root. Secrets
(API keys, the Telegram token and chat id, the OTEL auth header, the API
token) come from ``.env``. The dashboard may store overrides in SQLite;
those replace the file on the next run.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
load_dotenv(PROJECT_ROOT / ".env")


# Known provider endpoints. config.yaml selects one and may override the model
# or base URL. Keys are never stored here.
NVIDIA_MODEL = "nvidia/nemotron-3-super-120b-a12b"
OPENAI_MODEL = "gpt-4.1-mini"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"

PROVIDER_PRESETS: Dict[str, Dict[str, str]] = {
    "nvidia": {"ai_model": NVIDIA_MODEL, "ai_base_url": NVIDIA_BASE_URL},
    "openai": {"ai_model": OPENAI_MODEL, "ai_base_url": OPENAI_BASE_URL},
}

# Used only when config.yaml omits a key.
_FALLBACKS: Dict[str, Any] = {
    "ai": {
        "provider": "nvidia",
        "model": "",
        "base_url": "",
        "rate_limit_per_min": 30,
        "max_workers": 4,
        "timeout_seconds": 90,
        "rewrite": {"model": "", "base_url": "", "max_tries": 3, "timeout_seconds": 180},
        "checker": {"model": "", "base_url": ""},
    },
    "scrape": {
        "sites": ["linkedin"],
        "search_terms": ["AI Engineer"],
        "location": "Bengaluru",
        "results_wanted": 600,
        "hours_old": 2,
        "country_indeed": "india",
        "linkedin_fetch_description": True,
        "is_remote": True,
        "scrape_cooldown_minutes": 0.5,
        "proxies": [],
    },
    "screening": {
        "experience_min_years": 0,
        "experience_max_years": 2,
        "score_threshold": 70,
        "retention_days": 0,
    },
    "pipeline": {"rescore_after_hours": 72},
    "storage": {"data_dir": "data"},
    "scheduler": {
        "mode": "interval",
        "interval_minutes": 120,
        "active_from": 8,
        "active_to": 22,
        "cron_expression": "0 */2 * * *",
        "timezone": "Asia/Kolkata",
        "enabled": False,
    },
    "tracing": {"otel_endpoint": ""},
}


def clamp_int(value: Any, low: int, high: int, default: int) -> int:
    """Clamp ``value`` into ``[low, high]``; ``default`` when it is not an int."""
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default


def normalize_provider(value: str) -> str:
    """Return ``openai`` or ``nvidia``. Empty when the value is neither."""
    raw = (value or "").strip().lower().replace("-", "_")
    if raw in ("openai", "open_ai"):
        return "openai"
    if raw in ("nvidia", "nim", "nvidia_nim"):
        return "nvidia"
    return ""


def provider_for_base_url(base_url: str) -> str:
    """Which known host ``base_url`` points at, or empty for a custom host."""
    host = (base_url or "").lower()
    if "api.openai.com" in host:
        return "openai"
    if "api.nvidia.com" in host:
        return "nvidia"
    return ""


def _section(data: Mapping[str, Any], name: str) -> Dict[str, Any]:
    raw = data.get(name)
    file_section = raw if isinstance(raw, dict) else {}
    base = _FALLBACKS[name]
    return {**base, **file_section}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_int_or_none(value: Any, default: Optional[int]) -> Optional[int]:
    """Negative means no upper bound. A blank value keeps ``default``."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return None if number < 0 else number


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_list(value: Any, default: List[str]) -> List[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return list(default)


def _as_str(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def resolve_settings(data: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Resolve a config.yaml mapping. Missing keys use the built-in fallbacks."""
    data = data or {}
    ai = _section(data, "ai")
    rewrite = ai.get("rewrite") if isinstance(ai.get("rewrite"), dict) else {}
    rewrite = {**_FALLBACKS["ai"]["rewrite"], **rewrite}
    checker = ai.get("checker") if isinstance(ai.get("checker"), dict) else {}
    checker = {**_FALLBACKS["ai"]["checker"], **checker}
    scrape = _section(data, "scrape")
    screening = _section(data, "screening")
    pipeline = _section(data, "pipeline")
    storage = _section(data, "storage")
    scheduler = _section(data, "scheduler")
    tracing = _section(data, "tracing")

    provider = normalize_provider(_as_str(ai.get("provider"), "nvidia")) or "nvidia"
    preset = PROVIDER_PRESETS[provider]
    model = _as_str(ai.get("model"), "") or preset["ai_model"]
    base_url = _as_str(ai.get("base_url"), "") or preset["ai_base_url"]
    checker_model = _as_str(checker.get("model"), "") or model
    checker_base = _as_str(checker.get("base_url"), "") or base_url

    return {
        "ai_provider": provider,
        "ai_model": model,
        "ai_base_url": base_url,
        "ai_rate_limit_per_min": _as_int(ai.get("rate_limit_per_min"), 30),
        "ai_timeout_seconds": _as_float(ai.get("timeout_seconds"), 90.0),
        "max_workers": _as_int(ai.get("max_workers"), 4),
        "rewrite_model": _as_str(rewrite.get("model"), ""),
        "rewrite_base_url": _as_str(rewrite.get("base_url"), ""),
        "rewrite_max_tries": _as_int(rewrite.get("max_tries"), 3),
        "rewrite_timeout_seconds": _as_float(rewrite.get("timeout_seconds"), 180.0),
        "checker_model": checker_model,
        "checker_base_url": checker_base,
        "sites": _as_list(scrape.get("sites"), ["linkedin"]),
        "search_terms": _as_list(scrape.get("search_terms"), ["AI Engineer"]),
        "location": _as_str(scrape.get("location"), "Bengaluru"),
        "results_wanted": _as_int(scrape.get("results_wanted"), 600),
        "hours_old": _as_int(scrape.get("hours_old"), 2),
        "country_indeed": _as_str(scrape.get("country_indeed"), "india"),
        "linkedin_fetch_description": _as_bool(scrape.get("linkedin_fetch_description"), True),
        "is_remote": _as_bool(scrape.get("is_remote"), True),
        "scrape_cooldown_minutes": _as_float(scrape.get("scrape_cooldown_minutes"), 0.5),
        "proxies": _as_list(scrape.get("proxies"), []),
        "experience_min_years": _as_int(screening.get("experience_min_years"), 0),
        "experience_max_years": _as_int_or_none(screening.get("experience_max_years"), 2),
        "score_threshold": _as_int(screening.get("score_threshold"), 70),
        "retention_days": max(0, _as_int(screening.get("retention_days"), 0)),
        "rescore_after_hours": _as_int(pipeline.get("rescore_after_hours"), 72),
        "data_dir": _as_str(storage.get("data_dir"), "data"),
        "scheduler": {
            "mode": _as_str(scheduler.get("mode"), "interval"),
            "interval_minutes": _as_int(scheduler.get("interval_minutes"), 120),
            "active_from": _as_int(scheduler.get("active_from"), 8),
            "active_to": _as_int(scheduler.get("active_to"), 22),
            "cron_expression": _as_str(scheduler.get("cron_expression"), "0 */2 * * *"),
            "timezone": _as_str(scheduler.get("timezone"), "Asia/Kolkata"),
            "enabled": _as_bool(scheduler.get("enabled"), False),
        },
        "otel_endpoint": _as_str(tracing.get("otel_endpoint"), ""),
    }


def load_config_file(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Read config.yaml. A missing or empty file uses the fallbacks."""
    if not path.is_file():
        logger.warning("config.yaml missing at %s; using built-in fallbacks", path)
        return resolve_settings({})
    with path.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{path} must be a mapping")
    return resolve_settings(loaded)


_SETTINGS = load_config_file()

AI_PROVIDER: str = _SETTINGS["ai_provider"]
AI_MODEL: str = _SETTINGS["ai_model"]
AI_BASE_URL: str = _SETTINGS["ai_base_url"]
AI_RATE_LIMIT_PER_MIN: int = _SETTINGS["ai_rate_limit_per_min"]
AI_TIMEOUT_SECONDS: float = _SETTINGS["ai_timeout_seconds"]
MAX_WORKERS: int = _SETTINGS["max_workers"]
REWRITE_MODEL: str = _SETTINGS["rewrite_model"]
REWRITE_BASE_URL: str = _SETTINGS["rewrite_base_url"]
REWRITE_MAX_TRIES: int = _SETTINGS["rewrite_max_tries"]
REWRITE_TIMEOUT_SECONDS: float = _SETTINGS["rewrite_timeout_seconds"]
CHECKER_MODEL: str = _SETTINGS["checker_model"]
CHECKER_BASE_URL: str = _SETTINGS["checker_base_url"]

SITES: List[str] = _SETTINGS["sites"]
SEARCH_TERMS: List[str] = _SETTINGS["search_terms"]
LOCATION: str = _SETTINGS["location"]
RESULTS_WANTED: int = _SETTINGS["results_wanted"]
HOURS_OLD: int = _SETTINGS["hours_old"]
COUNTRY_INDEED: str = _SETTINGS["country_indeed"]
LINKEDIN_FETCH_DESCRIPTION: bool = _SETTINGS["linkedin_fetch_description"]
IS_REMOTE: bool = _SETTINGS["is_remote"]
SCRAPE_COOLDOWN_MINUTES: float = _SETTINGS["scrape_cooldown_minutes"]
PROXIES: List[str] = _SETTINGS["proxies"]

EXPERIENCE_MIN_YEARS: int = _SETTINGS["experience_min_years"]
EXPERIENCE_MAX_YEARS: Optional[int] = _SETTINGS["experience_max_years"]
SCORE_THRESHOLD: int = _SETTINGS["score_threshold"]
RETENTION_DAYS: int = _SETTINGS["retention_days"]
RESCORE_AFTER_HOURS: int = _SETTINGS["rescore_after_hours"]

_data_dir = Path(_SETTINGS["data_dir"])
DATA_DIR: Path = _data_dir if _data_dir.is_absolute() else PROJECT_ROOT / _data_dir
DB_PATH: Path = DATA_DIR / "jobspy-lite.db"
EXCEL_PATH: Path = DATA_DIR / "jobs.xlsx"

SCHEDULER_DEFAULTS: Dict[str, Any] = dict(_SETTINGS["scheduler"])
OTEL_ENDPOINT: str = _SETTINGS["otel_endpoint"]

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")


def resolve_api_key(base_url: str = "", explicit: Any = None) -> Optional[str]:
    """API key for ``base_url``. An explicit key wins over the environment.

    OpenAI hosts use ``OPENAI_API_KEY``. NVIDIA hosts use ``NVIDIA_API_KEY``.
    Any other host follows the provider in config.yaml.
    """
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    kind = provider_for_base_url(base_url)
    if not kind:
        kind = AI_PROVIDER
    if kind == "openai":
        return os.environ.get("OPENAI_API_KEY") or None
    return os.environ.get("NVIDIA_API_KEY") or None


def agent_defaults() -> Dict[str, Any]:
    """The AI agent parameter subset, as stored/served for the settings UI."""
    return {
        "ai_provider": AI_PROVIDER,
        "ai_model": AI_MODEL,
        "ai_base_url": AI_BASE_URL,
        "ai_rate_limit_per_min": AI_RATE_LIMIT_PER_MIN,
        "timeout_seconds": AI_TIMEOUT_SECONDS,
        "rewrite_model": REWRITE_MODEL,
        "rewrite_base_url": REWRITE_BASE_URL,
        "rewrite_max_tries": REWRITE_MAX_TRIES,
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
        **scrape_defaults(),
        **agent_defaults(),
        "proxies": list(PROXIES),
        "experience_min_years": EXPERIENCE_MIN_YEARS,
        "experience_max_years": EXPERIENCE_MAX_YEARS,
        "score_threshold": SCORE_THRESHOLD,
        "retention_days": RETENTION_DAYS,
        "ai_provider": AI_PROVIDER,
        "max_workers": MAX_WORKERS,
    }
