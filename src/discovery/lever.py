from datetime import datetime, timezone

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

BASE_URL = "https://api.lever.co/v0/postings/{token}"


def fetch_lever_jobs(token: str, client: httpx.Client | None = None) -> list[NormalizedJob]:
    owns_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        resp = client.get(BASE_URL.format(token=token), params={"mode": "json"})
        resp.raise_for_status()
        postings = resp.json()
    finally:
        if owns_client:
            client.close()

    jobs = []
    for raw in postings:
        categories = raw.get("categories", {})
        location = categories.get("location")
        description = strip_html(raw.get("description", "")) or strip_html(
            raw.get("descriptionPlain", "")
        )
        created_at_ms = raw.get("createdAt")
        work_auth_status, work_auth_reason = classify_work_authorization(f"{raw['text']} {description}")
        jobs.append(
            NormalizedJob(
                source="lever",
                source_id=str(raw["id"]),
                company=token,
                title=raw["text"],
                location=location,
                # Lever's workplaceType (remote/hybrid/onsite) is structured and
                # matches our vocabulary directly - prefer it over text guessing.
                remote_type=raw.get("workplaceType") or guess_remote_type(location, raw["text"], description),
                url=raw.get("hostedUrl", ""),
                description_raw=description,
                employment_type=classify_employment_type(raw["text"], commitment=categories.get("commitment")),
                # `country` is an authoritative ISO code - Lever's most reliable signal.
                location_category=classify_location(location, country_code=raw.get("country")),
                seniority=classify_seniority(raw["text"], description),
                posted_at=(
                    datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc)
                    if created_at_ms
                    else None
                ),
                work_auth_status=work_auth_status,
                work_auth_reason=work_auth_reason,
            )
        )
    return jobs


def fetch_all_lever_jobs(tokens: list[str]) -> list[NormalizedJob]:
    with httpx.Client(timeout=30) as client:
        jobs = []
        for token in tokens:
            jobs.extend(fetch_lever_jobs(token, client=client))
        return jobs
