"""Pipeline tests with fake AI agent + scraper (no network)."""

from datetime import datetime, timedelta, timezone

import pytest

from app import storage
from app.pipeline import PipelineContext, _needs_scoring, run_pipeline


class FakeAgent:
    """Deterministic stand-in for AIJobAgent."""

    def __init__(self, score=85):
        self.score = score

    def _estimate_required_years(self, job):
        return {"min": 1, "max": 2}

    def assess_suitability(self, job, resume_text):
        return {
            "score": self.score,
            "final_score": self.score,
            "verdict": "strong",
            "matched_skills": ["python"],
            "missing_skills": ["kafka"],
            "reasoning": "solid fit",
        }


def _seed_context(resume_hash="hash1"):
    storage.set_setting(
        "resume", {"text": "my resume", "name": "r.docx", "hash": resume_hash}
    )
    return PipelineContext()


def test_needs_scoring_rules(temp_db):
    context = _seed_context()
    assert _needs_scoring(None, context) is True  # brand new

    scored_row = {
        "score": 80,
        "required_years_min": 1,
        "scored_with_resume": "hash1",
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }
    assert _needs_scoring(scored_row, context) is False  # fresh + same resume

    other_resume = dict(scored_row, scored_with_resume="hash2")
    assert _needs_scoring(other_resume, context) is True  # resume changed

    stale = dict(
        scored_row,
        scored_at=(datetime.now(timezone.utc) - timedelta(hours=73)).isoformat(),
    )
    assert _needs_scoring(stale, context) is True  # older than 72h

    unscored = {"score": None, "required_years_min": None, "scored_at": None}
    assert _needs_scoring(unscored, context) is True

    # Poisoned by the old fake-verdict bug: verdict 'unknown' but no score —
    # must re-score instead of waiting out the rescore window.
    poisoned = {
        "score": None,
        "required_years_min": 1,
        "verdict": "unknown",
        "scored_with_resume": "hash1",
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }
    assert _needs_scoring(poisoned, context) is True
    assert _needs_scoring(dict(poisoned, verdict="weak"), context) is False  # genuine low score stays


def test_needs_scoring_disabled_rescore_window(temp_db, monkeypatch):
    """RESCORE_AFTER_HOURS=0 means: never re-score stale jobs (a new resume
    or an unscored row still triggers scoring)."""
    import app.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "RESCORE_AFTER_HOURS", 0)
    context = _seed_context()
    stale = {
        "score": 80,
        "required_years_min": 1,
        "scored_with_resume": "hash1",
        "scored_at": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
    }
    assert _needs_scoring(stale, context) is False
    assert _needs_scoring(dict(stale, scored_with_resume="hash2"), context) is True
    assert _needs_scoring({"score": None, "required_years_min": None, "scored_at": None}, context) is True


def test_needs_scoring_force(temp_db):
    context = _seed_context()
    context.force_rescore = True
    fresh_row = {
        "score": 80,
        "required_years_min": 1,
        "scored_with_resume": "hash1",
        "scored_at": datetime.now(timezone.utc).isoformat(),
    }
    assert _needs_scoring(fresh_row, context) is True


def test_concurrent_run_is_skipped(temp_db, monkeypatch):
    """A second run_pipeline while one holds the lock returns skipped without
    scraping — cron and manual runs must never overlap."""
    import threading

    import app.pipeline as pipeline_module

    _seed_context()
    started = threading.Event()
    release = threading.Event()

    def slow_execute(trigger="manual", force_rescore=False, progress_callback=None):
        started.set()
        release.wait(timeout=5)
        return {"status": "completed", "scored": 0}

    monkeypatch.setattr(pipeline_module, "_execute_pipeline", slow_execute)

    results = {}

    def first():
        results["first"] = run_pipeline(trigger="cron")

    t = threading.Thread(target=first)
    t.start()
    assert started.wait(timeout=5)  # first run holds the lock now

    second = run_pipeline(trigger="manual")
    release.set()
    t.join(timeout=5)

    assert second["status"] == "skipped"
    assert "already in progress" in second["error"]
    assert results["first"]["status"] == "completed"


