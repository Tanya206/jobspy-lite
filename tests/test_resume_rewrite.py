"""Resume rewrite loop, grounding, and golden-set export. No network."""

from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.agent.hallucination import scan_patch
from app.agent.resume_rewrite import accept_patches, run_loop
from app.agent.resume_rewrite_schema import (
    AtsScore,
    ClaimJudgement,
    EditPlan,
    ExecutorOutput,
    HallucinationCheck,
    RewriterOutput,
    TextPatch,
)


def _ats(total: int) -> AtsScore:
    base = total // 7
    extra = total - (base * 7)
    values = [base + (1 if index < extra else 0) for index in range(7)]
    return AtsScore(
        keyword_match=values[0],
        quantification=values[1],
        action_verbs=values[2],
        parsability=values[3],
        readability=values[4],
        title_alignment=values[5],
        summary_punch=values[6],
        notes="notes",
    )


def _docx_bytes() -> bytes:
    document = Document()
    document.add_paragraph("Jane Doe")
    document.add_paragraph("Built APIs in Python.")
    document.add_paragraph("Python services")
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class _Scripted:
    """Seven-step script. The second try scores lower and proposes a bad metric."""

    def __init__(self, config=None):
        self.config = config or {}
        self.labels = []
        self._exec = 0
        self._ats = 0

    def parse(self, messages, text_format, temperature=0.0, label="ai-call"):
        self.labels.append(label)
        if text_format is EditPlan:
            return EditPlan(targets=["Jane Doe", "Built APIs in Python."])
        if text_format is RewriterOutput:
            return RewriterOutput(patches=[])
        if text_format is AtsScore:
            self._ats += 1
            # ats, ats-2, ats, ats-2 → 40, 50, 40, 30
            totals = {1: 40, 2: 50, 3: 40, 4: 30}
            return _ats(totals[self._ats])
        if text_format is ExecutorOutput:
            if label != "executor-2":
                return ExecutorOutput(patches=[])
            self._exec += 1
            if self._exec == 1:
                return ExecutorOutput(
                    patches=[
                        TextPatch(original="Jane Doe", replacement="Jane Doe, engineer"),
                        TextPatch(
                            original="Built APIs in Python.",
                            replacement="Built APIs in Python and Kubernetes.",
                        ),
                        TextPatch(original="Python", replacement="Python expert"),
                        TextPatch(
                            original="Built APIs in Python.",
                            replacement="Built APIs in Python and saved $2M.",
                        ),
                    ],
                    keyword_summary="kubernetes",
                    weak_spots="none",
                )
            return ExecutorOutput(
                patches=[
                    TextPatch(original="Jane Doe", replacement="Jane Doe, intern"),
                ]
            )
        raise AssertionError(text_format)


@pytest.fixture()
def client(temp_db, monkeypatch):
    monkeypatch.setattr("app.agent.resume_rewrite.AIClient", _Scripted)
    from app.api import app

    return TestClient(app)


def _finished(client, key, rewrite_id):
    import time

    for _ in range(100):
        rows = client.get(f"/jobs/{key}/resume-rewrites").json()["rewrites"]
        match = next((row for row in rows if row["id"] == rewrite_id), None)
        if match and match.get("status") != "running":
            return match
        time.sleep(0.05)
    raise AssertionError("rewrite did not finish")


