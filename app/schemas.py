"""Pydantic models for the JobSpy Lite API."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str
    ai_model: str
    scheduler_running: bool
    resume_uploaded: bool
    telegram_configured: bool
    db_ready: bool


class ResumeInfo(BaseModel):
    name: Optional[str] = None
    chars: Optional[int] = None
    hash: Optional[str] = None
    uploaded_at: Optional[str] = None


class JobListResponse(BaseModel):
    count: int
    total: int
    # Rows matching status/auto_rejected before pagination — lets the UI show
    # an accurate badge when only the first N jobs are loaded.
    filtered_total: int = 0
    jobs: List[Dict[str, Any]]


class StatsResponse(BaseModel):
    total: int
    new: int
    accepted: int
    rejected: int
    # rejected split by provenance: the operator's own rejects vs the
    # pipeline's experience-window rejects.
    manual_rejected: int = 0
    auto_rejected: int = 0
    avg_score: Optional[float] = None


class RunLatest(BaseModel):
    running: bool
    progress: Optional[str] = None
    percent: Optional[float] = None
    summary: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    last_run: Optional[Dict[str, Any]] = None


class SchedulerStatus(BaseModel):
    running: bool
    enabled: bool
    config: Dict[str, Any]
    next_run_at: Optional[str] = None
    last_run: Optional[Dict[str, Any]] = None


class SettingsIn(BaseModel):
    scrape_params: Optional[Dict[str, Any]] = None
    agent_params: Optional[Dict[str, Any]] = None
    experience_min_years: Optional[int] = None
    experience_max_years: Optional[int] = None
    score_threshold: Optional[int] = None
    # 0 = keep everything; N = delete postings not seen in N days each run.
    retention_days: Optional[int] = None


class JobStatusIn(BaseModel):
    new_status: str


class PurgeIn(BaseModel):
    older_than_days: int


class ApplicationWrite(BaseModel):
    """A tracker row. CamelCase aliases match the spreadsheet the UI edits."""

    model_config = ConfigDict(populate_by_name=True)

    company: str = ""
    job_role: str = Field("", alias="jobRole")
    job_id: str = Field("", alias="jobId")
    link: str = ""
    referral: str = ""
    status: str = "Not Applied"
    date_applied: str = Field("", alias="dateApplied")
    notes: str = ""


class ApplicationPatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    company: Optional[str] = None
    job_role: Optional[str] = Field(None, alias="jobRole")
    job_id: Optional[str] = Field(None, alias="jobId")
    link: Optional[str] = None
    referral: Optional[str] = None
    status: Optional[str] = None
    date_applied: Optional[str] = Field(None, alias="dateApplied")
    notes: Optional[str] = None


class ApplicationImportItem(ApplicationWrite):
    id: Optional[str] = None
    job_key: Optional[str] = Field(None, alias="jobKey")
    date_added: Optional[str] = Field(None, alias="dateAdded")


class ApplicationImport(BaseModel):
    applications: List[ApplicationImportItem]
