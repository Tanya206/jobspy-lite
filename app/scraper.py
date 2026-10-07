"""Job scraper using jobsniffer, trimmed from the original JobSpy scraper.

Exposes a low-level ``scrape_jobs_from_sites`` plus a config-driven
``JobScraper`` class that iterates one (location, search term) pair at a
time — so a comma-separated location runs each city separately — deduplicates,
and can save its results as JSON.
"""

import importlib.util
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

import pandas as pd

try:
    from jobsniffer import scrape_jobs

    JOBSNIFFER_AVAILABLE = True
except ImportError:
    JOBSNIFFER_AVAILABLE = False
    scrape_jobs = None

from .util import cooldown_sleep, deduplicate_jobs, make_json_safe

logger = logging.getLogger(__name__)


def scrape_jobs_from_sites(
    site_name: Union[str, List[str]],
    search_term: str,
    location: str,
    results_wanted: int = 15,
    hours_old: int = 72,
    country_indeed: str = "usa",
    linkedin_fetch_description: bool = False,
    proxies: Optional[List[str]] = None,
    distance: int = 80,
    is_remote: bool = False,
    easy_apply: bool = False,
    description_format: str = "markdown",
    google_search_term: Optional[str] = None,
    employment_type: Optional[str] = None,
) -> pd.DataFrame:
    """Scrape job postings from job boards using jobsniffer.

    Supported sites: linkedin, indeed, glassdoor, google, ziprecruiter, bayt,
    naukri, bdjobs. Raises ImportError when jobsniffer is not installed.
    """
    if not JOBSNIFFER_AVAILABLE:
        raise ImportError(
            "jobsniffer is not installed. Install it with: pip install jobsniffer"
        )

    kwargs: Dict[str, Any] = {
        "site_name": site_name,
        "search_term": search_term,
        "location": location,
        "results_wanted": results_wanted,
        "hours_old": hours_old,
        "country_indeed": country_indeed,
        "linkedin_fetch_description": linkedin_fetch_description,
        "proxies": proxies,
        "distance": distance,
        "is_remote": is_remote,
        "easy_apply": easy_apply,
        "description_format": description_format,
        "verbose": 2,
    }
    if google_search_term is not None:
        kwargs["google_search_term"] = google_search_term
    if employment_type is not None:
        kwargs["employment_type"] = employment_type

    return scrape_jobs(**kwargs)


