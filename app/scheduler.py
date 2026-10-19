"""Cron scheduler (APScheduler) driving unattended pipeline runs.

Two config modes:
  - ``interval``: every ``interval_minutes`` minutes between ``active_from``
    and ``active_to`` (hour-of-day window, e.g. 8-22).
  - ``cron``: raw 5-field cron expression (``minute hour day month weekday``).

The scheduler lives inside the FastAPI process. Re-applying the config
re-registers the trigger live; start/stop is exposed to the UI. Runs execute
on a single worker (``max_instances=1``) so a slow LLM run never stacks, with
a misfire grace period so a briefly-busy process still catches up.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger  # noqa: F401 (kept for API compat)

from . import storage
from .config import SCHEDULER_DEFAULTS

logger = logging.getLogger(__name__)

# Hours and cron fields are interpreted in ``timezone``.
DEFAULT_CONFIG: Dict[str, Any] = dict(SCHEDULER_DEFAULTS)

SCHEDULER_JOB_ID = "jobspy-lite-pipeline"

_scheduler: Optional[BackgroundScheduler] = None
_lock = threading.Lock()
_last_result: Optional[Dict[str, Any]] = None


# --------------------------------------------------------------------------- #
# Config persistence
# --------------------------------------------------------------------------- #
def get_config() -> Dict[str, Any]:
    """Stored scheduler config merged over the defaults."""
    stored = storage.get_setting("scheduler_config", {}) or {}
    config = {**DEFAULT_CONFIG, **stored}
    return config


def validate_config(config: Dict[str, Any]) -> Optional[str]:
    """Return an error string for invalid config, or None when valid."""
    mode = config.get("mode")
    if mode not in ("interval", "cron"):
        return "mode must be 'interval' or 'cron'."
    try:
        ZoneInfo(str(config.get("timezone", "UTC")))
    except Exception:
        return "timezone must be a valid IANA name (e.g. Asia/Kolkata)."
    if mode == "interval":
        try:
            minutes = int(config.get("interval_minutes", 0))
        except (TypeError, ValueError):
            return "interval_minutes must be an integer."
        if minutes < 5:
            return "interval_minutes must be >= 5 (be kind to the job boards)."
        try:
            start = int(config.get("active_from", 0))
            end = int(config.get("active_to", 24))
        except (TypeError, ValueError):
            return "active_from/active_to must be integers (hours)."
        if not (0 <= start <= 23 and 1 <= end <= 24 and start < end):
            return "active hours must satisfy 0 <= from < to <= 24."
    else:
        expression = str(config.get("cron_expression", "")).strip()
        if not expression:
            return "cron_expression is required in cron mode."
        try:
            CronTrigger.from_crontab(expression)
        except ValueError as exc:
            return f"Invalid cron expression: {exc}"
    return None


def set_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + persist a new scheduler config and re-register the trigger.

    Partial updates are allowed: the incoming keys are merged over the stored
    config and the MERGED result is validated, so ``{"enabled": false}`` or
    ``{"interval_minutes": 90}`` alone are valid.
    """
    merged = {**get_config(), **config}
    error = validate_config(merged)
    if error:
        raise ValueError(error)
    storage.set_setting("scheduler_config", merged)
    apply_config(merged)
    return merged


# --------------------------------------------------------------------------- #
# Trigger construction
# --------------------------------------------------------------------------- #
def _tz(config: Dict[str, Any]) -> ZoneInfo:
    """The configured display/schedule timezone (default Asia/Kolkata)."""
    return ZoneInfo(str(config.get("timezone", "Asia/Kolkata")))


def _next_window_start(config: Dict[str, Any]) -> datetime:
    """Today's active_from in the config tz, or tomorrow if it already passed."""
    tz = _tz(config)
    start_hour = int(config.get("active_from", 0))
    now = datetime.now(tz)
    anchor = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if anchor <= now:
        anchor += timedelta(days=1)
    return anchor


class DailyWindowTrigger(BaseTrigger):
    """Fire every ``interval_minutes`` minutes, re-anchored to ``active_from``
    local time each day, only inside the active window.

    active_from=8, active_to=22, interval=110 →
      day 1: 8:00, 9:50, 11:40, 13:30, 15:20, 17:10, 19:00, 20:50
      day 2: 8:00, 9:50, …   (re-anchored — no overnight drift)
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.tz = _tz(config)
        self.interval = timedelta(minutes=int(config["interval_minutes"]))
        self.start_hour = int(config.get("active_from", 0))
        self.end_hour = int(config.get("active_to", 24))

    def _day_anchor(self, reference: datetime) -> datetime:
        """8:00 (start_hour) local time on the reference's own calendar day."""
        local = reference.astimezone(self.tz)
        return local.replace(
            hour=self.start_hour, minute=0, second=0, microsecond=0
        )

    def get_next_fire_time(
        self, previous_fire_time, now: datetime
    ) -> Optional[datetime]:
        now_local = now.astimezone(self.tz)
        anchor = self._day_anchor(now if previous_fire_time is None else now)

        # Candidate: next multiple of the interval after `now` within today's
        # chain (anchor, anchor+interval, …); else tomorrow's anchor.
        candidate = anchor
        while candidate <= now_local:
            candidate += self.interval

        end_of_day = anchor.replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
        if self.end_hour < 24:
            end_of_day = anchor.replace(
                hour=self.end_hour, minute=0, second=0, microsecond=0
            )
        if candidate < end_of_day:
            return candidate
        next_anchor = anchor + timedelta(days=1)
        return next_anchor


