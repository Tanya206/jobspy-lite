"""Tests for the SQLite storage layer."""

import pytest

from app import storage


def _job(key="u1", **overrides):
    base = {
        "title": "AI Engineer",
        "company": "Acme",
        "location": "Bengaluru",
        "site": "linkedin",
        "job_url": f"https://example.com/{key}",
        "description": "Build things",
    }
    base.update(overrides)
    return base


def test_raw_rescape_keeps_stored_ai_results(temp_db):
    """Re-scraping a scored job must NOT wipe its score back to unscored —
    that bug made the AI agent re-run on every job every run."""
    key = storage.upsert_job(_job())
    storage.upsert_job(
        _job(
            score=91,
            final_score=91,
            verdict="strong",
            matched_skills=["python"],
            missing_skills=["sql"],
            reasoning="great fit",
            required_years={"min": 1, "max": 3},
            experience_match=True,
            scored_with_resume="hash1",
        )
    )

    # Raw re-scrape of the same posting (fresh listing data, no AI fields).
    storage.upsert_job(_job(description="Updated description v2"))

    job = storage.get_job(key)
    assert job["score"] == 91
    assert job["verdict"] == "strong"
    assert job["matched_skills"] == ["python"]
    assert job["required_years_min"] == 1
    assert job["scored_with_resume"] == "hash1"
    assert job["description"] == "Updated description v2"  # listing data refreshed
    assert job["scored_at"] is not None


def test_upsert_insert_and_preserves_user_state(temp_db):
    key = storage.upsert_job(_job())

    # User accepts the job...
    assert storage.set_job_status(key, "accepted") is True

    # ...then a re-scrape upserts the same posting: status must survive.
    storage.upsert_job(_job(score=88, verdict="strong"))
    job = storage.get_job(key)
    assert job["status"] == "accepted"
    assert job["score"] == 88
    assert job["verdict"] == "strong"


def test_upsert_new_and_last_seen(temp_db):
    key = storage.upsert_job(_job())
    first = storage.get_job(key)
    assert first["first_seen_at"] == first["last_seen_at"]
    assert first["status"] == "new"


def test_list_jobs_orders_by_score(temp_db):
    storage.upsert_job(_job("a", score=50))
    storage.upsert_job(_job("b", score=90))
    storage.upsert_job(_job("c", score=70))
    jobs = storage.list_jobs()
    assert [j["job_key"] for j in jobs] == ["https://example.com/b", "https://example.com/c", "https://example.com/a"]


def test_list_jobs_status_filter(temp_db):
    k1 = storage.upsert_job(_job("a"))
    storage.upsert_job(_job("b"))
    storage.set_job_status(k1, "rejected")
    assert [j["job_key"] for j in storage.list_jobs(status="rejected")] == [k1]
    assert len(storage.list_jobs(status="new")) == 1


def test_counts_by_status(temp_db):
    k1 = storage.upsert_job(_job("a", score=80))
    storage.upsert_job(_job("b", score=60))
    storage.set_job_status(k1, "accepted")
    counts = storage.counts_by_status()
    assert counts["total"] == 2
    assert counts["accepted"] == 1
    assert counts["new"] == 1
    assert counts["avg_score"] == 70.0


def test_manual_vs_auto_reject_tracking(temp_db):
    """Auto-rejects carry auto_rejected=1; a user's own reject never does, and
    a later manual decision clears the auto mark."""
    auto_key = storage.upsert_job(_job("auto"))
    manual_key = storage.upsert_job(_job("manual"))

    storage.set_job_status(auto_key, "rejected", only_if_status="new", auto=True)
    storage.set_job_status(manual_key, "rejected")

    assert storage.get_job(auto_key)["auto_rejected"] == 1
    assert storage.get_job(manual_key)["auto_rejected"] == 0

    # The filter separates them.
    assert [j["job_key"] for j in storage.list_jobs(status="rejected", auto_rejected=True)] == [auto_key]
    assert [j["job_key"] for j in storage.list_jobs(status="rejected", auto_rejected=False)] == [manual_key]
    assert storage.count_jobs(status="rejected") == 2
    assert storage.count_jobs(status="rejected", auto_rejected=False) == 1

    # A manual accept clears the auto mark: the user is now in charge.
    assert storage.set_job_status(auto_key, "accepted") is True
    assert storage.get_job(auto_key)["auto_rejected"] == 0


def test_counts_split_manual_and_auto_rejected(temp_db):
    auto_key = storage.upsert_job(_job("auto"))
    manual_key = storage.upsert_job(_job("manual"))
    storage.set_job_status(auto_key, "rejected", auto=True)
    storage.set_job_status(manual_key, "rejected")

    counts = storage.counts_by_status()
    assert counts["rejected"] == 2
    assert counts["auto_rejected"] == 1
    assert counts["manual_rejected"] == 1


