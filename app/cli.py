"""JobSpy Lite command-line interface.

Examples
--------
Run one scrape+screen+store pass (no server)::

    python -m app.cli --once

Serve the API + scheduler + UI::

    python -m app.cli --serve --port 8000
"""

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(PROJECT_ROOT),):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from app import scheduler, storage  # noqa: E402
from app.agent.tracing import setup_tracing, shutdown_tracing  # noqa: E402
from app.pipeline import run_pipeline  # noqa: E402


def _print_summary(summary: dict) -> None:
    print("\n=== Run summary ===")
    for key in ("run_id", "status", "trigger", "jobs_scraped", "new_jobs",
                "scored", "alerts_sent", "duration_s", "error"):
        print(f"  {key:>12}: {summary.get(key)}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="JobSpy Lite")
    parser.add_argument("--once", action="store_true",
                        help="run one scrape+screen pass and exit")
    parser.add_argument("--serve", action="store_true",
                        help="start the API server (scheduler included)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--force-rescore", action="store_true",
                        help="re-score every job even if cached")
    args = parser.parse_args()
    setup_tracing()

    if args.once:
        try:
            summary = run_pipeline(trigger="manual", force_rescore=args.force_rescore)
            _print_summary(summary)
        finally:
            shutdown_tracing()
            storage.close()
        return

    if args.serve:
        import uvicorn

        # A restart/deploy orphans any in-flight run rows; close them all now
        # so the Runs view reflects reality immediately, not on the next
        # trigger. (Single process + max_instances=1: nothing can legitimately
        # still be running across a restart.)
        reaped = storage.reap_stale_runs(max_age_minutes=None)
        if reaped:
            print(f"Reaped {reaped} stale run row(s) left by the previous process.")

        # Start the cron scheduler if it was left enabled in settings.
        if scheduler.get_config().get("enabled"):
            scheduler.start()
            print("Scheduler running:", scheduler.status())
        uvicorn.run(
            "app.api:app",
            host=args.host,
            port=args.port,
            reload=False,
        )
        return

    parser.print_help()


if __name__ == "__main__":
    main()