def _build_trigger(config: Dict[str, Any]):
    """Build the APScheduler trigger for a validated config."""
    if config["mode"] == "interval":
        return DailyWindowTrigger(config)
    return CronTrigger.from_crontab(
        str(config["cron_expression"]), timezone=_tz(config)
    )



def _hour_window_ok(config: Dict[str, Any], now_hour: int) -> bool:
    """Interval-mode active-hours gate (cron mode handles its own hours)."""
    if config["mode"] != "interval":
        return True
    start = int(config.get("active_from", 0))
    end = int(config.get("active_to", 24))
    if end >= 24:
        return now_hour >= start
    return start <= now_hour < end


# --------------------------------------------------------------------------- #
# Scheduler lifecycle
# --------------------------------------------------------------------------- #
def _run_job() -> None:
    """The scheduled callable: one pipeline run, guarded by the hour window."""
    global _last_result
    config = get_config()
    # Window hours are in the CONFIG timezone (user-local), never the
    # server's local zone (UTC on cloud VMs) — that mismatch let runs fire
    # in the middle of the user's night.
    now_hour = datetime.now(_tz(config)).hour
    if not _hour_window_ok(config, now_hour):
        logger.info("Scheduler tick skipped (outside %s:00-%s:00 active window).",
                    config.get("active_from"), config.get("active_to"))
        return
    from .pipeline import run_pipeline

    logger.info("Scheduled pipeline run starting (trigger=cron).")
    _last_result = run_pipeline(trigger="cron")


def _get_scheduler() -> BackgroundScheduler:
    global _scheduler
    with _lock:
        if _scheduler is None:
            _scheduler = BackgroundScheduler(timezone="UTC")
        return _scheduler


def _jobspec(config: Dict[str, Any]) -> Dict[str, Any]:
    """Common add_job kwargs (one source of truth for start/apply)."""
    return {
        "trigger": _build_trigger(config),
        "id": SCHEDULER_JOB_ID,
        "max_instances": 1,
        "misfire_grace_time": 300,
        "coalesce": True,
        "replace_existing": True,
    }


def start() -> None:
    """Start the scheduler and (re)register the pipeline job from stored config."""
    scheduler = _get_scheduler()
    config = get_config()
    with _lock:
        if scheduler.get_job(SCHEDULER_JOB_ID) is not None:
            scheduler.remove_job(SCHEDULER_JOB_ID)
        scheduler.add_job(_run_job, **_jobspec(config))
        if not scheduler.running:
            scheduler.start()
    logger.info("Scheduler started with config: %s", config)


def stop() -> None:
    """Stop the scheduler and drop the registered job (config is kept)."""
    global _scheduler
    with _lock:
        if _scheduler is not None:
            if _scheduler.get_job(SCHEDULER_JOB_ID) is not None:
                _scheduler.remove_job(SCHEDULER_JOB_ID)
            if _scheduler.running:
                _scheduler.shutdown(wait=False)
            _scheduler = None
    logger.info("Scheduler stopped.")


def apply_config(config: Dict[str, Any]) -> None:
    """Re-register the trigger from ``config`` when the scheduler is running."""
    scheduler = _get_scheduler()
    with _lock:
        if scheduler.running:
            if scheduler.get_job(SCHEDULER_JOB_ID) is not None:
                scheduler.remove_job(SCHEDULER_JOB_ID)
            scheduler.add_job(_run_job, **_jobspec(config))


def toggle(enabled: bool) -> Dict[str, Any]:
    """Persist + apply the enabled flag; start/stop the scheduler to match."""
    merged = set_config({"enabled": enabled})
    if enabled:
        start()
    else:
        stop()
    return merged


def status() -> Dict[str, Any]:
    """Everything the UI scheduler card needs."""
    scheduler = _get_scheduler()
    with _lock:
        job = scheduler.get_job(SCHEDULER_JOB_ID) if scheduler.running else None
        next_run = None
        if job is not None and job.next_run_time is not None:
            next_run = job.next_run_time.isoformat()
        running = scheduler.running
    config = get_config()
    return {
        "running": running,
        "enabled": bool(config.get("enabled", False)),
        "config": config,
        "next_run_at": next_run,
        "last_run": _last_result,
    }


def is_healthy() -> bool:
    """The scheduler counts as healthy when it is running with a job queued."""
    scheduler = _get_scheduler()
    with _lock:
        return bool(
            scheduler.running
            and scheduler.get_job(SCHEDULER_JOB_ID) is not None
        )
