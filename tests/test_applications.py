"""Applications tab: accept fills a tracker row in the same database."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(temp_db, monkeypatch):
    monkeypatch.setattr("app.api.notify.notify_above_threshold", lambda *a, **k: 0)
    from app.api import app

    return TestClient(app)


def _job(storage, url="https://example.com/jobs/1", **extra):
    payload = {
        "job_url": url,
        "title": "Engineer",
        "company": "Acme",
        "location": "Bengaluru",
        "score": 88,
        "verdict": "Strong fit",
    }
    payload.update(extra)
    return storage.upsert_job(payload)


def test_accept_inserts_one_not_applied_row(client):
    from app import storage

    key = _job(storage)
    response = client.post(f"/jobs/{key}/status", json={"new_status": "accepted"})
    assert response.status_code == 200

    rows = client.get("/applications").json()["applications"]
    assert len(rows) == 1
    row = rows[0]
    assert row["job_key"] == key
    assert row["company"] == "Acme"
    assert row["jobRole"] == "Engineer"
    assert row["link"] == "https://example.com/jobs/1"
    assert row["status"] == "Not Applied"
    assert row["referral"] == ""
    assert row["jobId"] == ""
    assert row["dateApplied"] == ""
    assert "Bengaluru" in row["notes"]
    assert "score 88" in row["notes"]
    assert "Strong fit" in row["notes"]


def test_blank_company_becomes_unknown(client):
    from app import storage

    key = _job(storage, company="")
    client.post(f"/jobs/{key}/status", json={"new_status": "accepted"})
    row = client.get("/applications").json()["applications"][0]
    assert row["company"] == "Unknown"


def test_repeat_accept_does_not_overwrite_edits(client):
    from app import storage

    key = _job(storage)
    client.post(f"/jobs/{key}/status", json={"new_status": "accepted"})
    app_id = client.get("/applications").json()["applications"][0]["id"]

    patched = client.patch(
        f"/applications/{app_id}",
        json={"status": "Applied", "notes": "keep me", "dateApplied": "10/14/2026"},
    )
    assert patched.status_code == 200

    again = client.post(f"/jobs/{key}/status", json={"new_status": "accepted"})
    assert again.status_code == 200

    rows = client.get("/applications").json()["applications"]
    assert len(rows) == 1
    assert rows[0]["status"] == "Applied"
    assert rows[0]["notes"] == "keep me"
    assert rows[0]["dateApplied"] == "10/14/2026"


def test_reject_and_auto_reject_do_not_insert(client):
    from app import storage

    rejected = _job(storage, url="https://example.com/jobs/rej")
    auto = _job(storage, url="https://example.com/jobs/auto")
    assert client.post(f"/jobs/{rejected}/status", json={"new_status": "rejected"}).status_code == 200
    assert storage.set_job_status(auto, "rejected", auto=True) is True

    assert client.get("/applications").json()["applications"] == []


def test_list_backfills_an_already_accepted_job_once(client):
    from app import storage

    key = _job(storage)
    assert storage.set_job_status(key, "accepted") is True

    first = client.get("/applications").json()["applications"]
    assert len(first) == 1
    assert first[0]["status"] == "Not Applied"
    assert first[0]["job_key"] == key

    client.patch(f"/applications/{first[0]['id']}", json={"status": "Interview", "notes": "edited"})
    second = client.get("/applications").json()["applications"]
    assert len(second) == 1
    assert second[0]["status"] == "Interview"
    assert second[0]["notes"] == "edited"


def test_deleted_accepted_job_stays_gone_until_accepted_again(client):
    from app import storage

    key = _job(storage, url="https://example.com/jobs/gone")
    client.post(f"/jobs/{key}/status", json={"new_status": "accepted"})
    app_id = client.get("/applications").json()["applications"][0]["id"]

    assert client.delete(f"/applications/{app_id}").status_code == 200
    assert client.get("/applications").json()["applications"] == []
    assert client.get("/applications").json()["applications"] == []

    again = client.post(f"/jobs/{key}/status", json={"new_status": "accepted"})
    assert again.status_code == 200
    rows = client.get("/applications").json()["applications"]
    assert len(rows) == 1
    assert rows[0]["id"] != app_id
    assert rows[0]["job_key"] == key
    assert rows[0]["status"] == "Not Applied"


def test_create_update_and_delete(client):
    created = client.post("/applications", json={})
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "Not Applied"
    assert body["company"] == ""
    assert body["job_key"] is None

    patched = client.patch(
        f"/applications/{body['id']}",
        json={"company": "Hand", "jobRole": "Role", "referral": "Yes"},
    )
    assert patched.status_code == 200
    assert patched.json()["company"] == "Hand"
    assert patched.json()["jobRole"] == "Role"
    assert patched.json()["referral"] == "Yes"

    assert client.delete(f"/applications/{body['id']}").status_code == 200
    assert client.get("/applications").json()["applications"] == []
    assert client.patch(f"/applications/{body['id']}", json={"company": "x"}).status_code == 404
    assert client.delete(f"/applications/{body['id']}").status_code == 404


def test_invalid_status_is_rejected(client):
    created = client.post("/applications", json={"company": "Acme"}).json()
    bad = client.patch(f"/applications/{created['id']}", json={"status": "Banana"})
    assert bad.status_code == 400
    assert client.get("/applications").json()["applications"][0]["status"] == "Not Applied"


def test_import_replaces_list_and_keeps_link_to_accepted_job(client):
    from app import storage

    key = _job(storage, url="https://example.com/jobs/keep")
    storage.set_job_status(key, "accepted")
    client.post("/applications", json={"company": "Discard me"})

    imported = client.post(
        "/applications/import",
        json={
            "applications": [
                {
                    "company": "Acme",
                    "jobRole": "Engineer",
                    "link": "https://example.com/jobs/keep",
                    "status": "Applied",
                    "notes": "from file",
                }
            ]
        },
    )
    assert imported.status_code == 200
    rows = imported.json()["applications"]
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme"
    assert rows[0]["notes"] == "from file"
    assert rows[0]["job_key"] == key

    listed = client.get("/applications").json()["applications"]
    assert len(listed) == 1
    assert listed[0]["status"] == "Applied"
    assert listed[0]["notes"] == "from file"
