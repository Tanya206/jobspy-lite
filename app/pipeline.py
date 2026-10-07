"""Pipeline: one full run of JobSpy Lite.

Sequence (each run, cron or manual):
  1. Scrape every configured search term/site (config + settings overrides).
  2. Upsert raw jobs so nothing is lost, and detect which are new.
  3. Score only what needs scoring:
     - new jobs, or rows with no score yet, or rows whose stored resume hash
       predates the current resume (resume changed since scoring), or rows
       last scored more than RESCORE_AFTER_HOURS ago, or ``force_rescore``.
  4. Persist annotations (required years, experience match, score, skills);
     user status/notified are preserved by storage.upsert_job. Jobs whose
     required experience is outside the window are auto-rejected (status
     ``rejected``) unless the user already triaged them.
  5. Export the jobs table to Excel and send Telegram alerts for
     above-threshold matches.
  6. Record everything on a ``runs`` row.
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import notify, storage
from .agent.ai_job_agent import AIJobAgent
from .scraper import JobScraper
from .util import make_json_safe

logger = logging.getLogger(__name__)

# A scored job is re-scored only after this many hours (env-tunable). Set 0
# to disable periodic re-scoring entirely (resume change still triggers it).
RESCORE_AFTER_HOURS = int(os.environ.get("RESCORE_AFTER_HOURS", "72"))

# One pipeline run at a time, process-wide. The scheduler guard
# (max_instances=1) and the API's 409 only protect their own path; this lock
# also stops cron and manual runs from overlapping (duplicate scrapes + the
# same new job scored twice).
_PIPELINE_LOCK = threading.Lock()


def _experience_matches(
    required: Optional[Dict[str, Any]],
    min_years: Optional[int],
    max_years: Optional[int],
) -> bool:
    """True when a posting's required-years window overlaps the target window.

    Same rule as ``AIJobAgent._experience_matches`` — kept here as a module
    function so the pipeline does not reach through the agent class.
    """
    if min_years is None and max_years is None:
        return True
    if required is None:
        return False
    user_min = min_years or 0
    job_min, job_max = required.get("min"), required.get("max")
    below = max_years is not None and (job_min or 0) > max_years
    above = job_max is not None and job_max < user_min
    return not (below or above)


class PipelineContext:
    """Everything one pipeline run needs, resolved from config + settings."""

    def __init__(self) -> None:
        from .config import load_config

        self.config = load_config()
        self.settings = storage.all_settings()
        # Scrape + agent params: stored settings override config.py/env
        # defaults (the dashboard edits them via POST /settings).
        stored_scrape = self.settings.get("scrape_params") or {}
        stored_agent = self.settings.get("agent_params") or {}
        self.scrape_params: Dict[str, Any] = {**self.config, **stored_scrape}
        self.config = {**self.config, **stored_agent}
        self.experience_min: int = self.settings.get(
            "experience_min_years", self.config["experience_min_years"]
        )
        stored_max = self.settings.get("experience_max_years")
        if stored_max is None:
            self.experience_max = self.config["experience_max_years"]
        else:
            # Stored 0/-1 means "open-ended upper bound".
            self.experience_max = None if stored_max <= 0 else stored_max
        self.score_threshold: int = self.settings.get(
            "score_threshold", self.config.get("score_threshold", 70)
        )
        # Runtime-editable AI knobs (dashboard → AI agent panel).
        self.ai_rate_limit_per_min: int = self.config.get("ai_rate_limit_per_min", 30)
        self.max_workers: int = self.config.get("max_workers", 4)
        self.force_rescore: bool = bool(self.settings.get("force_rescore", False))
        resume = self.settings.get("resume") or {}
        self.resume_text: str = resume.get("text", "")
        self.resume_hash: str = resume.get("hash", "")


def _scrape(context: PipelineContext, progress_callback=None) -> List[Dict[str, Any]]:
    scraper = JobScraper(dict(context.scrape_params))
    jobs = scraper.scrape(progress_callback=progress_callback)
    return [make_json_safe(job) for job in jobs]


class ScoreFailure(Exception):
    """Both AI agents failed for one job after in-call retries."""


def _screen_one(
    agent: AIJobAgent,
    job: Dict[str, Any],
    context: PipelineContext,
) -> Dict[str, Any]:
    """Run both agents for one posting and return the annotated copy.

    Agent 1 estimates required experience and, only when it matches the
    window, Agent 2 scores resume suitability (same two-call pattern as the
    original project).

    Raises ScoreFailure when an agent call fails — the caller records the
    attempt so a later run retries the job. Failures are NEVER written as
    fake verdicts ("outside experience window" / "unknown"), which used to
    permanently misclassify real postings.
    """
    required = None
    try:
        required = agent._estimate_required_years(job)
    except Exception as exc:
        raise ScoreFailure(f"experience agent: {exc}") from exc

    matched = _experience_matches(
        required, context.experience_min, context.experience_max
    )
    annotated = dict(job)
    annotated["required_years"] = required
    annotated["experience_match"] = matched
    if matched:
        try:
            suitability = agent.assess_suitability(job, context.resume_text)
        except Exception as exc:
            raise ScoreFailure(f"suitability agent: {exc}") from exc
        annotated["score"] = suitability.get("score")
        annotated["final_score"] = suitability.get("final_score")
        annotated["verdict"] = suitability.get("verdict")
        annotated["matched_skills"] = suitability.get("matched_skills") or []
        annotated["missing_skills"] = suitability.get("missing_skills") or []
        annotated["reasoning"] = suitability.get("reasoning") or ""
    else:
        annotated["score"] = None
        annotated["final_score"] = None
        annotated["verdict"] = "outside experience window"
        annotated["matched_skills"] = []
        annotated["missing_skills"] = []
        annotated["reasoning"] = "Required experience is outside the selected window."
        # Flag for the scoring loop's auto-reject rule. Set ONLY in this
        # genuine-mismatch branch — never when the estimate itself failed.
        annotated["auto_rejected"] = True
    annotated["scored_with_resume"] = context.resume_hash or None
    return annotated


def _needs_scoring(row: Optional[Dict[str, Any]], context: PipelineContext) -> bool:
    """Decide whether a posting needs (re-)scoring this run.

    A row needs scoring when it is new/unscored, was scored against a
    different resume, is older than RESCORE_AFTER_HOURS, or the operator
    forced a rescore.
    """
    if context.force_rescore:
        return True
    if row is None:
        return True
    # A previous AI failure with attempts exhausted is left alone (its error
    # stays visible in the UI); with attempts left it retries via
    # needs_score_retry in the run loop.
    if (
        row.get("score_error")
        and (row.get("score_attempts") or 0) >= storage.MAX_SCORE_ATTEMPTS
    ):
        return False
    if row.get("score") is None and row.get("required_years_min") is None:
        return True
    # Self-heal rows poisoned by the old bug that persisted a fake
    # "unknown" verdict when the suitability agent failed: they look scored
    # but have no score, so re-run them instead of waiting out the 72h
    # rescore window.
    if row.get("score") is None and (row.get("verdict") or "").lower() == "unknown":
        return True
    if context.resume_hash and row.get("scored_with_resume") != context.resume_hash:
        return True
    scored_at = row.get("scored_at")
    if not scored_at:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(scored_at)
    except ValueError:
        return True
    if RESCORE_AFTER_HOURS <= 0:
        return False
    return age > timedelta(hours=RESCORE_AFTER_HOURS)


def run_pipeline(
    trigger: str = "manual",
    force_rescore: bool = False,
    progress_callback=None,
) -> Dict[str, Any]:
    """Execute one full run; refuse (status ``skipped``) if one is in progress."""
    if not _PIPELINE_LOCK.acquire(blocking=False):
        logger.warning(
            "Run skipped (%s) — another pipeline run is already in progress.", trigger
        )
        return {
            "run_id": None,
            "trigger": trigger,
            "status": "skipped",
            "jobs_scraped": 0,
            "new_jobs": 0,
            "scored": 0,
            "auto_rejected": 0,
            "alerts_sent": 0,
            "duration_s": 0.0,
            "error": "Another pipeline run is already in progress.",
        }
    try:
        return _execute_pipeline(trigger, force_rescore, progress_callback)
    finally:
        _PIPELINE_LOCK.release()


def _execute_pipeline(
    trigger: str = "manual",
    force_rescore: bool = False,
    progress_callback=None,
) -> Dict[str, Any]:
    """One full pipeline pass (caller must hold _PIPELINE_LOCK).

    Trigger is ``cron`` or ``manual``; ``force_rescore`` bypasses the
    scoring-skip rules for this run only.
    """
    context = PipelineContext()
    if force_rescore:
        context.force_rescore = True

    run_id = storage.create_run(trigger)
    started = time.monotonic()
    summary: Dict[str, Any] = {
        "run_id": run_id,
        "trigger": trigger,
        "status": "failed",
        "jobs_scraped": 0,
        "new_jobs": 0,
        "scored": 0,
        "auto_rejected": 0,
        "alerts_sent": 0,
        "duration_s": None,
        "error": None,
    }

    def _report(message: str, percent: Optional[float] = None) -> None:
        if progress_callback is not None:
            progress_callback(message, percent)

    try:
        # Close out runs left 'running' by a crash/earlier deploy.
        reaped = storage.reap_stale_runs()
        if reaped:
            logger.info("Reaped %d stale run row(s) from interrupted processes.", reaped)

        if not context.resume_text:
            logger.warning("No resume stored — run aborted; upload a resume first.")
            summary["error"] = "No resume stored. Upload a resume before running."
            storage.finish_run(
                run_id, "failed", jobs_scraped=0, new_jobs=0, scored=0,
                alerts_sent=0, error=summary["error"],
            )
            return summary

        # ---- 1. Scrape -------------------------------------------------- #
        _report("Scraping job boards…", 0)

        # Stream the scraper's per-(location, term) messages so a
        # comma-separated location shows "Scraping 'x' in Hyderabad (1/4)"
        # instead of a silent multi-city crawl. Scrape owns 0–10% of the bar.
        def _scrape_progress(completed: int, total: int, message: str) -> None:
            _report(message, (completed / total) * 10 if total else 0)

        scraped = _scrape(context, progress_callback=_scrape_progress)

        # ---- 2. Persist raw + detect new + pick up AI retries ----------- #
        _report(f"Persisting {len(scraped)} scraped postings…", 10)
        new_count = 0
        to_score: List[Tuple[str, Dict[str, Any]]] = []
        retry_count = 0
        for raw_job in scraped:
            key = storage.upsert_job(raw_job)
            existing = storage.get_job(key)
            if existing.get("first_seen_at") == existing.get("last_seen_at"):
                new_count += 1
            retrying = storage.needs_score_retry(existing)
            if retrying:
                retry_count += 1
            if _needs_scoring(existing, context) or retrying:
                to_score.append((key, existing))
        if retry_count:
            _report(f"{retry_count} failed scoring attempt(s) being retried…", 20)

        # ---- 3+4. Score fresh postings concurrently ---------------------- #
        _report(f"{len(to_score)} postings need scoring…", 25)
        scored_count = 0
        failed_count = 0
        auto_rejected = 0
        if to_score:
            agent = AIJobAgent(context.config)
            from .agent.rate_limiter import NVIDIA_RATE_LIMITER

            if (
                context.ai_rate_limit_per_min > 0
                and NVIDIA_RATE_LIMITER.max_calls != context.ai_rate_limit_per_min
            ):
                NVIDIA_RATE_LIMITER.max_calls = context.ai_rate_limit_per_min
                logger.info(
                    "AI rate limit set to %d calls/minute", context.ai_rate_limit_per_min
                )
            max_workers = min(
                context.max_workers or 4, max(1, len(to_score))
            )
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(_screen_one, agent, job, context): key
                    for key, job in to_score
                }
                for future in as_completed(futures):
                    key = futures[future]
                    try:
                        annotated = future.result()
                    except ScoreFailure as exc:
                        failed_count += 1
                        logger.error("Scoring failed for %s: %s", key, exc)
                        storage.record_score_failure(key, str(exc))
                        continue
                    except Exception as exc:  # noqa: BLE001 - per-job failure
                        failed_count += 1
                        logger.exception("Screening failed for %s", key)
                        storage.record_score_failure(key, repr(exc))
                        continue
                    storage.upsert_job(annotated, job_key_value=key)
                    # Auto-reject genuine experience mismatches. only_if
                    # status='new' keeps the user in charge: an accepted or
                    # manually rejected posting is never re-classified, and a
                    # mismatch the user accepted stays accepted.
                    if annotated.get("auto_rejected") and storage.set_job_status(
                        key, "rejected", only_if_status="new"
                    ):
                        auto_rejected += 1
                        logger.info(
                            "Auto-rejected %s — required experience %s is "
                            "outside the window.",
                            key,
                            annotated.get("required_years"),
                        )
                    if annotated.get("score") is not None or annotated.get("verdict"):
                        storage.clear_score_failure(key)
                    scored_count += 1
                    done = 25 + int(70 * scored_count / len(to_score))
                    _report(f"Scored {scored_count}/{len(to_score)}…", min(done, 95))
        if failed_count:
            logger.warning(
                "%d job(s) failed AI scoring this run; they retry automatically "
                "on the next run (up to %d attempts).",
                failed_count, storage.MAX_SCORE_ATTEMPTS,
            )

        # ---- 5. Export + notify ------------------------------------------ #
        _report("Exporting to Excel…", 96)
        from .excel import export_excel

        export_excel()

        _report("Checking notifications…", 97)
        alerts = notify.notify_above_threshold(context.score_threshold)

        summary.update(
            status="completed",
            jobs_scraped=len(scraped),
            new_jobs=new_count,
            scored=scored_count,
            auto_rejected=auto_rejected,
            alerts_sent=alerts,
        )
        storage.finish_run(
            run_id,
            "completed",
            jobs_scraped=len(scraped),
            new_jobs=new_count,
            scored=scored_count,
            alerts_sent=alerts,
        )
        _report("Done.", 100)
    except Exception as exc:  # noqa: BLE001 - record failure on the run row
        logger.exception("Pipeline run failed")
        summary["error"] = str(exc)
        storage.finish_run(run_id, "failed", error=str(exc))

    summary["duration_s"] = round(time.monotonic() - started, 1)
    return summary
