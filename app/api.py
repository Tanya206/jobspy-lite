"""FastAPI application for JobSpy Lite.

Serves the JSON API and (when built) the React SPA from ``frontend/dist``.
Routes
------
GET  /health                liveness + component status
GET  /                      the SPA (when built)
POST /resume                upload a .docx resume (stored for all runs)
GET  /resume                current resume metadata
GET  /jobs                  list jobs (status filter, auto/manual reject filter, order, pagination)
GET  /jobs/{key}            one job with full details
POST /jobs/{key}/status     accept/reject/reset a job (accept also fills Applications)
GET  /applications          tracker rows (backfills jobs already accepted)
POST /applications          add a tracker row by hand
POST /applications/import   replace the tracker list
PATCH /applications/{id}    edit a tracker row
DELETE /applications/{id}   delete a tracker row
GET  /stats                 counts by status (incl. manual vs auto rejected) + average score
GET  /runs                  run history (newest first)
POST /run                   start a manual pipeline run (background)
GET  /run/latest            poll the in-flight/last manual run + progress
GET  /scheduler             scheduler status + config
POST /scheduler/config      update cron/interval config (live re-applied)
POST /scheduler/toggle      start/stop the scheduler
GET  /settings              effective settings (config + stored overrides)
POST /settings              persist scrape params / experience window / threshold
GET  /export.xlsx           download the Excel export
"""

import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from . import notify, scheduler, storage
from .agent.resume_io import extract_text
from .config import EXCEL_PATH, PROJECT_ROOT, clamp_int
from .pipeline import PipelineContext, request_shutdown, run_pipeline, wait_for_idle
from .schemas import (
    ApplicationImport,
    ApplicationPatch,
    ApplicationWrite,
    HealthResponse,
    JobListResponse,
    JobStatusIn,
    PurgeIn,
    ResumeInfo,
    RunLatest,
    SchedulerStatus,
    SettingsIn,
    StatsResponse,
)

MAX_RESUME_BYTES = 2 * 1024 * 1024

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """On shutdown, stop taking new scores and wait for the current run."""
    yield
    request_shutdown()
    wait_for_idle(180)


