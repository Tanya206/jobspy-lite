"""SQLite persistence layer for JobSpy Lite.

Four tables, all created idempotently:

- ``jobs``         — one row per unique posting (keyed by the stable job identity).
                     Screening annotations are upserted; user state (``status``,
                     ``notified``) is never clobbered by re-scrapes.
- ``runs``         — one row per pipeline execution (cron or manual), for history
                     and for verifying the scheduler actually fired.
- ``settings``     — JSON-key/value store for runtime-editable configuration
                     (threshold, experience window, scrape params, cron config,
                     the stored resume).
- ``applications`` — spreadsheet rows for the Applications tab. Accepting a job
                     inserts one row; later edits are not overwritten.

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

CREATE TABLE IF NOT EXISTS applications (
    id           TEXT PRIMARY KEY,
    job_key      TEXT UNIQUE,
    company      TEXT NOT NULL DEFAULT '',
    job_role     TEXT NOT NULL DEFAULT '',
    job_id       TEXT NOT NULL DEFAULT '',
    link         TEXT NOT NULL DEFAULT '',
    referral     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'Not Applied',
    date_applied TEXT NOT NULL DEFAULT '',
    notes        TEXT NOT NULL DEFAULT '',
    date_added   TEXT NOT NULL
);

-- A deleted tracker row for an accepted job must not be recreated on the
-- next list. Accepting that job again clears this and inserts a new row.
CREATE TABLE IF NOT EXISTS application_dismissals (
    job_key      TEXT PRIMARY KEY,
    dismissed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resume_rewrites (
    id              TEXT PRIMARY KEY,
    job_key         TEXT NOT NULL,
    model           TEXT,
    base_url        TEXT,
    resume_hash     TEXT,
    created_at      TEXT NOT NULL,
    max_tries       INTEGER,
    trace           TEXT,
    winning_total   INTEGER,
    winning_patches TEXT,
    patch_chain     TEXT,
    docx_path       TEXT,
    error           TEXT,
    additional_info TEXT,
    company_type    TEXT,
    status          TEXT NOT NULL DEFAULT 'done'
);

CREATE INDEX IF NOT EXISTS idx_resume_rewrites_job ON resume_rewrites(job_key);
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
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS resume_rewrites (
            id              TEXT PRIMARY KEY,
            job_key         TEXT NOT NULL,
            model           TEXT,
            base_url        TEXT,
            resume_hash     TEXT,
            created_at      TEXT NOT NULL,
            max_tries       INTEGER,
            trace           TEXT,
            winning_total   INTEGER,
            winning_patches TEXT,
            patch_chain     TEXT,
            docx_path       TEXT,
            error           TEXT,
            additional_info TEXT,
            company_type    TEXT,
            status          TEXT NOT NULL DEFAULT 'done'
        )
        """
    )
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resume_rewrites_job ON resume_rewrites(job_key)"
    )
    rewrite_cols = {row["name"] for row in _conn.execute("PRAGMA table_info(resume_rewrites)")}
    if "status" not in rewrite_cols:
        _conn.execute(
            "ALTER TABLE resume_rewrites ADD COLUMN status TEXT NOT NULL DEFAULT 'done'"
        )
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
        if cursor.rowcount > 0:
            return True
        # SQLite can report zero changes when the new values match the row.
        # A repeat Accept must still succeed so the tracker insert can run.
        exists_sql = "SELECT 1 FROM jobs WHERE job_key = ?"
        exists_params: list = [job_key_value]
        if only_if_status is not None:
            exists_sql += " AND status = ?"
            exists_params.append(only_if_status)
        return conn.execute(exists_sql, tuple(exists_params)).fetchone() is not None


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
# Applications (the in-app tracker)
# --------------------------------------------------------------------------- #
APPLICATION_STATUSES = {
    "",
    "Wishlist",
    "Not Applied",
    "Applied",
    "Interview",
    "Offer",
    "Rejected",
}
APPLICATION_REFERRALS = {"", "Yes", "No"}