def test_failed_scoring_is_recorded_and_retried(temp_db, monkeypatch):
    """AI failures must be tracked (attempts + error) and retried next run —
    never written as fake verdicts that stick forever."""
    _seed_context()

    attempts = {"n": 0}

    class FlakyAgent:
        def _estimate_required_years(self, job):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("NVIDIA down")
            return {"min": 1, "max": 2}

        def assess_suitability(self, job, resume_text):
            return {
                "score": 77, "final_score": 77, "verdict": "good",
                "matched_skills": ["python"], "missing_skills": [],
                "reasoning": "ok",
            }

    class OneJobScraper:
        def __init__(self, config):
            pass

        def scrape(self, progress_callback=None):
            return [{
                "title": "Retry Me", "company": "Acme", "site": "linkedin",
                "job_url": "https://example.com/retry", "description": "d",
            }]

    import app.pipeline as pipeline_module
    monkeypatch.setattr(pipeline_module, "JobScraper", OneJobScraper)
    monkeypatch.setattr(pipeline_module, "AIJobAgent", lambda config: FlakyAgent())
    monkeypatch.setattr(
        pipeline_module.notify, "notify_above_threshold", lambda *a, **k: 0
    )

    # Run 1: agent fails -> run is degraded (scores were attempted), job recorded
    # as failed, no fake verdict.
    s1 = run_pipeline(trigger="manual")
    assert s1["status"] == "degraded"
    job = storage.get_job("https://example.com/retry")
    assert job["score_attempts"] == 1
    assert "NVIDIA down" in job["score_error"]
    assert job["verdict"] is None and job["score"] is None

    # Run 2: agent healthy -> job scores, bookkeeping cleared
    s2 = run_pipeline(trigger="manual")
    assert s2["status"] == "completed" and s2["scored"] == 1
    job = storage.get_job("https://example.com/retry")
    assert job["score"] == 77
    assert job["score_attempts"] == 0 and job["score_error"] is None