def _upload(client) -> None:
    client.post(
        "/resume",
        files={
            "resume": (
                "resume.docx",
                _docx_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )


def test_missing_resume_and_description(client):
    from app import storage

    key = storage.upsert_job(
        {"job_url": "https://x/1", "title": "Role", "description": "Build Python services."}
    )
    missing_resume = client.post(f"/jobs/{key}/resume-rewrite", json={})
    assert missing_resume.status_code == 400
    assert "resume" in missing_resume.json()["detail"].lower()

    _upload(client)
    empty = storage.upsert_job({"job_url": "https://x/2", "title": "Empty", "description": "  "})
    missing_desc = client.post(f"/jobs/{empty}/resume-rewrite", json={})
    assert missing_desc.status_code == 400
    assert "description" in missing_desc.json()["detail"].lower()


def test_keeps_highest_score_and_leaves_scoring_resume(client):
    from app.agent.resume_io import extract_text, load_resume_docx, paragraph_count
    from app import storage

    _upload(client)
    before = storage.get_setting("resume")
    storage.set_setting("additional_info", "I write Python.")
    key = storage.upsert_job(
        {
            "job_url": "https://x/rewrite",
            "title": "Backend Engineer",
            "company": "Acme",
            "score": 70,
            "verdict": "strong",
            "description": "Python Python services for backend systems.",
        }
    )
    response = client.post(
        f"/jobs/{key}/resume-rewrite",
        json={
            "max_tries": 2,
            "model": "other-model",
            "base_url": "http://rewrite.example/v1",
            "additional_info": "I write Python.\nI used Kubernetes.",
            "company_type": "startup",
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "running"
    body = _finished(client, key, response.json()["id"])
    assert body["model"] == "other-model"
    assert body["winning_total"] == 50
    assert body["max_tries"] == 2
    ok = [row for row in body["trace"] if row["ok"]]
    assert len(ok) == 2
    assert ok[1]["total"] == 30
    assert ok[0]["winner"] is True
    assert ok[1]["winner"] is False
    texts = [patch["replacement"] for patch in body["winning_patches"]]
    assert "Built APIs in Python and Kubernetes." in texts
    assert not any("$2M" in text for text in texts)
    assert not any(patch["original"] == "Python" for patch in body["winning_patches"])
    assert storage.get_setting("resume")["hash"] == before["hash"]
    assert storage.get_setting("resume")["text"] == before["text"]
    assert storage.get_setting("additional_info") == "I write Python."
    assert storage.get_job(key)["score"] == 70
    assert body["docx_ready"] is True

    original = load_resume_docx()
    assert paragraph_count(original) == 3
    download = client.get(f"/jobs/{key}/resume-rewrites/{body['id']}/docx")
    assert download.status_code == 200
    assert "Rohil_Chaturvedi_Acme.docx" in download.headers["content-disposition"]
    assert paragraph_count(download.content) == paragraph_count(original)
    patched = extract_text(download.content)
    assert "Jane Doe, engineer" in patched
    assert "Kubernetes" in patched
    assert "intern" not in patched

    listed = client.get(f"/jobs/{key}/resume-rewrites").json()["rewrites"]
    assert listed[0]["id"] == body["id"]


def test_failed_tries_do_not_write_a_file(client):
    from app import storage

    class _Boom:
        def __init__(self, config=None):
            self.config = config

        def parse(self, messages, text_format, temperature=0.0, label="ai-call"):
            raise RuntimeError("unusable")

    # The fixture patched AIClient; this test replaces it again.
    import app.agent.resume_rewrite as rewrite_module

    rewrite_module.AIClient = _Boom
    _upload(client)
    key = storage.upsert_job(
        {"job_url": "https://x/fail", "title": "Role", "description": "A real description."}
    )
    response = client.post(f"/jobs/{key}/resume-rewrite", json={"max_tries": 3})
    assert response.status_code == 202
    body = _finished(client, key, response.json()["id"])
    assert body["error"]
    assert body["docx_ready"] is False
    assert body["winning_patches"] == []
    assert len(body["trace"]) == 2


def test_non_unique_and_ungrounded_patches_are_dropped():
    current = "Jane Doe knows Python.\nBuilt APIs in Python."
    allowed = current + "\nI used Kubernetes."
    result = accept_patches(
        current,
        [
            TextPatch(original="Python", replacement="Python expert"),
            TextPatch(original="Built APIs in Python.", replacement="Built APIs in Python and saved $2M."),
            TextPatch(original="Built APIs in Python.", replacement="Built APIs in Python and Kubernetes."),
        ],
        allowed,
    )
    assert [row["reason"] for row in result["dropped"]] == ["not_unique", "ungrounded"]
    assert result["kept"][0]["replacement"].endswith("Kubernetes.")


def test_skill_only_in_the_job_description_is_a_hallucination():
    from app.eval.rewrite import audit_patches

    resume = "Built APIs in Python."
    misses = scan_patch(
        resume,
        "Built APIs in Python and Kubernetes.",
        resume,
    )
    assert any(item["span"] == "Kubernetes" and item["kind"] == "invented_tool" for item in misses)

    def checker(messages, text_format):
        return HallucinationCheck(
            claims=[ClaimJudgement(claim="knows Kubernetes", supported=False)]
        )

    findings = audit_patches(
        [{"original": resume, "replacement": "Built APIs in Python and Kubernetes."}],
        resume,
        "",
        checker,
    )
    assert any(item["kind"] == "unsupported_claim" for item in findings)


def test_loop_stops_after_successful_tries():
    calls = {"n": 0}

    def complete(messages, text_format, label):
        calls["n"] += 1
        if text_format is EditPlan:
            return EditPlan()
        if text_format is RewriterOutput:
            return RewriterOutput()
        if text_format is AtsScore:
            return _ats(50 if label == "ats-2" else 10)
        return ExecutorOutput(
            patches=[TextPatch(original="Jane Doe", replacement="Jane Doe, engineer")]
            if label == "executor-2"
            else []
        )

    result = run_loop(
        resume_text="Jane Doe\nBuilt APIs in Python.",
        job={"title": "Role", "description": "Backend role."},
        additional_info="",
        max_tries=2,
        complete=complete,
    )
    assert calls["n"] == 14
    assert result["winning_total"] == 50
    assert len([row for row in result["trace"] if row["ok"]]) == 2


def test_patch_that_grows_the_page_is_dropped():
    from docx.shared import Inches

    from app.agent.resume_io import apply_patches_to_docx, keep_one_page, page_metrics

    document = Document()
    section = document.sections[0]
    section.page_width = Inches(2.5)
    section.page_height = Inches(1.0)
    section.left_margin = Inches(0.4)
    section.right_margin = Inches(0.4)
    section.top_margin = Inches(0.3)
    section.bottom_margin = Inches(0.3)
    document.add_paragraph("Short line")
    buffer = BytesIO()
    document.save(buffer)
    original = buffer.getvalue()
    budget = page_metrics(original)[1]

    kept, dropped = keep_one_page(
        original,
        original,
        [{
            "original": "Short line",
            "replacement": "Short line plus many extra words that wrap onto another line",
        }],
    )
    assert kept == []
    assert dropped[0]["reason"] == "overflows_page"

    kept, dropped = keep_one_page(
        original,
        original,
        [{"original": "Short line", "replacement": "Line"}],
    )
    assert dropped == []
    patched, _applied = apply_patches_to_docx(original, [kept])
    assert page_metrics(patched)[1] <= budget


def test_packed_resume_stays_on_one_page_after_calibration():
    from docx.shared import Inches, Pt

    from app.agent.resume_io import enforce_one_page, one_page_factor, page_metrics

    document = Document()
    section = document.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.left_margin = Inches(0.5)
    section.right_margin = Inches(0.5)
    section.top_margin = Inches(0.5)
    section.bottom_margin = Inches(0.5)
    paragraph = document.add_paragraph("word " * 2000)
    for run in paragraph.runs:
        run.font.size = Pt(11)
    buffer = BytesIO()
    document.save(buffer)
    original = buffer.getvalue()
    assert page_metrics(original)[0] >= 2
    factor = one_page_factor(original)
    assert page_metrics(original, factor)[0] == 1

    span = "word " * 40
    fitted, rows = enforce_one_page(
        original,
        [[{"original": span, "replacement": span + ("extra " * 200)}]],
    )
    assert rows[0]["applied"] is False
    assert rows[0]["reason"] == "overflows_page"
    assert page_metrics(fitted, factor)[0] == 1


def test_docx_chain_applies_winning_lineage_in_order():
    from app.agent.resume_io import apply_patches_to_docx, extract_text

    patched, applied = apply_patches_to_docx(
        _docx_bytes(),
        [
            [{"original": "Jane Doe", "replacement": "Jane Doe, engineer"}],
            [{"original": "Jane Doe, engineer", "replacement": "Jane Doe, engineer, remote"}],
        ],
    )
    text = extract_text(patched)
    assert "Jane Doe, engineer, remote" in text
    assert all(row["applied"] for row in applied)
    from app.agent.resume_io import paragraph_count

    assert paragraph_count(patched) == paragraph_count(_docx_bytes())


def test_max_tries_is_clamped(client, monkeypatch):
    from app import storage
    import app.agent.resume_rewrite as rewrite_module

    class _Boom:
        def __init__(self, config=None):
            self.config = config

        def parse(self, messages, text_format, temperature=0.0, label="ai-call"):
            raise RuntimeError("unusable")

    monkeypatch.setattr(rewrite_module, "AIClient", _Boom)
    _upload(client)
    key = storage.upsert_job(
        {"job_url": "https://x/clamp", "title": "Role", "description": "A real description."}
    )
    response = client.post(f"/jobs/{key}/resume-rewrite", json={"max_tries": 9})
    assert response.status_code == 202
    assert response.json()["max_tries"] == 5
    assert _finished(client, key, response.json()["id"])["status"] == "error"


def test_export_golden_skips_empty_descriptions(temp_db):
    from app import storage
    from app.eval.rewrite import export_golden

    storage.set_setting("resume", {"text": "Jane Doe\nBuilt APIs in Python.", "hash": "abc"})
    storage.set_setting("additional_info", "I used Kubernetes.")
    storage.upsert_job(
        {"job_url": "https://x/s", "title": "Strong", "description": "Alpha role", "verdict": "strong", "score": 80}
    )
    storage.upsert_job(
        {"job_url": "https://x/m", "title": "Mid", "description": "Beta role", "verdict": "moderate", "score": 60}
    )
    storage.upsert_job(
        {"job_url": "https://x/w", "title": "Weak", "description": "Gamma role", "verdict": "weak", "score": 20}
    )
    storage.upsert_job(
        {"job_url": "https://x/e", "title": "Blank", "description": "", "verdict": "strong", "score": 90}
    )
    path = export_golden(limit=30)
    import json

    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) == 3
    assert {row["verdict"] for row in rows} == {"strong", "moderate", "weak"}
    folder = path.parent
    assert "Jane Doe" in (folder / "resume.txt").read_text(encoding="utf-8")
    assert "Kubernetes" in (folder / "additional_info.txt").read_text(encoding="utf-8")


def test_rewrite_target_picks_the_key_for_the_host(monkeypatch):
    from app.agent.resume_rewrite import rewrite_target

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-key")
    monkeypatch.delenv("REWRITE_API_KEY", raising=False)

    openai = rewrite_target("", "https://api.openai.com/v1", {})
    assert openai["api_key"] == "sk-openai"
    assert openai["base_url"] == "https://api.openai.com/v1"
    assert openai["rate_limited"] is False

    nvidia = rewrite_target("", "https://integrate.api.nvidia.com/v1", {})
    assert nvidia["api_key"] == "nv-key"
    assert nvidia["rate_limited"] is True

    monkeypatch.setenv("REWRITE_API_KEY", "rewrite-key")
    overridden = rewrite_target("", "https://api.openai.com/v1", {})
    assert overridden["api_key"] == "rewrite-key"


def test_changes_keep_earlier_patches_when_the_winning_try_adds_none():
    from app.api import _stamp_rewrite

    result = {
        "winner_try": 2,
        "patch_chain": [
            [{"original": "old line", "replacement": "new line"}],
            [],
        ],
        "trace": [
            {"try": 1, "patches": [{"original": "old line", "replacement": "new line"}]},
            {"try": 2, "patches": []},
        ],
    }
    winning = _stamp_rewrite(
        result,
        [{"original": "old line", "replacement": "new line", "applied": True}],
    )
    assert result["trace"][0]["winner"] is False
    assert result["trace"][1]["winner"] is True
    assert winning == [
        {"original": "old line", "replacement": "new line", "applied": True},
    ]