_APPLICATION_COLUMNS = (
    "company",
    "job_role",
    "job_id",
    "link",
    "referral",
    "status",
    "date_applied",
    "notes",
)


def _application_public(row: Any) -> Dict[str, Any]:
    data = row if isinstance(row, dict) else dict(row)
    return {
        "id": data["id"],
        "job_key": data["job_key"],
        "company": data["company"] or "",
        "jobRole": data["job_role"] or "",
        "jobId": data["job_id"] or "",
        "link": data["link"] or "",
        "referral": data["referral"] or "",
        "status": data["status"] if data["status"] is not None else "",
        "dateApplied": data["date_applied"] or "",
        "notes": data["notes"] or "",
        "dateAdded": data["date_added"],
    }


def _check_application_choices(fields: Dict[str, Any]) -> None:
    status = fields.get("status")
    if status is not None and status not in APPLICATION_STATUSES:
        raise ValueError(f"Invalid status '{status}'.")
    referral = fields.get("referral")
    if referral is not None and referral not in APPLICATION_REFERRALS:
        raise ValueError(f"Invalid referral '{referral}'.")


def _score_note(score: Any) -> Optional[str]:
    if score is None:
        return None
    try:
        number = float(score)
    except (TypeError, ValueError):
        return f"score {score}"
    if number.is_integer():
        return f"score {int(number)}"
    return f"score {number}"


def _fields_from_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Map a scraped job onto a new tracker row. Used only on insert."""
    company = (job.get("company") or "").strip() or "Unknown"
    notes: List[str] = []
    location = (job.get("location") or "").strip()
    if location:
        notes.append(location)
    score = _score_note(job.get("score"))
    if score:
        notes.append(score)
    verdict = (job.get("verdict") or "").strip()
    if verdict:
        notes.append(verdict)
    return {
        "job_key": job["job_key"],
        "company": company,
        "job_role": (job.get("title") or "").strip(),
        "job_id": "",
        "link": (job.get("job_url") or "").strip(),
        "referral": "",
        "status": "Not Applied",
        "date_applied": "",
        "notes": " · ".join(notes),
    }


def _insert_application(conn: sqlite3.Connection, fields: Dict[str, Any]) -> Dict[str, Any]:
    _check_application_choices(fields)
    app_id = (fields.get("id") or "").strip() or uuid.uuid4().hex
    job_key = (fields.get("job_key") or "").strip() or None
    status = fields["status"] if "status" in fields and fields["status"] is not None else "Not Applied"
    date_added = (fields.get("date_added") or "").strip() or _utc_now()
    conn.execute(
        """
        INSERT INTO applications (
            id, job_key, company, job_role, job_id, link, referral,
            status, date_applied, notes, date_added
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            app_id,
            job_key,
            fields.get("company") or "",
            fields.get("job_role") or "",
            fields.get("job_id") or "",
            fields.get("link") or "",
            fields.get("referral") or "",
            status,
            fields.get("date_applied") or "",
            fields.get("notes") or "",
            date_added,
        ),
    )
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    return _application_public(row)


def list_applications() -> List[Dict[str, Any]]:
    """Tracker rows, newest first.

    Accepted jobs that have never had a tracker row are inserted. A row the
    user deleted stays deleted until that job is accepted again.
    """
    backfill_accepted_applications()
    rows = query(
        "SELECT * FROM applications ORDER BY date_added DESC, id DESC"
    )
    return [_application_public(row) for row in rows]


def get_application(app_id: str) -> Optional[Dict[str, Any]]:
    row = query_one("SELECT * FROM applications WHERE id = ?", (app_id,))
    return _application_public(row) if row else None