def test_stale_running_runs_are_reaped(temp_db):
    """Runs stuck in 'running' (deploy/crash mid-run) get closed out."""
    from datetime import datetime, timedelta, timezone

    old = storage.create_run("manual")
    # Backdate the row beyond the reap window.
    storage.query("UPDATE runs SET started_at = ?", (
        (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
    ))
    fresh = storage.create_run("manual")

    reaped = storage.reap_stale_runs(max_age_minutes=30)
    assert reaped == 1
    assert storage.get_run(old)["status"] == "failed"
    assert "interrupted" in storage.get_run(old)["error"]
    assert storage.get_run(fresh)["status"] == "running"


def test_run_pipeline_no_resume_fails_cleanly(temp_db):
    summary = run_pipeline(trigger="manual")
    assert summary["status"] == "failed"
    assert "resume" in summary["error"].lower()
    runs = storage.list_runs()
    assert runs[0]["status"] == "failed"


class MismatchAgent:
    """Agent 1 places the posting far above the window; Agent 2 must never run."""

    def __init__(self):
        self.suitability_calls = 0

    def _estimate_required_years(self, job):
        return {"min": 6, "max": 9}

    def assess_suitability(self, job, resume_text):
        self.suitability_calls += 1
        raise AssertionError("suitability must not run for outside-window jobs")


class SingleJobScraper:
    def __init__(self, config):
        pass

    def scrape(self, progress_callback=None):
        return [
            {
                "title": "Principal Engineer",
                "company": "Acme",
                "site": "linkedin",
                "job_url": self.job_url,
                "description": "Needs 6-9 years of experience.",
            }
        ]


def _one_job_scraper(job_url):
    return type(
        "OneJobScraper",
        (SingleJobScraper,),
        {"job_url": job_url},
    )


def _set_window(min_years=0, max_years=2):
    storage.set_setting("experience_min_years", min_years)
    storage.set_setting("experience_max_years", max_years)


def _fake_notify(monkeypatch):
    import app.pipeline as pipeline_module

    monkeypatch.setattr(
        pipeline_module.notify, "notify_above_threshold", lambda *a, **k: 0
    )


def test_experience_mismatch_is_auto_rejected(temp_db, monkeypatch):
    """A genuine experience-window mismatch is auto-rejected while its status
    is still 'new' — it must never appear as an alert candidate again."""
    _seed_context()
    _set_window(0, 2)
    _fake_notify(monkeypatch)

    agent = MismatchAgent()
    import app.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JobScraper", _one_job_scraper("https://example.com/senior"))
    monkeypatch.setattr(pipeline_module, "AIJobAgent", lambda config: agent)

    summary = run_pipeline(trigger="manual")

    assert summary["status"] == "completed"
    assert summary["auto_rejected"] == 1
    job = storage.get_job("https://example.com/senior")
    assert job["status"] == "rejected"
    assert job["auto_rejected"] == 1  # flagged so the board can label it as automatic
    assert job["verdict"] == "outside experience window"
    assert job["required_years_min"] == 6
    assert agent.suitability_calls == 0  # no resume-fit call wasted on it


def test_user_triaged_mismatch_is_not_clobbered(temp_db, monkeypatch):
    """Auto-reject only applies to 'new' jobs: an accepted (or manually
    rejected) posting keeps the user's decision even when it mismatches."""
    _seed_context()
    _set_window(0, 2)
    _fake_notify(monkeypatch)

    key = storage.upsert_job({"job_url": "https://example.com/senior", "title": "x"})
    storage.set_job_status(key, "accepted")

    agent = MismatchAgent()
    import app.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JobScraper", _one_job_scraper("https://example.com/senior"))
    monkeypatch.setattr(pipeline_module, "AIJobAgent", lambda config: agent)

    summary = run_pipeline(trigger="manual")

    assert summary["status"] == "completed"
    assert summary["auto_rejected"] == 0
    job = storage.get_job(key)
    assert job["status"] == "accepted"
    assert job["verdict"] == "outside experience window"  # still annotated


def test_estimate_failure_is_never_auto_rejected(temp_db, monkeypatch):
    """An AI outage must leave the job unscored (retried next run), NOT
    rejected as outside-window — rejection only follows a real estimate."""
    _seed_context()
    _fake_notify(monkeypatch)

    class DownAgent:
        def _estimate_required_years(self, job):
            raise RuntimeError("NVIDIA down")

        def assess_suitability(self, job, resume_text):
            raise AssertionError("not reached")

    import app.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JobScraper", _one_job_scraper("https://example.com/down"))
    monkeypatch.setattr(pipeline_module, "AIJobAgent", lambda config: DownAgent())

    summary = run_pipeline(trigger="manual")

    assert summary["status"] == "degraded"
    assert summary["auto_rejected"] == 0
    job = storage.get_job("https://example.com/down")
    assert job["status"] == "new"
    assert "NVIDIA down" in (job["score_error"] or "")
    assert job["verdict"] is None


def test_agent_estimate_failure_propagates(temp_db, monkeypatch):
    """AIJobAgent._estimate_required_years must re-raise: returning None used
    to make the window check classify every job as outside-window, which the
    auto-reject rule would turn into a mass rejection during any outage."""
    from app.agent.ai_job_agent import AIJobAgent

    agent = AIJobAgent(config={"ai_model": "test-model", "api_key": "unused"})

    def boom(*args, **kwargs):
        raise RuntimeError("NVIDIA down")

    monkeypatch.setattr(agent.client, "parse", boom)
    with pytest.raises(RuntimeError):
        agent._estimate_required_years(
            {"title": "T", "company": "C", "description": "d"}
        )


def test_agent_suitability_failure_propagates(temp_db, monkeypatch):
    """AIJobAgent.assess_suitability must re-raise: returning the fake
    'unknown' verdict used to persist the job as scored, so it was never
    retried and the real failure stayed invisible."""
    from app.agent.ai_job_agent import AIJobAgent

    agent = AIJobAgent(config={"ai_model": "test-model", "api_key": "unused"})

    def boom(*args, **kwargs):
        raise RuntimeError("NVIDIA down")

    monkeypatch.setattr(agent.client, "parse", boom)
    with pytest.raises(RuntimeError):
        agent.assess_suitability(
            {"title": "T", "company": "C", "description": "d"}, "resume text"
        )


def test_suitability_failure_is_recorded_and_retried(temp_db, monkeypatch):
    """A suitability-agent outage must leave the job unscored (retried next
    run), NOT persisted with a fake 'unknown' verdict that sticks for 72h."""
    _seed_context()
    _fake_notify(monkeypatch)

    class DownFitAgent:
        def _estimate_required_years(self, job):
            return {"min": 1, "max": 2}  # in-window so the fit agent runs

        def assess_suitability(self, job, resume_text):
            raise RuntimeError("NVIDIA down")

    import app.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JobScraper", _one_job_scraper("https://example.com/fitfail"))
    monkeypatch.setattr(pipeline_module, "AIJobAgent", lambda config: DownFitAgent())

    summary = run_pipeline(trigger="manual")

    assert summary["status"] == "degraded"
    assert summary["auto_rejected"] == 0
    job = storage.get_job("https://example.com/fitfail")
    assert job["status"] == "new"
    assert "NVIDIA down" in (job["score_error"] or "")
    assert job["verdict"] is None and job["score"] is None
    assert job["score_attempts"] == 1


def test_retention_prunes_unseen_jobs(temp_db, monkeypatch):
    """With retention_days set, postings not seen in that many days are
    deleted at the end of the run; recently seen ones survive."""
    _seed_context()
    _fake_notify(monkeypatch)

    class NoJobScraper:
        def __init__(self, config):
            pass

        def scrape(self, progress_callback=None):
            return []

    import app.pipeline as pipeline_module
    monkeypatch.setattr(pipeline_module, "JobScraper", NoJobScraper)
    monkeypatch.setattr(pipeline_module, "AIJobAgent", lambda config: FakeAgent())

    storage.set_setting("retention_days", 30)
    old_key = storage.upsert_job({"job_url": "https://example.com/old", "title": "old"})
    fresh_key = storage.upsert_job({"job_url": "https://example.com/fresh", "title": "fresh"})
    storage.query(
        "UPDATE jobs SET last_seen_at = ? WHERE job_key = ?",
        (
            (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(),
            old_key,
        ),
    )

    summary = run_pipeline(trigger="manual")

    assert summary["status"] == "completed"
    assert summary["pruned"] == 1
    assert storage.get_job(old_key) is None
    assert storage.get_job(fresh_key) is not None


def test_retention_disabled_keeps_everything(temp_db, monkeypatch):
    """No retention_days setting (0) means nothing is ever deleted."""
    _seed_context()
    _fake_notify(monkeypatch)

    class NoJobScraper:
        def __init__(self, config):
            pass

        def scrape(self, progress_callback=None):
            return []

    import app.pipeline as pipeline_module
    monkeypatch.setattr(pipeline_module, "JobScraper", NoJobScraper)
    monkeypatch.setattr(pipeline_module, "AIJobAgent", lambda config: FakeAgent())

    old_key = storage.upsert_job({"job_url": "https://example.com/old", "title": "old"})
    storage.query(
        "UPDATE jobs SET last_seen_at = ? WHERE job_key = ?",
        (
            (datetime.now(timezone.utc) - timedelta(days=400)).isoformat(),
            old_key,
        ),
    )

    summary = run_pipeline(trigger="manual")

    assert summary["status"] == "completed"
    assert summary["pruned"] == 0
    assert storage.get_job(old_key) is not None


def test_run_pipeline_end_to_end_with_fakes(temp_db, monkeypatch):
    _seed_context()

    # Fake scraper: two postings, one already in the DB with user-accepted state.
    class FakeScraper:
        def __init__(self, config):
            pass

        def scrape(self, progress_callback=None):
            return [
                {
                    "title": "AI Engineer",
                    "company": "Acme",
                    "location": "Bengaluru",
                    "site": "linkedin",
                    "job_url": "https://example.com/1",
                    "description": "Build AI things",
                },
                {
                    "title": "Data Engineer",
                    "company": "Globex",
                    "location": "Remote",
                    "site": "indeed",
                    "job_url": "https://example.com/2",
                    "description": "Move data",
                },
            ]

    import app.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JobScraper", FakeScraper)
    monkeypatch.setattr(pipeline_module, "AIJobAgent", lambda config: FakeAgent())

    # Pre-store posting 1 as accepted so we can prove upsert preserves it.
    key1 = storage.upsert_job({"job_url": "https://example.com/1", "title": "x"})
    storage.set_job_status(key1, "accepted")

    # Pre-mark posting 2 as notified so it must NOT be alerted again.
    key2 = storage.upsert_job({"job_url": "https://example.com/2", "title": "y"})
    storage.mark_notified([key2])

    sent = {}

    def fake_notify(threshold, **kwargs):
        sent["threshold"] = threshold
        return 1  # pretend one alert went out

    monkeypatch.setattr(pipeline_module.notify, "notify_above_threshold", fake_notify)

    summary = run_pipeline(trigger="cron")

    assert summary["status"] == "completed"
    assert summary["jobs_scraped"] == 2
    assert summary["new_jobs"] == 0  # both pre-existed
    assert summary["scored"] == 2
    assert summary["alerts_sent"] == 1
    assert sent["threshold"] == 70  # default threshold

    job1 = storage.get_job(key1)
    assert job1["status"] == "accepted"  # user state preserved through re-score
    assert job1["score"] == 85
    assert job1["scored_with_resume"] == "hash1"

    run_row = storage.get_run(summary["run_id"])
    assert run_row["status"] == "completed"
    assert run_row["scored"] == 2


def test_run_pipeline_reports_per_city_scrape_progress(temp_db, monkeypatch):
    """A comma-separated location surfaces one progress message per city and
    keeps the scrape inside the 0–10% band of the run."""
    _seed_context()

    class ProgressScraper:
        def __init__(self, config):
            self.config = config

        def scrape(self, progress_callback=None):
            cities = ["Hyderabad", "Bengaluru"]
            for i, city in enumerate(cities):
                progress_callback(
                    i, len(cities),
                    f"Scraping 'AI Engineer' in {city} ({i + 1}/{len(cities)})",
                )
                progress_callback(
                    i + 1, len(cities),
                    f"Found 1 jobs for 'AI Engineer' in {city}",
                )
            return [
                {
                    "title": "AI Engineer",
                    "company": "Acme",
                    "location": "Hyderabad",
                    "site": "linkedin",
                    "job_url": "https://example.com/1",
                    "description": "Build AI things",
                }
            ]

    import app.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JobScraper", ProgressScraper)
    monkeypatch.setattr(pipeline_module, "AIJobAgent", lambda config: FakeAgent())
    monkeypatch.setattr(pipeline_module.notify, "notify_above_threshold", lambda *a, **k: 0)

    seen = []
    summary = run_pipeline(
        trigger="manual",
        progress_callback=lambda message, percent: seen.append((message, percent)),
    )

    assert summary["status"] == "completed"
    messages = [msg for msg, _ in seen]
    assert "Scraping 'AI Engineer' in Hyderabad (1/2)" in messages
    assert "Scraping 'AI Engineer' in Bengaluru (2/2)" in messages

    scrape_percents = [pct for msg, pct in seen if msg.startswith(("Scraping '", "Found "))]
    assert scrape_percents and max(scrape_percents) <= 10
    assert min(scrape_percents) >= 0
    # The run keeps moving past the scrape band (persist/score/export).
    assert any(pct is not None and pct > 10 for _, pct in seen)


def test_every_scrape_pair_failing_fails_the_run(temp_db, monkeypatch):
    """A blocked board must not look like an empty market."""
    from app.scraper import ScrapeResult

    _seed_context()
    notified = {"n": 0}

    class BlockedScraper:
        def __init__(self, config):
            pass

        def scrape(self, progress_callback=None, on_batch=None, should_stop=None):
            return ScrapeResult(
                jobs=[],
                errors=["AI Engineer in Hyderabad: blocked", "AI Engineer in Bengaluru: blocked"],
                pair_count=2,
                failed_pairs=2,
                streamed=True,
            )

    import app.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JobScraper", BlockedScraper)
    monkeypatch.setattr(
        pipeline_module.notify, "notify_above_threshold",
        lambda *a, **k: notified.__setitem__("n", notified["n"] + 1),
    )

    summary = run_pipeline(trigger="manual")
    assert summary["status"] == "failed"
    assert "blocked" in summary["error"]
    assert notified["n"] == 0
    assert storage.get_run(summary["run_id"])["status"] == "failed"


def test_partial_scrape_failure_is_degraded(temp_db, monkeypatch):
    from app.scraper import ScrapeResult

    _seed_context()

    class PartialScraper:
        def __init__(self, config):
            pass

        def scrape(self, progress_callback=None, on_batch=None, should_stop=None):
            return ScrapeResult(
                jobs=[{
                    "title": "AI Engineer",
                    "company": "Acme",
                    "site": "linkedin",
                    "job_url": "https://example.com/partial",
                    "description": "d",
                }],
                errors=["AI Engineer in Hyderabad: blocked"],
                pair_count=2,
                failed_pairs=1,
                streamed=False,
            )

    import app.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JobScraper", PartialScraper)
    monkeypatch.setattr(pipeline_module, "AIJobAgent", lambda config: FakeAgent())
    monkeypatch.setattr(pipeline_module.notify, "notify_above_threshold", lambda *a, **k: 0)

    summary = run_pipeline(trigger="manual")
    assert summary["status"] == "degraded"
    assert "blocked" in summary["error"]
    assert storage.get_job("https://example.com/partial")["score"] == 85
    assert "blocked" in (storage.get_run(summary["run_id"])["error"] or "")


def test_excel_failure_after_scoring_is_degraded(temp_db, monkeypatch):
    """A broken export must not throw away a score that was already saved."""
    _seed_context()

    import app.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "JobScraper", _one_job_scraper("https://example.com/scored"))
    monkeypatch.setattr(pipeline_module, "AIJobAgent", lambda config: FakeAgent())
    monkeypatch.setattr(pipeline_module.notify, "notify_above_threshold", lambda *a, **k: 0)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("app.excel.export_excel", boom)

    summary = run_pipeline(trigger="manual")
    assert summary["status"] == "degraded"
    assert summary["scored"] == 1
    assert "disk full" in summary["error"]
    job = storage.get_job("https://example.com/scored")
    assert job["score"] == 85
    run = storage.get_run(summary["run_id"])
    assert run["status"] == "degraded"
    assert run["scored"] == 1
