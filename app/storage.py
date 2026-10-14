"""SQLite persistence layer for JobSpy Lite.

Three tables, all created idempotently:

- ``jobs``     — one row per unique posting (keyed by the stable job identity).
                 Screening annotations are upserted; user state (``status``,
                 ``notified``) is never clobbered by re-scrapes.
- ``runs``     — one row per pipeline execution (cron or manual), for history
                 and for verifying the scheduler actually fired.
- ``settings`` — JSON-key/value store for runtime-editable configuration
                 (threshold, experience window, scrape params, cron config,
                 the stored resume).

WAL mode is enabled for safe concurrent reads while the pipeline writes.
"""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import DB_PATH
from .util import make_json_safe

_conn_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_key            TEXT PRIMARY KEY,
    site               TEXT,
    title              TEXT,
    company            TEXT,
    location           TEXT,
    description        TEXT,
    job_url            TEXT,
    date_posted        TEXT,
    job_type           TEXT,
    is_remote          INTEGER,
    min_amount         REAL,
    max_amount         REAL,
    currency           TEXT,
    interval           TEXT,
    required_years_min INTEGER,
    required_years_max INTEGER,
    experience_match   INTEGER,
    score              REAL,
    verdict            TEXT,
    matched_skills     TEXT,
    missing_skills     TEXT,
    reasoning          TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    notified           INTEGER NOT NULL DEFAULT 0,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    scored_at          TEXT,
    scored_with_resume TEXT,
    score_attempts     INTEGER NOT NULL DEFAULT 0,
    score_error        TEXT,
    auto_rejected      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score);
CREATE INDEX IF NOT EXISTS idx_jobs_seen ON jobs(first_seen_at);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL UNIQUE,
    trigger       TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    jobs_scraped  INTEGER,
    new_jobs      INTEGER,
    scored        INTEGER,
    alerts_sent   INTEGER,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    """Open (once) and return the process-wide SQLite connection."""
    global _conn
    with _conn_lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            # A second process (--once while --serve is up) waits instead of
            # raising "database is locked" on the first collision.
            _conn.execute("PRAGMA busy_timeout=5000")
            _conn.executescript(_SCHEMA)
            _migrate()
            _conn.execute(_STALE_FAILURE_SQL)
            _conn.execute(_UNSCORED_MATCH_SQL)
            _conn.commit()
        return _conn


# One-time backfill marker: legacy rows carry no auto/manual provenance, so
# the heuristic below must not re-run (it would flip a user's later manual
# reject of an experience-mismatch job back to 'auto').
_AUTO_REJECT_BACKFILL_KEY = "auto_rejected_backfilled"


def _migrate() -> None:
    """Lightweight column additions for pre-existing databases."""
    cols = {row["name"] for row in _conn.execute("PRAGMA table_info(jobs)")}
    for name, ddl in (
        ("score_attempts", "ALTER TABLE jobs ADD COLUMN score_attempts INTEGER NOT NULL DEFAULT 0"),
        ("score_error", "ALTER TABLE jobs ADD COLUMN score_error TEXT"),
        ("auto_rejected", "ALTER TABLE jobs ADD COLUMN auto_rejected INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in cols:
            _conn.execute(ddl)
    run_cols = {row["name"] for row in _conn.execute("PRAGMA table_info(runs)")}
    for name, ddl in (
        ("score_failed", "ALTER TABLE runs ADD COLUMN score_failed INTEGER"),
        ("auto_rejected", "ALTER TABLE runs ADD COLUMN auto_rejected INTEGER"),
        ("pruned", "ALTER TABLE runs ADD COLUMN pruned INTEGER"),
        ("scrape_errors", "ALTER TABLE runs ADD COLUMN scrape_errors TEXT"),
        ("ai_metrics", "ALTER TABLE runs ADD COLUMN ai_metrics TEXT"),
    ):
        if name not in run_cols:
            _conn.execute(ddl)
    # One-time backfill for rows written before auto/manual was tracked:
    # rejected rows the pipeline flagged as experience mismatches are the
    # auto-rejected ones. Runs once (guarded by a settings row) because a
    # later manual reject can legitimately carry the same mismatch data.
    marker = _conn.execute(
        "SELECT value FROM settings WHERE key = ?", (_AUTO_REJECT_BACKFILL_KEY,)
    ).fetchone()
    if marker is None:
        _conn.execute(
            "UPDATE jobs SET auto_rejected = 1 "
            "WHERE status = 'rejected' AND experience_match = 0"
        )
        _conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, 'true')",
            (_AUTO_REJECT_BACKFILL_KEY,),
        )


