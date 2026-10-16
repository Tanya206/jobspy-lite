"""API tests via FastAPI TestClient on a temporary database."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(temp_db, monkeypatch):
    # Keep the notify test-hook out of the way: no real Telegram config.
    monkeypatch.setattr("app.api.notify.notify_above_threshold", lambda *a, **k: 0)
    from app.api import app

    return TestClient(app)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "ai_model" in body
    assert body["db_ready"] is True


def test_resume_upload_and_get(client, tmp_path):
    # Build a minimal real .docx so extract_text succeeds.
    from io import BytesIO

    from docx import Document

    document = Document()
    document.add_paragraph("John Doe — Python developer")
    buffer = BytesIO()
    document.save(buffer)

    response = client.post(
        "/resume",
        files={"resume": ("resume.docx", buffer.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert response.status_code == 200
    info = response.json()
    assert info["name"] == "resume.docx"
    assert info["hash"]

    fetched = client.get("/resume").json()
    assert fetched["name"] == "resume.docx"
    assert fetched["chars"] > 0


def test_resume_requires_docx(client):
    response = client.post("/resume", files={"resume": ("x.txt", b"plain text", "text/plain")})
    assert response.status_code == 400


def test_jobs_status_flow(client):
    from app import storage

    key = storage.upsert_job({"job_url": "https://x/1", "title": "Role", "score": 90})
    response = client.post(f"/jobs/{key}/status", json={"new_status": "accepted"})
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert storage.get_job(key)["status"] == "accepted"

    bad = client.post(f"/jobs/{key}/status", json={"new_status": "banana"})
    assert bad.status_code == 400 or bad.status_code == 500

    missing = client.post("/jobs/unknown-key/status", json={"new_status": "new"})
    assert missing.status_code == 404


def test_jobs_listing_and_stats(client):
    from app import storage

    k1 = storage.upsert_job({"job_url": "https://x/1", "title": "A", "score": 90, "company": "C1"})
    k2 = storage.upsert_job({"job_url": "https://x/2", "title": "B", "score": 40, "company": "C2"})
    storage.set_job_status(k1, "accepted")

    listing = client.get("/jobs?status=accepted").json()
    assert listing["count"] == 1
    assert listing["jobs"][0]["job_key"] == k1

    stats = client.get("/stats").json()
    assert stats["total"] == 2
    assert stats["accepted"] == 1
    assert stats["new"] == 1
    assert stats["rejected"] == 0
    assert stats["auto_rejected"] == 0
    assert stats["manual_rejected"] == 0


def test_rejected_filter_split_and_filtered_total(client):
    """/jobs separates manual from pipeline rejects and reports the filtered
    total so badges stay accurate when only one page is loaded."""
    from app import storage

    auto_key = storage.upsert_job({"job_url": "https://x/auto", "title": "A"})
    manual_key = storage.upsert_job({"job_url": "https://x/manual", "title": "B"})
    storage.set_job_status(auto_key, "rejected", auto=True)
    storage.set_job_status(manual_key, "rejected")

    manual = client.get("/jobs?status=rejected&auto_rejected=false").json()
    assert manual["filtered_total"] == 1
    assert [j["job_key"] for j in manual["jobs"]] == [manual_key]

    auto = client.get("/jobs?status=rejected&auto_rejected=true").json()
    assert auto["filtered_total"] == 1
    assert [j["job_key"] for j in auto["jobs"]] == [auto_key]

    all_rejected = client.get("/jobs?status=rejected").json()
    assert all_rejected["filtered_total"] == 2
    assert {j["job_key"] for j in all_rejected["jobs"]} == {auto_key, manual_key}

    stats = client.get("/stats").json()
    assert stats["rejected"] == 2
    assert stats["auto_rejected"] == 1
    assert stats["manual_rejected"] == 1


def test_recent_order_excludes_rejected_before_limit(client):
    """The jobs board asks for a page with rejected rows removed first, so
    recency does not return a shorter list than score or title."""
    from app import storage

    keep = storage.upsert_job({"job_url": "https://x/keep", "title": "Keep", "score": 80})
    for i in range(3):
        key = storage.upsert_job({"job_url": f"https://x/rej-{i}", "title": f"Rej {i}", "score": 10})
        storage.set_job_status(key, "rejected", auto=True)
    with storage.transaction() as conn:
        conn.execute("UPDATE jobs SET last_seen_at = ? WHERE job_key = ?", ("2026-01-01T00:00:00+00:00", keep))
        conn.execute("UPDATE jobs SET last_seen_at = ? WHERE status = 'rejected'", ("2026-10-01T00:00:00+00:00",))

    recent = client.get("/jobs?order=recent&limit=2&exclude_status=rejected").json()
    score = client.get("/jobs?order=score&limit=2&exclude_status=rejected").json()
    assert recent["filtered_total"] == score["filtered_total"] == 1
    assert [job["job_key"] for job in recent["jobs"]] == [keep]
    assert [job["job_key"] for job in score["jobs"]] == [keep]

    bad = client.get("/jobs?exclude_status=banana")
    assert bad.status_code == 400


def test_scheduler_config_validation(client):
    # Invalid mode rejected.
    bad = client.post("/scheduler/config", json={"mode": "weekly"})
    assert bad.status_code == 400

    # Valid cron accepted and persisted.
    ok = client.post("/scheduler/config", json={"mode": "cron", "cron_expression": "0 */2 * * *"})
    assert ok.status_code == 200
    assert ok.json()["config"]["cron_expression"] == "0 */2 * * *"

    # Invalid cron expression rejected with detail.
    bad_cron = client.post("/scheduler/config", json={"mode": "cron", "cron_expression": "not a cron"})
    assert bad_cron.status_code == 400

    status = client.get("/scheduler").json()
    assert status["enabled"] is False  # config saved but scheduler not started


def test_scheduler_toggle_starts_and_stops(client):
    on = client.post("/scheduler/toggle?enabled=true").json()
    assert on["running"] is True
    assert on["enabled"] is True
    assert on["next_run_at"]

    off = client.post("/scheduler/toggle?enabled=false").json()
    assert off["running"] is False
    assert off["enabled"] is False


def test_settings_roundtrip(client):
    updated = client.post(
        "/settings",
        json={"score_threshold": 65, "experience_min_years": 1, "experience_max_years": 3,
              "retention_days": 45},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["score_threshold"] == 65
    assert body["experience_min_years"] == 1
    assert body["experience_max_years"] == 3
    assert body["retention_days"] == 45

    # 0 (keep everything) is a valid explicit value; negatives clamp to 0.
    assert client.post("/settings", json={"retention_days": 0}).json()["retention_days"] == 0
    assert client.post("/settings", json={"retention_days": -5}).json()["retention_days"] == 0


def test_agent_params_roundtrip_and_sanitization(client):
    updated = client.post(
        "/settings",
        json={"agent_params": {
            "ai_model": "mymodel/v2",
            "ai_base_url": "https://example.invalid/v1",
            "ai_rate_limit_per_min": 15,
            "max_workers": 6,
            "api_key": "should-not-persist",
        }},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["agent_params"]["ai_model"] == "mymodel/v2"
    assert body["agent_params"]["ai_base_url"] == "https://example.invalid/v1"
    assert body["agent_params"]["ai_rate_limit_per_min"] == 15
    assert body["max_workers"] == 6
    assert "api_key" not in body["agent_params"]

    from app import storage
    assert "api_key" not in (storage.get_setting("agent_params") or {})

    # Empty model is dropped (falls back to env default); junk rate limit -> 30.
    fallback = client.post(
        "/settings",
        json={"agent_params": {"ai_model": "  ", "ai_rate_limit_per_min": "bogus"}},
    )
    assert fallback.status_code == 200
    params = fallback.json()["agent_params"]
    assert params["ai_model"] != "  "
    assert params["ai_rate_limit_per_min"] == 30


def test_scrape_params_roundtrip(client):
    updated = client.post(
        "/settings",
        json={"scrape_params": {"search_terms": ["ML Engineer"], "hours_old": 6, "location": "Remote"}},
    )
    assert updated.status_code == 200
    params = updated.json()["scrape_params"]
    assert params["search_terms"] == ["ML Engineer"]
    assert params["hours_old"] == 6
    assert params["location"] == "Remote"


def test_export_xlsx(client):
    from app import storage

    storage.upsert_job({"job_url": "https://x/1", "title": "A", "score": 90})
    response = client.get("/export.xlsx")
    assert response.status_code == 200
    assert response.content.startswith(b"PK")  # zip magic = valid xlsx


def test_runs_listed(client):
    from app import storage

    run_id = storage.create_run("manual")
    storage.finish_run(run_id, "completed", jobs_scraped=3)
    runs = client.get("/runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["trigger"] == "manual"


def test_api_token_required_when_set(client, monkeypatch):
    monkeypatch.setenv("JOBSPY_API_TOKEN", "secret-token")
    assert client.get("/health").status_code == 200
    assert client.get("/stats").status_code == 401
    ok = client.get("/stats", headers={"X-API-Token": "secret-token"})
    assert ok.status_code == 200


def test_settings_clamp_results_and_workers(client):
    updated = client.post(
        "/settings",
        json={
            "scrape_params": {"results_wanted": 9000, "hours_old": 0},
            "agent_params": {"max_workers": 40, "ai_model": "m"},
        },
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["scrape_params"]["results_wanted"] == 200
    assert body["scrape_params"]["hours_old"] == 1
    assert body["agent_params"]["max_workers"] == 8


def test_purge_endpoint(client):
    from datetime import datetime, timedelta, timezone

    from app import storage

    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    kept = storage.upsert_job({"job_url": "https://x/keep", "title": "accepted"})
    storage.set_job_status(kept, "accepted")
    gone = storage.upsert_job({"job_url": "https://x/gone", "title": "stale"})
    storage.query("UPDATE jobs SET first_seen_at = ? WHERE job_key = ?", (old, kept))
    storage.query("UPDATE jobs SET first_seen_at = ? WHERE job_key = ?", (old, gone))
    run_id = storage.create_run("manual")
    storage.query("UPDATE runs SET started_at = ? WHERE run_id = ?", (old, run_id))

    assert client.post("/maintenance/purge", json={"older_than_days": 0}).status_code == 400
    response = client.post("/maintenance/purge", json={"older_than_days": 30})
    assert response.status_code == 200
    body = response.json()
    assert body["jobs_deleted"] == 1
    assert body["runs_deleted"] == 1
    assert storage.get_job(kept) is not None
    assert storage.get_job(gone) is None
    assert storage.get_run(run_id) is None
