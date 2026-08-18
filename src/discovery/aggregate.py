"""Fetches + deterministically filters candidate jobs per company/source.

Deliberately does NOT touch the DB or decide how many jobs to keep - that's
pipeline.py's job (dedup + per-company round-robin + persistence). This
module's only responsibility: fetch raw postings, then apply the cheap,
deterministic filters (location, employment type, title relevance,
seniority), per the pipeline order:

    Normalize -> US-only -> Full-time -> Title/role relevance -> Seniority

The seniority filter only rejects postings whose TITLE explicitly signals a
senior-tier role (Senior/Staff/Principal/Lead - normalize.classify_seniority).
"unknown" and every non-senior bucket pass through unchanged.
"""

import httpx

from src.db.models import SearchConfig
from src.discovery.greenhouse import fetch_greenhouse_jobs
from src.discovery.lever import fetch_lever_jobs
from src.discovery.normalize import NormalizedJob, is_target_role
from src.discovery.web_search import fetch_web_search_jobs

_NON_FULL_TIME_TYPES = {"part_time", "internship", "contract", "temporary"}


def _filter(raw_jobs: list[NormalizedJob], stats: dict) -> list[NormalizedJob]:
    eligible = []
    for job in raw_jobs:
        stats["raw_fetched"] += 1
        if job.location_category == "non_us":
            stats["non_us_rejected"] += 1
            continue
        if job.employment_type in _NON_FULL_TIME_TYPES:
            stats["non_full_time_rejected"] += 1
            continue
        if not is_target_role(job.title):
            stats["title_rejected"] += 1
            continue
        if job.seniority == "senior":
            stats["senior_rejected"] += 1
            continue
        stats["eligible"] += 1
        eligible.append(job)
    return eligible


def discover_candidates(config: SearchConfig) -> tuple[dict[str, list[NormalizedJob]], dict]:
    """Returns (candidates grouped by "source:company", filter stats).

    Grouping by company (rather than one flat list) is what lets the pipeline
    round-robin across companies instead of one large board exhausting the run.
    """
    stats = {
        "raw_fetched": 0,
        "non_us_rejected": 0,
        "non_full_time_rejected": 0,
        "title_rejected": 0,
        "senior_rejected": 0,
        "eligible": 0,
    }
    by_company: dict[str, list[NormalizedJob]] = {}

    with httpx.Client(timeout=30) as client:
        for token in config.companies.get("greenhouse", []):
            raw_jobs = fetch_greenhouse_jobs(token, client=client)
            by_company[f"greenhouse:{token}"] = _filter(raw_jobs, stats)
        for token in config.companies.get("lever", []):
            raw_jobs = fetch_lever_jobs(token, client=client)
            by_company[f"lever:{token}"] = _filter(raw_jobs, stats)

    web_jobs = fetch_web_search_jobs(
        {
            "title_keywords": config.title_keywords,
            "locations": config.locations,
            "remote_pref": config.remote_pref,
            "experience_level": config.experience_level,
        }
    )
    by_company["web:search"] = _filter(web_jobs, stats)

    return by_company, stats