def create_application(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Insert one tracker row (a hand-typed blank row, or an accepted job)."""
    with transaction() as conn:
        return _insert_application(conn, fields)


def update_application(app_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Apply a partial edit. Returns None when the id is unknown."""
    _check_application_choices(patch)
    assignments = []
    params: List[Any] = []
    for column in _APPLICATION_COLUMNS:
        if column in patch:
            assignments.append(f"{column} = ?")
            params.append(patch[column] if patch[column] is not None else "")
    if not assignments:
        return get_application(app_id)
    params.append(app_id)
    with transaction() as conn:
        cursor = conn.execute(
            f"UPDATE applications SET {', '.join(assignments)} WHERE id = ?",
            tuple(params),
        )
        if cursor.rowcount == 0 and conn.execute(
            "SELECT 1 FROM applications WHERE id = ?", (app_id,)
        ).fetchone() is None:
            return None
        row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
        return _application_public(row) if row else None


def delete_application(app_id: str) -> bool:
    """Delete a tracker row. An accepted job's key is remembered so list does not recreate it."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT job_key FROM applications WHERE id = ?", (app_id,)
        ).fetchone()
        if row is None:
            return False
        job_key = row["job_key"]
        if job_key:
            conn.execute(
                """
                INSERT INTO application_dismissals (job_key, dismissed_at)
                VALUES (?, ?)
                ON CONFLICT(job_key) DO UPDATE SET dismissed_at = excluded.dismissed_at
                """,
                (job_key, _utc_now()),
            )
        cursor = conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
        return cursor.rowcount > 0


def ensure_application_for_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Insert a Not Applied row for this job when none is tracked yet.

    A repeat accept, or a row already edited on the Applications page, is
    returned unchanged. A hand-added or imported row with the same link and
    no job key is claimed instead of duplicated. Calling this after a delete
    (an explicit accept) clears the dismissal and inserts a fresh row.
    """
    key = job["job_key"]
    with transaction() as conn:
        conn.execute("DELETE FROM application_dismissals WHERE job_key = ?", (key,))
        existing = conn.execute(
            "SELECT * FROM applications WHERE job_key = ?", (key,)
        ).fetchone()
        if existing:
            return _application_public(existing)
        link = (job.get("job_url") or "").strip()
        if link:
            by_link = conn.execute(
                "SELECT * FROM applications WHERE link = ? AND job_key IS NULL",
                (link,),
            ).fetchone()
            if by_link:
                conn.execute(
                    "UPDATE applications SET job_key = ? WHERE id = ? AND job_key IS NULL",
                    (key, by_link["id"]),
                )
                row = conn.execute(
                    "SELECT * FROM applications WHERE id = ?", (by_link["id"],)
                ).fetchone()
                return _application_public(row)
        try:
            return _insert_application(conn, _fields_from_job(job))
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM applications WHERE job_key = ?", (key,)
            ).fetchone()
            if row is None:
                raise
            return _application_public(row)


def backfill_accepted_applications() -> int:
    """Insert tracker rows for accepted jobs the user has not deleted.

    Returns the number of new rows. Dismissed job keys are left alone so a
    reload does not bring a deleted application back.
    """
    jobs = query(
        """
        SELECT jobs.* FROM jobs
        WHERE status = 'accepted'
          AND NOT EXISTS (
              SELECT 1 FROM application_dismissals AS dismissed
              WHERE dismissed.job_key = jobs.job_key
          )
        """
    )
    created = 0
    for job in jobs:
        before = query_one(
            "SELECT id FROM applications WHERE job_key = ?", (job["job_key"],)
        )
        ensure_application_for_job(job)
        if before is None:
            after = query_one(
                "SELECT id FROM applications WHERE job_key = ?", (job["job_key"],)
            )
            if after is not None:
                created += 1
    return created