def _load_project_config() -> Dict[str, Any]:
    """Load ``load_config()`` from the project-root ``config.py`` via importlib."""
    project_root = Path(__file__).resolve().parents[1]
    config_path = project_root / "config.py"
    spec = importlib.util.spec_from_file_location("jobspy_lite_config", config_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"Could not load config from {config_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_config()


def parse_locations(raw: Any) -> List[str]:
    """Split a location setting into the list of locations to scrape.

    ``"Hyderabad, Bengaluru"`` → ``["Hyderabad", "Bengaluru"]``. Each part
    must be scraped separately: job boards geocode the whole string to a
    *single* city, so passing "Hyderabad, Bengaluru" as one location silently
    returns one city's jobs and drops the other. A blank/missing value means
    "no location filter" and yields ``[""]``.
    """
    if raw is None:
        return [""]
    locations = [part.strip() for part in str(raw).split(",")]
    return [part for part in locations if part] or [""]


class JobScraper:
    """Config-driven job scraper.

    Reads settings from the root ``config.py`` (or an override dict), scrapes
    every configured location × search term combination across every
    configured site — one pair at a time, in order — and yields one batch per
    pair via :meth:`scrape_iter` so callers can stream progress.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config if config is not None else _load_project_config()

    def scrape_iter(self, progress_callback=None) -> Iterator[Dict[str, Any]]:
        """Yield scraped jobs one (location, search term) pair at a time.

        A comma-separated ``location`` setting (e.g. ``"Hyderabad,
        Bengaluru"``) is scraped city by city: all search terms for the first
        location, then the next, and so on — never in parallel, and never as
        one comma-joined query. Each yield is
        ``{"term", "location", "index", "total", "jobs": [...], "error"}``.
        Deduplication is left to the caller. Cooldown sleeps happen between
        pairs (never after the last one).
        """
        sites = self.config.get("sites", [])
        search_terms = self.config.get("search_terms", [])
        locations = parse_locations(self.config.get("location", ""))
        cooldown_minutes = self.config.get("scrape_cooldown_minutes", 0)
        proxies = self.config.get("proxies") or None

        scraper_params = {
            "results_wanted": self.config.get("results_wanted", 15),
            "hours_old": self.config.get("hours_old", 72),
            "country_indeed": self.config.get("country_indeed", "usa"),
            "linkedin_fetch_description": self.config.get(
                "linkedin_fetch_description", False
            ),
            "is_remote": self.config.get("is_remote", False),
        }

        # One work item per (location, term); locations loop outside so each
        # city finishes before the next one starts.
        work = [(location, term) for location in locations for term in search_terms]
        total = len(work)

        def _report(completed: int, message: str) -> None:
            if progress_callback is not None:
                progress_callback(completed, total, message)

        for index, (location, term) in enumerate(work):
            where = f" in {location}" if location else ""
            _report(index, f"Scraping '{term}'{where} ({index + 1}/{total})")
            logger.info(
                "Scraping '%s' in %s across %s",
                term,
                location or "any location",
                sites,
            )
            jobs: List[Dict[str, Any]] = []
            error: Optional[str] = None
            try:
                df = scrape_jobs_from_sites(
                    site_name=sites,
                    search_term=term,
                    location=location,
                    proxies=proxies,
                    **scraper_params,
                )
                if df is not None and not df.empty:
                    jobs = df.to_dict("records")
                    logger.info(
                        "Retrieved %d jobs for '%s'%s", len(jobs), term, where
                    )
                    _report(index + 1, f"Found {len(jobs)} jobs for '{term}'{where}")
                else:
                    logger.warning("No jobs returned for '%s'%s", term, where)
                    _report(index + 1, f"No jobs for '{term}'{where}")
            except Exception as exc:  # noqa: BLE001 - log and continue other pairs
                error = str(exc)
                logger.error("Error scraping '%s'%s: %s", term, where, exc, exc_info=True)
                _report(index + 1, f"Error on '{term}'{where}")

            yield {
                "term": term,
                "location": location,
                "index": index,
                "total": total,
                "jobs": jobs,
                "error": error,
            }

            if cooldown_minutes and index < total - 1:
                cooldown_sleep(cooldown_minutes, logger)

    def scrape(self, progress_callback=None) -> List[Dict[str, Any]]:
        """Scrape all configured locations × terms × sites, deduplicated.

        Dedup runs across the whole set, so a posting that shows up in two
        cities' scrapes is stored once.
        """
        total_scraped = 0
        collected: List[Dict[str, Any]] = []
        for event in self.scrape_iter(progress_callback=progress_callback):
            total_scraped += len(event["jobs"])
            collected.extend(event["jobs"])

        unique_jobs = deduplicate_jobs(collected)
        logger.info("%d jobs scraped -> %d after dedup", total_scraped, len(unique_jobs))
        return unique_jobs

    def scrape_and_save(
        self, output_path: Optional[Union[str, Path]] = None, progress_callback=None
    ) -> List[Dict[str, Any]]:
        """Scrape jobs and write them to ``output_path`` (or data/scraped_jobs.json)."""
        from .config import DATA_DIR

        jobs = self.scrape(progress_callback=progress_callback)
        path = (
            Path(output_path)
            if output_path
            else (DATA_DIR / "scraped_jobs.json")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as output_file:
            json.dump(make_json_safe(jobs), output_file, ensure_ascii=False, indent=2)
        logger.info("Saved %d jobs to %s", len(jobs), path)
        return jobs
