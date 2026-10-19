"""Pipeline: one full run of JobSpy Lite.

Sequence (each run, cron or manual):
  1. Scrape every configured search term/site (config + settings overrides).
  2. Upsert raw jobs so nothing is lost, and detect which are new.
  3. Score fresh postings first (new, unscored, resume change, stale, or
     ``force_rescore``). Pending scoring failures are not in that batch.
  4. Then score the retry tail: stored new jobs that already failed and still
     have attempts left, oldest first, including ones this scrape did not
     return. A failure during step 3 waits for a later run. If step 3 stops
     early, step 4 is left for the next run.
  5. Persist annotations (required years, experience match, score, skills);
     user status/notified are preserved by storage.upsert_job. Jobs whose
     required experience is outside the window are auto-rejected (status
     ``rejected``) unless the user already triaged them; auto-rejects are
     flagged so the UI separates them from the user's own rejects.
  6. Prune postings not seen for ``retention_days`` (settings, 0 = never),
     export the jobs table to Excel and send Telegram alerts for
     above-threshold matches.
  7. Record everything on a ``runs`` row.
"""

import contextvars
import inspect
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import notify, storage
from .agent.ai_job_agent import AIJobAgent
from .agent.observability import AI_METRICS
from .agent.tracing import (
    annotate_pipeline,
    experience_match_span,
    pipeline_span,
    record_output,
    resume_match_span,
)
from .config import RESCORE_AFTER_HOURS, clamp_int
from .scraper import JobScraper, ScrapeResult
from .util import job_key, make_json_safe

logger = logging.getLogger(__name__)

# One pipeline run at a time, process-wide. The scheduler guard
# (max_instances=1) and the API's 409 only protect their own path; this lock
# also stops cron and manual runs from overlapping (duplicate scrapes + the
# same new job scored twice).
_PIPELINE_LOCK = threading.Lock()

# Set on process shutdown. In-flight scoring finishes the current job, then
# the pool stops taking new ones; retention, export, and notify still run.
_SHUTDOWN = threading.Event()

# Stop the rest of a run when the provider is clearly dead: the same
# permanent status (401/403) three times, or five scoring failures in a row.
PERMANENT_STATUSES = {401, 403}
PERMANENT_STOP_AFTER = 3
FAILURE_STREAK_STOP = 5


def request_shutdown() -> None:
    """Ask the in-flight pipeline to stop scoring and finish the tail."""
    _SHUTDOWN.set()


def shutdown_requested() -> bool:
    return _SHUTDOWN.is_set()


def wait_for_idle(timeout: float = 180) -> bool:
    """Block until no pipeline run holds the lock, or ``timeout`` seconds."""
    acquired = _PIPELINE_LOCK.acquire(timeout=timeout)
    if acquired:
        _PIPELINE_LOCK.release()
    return acquired


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
        merged_scrape: Dict[str, Any] = {**self.config, **stored_scrape}
        if "results_wanted" in merged_scrape:
            merged_scrape["results_wanted"] = clamp_int(
                merged_scrape.get("results_wanted"), 1, 200, 15
            )
        if "hours_old" in merged_scrape:
            merged_scrape["hours_old"] = clamp_int(
                merged_scrape.get("hours_old"), 1, 168, 24
            )
        self.scrape_params = merged_scrape
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
        # Retention: postings not seen for this many days are deleted at the
        # end of each run (0 = keep everything).
        try:
            self.retention_days: int = max(
                0, int(self.settings.get("retention_days", self.config.get("retention_days", 0)) or 0)
            )
        except (TypeError, ValueError):
            self.retention_days = 0
        # Runtime-editable AI knobs (dashboard → AI agent panel).
        self.ai_rate_limit_per_min: int = self.config.get("ai_rate_limit_per_min", 30)
        self.max_workers: int = clamp_int(self.config.get("max_workers", 4), 1, 8, 4)
        self.force_rescore: bool = bool(self.settings.get("force_rescore", False))
        resume = self.settings.get("resume") or {}
        self.resume_text: str = resume.get("text", "")
        self.resume_hash: str = resume.get("hash", "")