def test_delete_jobs_older_than(temp_db):
    """Retention deletes rows not seen within the window; 0 disables it."""
    from datetime import datetime, timedelta, timezone

    old_key = storage.upsert_job(_job("old"))
    fresh_key = storage.upsert_job(_job("fresh"))
    storage.query(
        "UPDATE jobs SET last_seen_at = ? WHERE job_key = ?",
        ((datetime.now(timezone.utc) - timedelta(days=40)).isoformat(), old_key),
    )

    assert storage.delete_jobs_older_than(0) == 0  # disabled
    assert storage.get_job(old_key) is not None

    assert storage.delete_jobs_older_than(30) == 1
    assert storage.get_job(old_key) is None
    assert storage.get_job(fresh_key) is not None


def test_invalid_status_raises(temp_db):
    key = storage.upsert_job(_job())
    with pytest.raises(ValueError):
        storage.set_job_status(key, "maybe")


def test_mark_notified(temp_db):
    k1 = storage.upsert_job(_job("a"))
    k2 = storage.upsert_job(_job("b"))
    storage.mark_notified([k1])
    assert storage.get_job(k1)["notified"] == 1
    assert storage.get_job(k2)["notified"] == 0


def test_settings_roundtrip(temp_db):
    storage.set_setting("score_threshold", 75)
    storage.set_setting("scrape_params", {"sites": ["indeed"], "location": "Remote"})
    assert storage.get_setting("score_threshold") == 75
    assert storage.get_setting("scrape_params")["location"] == "Remote"
    assert storage.get_setting("missing", "fallback") == "fallback"


def test_run_lifecycle(temp_db):
    run_id = storage.create_run("cron")
    run = storage.get_run(run_id)
    assert run["status"] == "running"
    assert run["trigger"] == "cron"
    storage.finish_run(run_id, "completed", jobs_scraped=5, new_jobs=2, scored=2, alerts_sent=1)
    run = storage.get_run(run_id)
    assert run["status"] == "completed"
    assert run["jobs_scraped"] == 5
    assert len(storage.list_runs()) == 1


def test_skills_stored_as_json(temp_db):
    storage.upsert_job(_job(matched_skills=["python", "sql"], missing_skills=["go"]))
    job = storage.list_jobs()[0]
    assert job["matched_skills"] == ["python", "sql"]


def test_raw_insert_has_null_experience_match(temp_db):
    """Unscored rows must carry experience_match NULL (not 0) — a raw 0 made
    the UI claim 'outside window' on jobs the AI never assessed."""
    key = storage.upsert_job(_job())
    assert storage.get_job(key)["experience_match"] is None

    storage.upsert_job(_job(score=80, verdict="strong", experience_match=True))
    assert storage.get_job(key)["experience_match"] == 1


def test_failure_marks_refused_on_scored_rows(temp_db):
    """A scored row never takes failure marks again: the marks are cleared on
    scoring, so marks appearing next to a score are stale bookkeeping that
    flags healthy jobs as 'AI failed' in the UI."""
    key = storage.upsert_job(_job())
    storage.upsert_job(_job(score=77, verdict="moderate"))

    assert storage.record_score_failure(key, "late 503") is False
    job = storage.get_job(key)
    assert job["score"] == 77
    assert job["score_attempts"] == 0 and job["score_error"] is None


def test_stale_failure_marks_cleaned_at_connect(temp_db):
    """Startup cleanup removes marks stranded next to a score (deploy-restart
    artifact) without touching genuinely-failed unscored rows."""
    scored_key = storage.upsert_job(_job("scored"))
    storage.upsert_job(_job("scored", score=88, verdict="strong"))
    failed_key = storage.upsert_job(_job("failed"))
    storage.record_score_failure(failed_key, "NVIDIA down")

    # Simulate the torn write the cleanup exists for.
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET score_attempts = 1, score_error = 'torn' WHERE job_key = ?",
            (scored_key,),
        )

    storage.close()
    storage.connect()  # re-runs the startup cleanup

    healed = storage.get_job(scored_key)
    assert healed["score"] == 88
    assert healed["score_attempts"] == 0 and healed["score_error"] is None
    failed = storage.get_job(failed_key)
    assert failed["score_error"] == "NVIDIA down"  # real failure untouched


def test_unscored_match_zero_cleaned_at_connect(temp_db):
    """Startup cleanup NULLs experience_match on rows the old build wrote as
    raw-0 (no score, no verdict, no years); assessed rows stay 1/0."""
    raw_key = storage.upsert_job(_job("raw"))
    rejected_key = storage.upsert_job(
        _job("rejected", required_years={"min": 10, "max": None},
             experience_match=False, verdict="outside experience window")
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET experience_match = 0 WHERE job_key = ?", (raw_key,)
        )

    storage.close()
    storage.connect()

    assert storage.get_job(raw_key)["experience_match"] is None
    assert storage.get_job(rejected_key)["experience_match"] == 0  # genuine mismatch kept