@contextmanager
def transaction():
    """Yield a connection with an open transaction; commit or roll back."""
    conn = connect()
    with _conn_lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Run a read-only query, returning rows as dicts."""
    conn = connect()
    with _conn_lock:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def query_one(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = query(sql, params)
    return rows[0] if rows else None


def _json_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(make_json_safe(value), ensure_ascii=False)


def _experience_match_value(annotated: Dict[str, Any]) -> Optional[int]:
    """experience_match column: 1/0 once AI has assessed the posting, NULL
    before that. Storing 0 on raw (unscored) inserts made the UI show
    'outside window' on jobs that were never screened."""
    match = annotated.get("experience_match")
    if match is not None:
        return 1 if match else 0
    if annotated.get("required_years") is not None:
        return 0
    return None


def _job_row_values(job_key_value: str, annotated: Dict[str, Any], now: str,
                    existing: Optional[Dict[str, Any]]) -> tuple:
    """Build the column tuple for an upsert of one annotated job."""
    required = annotated.get("required_years") or {}
    required_min = required.get("min") if isinstance(required, dict) else None
    required_max = required.get("max") if isinstance(required, dict) else None
    score = annotated.get("final_score")
    if score is None:
        score = annotated.get("score")
    scored = bool(
        annotated.get("score") is not None or annotated.get("required_years") is not None
    )
    return (
        job_key_value,
        annotated.get("site"),
        annotated.get("title"),
        annotated.get("company"),
        annotated.get("location"),
        annotated.get("description"),
        annotated.get("job_url") or annotated.get("url"),
        str(annotated.get("date_posted")) if annotated.get("date_posted") is not None else None,
        annotated.get("job_type") or annotated.get("employment_type"),
        1 if annotated.get("is_remote") else 0,
        annotated.get("min_amount"),
        annotated.get("max_amount"),
        annotated.get("currency"),
        annotated.get("interval"),
        required_min,
        required_max,
        _experience_match_value(annotated),
        score,
        annotated.get("verdict"),
        _json_or_none(annotated.get("matched_skills")),
        _json_or_none(annotated.get("missing_skills")),
        annotated.get("reasoning"),
        # User state: on insert honor what the caller supplied (tests seed
        # status/notified this way; real scrapes never set them); on update
        # preserve the stored values so re-scrapes never clobber a decision.
        (existing or {}).get("status") or annotated.get("status") or "new",
        (existing or {}).get("notified") if existing else (1 if annotated.get("notified") else 0),
        (existing or {}).get("first_seen_at") or now,
        now,  # last_seen_at always refreshed
        now if scored else (existing or {}).get("scored_at"),
        annotated.get("scored_with_resume"),
    )


_UPSERT_SQL = """
INSERT INTO jobs (
    job_key, site, title, company, location, description, job_url, date_posted,
    job_type, is_remote, min_amount, max_amount, currency, interval,
    required_years_min, required_years_max, experience_match, score, verdict,
    matched_skills, missing_skills, reasoning, status, notified,
    first_seen_at, last_seen_at, scored_at, scored_with_resume
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(job_key) DO UPDATE SET
    site=excluded.site, title=excluded.title, company=excluded.company,
    location=excluded.location, description=excluded.description,
    job_url=excluded.job_url, date_posted=excluded.date_posted,
    job_type=excluded.job_type, is_remote=excluded.is_remote,
    min_amount=excluded.min_amount, max_amount=excluded.max_amount,
    currency=excluded.currency, interval=excluded.interval,
    required_years_min=excluded.required_years_min,
    required_years_max=excluded.required_years_max,
    experience_match=excluded.experience_match,
    score=excluded.score, verdict=excluded.verdict,
    matched_skills=excluded.matched_skills, missing_skills=excluded.missing_skills,
    reasoning=excluded.reasoning, last_seen_at=excluded.last_seen_at,
    scored_at=excluded.scored_at, scored_with_resume=excluded.scored_with_resume,
    status=excluded.status, notified=excluded.notified
"""

# Same upsert for RAW scrapes (no AI annotation yet): refresh listing fields
# and user state but KEEP the stored AI results, so re-scraping a job never
# wipes its score or tricks the pipeline into re-running the agent on it.
_UPSERT_SQL_RAW = """
INSERT INTO jobs (
    job_key, site, title, company, location, description, job_url, date_posted,
    job_type, is_remote, min_amount, max_amount, currency, interval,
    required_years_min, required_years_max, experience_match, score, verdict,
    matched_skills, missing_skills, reasoning, status, notified,
    first_seen_at, last_seen_at, scored_at, scored_with_resume
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(job_key) DO UPDATE SET
    site=excluded.site, title=excluded.title, company=excluded.company,
    location=excluded.location, description=excluded.description,
    job_url=excluded.job_url, date_posted=excluded.date_posted,
    job_type=excluded.job_type, is_remote=excluded.is_remote,
    min_amount=excluded.min_amount, max_amount=excluded.max_amount,
    currency=excluded.currency, interval=excluded.interval,
    last_seen_at=excluded.last_seen_at,
    status=excluded.status, notified=excluded.notified
"""


def upsert_job(annotated: Dict[str, Any], job_key_value: Optional[str] = None) -> str:
    """Insert or update one job row. Returns the job key.

    User state (``status``/``notified``) is preserved when the row already
    exists — re-scrapes and re-scores never overwrite an accept/reject.
    When the payload carries AI results (score/required_years/verdict) they
    replace the stored ones; a RAW scrape (no AI fields) keeps them, so a
    job scored once is never silently reset to unscored.
    """
    from .util import job_key as compute_key

    key = job_key_value or compute_key(annotated)
    now = _utc_now()
    has_ai = (
        annotated.get("required_years") is not None
        or annotated.get("score") is not None
        or annotated.get("experience_match") is not None
        or bool(annotated.get("verdict"))
    )
    with transaction() as conn:
        existing = conn.execute(
            "SELECT status, notified, first_seen_at, scored_at FROM jobs WHERE job_key = ?",
            (key,),
        ).fetchone()
        existing_dict = dict(existing) if existing else None
        values = _job_row_values(key, annotated, now, existing_dict)
        conn.execute(_UPSERT_SQL if has_ai else _UPSERT_SQL_RAW, values)
    return key


def get_job(job_key_value: str) -> Optional[Dict[str, Any]]:
    """One job row (with parsed skill lists), or None."""
    row = query_one("SELECT * FROM jobs WHERE job_key = ?", (job_key_value,))
    return _decode_job(row) if row else None


def _decode_job(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    job = dict(row)
    for column in ("matched_skills", "missing_skills"):
        try:
            job[column] = json.loads(job[column]) if job.get(column) else []
        except (TypeError, json.JSONDecodeError):
            job[column] = []
    return job


def _status_filter(
    status: Optional[str], auto_rejected: Optional[bool]
) -> Tuple[str, List[Any]]:
    """WHERE clause fragment (leading ' WHERE ' when non-empty) + params."""
    clauses: List[str] = []
    params: List[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if auto_rejected is not None:
        clauses.append("auto_rejected = ?")
        params.append(1 if auto_rejected else 0)
    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), params


def count_jobs(
    status: Optional[str] = None, auto_rejected: Optional[bool] = None
) -> int:
    """Number of jobs matching the filter, ignoring pagination."""
    where, params = _status_filter(status, auto_rejected)
    row = query_one(f"SELECT COUNT(*) AS n FROM jobs{where}", tuple(params))
    return row["n"] if row else 0


def list_jobs(
    status: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    order: str = "score",
    auto_rejected: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """Jobs as dicts, newest-first within equal scores, with parsed skills.

    ``auto_rejected`` narrows to pipeline-rejected (True) or manually
    rejected (False) rows; combined with ``status='rejected'`` it keeps the
    board's Rejected section to the operator's own decisions.
    """
    allowed_orders = {"score": "score DESC", "recent": "last_seen_at DESC", "title": "title ASC"}
    order_clause = allowed_orders.get(order, allowed_orders["score"])
    where, params = _status_filter(status, auto_rejected)
    sql = f"SELECT * FROM jobs{where} ORDER BY {order_clause}"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    return [job for job in (_decode_job(row) for row in query(sql, tuple(params))) if job]


def counts_by_status() -> Dict[str, int]:
    """Job counts keyed by status, plus total and average score.

    ``rejected`` stays the full rejected count; ``auto_rejected`` /
    ``manual_rejected`` split it so the UI can show the operator's own
    rejects separately from the pipeline's experience-window rejects.
    """
    rows = query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    counts = {row["status"]: row["n"] for row in rows}
    total = sum(counts.values())
    avg = query_one("SELECT AVG(score) AS avg_score FROM jobs WHERE score IS NOT NULL")
    counts["total"] = total
    counts["avg_score"] = round(avg["avg_score"], 1) if avg and avg["avg_score"] is not None else None
    split = query_one(
        "SELECT SUM(CASE WHEN auto_rejected = 1 THEN 1 ELSE 0 END) AS auto_n, "
        "       SUM(CASE WHEN auto_rejected = 0 THEN 1 ELSE 0 END) AS manual_n "
        "FROM jobs WHERE status = 'rejected'"
    ) or {}
    counts["auto_rejected"] = split.get("auto_n") or 0
    counts["manual_rejected"] = split.get("manual_n") or 0
    return counts


def set_job_status(
    job_key_value: str,
    status: str,
    *,
    only_if_status: Optional[str] = None,
    auto: bool = False,
) -> bool:
    """Set a job's user status (new/accepted/rejected). Returns True if it exists.

    Pass ``only_if_status`` for a compare-and-set update: the status changes
    only when the stored status still equals that value (used by the
    auto-reject rule so it never clobbers a user's accept/reject).

    ``auto=True`` marks the row as pipeline-rejected (hidden from the
    board's Rejected section); any other call clears that mark, so a user
    who rejects a job themselves always owns the decision.
    """
    if status not in ("new", "accepted", "rejected"):
        raise ValueError(f"Invalid status '{status}'.")
    if only_if_status is not None and only_if_status not in ("new", "accepted", "rejected"):
        raise ValueError(f"Invalid guard status '{only_if_status}'.")
    with transaction() as conn:
        sql = "UPDATE jobs SET status = ?, auto_rejected = ? WHERE job_key = ?"
        params: list = [status, 1 if (auto and status == "rejected") else 0, job_key_value]
        if only_if_status is not None:
            sql += " AND status = ?"
            params.append(only_if_status)
        cursor = conn.execute(sql, tuple(params))
        return cursor.rowcount > 0


def delete_jobs_older_than(days: int) -> int:
    """Delete jobs not seen for ``days`` days; returns rows removed.

    ``0`` (or less) disables pruning. Cutoff uses ``last_seen_at`` so a
    posting still live on the boards keeps being refreshed and is never
    aged out — only rows that disappeared from the scrapes get deleted.
    """
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with transaction() as conn:
        cursor = conn.execute("DELETE FROM jobs WHERE last_seen_at < ?", (cutoff,))
        return cursor.rowcount


def mark_notified(job_keys: List[str]) -> None:
    """Flag jobs as notified so they are never re-alerted."""
    if not job_keys:
        return
    with transaction() as conn:
        conn.executemany(
            "UPDATE jobs SET notified = 1 WHERE job_key = ?",
            [(key,) for key in job_keys],
        )


def create_run(trigger: str) -> str:
    """Register a new pipeline run and return its run_id."""
    run_id = uuid.uuid4().hex
    with transaction() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, trigger, status, started_at) VALUES (?,?,?,?)",
            (run_id, trigger, "running", _utc_now()),
        )
    return run_id


def finish_run(
    run_id: str,
    status: str,
    *,
    jobs_scraped: Optional[int] = None,
    new_jobs: Optional[int] = None,
    scored: Optional[int] = None,
    alerts_sent: Optional[int] = None,
    error: Optional[str] = None,
    score_failed: Optional[int] = None,
    auto_rejected: Optional[int] = None,
    pruned: Optional[int] = None,
    scrape_errors: Optional[List[str]] = None,
    ai_metrics: Optional[Dict[str, Any]] = None,
) -> None:
    """Complete a run row with its outcome counters.

    ``scrape_errors`` and ``ai_metrics`` are stored as JSON text. Null leaves
    the column unset (early failures that never reached that stage).
    """
    errors_json = json.dumps(scrape_errors, ensure_ascii=False) if scrape_errors else None
    metrics_json = (
        json.dumps(make_json_safe(ai_metrics), ensure_ascii=False) if ai_metrics else None
    )
    with transaction() as conn:
        conn.execute(
            """
            UPDATE runs
               SET status = ?, finished_at = ?, jobs_scraped = ?, new_jobs = ?,
                   scored = ?, alerts_sent = ?, error = ?,
                   score_failed = ?, auto_rejected = ?, pruned = ?,
                   scrape_errors = ?, ai_metrics = ?
             WHERE run_id = ?
            """,
            (
                status, _utc_now(), jobs_scraped, new_jobs, scored, alerts_sent, error,
                score_failed, auto_rejected, pruned, errors_json, metrics_json, run_id,
            ),
        )


def list_runs(limit: int = 50) -> List[Dict[str, Any]]:
    """Recent runs, newest first, with JSON columns decoded."""
    return [_decode_run(row) for row in query(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
    )]


def _decode_run(row: Dict[str, Any]) -> Dict[str, Any]:
    run = dict(row)
    for column in ("scrape_errors", "ai_metrics"):
        raw = run.get(column)
        if not raw:
            run[column] = [] if column == "scrape_errors" else None
            continue
        try:
            run[column] = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            run[column] = [] if column == "scrape_errors" else None
    return run


def reap_stale_runs(max_age_minutes: Optional[int] = 30) -> int:
    """Close out runs stuck in 'running' with no heartbeat.

    Covers hard crashes and container restarts mid-run (deploys kill the
    thread; the DB row used to stay 'running' forever). Pass
    ``max_age_minutes=None`` to reap ALL running rows — correct at process
    startup, where a 'running' row is by definition orphaned (single
    process, max_instances=1). Returns rows closed.
    """
    where = "WHERE status = 'running'"
    params: list = []
    if max_age_minutes is not None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        ).isoformat()
        where += " AND started_at < ?"
        params.append(cutoff)
    with transaction() as conn:
        cur = conn.execute(
            f"""
            UPDATE runs
               SET status = 'failed',
                   finished_at = ?,
                   error = COALESCE(error, 'run interrupted (process restart/deploy)')
             {where}
            """,
            [_utc_now(), *params],
        )
    return cur.rowcount


MAX_SCORE_ATTEMPTS = 3

# A scored row must never wear failure bookkeeping: it clears its marks on
# every successful scoring, so marks surviving next to a score can only come
# from a failure recorded AFTER the score (a torn multi-step write during a
# deploy restart). They make the UI show 'AI failed' on healthy jobs, so any
# leftover marks on scored rows are removed at startup.
_STALE_FAILURE_SQL = """
UPDATE jobs
   SET score_attempts = 0, score_error = NULL
 WHERE score IS NOT NULL AND (score_attempts > 0 OR score_error IS NOT NULL)
"""

# Older builds wrote experience_match=0 on RAW (unscored) inserts, which the
# UI renders as 'outside window' on jobs the AI never assessed. Rows with no
# score, no verdict, and no estimated years are exactly those raw rows.
_UNSCORED_MATCH_SQL = """
UPDATE jobs
   SET experience_match = NULL
 WHERE experience_match = 0
   AND score IS NULL AND verdict IS NULL
   AND required_years_min IS NULL AND required_years_max IS NULL
"""


def record_score_failure(job_key: str, error: str) -> None:
    """Bump the attempt counter and store the last error for one job.

    Rows that already carry a real score are refused: their marks were
    cleared on scoring, so new marks would be stale bookkeeping — the job
    would sit flagged 'AI failed' in the UI until the 72h rescore despite
    having a perfectly good score."""
    with transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE jobs
               SET score_attempts = score_attempts + 1,
                   score_error = ?
             WHERE job_key = ? AND score IS NULL
            """,
            (error[:500], job_key),
        )
        return cursor.rowcount > 0


def clear_score_failure(job_key: str) -> None:
    """Reset the failure bookkeeping after a successful score."""
    with transaction() as conn:
        conn.execute(
            "UPDATE jobs SET score_attempts = 0, score_error = NULL WHERE job_key = ?",
            (job_key,),
        )


def needs_score_retry(row: Dict[str, Any]) -> bool:
    """A row failed AI scoring before and still has attempts left."""
    attempts = row.get("score_attempts") or 0
    return (
        row.get("score") is None
        and bool(row.get("score_error"))
        and attempts < MAX_SCORE_ATTEMPTS
    )


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    row = query_one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
    return _decode_run(row) if row else None


def purge_older_than(days: int) -> Dict[str, int]:
    """Delete jobs and run history older than ``days`` (by first seen / start).

    Accepted jobs are kept. ``days`` must be at least 1 — callers reject 0
    so this cannot wipe the board. Returns ``{"jobs_deleted", "runs_deleted"}``.
    """
    if days < 1:
        raise ValueError("older_than_days must be >= 1.")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with transaction() as conn:
        jobs = conn.execute(
            "DELETE FROM jobs WHERE first_seen_at < ? AND status != 'accepted'",
            (cutoff,),
        )
        runs = conn.execute(
            "DELETE FROM runs WHERE started_at < ?",
            (cutoff,),
        )
        return {"jobs_deleted": jobs.rowcount, "runs_deleted": runs.rowcount}


# --------------------------------------------------------------------------- #
# Settings (JSON key/value)
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        return default


def set_setting(key: str, value: Any) -> None:
    encoded = json.dumps(make_json_safe(value), ensure_ascii=False)
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, encoded),
        )


def all_settings() -> Dict[str, Any]:
    return {row["key"]: json.loads(row["value"]) for row in query("SELECT * FROM settings")}


def close() -> None:
    """Close the shared connection (used by tests)."""
    global _conn
    with _conn_lock:
        if _conn is not None:
            _conn.close()
            _conn = None