def _scrape(
    context: PipelineContext,
    progress_callback=None,
    on_batch=None,
    should_stop=None,
) -> ScrapeResult:
    """Run the scraper. Test doubles that return a bare list still work.

    ``on_batch`` is forwarded only when the scraper accepts it, so a fake
    ``scrape(self, progress_callback=None)`` is not broken. When it is
    forwarded, each successful pair is persisted before the next cooldown.
    """
    scraper = JobScraper(dict(context.scrape_params))
    fn = scraper.scrape
    kwargs: Dict[str, Any] = {"progress_callback": progress_callback}
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if on_batch is not None and "on_batch" in params:
        kwargs["on_batch"] = on_batch
    if should_stop is not None and "should_stop" in params:
        kwargs["should_stop"] = should_stop
    raw = fn(**kwargs)
    if isinstance(raw, ScrapeResult):
        raw.jobs = [make_json_safe(job) for job in raw.jobs]
        return raw
    jobs = list(raw or [])
    return ScrapeResult(
        jobs=[make_json_safe(job) for job in jobs],
        errors=[],
        pair_count=0,
        failed_pairs=0,
        streamed=False,
    )


class ScoreFailure(Exception):
    """An AI agent failed for one job after in-call retries."""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class ScoringSkipped(Exception):
    """This job was not scored because the run is stopping early."""


