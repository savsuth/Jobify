from datetime import datetime

import httpx

from src.discovery.normalize import (
    NormalizedJob,
    classify_employment_type,
    classify_location,
    classify_seniority,
    classify_work_authorization,
    guess_remote_type,
    strip_html,
)

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def fetch_greenhouse_jobs(token: str, client: httpx.Client | None = None) -> list[NormalizedJob]:
    owns_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        resp = client.get(BASE_URL.format(token=token), params={"content": "true"})
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns_client:
            client.close()

    jobs = []
    for raw in data.get("jobs", []):
        location = (raw.get("location") or {}).get("name")
        description = strip_html(raw.get("content", ""))
        office_names = [o["name"] for o in raw.get("offices", []) if o.get("name")]
        work_auth_status, work_auth_reason = classify_work_authorization(f"{raw['title']} {description}")
        jobs.append(
            NormalizedJob(
                source="greenhouse",
                source_id=str(raw["id"]),
                company=token,
                title=raw["title"],
                location=location,
                remote_type=guess_remote_type(location, raw["title"], description),
                url=raw.get("absolute_url", ""),
                description_raw=description,
                # Greenhouse's public API exposes no employment-type field (metadata
                # is null in practice) - title-only heuristic, honestly "unknown" otherwise.
                employment_type=classify_employment_type(raw["title"]),
                # `offices[].name` (e.g. "US" vs "Japan Locations") is more reliable
                # than the freeform `location.name` string - prefer it when present.
                location_category=classify_location(location, office_names=office_names),
                seniority=classify_seniority(raw["title"], description),
                posted_at=_parse_datetime(raw.get("first_published")),
                work_auth_status=work_auth_status,
                work_auth_reason=work_auth_reason,
            )
        )
    return jobs


def fetch_all_greenhouse_jobs(tokens: list[str]) -> list[NormalizedJob]:
    with httpx.Client(timeout=30) as client:
        jobs = []
        for token in tokens:
            jobs.extend(fetch_greenhouse_jobs(token, client=client))
        return jobs
