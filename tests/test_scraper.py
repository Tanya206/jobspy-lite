"""Multi-location scrape tests: one city at a time, never one merged query."""

import pandas as pd

from app.scraper import JobScraper, parse_locations


def _config(**overrides):
    cfg = {
        "sites": ["linkedin"],
        "search_terms": ["AI Engineer"],
        "location": "Bengaluru",
        "results_wanted": 5,
        "hours_old": 3,
    }
    cfg.update(overrides)
    return cfg


def test_parse_locations_splits_cities_and_drops_blanks():
    assert parse_locations("Hyderabad, Bengaluru") == ["Hyderabad", "Bengaluru"]
    assert parse_locations(" Bengaluru ,  Chennai,") == ["Bengaluru", "Chennai"]
    assert parse_locations("Bengaluru") == ["Bengaluru"]
    # Blank / missing means "no location filter" — one unfiltered scrape.
    assert parse_locations("") == [""]
    assert parse_locations("   ") == [""]
    assert parse_locations(None) == [""]
    assert parse_locations(",,,") == [""]


def _recording_scraper(monkeypatch, rows_for=None):
    """Replace scrape_jobs_from_sites with a call recorder."""
    calls = []

    def fake(site_name, search_term, location, proxies=None, **kwargs):
        calls.append(
            {"site_name": site_name, "search_term": search_term, "location": location}
        )
        rows = rows_for(location) if rows_for else [
            {
                "title": f"{search_term} @ {location}",
                "company": "Acme",
                "location": location,
                "site": "linkedin",
                "job_url": f"https://example.com/{location}/{search_term}",
                "description": "d",
            }
        ]
        return pd.DataFrame(rows)

    monkeypatch.setattr("app.scraper.scrape_jobs_from_sites", fake)
    return calls


def test_comma_separated_location_scrapes_cities_one_at_a_time(monkeypatch):
    calls = _recording_scraper(monkeypatch)
    scraper = JobScraper(
        _config(search_terms=["AI Engineer", "ML Engineer"], location="Hyderabad, Bengaluru")
    )
    progress = []
    events = list(
        scraper.scrape_iter(
            progress_callback=lambda done, total, msg: progress.append((done, total, msg))
        )
    )

    # Every term for the first city finishes before the next city starts.
    assert [(c["location"], c["search_term"]) for c in calls] == [
        ("Hyderabad", "AI Engineer"),
        ("Hyderabad", "ML Engineer"),
        ("Bengaluru", "AI Engineer"),
        ("Bengaluru", "ML Engineer"),
    ]
    # Each call targets exactly one city — never the comma-joined string.
    assert all("," not in c["location"] for c in calls)

    assert [e["location"] for e in events] == ["Hyderabad", "Hyderabad", "Bengaluru", "Bengaluru"]
    assert [e["index"] for e in events] == [0, 1, 2, 3]
    assert [e["total"] for e in events] == [4, 4, 4, 4]
    assert all(not e["error"] for e in events)
    assert all(len(e["jobs"]) == 1 for e in events)

    scraping_msgs = [msg for _, _, msg in progress if msg.startswith("Scraping")]
    assert scraping_msgs[0] == "Scraping 'AI Engineer' in Hyderabad (1/4)"
    assert scraping_msgs[-1] == "Scraping 'ML Engineer' in Bengaluru (4/4)"
    assert all(total == 4 for _, total, _ in progress)
    assert "Found 1 jobs for 'AI Engineer' in Hyderabad" in [msg for _, _, msg in progress]


def test_single_location_still_scrapes_once_per_term(monkeypatch):
    calls = _recording_scraper(monkeypatch)
    scraper = JobScraper(
        _config(search_terms=["AI Engineer", "ML Engineer"], location="Bengaluru")
    )
    events = list(scraper.scrape_iter())

    assert [c["location"] for c in calls] == ["Bengaluru", "Bengaluru"]
    assert len(events) == 2
    assert events[0]["location"] == "Bengaluru"
    assert events[0]["total"] == 2


def test_blank_location_scrapes_once_without_a_filter(monkeypatch):
    calls = _recording_scraper(monkeypatch)
    events = list(JobScraper(_config(location="")).scrape_iter())

    assert len(calls) == 1
    assert calls[0]["location"] == ""
    assert len(events) == 1
    assert events[0]["location"] == ""


def test_scrape_dedupes_a_posting_seen_in_two_cities(monkeypatch):
    same = [
        {
            "title": "AI Engineer",
            "company": "Acme",
            "location": location,
            "site": "linkedin",
            "job_url": "https://example.com/same",
            "description": "d",
        }
        for location in ("Hyderabad", "Bengaluru")
    ]

    def fake(site_name, search_term, location, proxies=None, **kwargs):
        return pd.DataFrame([row for row in same if row["location"] == location])

    monkeypatch.setattr("app.scraper.scrape_jobs_from_sites", fake)

    jobs = JobScraper(
        _config(search_terms=["AI Engineer"], location="Hyderabad, Bengaluru")
    ).scrape().jobs
    assert len(jobs) == 1

    # Distinct postings from different cities both survive.
    def fake_distinct(site_name, search_term, location, proxies=None, **kwargs):
        return pd.DataFrame(
            [
                {
                    "title": "AI Engineer",
                    "company": location,
                    "location": location,
                    "site": "linkedin",
                    "job_url": f"https://example.com/{location}",
                    "description": "d",
                }
            ]
        )

    monkeypatch.setattr("app.scraper.scrape_jobs_from_sites", fake_distinct)
    jobs = JobScraper(
        _config(search_terms=["AI Engineer"], location="Hyderabad, Bengaluru")
    ).scrape().jobs
    assert [job["location"] for job in jobs] == ["Hyderabad", "Bengaluru"]


def test_scrape_forwards_the_progress_callback(monkeypatch):
    """`_scrape` must hand the pipeline's progress sink to JobScraper."""
    from types import SimpleNamespace

    import app.pipeline as pipeline_module

    reported = []

    class SpyScraper:
        def __init__(self, config):
            self.config = config

        def scrape(self, progress_callback=None):
            progress_callback(1, 2, "Scraping 'AI Engineer' in Hyderabad (1/2)")
            return []

    monkeypatch.setattr(pipeline_module, "JobScraper", SpyScraper)

    context = SimpleNamespace(scrape_params={"location": "Hyderabad, Bengaluru"})
    result = pipeline_module._scrape(
        context, progress_callback=lambda done, total, msg: reported.append((done, total, msg))
    )

    assert result.jobs == []
    assert reported == [(1, 2, "Scraping 'AI Engineer' in Hyderabad (1/2)")]