def _http_status(exc: BaseException) -> Optional[int]:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _screen_one(
    agent: AIJobAgent,
    job: Dict[str, Any],
    context: PipelineContext,
    stop_event: Optional[threading.Event] = None,
    key: str = "",
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
    if shutdown_requested() or (stop_event is not None and stop_event.is_set()):
        raise ScoringSkipped()
    required = None
    trace_key = key or job_key(job)
    try:
        with experience_match_span(trace_key, job) as span:
            required = agent._estimate_required_years(job)
            record_output(span, {"required_years": required})
    except Exception as exc:
        raise ScoreFailure(
            f"experience agent: {exc}", status=_http_status(exc)
        ) from exc

    matched = _experience_matches(
        required, context.experience_min, context.experience_max
    )
    annotated = dict(job)
    annotated["required_years"] = required
    annotated["experience_match"] = matched
    if matched:
        try:
            with resume_match_span(trace_key, job, context.resume_text) as span:
                suitability = agent.assess_suitability(job, context.resume_text)
                record_output(span, suitability)
        except Exception as exc:
            raise ScoreFailure(
                f"suitability agent: {exc}", status=_http_status(exc)
            ) from exc
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
    # stays visible in the UI). Attempts still left are not scored here:
    # the run queues them after fresh postings.
    if (
        row.get("score_error")
        and (row.get("score_attempts") or 0) >= storage.MAX_SCORE_ATTEMPTS
    ):
        return False
    if storage.needs_score_retry(row):
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


def _score_postings(
    jobs: List[Tuple[str, Dict[str, Any]]],
    *,
    agent: AIJobAgent,
    context: PipelineContext,
    report,
    progress_base: float,
    progress_span: float,
) -> Dict[str, Any]:
    """Score one batch. Returns counts and whether the run should stop.

    Five failures in a row, or three of the same 401/403, set ``stopped``.
    Jobs not started after that stop do not spend an attempt.
    """
    scored = 0
    failed = 0
    auto_rejected = 0
    stopped = False
    if not jobs or shutdown_requested():
        return {
            "scored": scored,
            "failed": failed,
            "auto_rejected": auto_rejected,
            "stopped": stopped,
        }
    max_workers = min(context.max_workers or 4, max(1, len(jobs)))
    failure_streak = 0
    permanent_status: Optional[int] = None
    permanent_count = 0
    stop_scoring = threading.Event()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        # Workers do not inherit contextvars. Copy the run's context so each
        # job trace records this run's id.
        futures = {
            pool.submit(
                contextvars.copy_context().run,
                _screen_one, agent, job, context, stop_scoring, key,
            ): key
            for key, job in jobs
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                annotated = future.result()
            except ScoringSkipped:
                continue
            except ScoreFailure as exc:
                failed += 1
                failure_streak += 1
                logger.error("Scoring failed for %s: %s", key, exc)
                storage.record_score_failure(key, str(exc))
                if exc.status in PERMANENT_STATUSES:
                    if exc.status == permanent_status:
                        permanent_count += 1
                    else:
                        permanent_status = exc.status
                        permanent_count = 1
                    if permanent_count >= PERMANENT_STOP_AFTER:
                        stopped = True
                        stop_scoring.set()
                else:
                    permanent_status = None
                    permanent_count = 0
                if failure_streak >= FAILURE_STREAK_STOP:
                    stopped = True
                    stop_scoring.set()
                continue
            except Exception as exc:  # noqa: BLE001 - per-job failure
                failed += 1
                failure_streak += 1
                logger.exception("Screening failed for %s", key)
                storage.record_score_failure(key, repr(exc))
                if failure_streak >= FAILURE_STREAK_STOP:
                    stopped = True
                    stop_scoring.set()
                continue
            failure_streak = 0
            permanent_status = None
            permanent_count = 0
            storage.upsert_job(annotated, job_key_value=key)
            if annotated.get("auto_rejected") and storage.set_job_status(
                key, "rejected", only_if_status="new", auto=True
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
            scored += 1
            done = progress_base + progress_span * scored / len(jobs)
            report(f"Scored {scored}/{len(jobs)}…", min(done, 95))
    return {
        "scored": scored,
        "failed": failed,
        "auto_rejected": auto_rejected,
        "stopped": stopped,
    }


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
            "pruned": 0,
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
        "pruned": 0,
        "alerts_sent": 0,
        "duration_s": None,
        "error": None,
    }

    def _report(message: str, percent: Optional[float] = None) -> None:
        if progress_callback is not None:
            progress_callback(message, percent)

    AI_METRICS.reset()
    _trace = pipeline_span(run_id, trigger)
    trace_span = _trace.__enter__()

    def _close(status: str) -> None:
        metrics = AI_METRICS.snapshot()
        summary["ai_metrics"] = metrics
        summary["status"] = status
        storage.finish_run(
            run_id,
            status,
            jobs_scraped=summary.get("jobs_scraped") or 0,
            new_jobs=summary.get("new_jobs") or 0,
            scored=summary.get("scored") or 0,
            alerts_sent=summary.get("alerts_sent") or 0,
            error=summary.get("error"),
            score_failed=summary.get("score_failed"),
            auto_rejected=summary.get("auto_rejected"),
            pruned=summary.get("pruned"),
            scrape_errors=summary.get("scrape_errors") or None,
            ai_metrics=metrics if metrics.get("calls") else None,
        )

    try:
        # Close out runs left 'running' by a crash/earlier deploy.
        reaped = storage.reap_stale_runs()
        if reaped:
            logger.info("Reaped %d stale run row(s) from interrupted processes.", reaped)

        if not context.resume_text:
            logger.warning("No resume stored — run aborted; upload a resume first.")
            summary["error"] = "No resume stored. Upload a resume before running."
            _close("failed")
            return summary

        # ---- 1+2. Scrape, persisting each pair before the next cooldown -- #
        _report("Scraping job boards…", 0)

        # Stream the scraper's per-(location, term) messages so a
        # comma-separated location shows "Scraping 'x' in Hyderabad (1/4)"
        # instead of a silent multi-city crawl. Scrape owns 0–10% of the bar.
        def _scrape_progress(completed: int, total: int, message: str) -> None:
            _report(message, (completed / total) * 10 if total else 0)

        new_count = 0
        to_score: List[Tuple[str, Dict[str, Any]]] = []
        seen_keys: set = set()

        def _persist_batch(jobs: List[Dict[str, Any]]) -> None:
            nonlocal new_count
            for raw_job in jobs:
                safe = make_json_safe(raw_job)
                key = storage.upsert_job(safe)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                existing = storage.get_job(key)
                if existing.get("first_seen_at") == existing.get("last_seen_at"):
                    new_count += 1
                # Pending retries wait until fresh postings in this run are
                # scored. force_rescore still must not pull them forward.
                if storage.needs_score_retry(existing):
                    continue
                if _needs_scoring(existing, context):
                    to_score.append((key, existing))

        result = _scrape(
            context,
            progress_callback=_scrape_progress,
            on_batch=_persist_batch,
            should_stop=shutdown_requested,
        )
        # Fakes that return a list never call on_batch. Persist those here.
        if not result.streamed:
            _persist_batch(result.jobs)

        summary["jobs_scraped"] = len(result.jobs) if result.streamed else len(seen_keys)
        if result.streamed:
            summary["jobs_scraped"] = len(seen_keys)
        summary["new_jobs"] = new_count
        summary["scrape_errors"] = list(result.errors)

        if result.pair_count and result.failed_pairs == result.pair_count:
            logger.error(
                "Every scrape pair failed (%d): %s",
                result.pair_count, "; ".join(result.errors),
            )
            summary["error"] = "Scrape failed: " + "; ".join(result.errors)
            _close("failed")
            return summary

        # Snapshot before fresh scoring. Failures from this run are not in
        # the list, so they wait for a later run.
        retry_tail = [
            (job["job_key"], job) for job in storage.jobs_pending_score_retry()
        ]

        if retry_tail:
            _report(
                f"{len(retry_tail)} failed scoring attempt(s) queued after new postings…",
                20,
            )
        else:
            _report(f"Persisting {summary['jobs_scraped']} scraped postings…", 10)

        # ---- 3. Score fresh postings, then the retry tail --------------- #
        _report(f"{len(to_score)} postings need scoring…", 25)
        scored_count = 0
        failed_count = 0
        auto_rejected = 0
        stopped_early = False
        if (to_score or retry_tail) and not shutdown_requested():
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
            if to_score:
                fresh = _score_postings(
                    to_score,
                    agent=agent,
                    context=context,
                    report=_report,
                    progress_base=25,
                    progress_span=50,
                )
                scored_count += fresh["scored"]
                failed_count += fresh["failed"]
                auto_rejected += fresh["auto_rejected"]
                stopped_early = fresh["stopped"]
            if retry_tail and not stopped_early and not shutdown_requested():
                _report(
                    f"Retrying {len(retry_tail)} failed scoring attempt(s)…",
                    75,
                )
                retried = _score_postings(
                    retry_tail,
                    agent=agent,
                    context=context,
                    report=_report,
                    progress_base=75,
                    progress_span=20,
                )
                scored_count += retried["scored"]
                failed_count += retried["failed"]
                auto_rejected += retried["auto_rejected"]
                stopped_early = stopped_early or retried["stopped"]
            elif retry_tail and stopped_early:
                logger.warning(
                    "Leaving %d scoring retry(s) for the next run after "
                    "fresh scoring stopped early.",
                    len(retry_tail),
                )
        if failed_count:
            logger.warning(
                "%d job(s) failed AI scoring this run; they retry automatically "
                "on the next run (up to %d attempts).",
                failed_count, storage.MAX_SCORE_ATTEMPTS,
            )
        if stopped_early:
            logger.warning(
                "Scoring stopped early after repeated AI failures; remaining "
                "jobs are left for the next run."
            )

        circuit_tripped = stopped_early

        # ---- 5. Retention + export + notify ------------------------------- #
        # Failures here must not throw away scores already committed.
        notes: List[str] = []
        if result.errors:
            notes.append("Scrape errors: " + "; ".join(result.errors))
        if failed_count:
            notes.append(f"{failed_count} job(s) failed scoring")
        if circuit_tripped:
            notes.append("Scoring stopped early after repeated AI failures")

        pruned = 0
        try:
            if context.retention_days > 0:
                pruned = storage.delete_jobs_older_than(context.retention_days)
                if pruned:
                    logger.info(
                        "Retention: deleted %d posting(s) not seen in %d days.",
                        pruned,
                        context.retention_days,
                    )
                    _report(f"Retention: deleted {pruned} old posting(s)…", 95)
        except Exception as exc:  # noqa: BLE001 - keep the scored rows
            logger.exception("Retention failed")
            notes.append(f"Retention failed: {exc}")

        _report("Exporting to Excel…", 96)
        try:
            from .excel import export_excel

            export_excel()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Excel export failed")
            notes.append(f"Excel export failed: {exc}")

        _report("Checking notifications…", 97)
        alerts = 0
        try:
            alerts = notify.notify_above_threshold(context.score_threshold)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Notification failed")
            notes.append(f"Notify failed: {exc}")

        degraded = bool(notes)
        summary.update(
            jobs_scraped=summary["jobs_scraped"],
            new_jobs=new_count,
            scored=scored_count,
            score_failed=failed_count,
            auto_rejected=auto_rejected,
            pruned=pruned,
            alerts_sent=alerts,
            error=" | ".join(notes) or None,
        )
        _close("degraded" if degraded else "completed")
        _report("Done.", 100)
    except Exception as exc:  # noqa: BLE001 - record failure on the run row
        logger.exception("Pipeline run failed")
        summary["error"] = str(exc)
        _close("failed")
    finally:
        summary["duration_s"] = round(time.monotonic() - started, 1)
        annotate_pipeline(trace_span, summary)
        _trace.__exit__(None, None, None)

    return summary