app = FastAPI(
    title="JobSpy Lite API",
    version="0.1.0",
    description="Scrape jobs, score them against a stored resume, run on a "
    "schedule, and get Telegram alerts for the best matches.",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _presented_token(request: Request) -> str:
    header = request.headers.get("x-api-token", "").strip()
    if header:
        return header
    return request.query_params.get("token", "").strip()


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    """Require JOBSPY_API_TOKEN only when that env var is set.

    Unset means the API is open, which is how the Oracle Cloud deploy is
    used: the dashboard is opened from a browser on the public IP, and no
    token is shared. /health, the SPA, and /assets stay open either way so
    the page and the Docker healthcheck can load.
    """
    path = request.url.path
    if (
        request.method == "OPTIONS"
        or path == "/health"
        or path == "/"
        or path.startswith("/assets")
    ):
        return await call_next(request)
    expected = os.environ.get("JOBSPY_API_TOKEN", "").strip()
    if not expected:
        return await call_next(request)
    presented = _presented_token(request)
    if len(presented) == len(expected) and secrets.compare_digest(presented, expected):
        return await call_next(request)
    return _unauthorized("Unauthorized")


def _unauthorized(detail: str) -> JSONResponse:
    response = JSONResponse(status_code=401, content={"detail": detail})
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response

# In-flight manual-run state (process-local; run rows live in SQLite).
_run_state_lock = threading.Lock()
_run_state: Dict[str, Any] = {"running": False, "progress": None, "percent": None,
                              "summary": None, "error": None}


def _resume_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    from .config import AI_MODEL, DB_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    db_ready = False
    try:
        storage.query("SELECT 1")
        db_ready = True
    except Exception:  # noqa: BLE001 - health must never raise
        db_ready = False
    resume = storage.get_setting("resume") or {}
    return HealthResponse(
        status="ok",
        ai_model=AI_MODEL,
        scheduler_running=scheduler.is_healthy(),
        resume_uploaded=bool(resume.get("text")),
        telegram_configured=bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        db_ready=db_ready,
    )


# --------------------------------------------------------------------------- #
# Resume (step 3)
# --------------------------------------------------------------------------- #
@app.post("/resume", response_model=ResumeInfo)
async def upload_resume(resume: UploadFile = File(...)) -> ResumeInfo:
    """Upload a .docx resume; its text is stored once and used by every run."""
    data = await resume.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty resume file.")
    if len(data) > MAX_RESUME_BYTES:
        raise HTTPException(status_code=413, detail="Resume must be 2 MB or smaller.")
    try:
        text = extract_text(data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=f"Could not read resume. Only valid .docx files are supported ({exc}).",
        )
    if not text.strip():
        raise HTTPException(status_code=400, detail="Resume contained no readable text.")

    fingerprint = _resume_hash(text)
    from datetime import datetime, timezone

    storage.set_setting(
        "resume",
        {"text": text, "name": resume.filename, "hash": fingerprint,
         "uploaded_at": datetime.now(timezone.utc).isoformat()},
    )
    logger.info("Resume stored: name=%s chars=%d", resume.filename, len(text))
    return ResumeInfo(name=resume.filename, chars=len(text), hash=fingerprint)


@app.get("/resume", response_model=ResumeInfo)
def resume_info() -> ResumeInfo:
    resume = storage.get_setting("resume") or {}
    return ResumeInfo(
        name=resume.get("name"),
        chars=len(resume.get("text") or "") or None,
        hash=resume.get("hash"),
        uploaded_at=resume.get("uploaded_at"),
    )


# --------------------------------------------------------------------------- #
# Jobs + accept/reject (step 6)
# --------------------------------------------------------------------------- #
@app.get("/jobs", response_model=JobListResponse)
def list_jobs_endpoint(
    status: Optional[str] = None,
    auto_rejected: Optional[bool] = None,
    order: str = "score",
    limit: Optional[int] = None,
    offset: int = 0,
) -> JobListResponse:
    """List jobs; ``auto_rejected`` narrows rejected rows to the pipeline's
    experience-window rejects (true) or the operator's own rejects (false)."""
    if status and status not in ("new", "accepted", "rejected"):
        raise HTTPException(status_code=400, detail="status must be new/accepted/rejected.")
    jobs = storage.list_jobs(
        status=status, limit=limit, offset=offset, order=order,
        auto_rejected=auto_rejected,
    )
    counts = storage.counts_by_status()
    filtered_total = storage.count_jobs(status=status, auto_rejected=auto_rejected)
    return JobListResponse(
        count=len(jobs),
        total=counts.get("total", 0),
        filtered_total=filtered_total,
        jobs=jobs,
    )


@app.get("/jobs/{job_key:path}")
def get_job_endpoint(job_key: str) -> Dict[str, Any]:
    job = storage.get_job(job_key)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job '{job_key}'.")
    return job


@app.post("/jobs/{job_key:path}/status")
def set_job_status_endpoint(job_key: str, payload: JobStatusIn) -> Dict[str, Any]:
    try:
        updated = storage.set_job_status(job_key, payload.new_status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=404, detail=f"Unknown job '{job_key}'.")
    if payload.new_status == "accepted":
        job = storage.get_job(job_key)
        if job is not None:
            storage.ensure_application_for_job(job)
    return {"job_key": job_key, "status": payload.new_status}


@app.get("/applications")
def list_applications() -> Dict[str, Any]:
    return {"applications": storage.list_applications()}


@app.post("/applications", status_code=201)
def create_application(payload: ApplicationWrite) -> Dict[str, Any]:
    try:
        return storage.create_application(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/applications/import")
def import_applications(payload: ApplicationImport) -> Dict[str, Any]:
    try:
        rows = storage.replace_applications(
            [item.model_dump() for item in payload.applications]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"applications": rows}


@app.patch("/applications/{app_id}")
def update_application(app_id: str, payload: ApplicationPatch) -> Dict[str, Any]:
    try:
        updated = storage.update_application(
            app_id, payload.model_dump(exclude_unset=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Unknown application '{app_id}'.")
    return updated


@app.delete("/applications/{app_id}")
def delete_application(app_id: str) -> Dict[str, Any]:
    if not storage.delete_application(app_id):
        raise HTTPException(status_code=404, detail=f"Unknown application '{app_id}'.")
    return {"id": app_id, "deleted": True}


@app.get("/stats", response_model=StatsResponse)
def stats() -> StatsResponse:
    counts = storage.counts_by_status()
    return StatsResponse(
        total=counts.get("total", 0),
        new=counts.get("new", 0),
        accepted=counts.get("accepted", 0),
        rejected=counts.get("rejected", 0),
        manual_rejected=counts.get("manual_rejected", 0),
        auto_rejected=counts.get("auto_rejected", 0),
        avg_score=counts.get("avg_score"),
    )


# --------------------------------------------------------------------------- #
# Runs + manual trigger (steps 1/4)
# --------------------------------------------------------------------------- #
@app.post("/run", status_code=202)
def start_run(
    background: BackgroundTasks, force_rescore: bool = False
) -> Dict[str, Any]:
    """Start a manual pipeline run in the background; poll /run/latest."""
    with _run_state_lock:
        if _run_state.get("running"):
            raise HTTPException(status_code=409, detail="A run is already in progress.")
        _run_state.update(running=True, progress="queued", percent=0,
                          summary=None, error=None)

    def _task() -> None:
        def _progress(message: str, percent: Optional[float]) -> None:
            with _run_state_lock:
                _run_state["progress"] = message
                _run_state["percent"] = percent

        try:
            summary = run_pipeline(
                trigger="manual", force_rescore=force_rescore,
                progress_callback=_progress,
            )
            with _run_state_lock:
                _run_state.update(running=False, summary=summary,
                                  error=summary.get("error"))
        except Exception as exc:  # noqa: BLE001 - surfaced via /run/latest
            logger.exception("Manual run crashed")
            with _run_state_lock:
                _run_state.update(running=False, error=str(exc))

    background.add_task(_task)
    return {"started": True}


@app.get("/run/latest", response_model=RunLatest)
def run_latest() -> RunLatest:
    with _run_state_lock:
        state = dict(_run_state)
    last_db = storage.list_runs(limit=1)
    return RunLatest(
        running=state.get("running", False),
        progress=state.get("progress"),
        percent=state.get("percent"),
        summary=state.get("summary"),
        error=state.get("error"),
        last_run=last_db[0] if last_db else None,
    )


@app.get("/runs")
def runs(limit: int = 50) -> Dict[str, Any]:
    return {"runs": storage.list_runs(limit=limit)}


# --------------------------------------------------------------------------- #
# Scheduler (step 4)
# --------------------------------------------------------------------------- #
@app.get("/scheduler", response_model=SchedulerStatus)
def scheduler_status() -> SchedulerStatus:
    return SchedulerStatus(**scheduler.status())


@app.post("/scheduler/config", response_model=SchedulerStatus)
def scheduler_config(config: Dict[str, Any]) -> SchedulerStatus:
    try:
        scheduler.set_config(config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return SchedulerStatus(**scheduler.status())


@app.post("/scheduler/toggle", response_model=SchedulerStatus)
def scheduler_toggle(enabled: bool) -> SchedulerStatus:
    scheduler.toggle(enabled)
    return SchedulerStatus(**scheduler.status())


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@app.get("/settings")
def get_settings() -> Dict[str, Any]:
    from .config import agent_defaults, scrape_defaults

    context = PipelineContext()
    return {
        "scrape_params": {**scrape_defaults(), **(storage.get_setting("scrape_params") or {})},
        "agent_params": {**agent_defaults(), **(storage.get_setting("agent_params") or {})},
        "experience_min_years": context.experience_min,
        "experience_max_years": context.experience_max,
        "score_threshold": context.score_threshold,
        "max_workers": context.max_workers,
        "retention_days": context.retention_days,
    }


@app.post("/settings")
def update_settings(settings: SettingsIn) -> Dict[str, Any]:
    if settings.scrape_params is not None:
        params = dict(settings.scrape_params)
        if "results_wanted" in params:
            params["results_wanted"] = clamp_int(params.get("results_wanted"), 1, 200, 15)
        if "hours_old" in params:
            params["hours_old"] = clamp_int(params.get("hours_old"), 1, 168, 24)
        storage.set_setting("scrape_params", params)
    if settings.agent_params is not None:
        params = dict(settings.agent_params)
        # API keys are env/server-only, never dashboard-editable.
        params.pop("api_key", None)
        model = str(params.get("ai_model") or "").strip()
        if not model:
            params.pop("ai_model", None)
        base_url = str(params.get("ai_base_url") or "").strip()
        params["ai_base_url"] = base_url  # empty string = fall back to env default
        rate = params.get("ai_rate_limit_per_min")
        try:
            params["ai_rate_limit_per_min"] = max(0, int(rate)) if rate is not None else 30
        except (TypeError, ValueError):
            params["ai_rate_limit_per_min"] = 30
        if "max_workers" in params:
            params["max_workers"] = clamp_int(params.get("max_workers"), 1, 8, 4)
        storage.set_setting("agent_params", params)
    if settings.experience_min_years is not None:
        storage.set_setting("experience_min_years", settings.experience_min_years)
    if settings.experience_max_years is not None:
        storage.set_setting("experience_max_years", settings.experience_max_years)
    if settings.score_threshold is not None:
        storage.set_setting("score_threshold", settings.score_threshold)
    if settings.retention_days is not None:
        # 0 = keep everything; negative values are clamped to 0.
        storage.set_setting("retention_days", max(0, int(settings.retention_days)))
    return get_settings()


# --------------------------------------------------------------------------- #
# Excel export (step 1)
# --------------------------------------------------------------------------- #
@app.get("/export.xlsx")
def export_xlsx() -> FileResponse:
    from .excel import export_excel

    path = export_excel()
    if not path.exists():
        raise HTTPException(status_code=500, detail="Excel export failed.")
    return FileResponse(path, filename="jobs.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --------------------------------------------------------------------------- #
# Notification test
# --------------------------------------------------------------------------- #
@app.post("/maintenance/purge")
def purge_old_data(payload: PurgeIn) -> Dict[str, int]:
    """Delete jobs and run history older than N days. Accepted jobs stay."""
    if payload.older_than_days < 1:
        raise HTTPException(status_code=400, detail="older_than_days must be >= 1.")
    try:
        return storage.purge_older_than(payload.older_than_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/notify/test")
def notify_test() -> Dict[str, Any]:
    """Send any pending above-threshold matches to Telegram right now."""
    context = PipelineContext()
    sent = notify.notify_above_threshold(context.score_threshold)
    return {"sent": sent}


# --------------------------------------------------------------------------- #
# Static SPA (built frontend)
# --------------------------------------------------------------------------- #
_DIST = PROJECT_ROOT / "frontend" / "dist"
if (_DIST / "index.html").exists():  # pragma: no cover - build-dependent
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_DIST / "index.html")
else:

    @app.get("/", include_in_schema=False)
    def index_dev() -> JSONResponse:
        return JSONResponse(
            {
                "message": "JobSpy Lite API is running. The UI is not built yet — "
                "run `npm run build` in frontend/ or use the Vite dev server."
            }
        )