def replace_applications(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Replace the tracker list. Links that match one job keep that job key."""
    with transaction() as conn:
        conn.execute("DELETE FROM applications")
        used_ids = set()
        used_keys = set()
        for item in items:
            app_id = (item.get("id") or "").strip() or uuid.uuid4().hex
            if app_id in used_ids:
                app_id = uuid.uuid4().hex
            used_ids.add(app_id)
            link = (item.get("link") or "").strip()
            job_key = (item.get("job_key") or "").strip() or None
            if job_key is None and link:
                matches = conn.execute(
                    "SELECT job_key FROM jobs WHERE job_url = ?", (link,)
                ).fetchall()
                if len(matches) == 1:
                    job_key = matches[0]["job_key"]
            if job_key in used_keys:
                job_key = None
            elif job_key:
                used_keys.add(job_key)
            _insert_application(
                conn,
                {
                    **item,
                    "id": app_id,
                    "job_key": job_key,
                    "link": link,
                },
            )
        rows = conn.execute(
            "SELECT * FROM applications ORDER BY date_added DESC, id DESC"
        ).fetchall()
        return [_application_public(row) for row in rows]


# --------------------------------------------------------------------------- #
# Resume rewrites (one row per attempt; the scoring resume is not replaced)
# --------------------------------------------------------------------------- #
def _decode_rewrite(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    item = dict(row)
    for column in ("trace", "winning_patches", "patch_chain"):
        try:
            item[column] = json.loads(item[column]) if item.get(column) else []
        except (TypeError, json.JSONDecodeError):
            item[column] = []
    item["docx_ready"] = bool(item.get("docx_path"))
    item["status"] = item.get("status") or "done"
    return item


def insert_resume_rewrite(record: Dict[str, Any]) -> Dict[str, Any]:
    rewrite_id = record.get("id") or str(uuid.uuid4())
    created = record.get("created_at") or _utc_now()
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO resume_rewrites (
                id, job_key, model, base_url, resume_hash, created_at, max_tries,
                trace, winning_total, winning_patches, patch_chain, docx_path,
                error, additional_info, company_type, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rewrite_id,
                record.get("job_key"),
                record.get("model"),
                record.get("base_url"),
                record.get("resume_hash"),
                created,
                record.get("max_tries"),
                json.dumps(make_json_safe(record.get("trace") or []), ensure_ascii=False),
                record.get("winning_total"),
                json.dumps(make_json_safe(record.get("winning_patches") or []), ensure_ascii=False),
                json.dumps(make_json_safe(record.get("patch_chain") or []), ensure_ascii=False),
                record.get("docx_path"),
                record.get("error"),
                record.get("additional_info") or "",
                record.get("company_type") or "",
                record.get("status") or "done",
            ),
        )
    found = get_resume_rewrite(rewrite_id)
    assert found is not None
    return found


def update_resume_rewrite(rewrite_id: str, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fill in a rewrite row after the background run finishes."""
    json_columns = {"trace", "winning_patches", "patch_chain"}
    allowed = json_columns | {
        "model", "base_url", "resume_hash", "max_tries", "winning_total",
        "docx_path", "error", "additional_info", "company_type", "status",
    }
    assignments: List[str] = []
    params: List[Any] = []
    for key, value in record.items():
        if key not in allowed:
            continue
        if key in json_columns:
            value = json.dumps(make_json_safe(value or []), ensure_ascii=False)
        assignments.append(f"{key} = ?")
        params.append(value)
    if not assignments:
        return get_resume_rewrite(rewrite_id)
    params.append(rewrite_id)
    with transaction() as conn:
        conn.execute(
            f"UPDATE resume_rewrites SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
    return get_resume_rewrite(rewrite_id)


def fail_interrupted_rewrites() -> int:
    """A restart kills the background thread, so a running row can never finish."""
    with transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE resume_rewrites
            SET status = 'error',
                error = 'Rewrite stopped because the app restarted. Use Rerun rewrite.'
            WHERE status = 'running'
            """
        )
        return cursor.rowcount


def get_resume_rewrite(rewrite_id: str) -> Optional[Dict[str, Any]]:
    row = query_one("SELECT * FROM resume_rewrites WHERE id = ?", (rewrite_id,))
    return _decode_rewrite(row)


def list_resume_rewrites(job_key_value: str) -> List[Dict[str, Any]]:
    rows = query(
        "SELECT * FROM resume_rewrites WHERE job_key = ? ORDER BY created_at DESC",
        (job_key_value,),
    )
    return [item for item in (_decode_rewrite(row) for row in rows) if item]


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
