# JobSpy Lite

A slimmed-down, standalone version of JobSpy: scrape job postings, screen them
against your resume with two AI agents, store everything in SQLite (with an
Excel export), run the whole thing on a cron schedule, and get a Telegram
alert whenever a job scores above your threshold. A React dashboard tracks
every job — accept or reject from the board, check whether the scheduler is
alive, and browse the run history.

## What it does

- Scrapes fresh postings from configured boards (LinkedIn, Indeed, …) via jobsniffer
- Estimates each posting's required experience (Agent 1), then scores resume fit (Agent 2)
- Stores every job in SQLite (`data/jobspy-lite.db`) and mirrors the table to `data/jobs.xlsx`
- Runs unattended on a schedule (interval with an active-hours window, or a raw cron expression)
- Sends ONE batched Telegram message per run for jobs above your score threshold (never twice for the same job)
- Serves a React dashboard: stats, resume upload, scheduler control, accept/reject per job, run history

## Layout

```
app/
  config.py        env-driven settings + defaults
  scraper.py       jobsniffer scraper (trimmed from JobSpy)
  agent/           AI client, job agent, schemas, prompts, rate limiter
  storage.py       SQLite layer (jobs / runs / settings)
  excel.py         .xlsx export via openpyxl
  pipeline.py      one run: scrape -> dedupe -> score -> store -> export -> notify
  notify.py        Telegram alerts with gating rules
  scheduler.py     APScheduler cron/interval management
  api.py           FastAPI app (API + serves the built SPA)
  schemas.py       Pydantic models
  cli.py           --once (one pass) / --serve (API + scheduler)
frontend/          Vite + React dashboard
tests/             pytest suite (storage, pipeline, notify, api, scheduler)
Dockerfile         multi-stage: node build -> python-slim serve
docker-compose.yml one container + ./data volume
```

## Quick start (local)

1. Create a virtualenv and install deps:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and fill in:

- `NVIDIA_API_KEY` — your LLM key (OpenAI-compatible endpoint via `AI_BASE_URL`/`AI_MODEL`)
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — create a bot with @BotFather, message it once,
  then read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`

3. Build the dashboard (optional — the API works without it):

```bash
cd frontend && npm install && npm run build && cd ..
```

4. Run one full pass (scrape + score + store + Excel + notify), no server:

```bash
python -m app.cli --once
```

5. Or serve the API + dashboard + scheduler:

```bash
python -m app.cli --serve --port 8000
```

Then open http://127.0.0.1:8000 (built UI) or http://localhost:5174 (Vite dev server).
For development with hot reload: `cd frontend && npm run dev` (proxies to :8000).

## Docker deployment

```bash
cp .env.example .env    # fill in keys
docker compose up --build
```

The container serves the API + dashboard on port 8000; SQLite, the Excel
export, and the stored resume persist in `./data`. Start the scheduler from
the dashboard (or `POST /scheduler/toggle?enabled=true`) — it resumes on the
next container start because `enabled` is stored in the settings table.

> Scraping from datacenter IPs often gets blocked by LinkedIn/Indeed. Set
> `PROXIES` in `.env` (comma-separated, e.g. residential proxies) if the
> container returns zero jobs.

## How scoring works

Each run upserts scraped postings into `jobs` (keyed by URL, falling back to a
title/company/location/date hash — so re-scrapes never duplicate). A posting
is re-scored only when it is new, was never scored, was scored against a
different resume, is older than 72h since its last score, or you press
"Force re-score". Your accept/reject decisions and notified flags are never
touched by re-runs.

A job is alerted when ALL of: `score >= threshold`, `experience_match`,
`status != rejected`, and `notified = 0`. Matched jobs are flagged in the same
run so nothing is sent twice. Use the Dashboard's "Test Telegram alert" to
flush pending matches manually.

### Rejected vs auto-rejected

Jobs whose required experience falls outside your window are auto-rejected
(`auto_rejected = 1`) so they never alert again. They appear in the board's
Rejected section with an "auto · outside experience window" label, and they
are still counted separately in the `auto-rejected` metric on the Overview.
A manual accept/reject always wins and clears the auto mark.

### Retention

Set "auto-delete after (days)" in the Screening panel (or `retention_days` via
`/settings`; `0` = keep everything). At the end of each run the pipeline
deletes postings that have not been seen by the scraper for that many days —
live listings keep being re-seen, so they are never aged out. The number
deleted appears in the run summary. "Delete old data" on the same panel
runs immediately: it removes jobs first seen more than N days ago (accepted
jobs are kept) and run-history rows of the same age.

## API overview

| Method | Path | Purpose |
| --- | --- | --- |
| GET | /health | liveness + component status (scheduler, resume, telegram, db) |
| POST | /resume | upload a .docx resume (stored once, used by all runs) |
| GET | /resume | current resume metadata |
| GET | /jobs | list jobs (`status=new/accepted/rejected`, `auto_rejected=true/false`, `order=score/recent/title`) |
| GET | /jobs/{key} | one job, full detail |
| POST | /jobs/{key}/status | `{"new_status": "accepted"\|"rejected"\|"new"}` |
| GET | /stats | counts by status (incl. `manual_rejected`/`auto_rejected` split) + average score |
| POST | /run | start a manual pipeline run (background) |
| GET | /run/latest | progress + last summary |
| GET | /runs | run history |
| GET | /scheduler | status + config |
| POST | /scheduler/config | update interval/cron config (live) |
| POST | /scheduler/toggle | start/stop the scheduler |
| GET/POST | /settings | scrape params, experience window, score threshold, retention days |
| GET | /export.xlsx | download the Excel export |
| POST | /notify/test | send any pending above-threshold alerts now |
| POST | /maintenance/purge | delete jobs and run history older than `older_than_days` (accepted jobs kept) |

## Configuration

All via `.env` (see `.env.example`); scrape params, experience window, and the
score threshold can also be changed at runtime from the dashboard (stored in
the `settings` table and applied from the next run).

## Traces

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to export OpenTelemetry traces. Each pipeline run is one trace, and each resume rewrite is another. Agent model calls are child spans of whichever trace is open (`experience`, `fit`, `checker`, or `other`). Prompts and resume text are not included. Leave the endpoint unset and nothing is exported.

Langfuse accepts this export. Point the endpoint at `https://cloud.langfuse.com/api/public/otel` (or a self-hosted `/api/public/otel`) and set `OTEL_EXPORTER_OTLP_HEADERS` to `Authorization=Basic` plus the base64 of `public_key:secret_key`. Any other OTLP HTTP collector uses the same two variables.

## Tests

```bash
pytest tests/
```

The suite uses a temporary database and mocked AI client / Telegram HTTP, so
it runs offline and costs nothing.
