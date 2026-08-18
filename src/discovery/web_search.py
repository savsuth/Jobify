"""Finds job postings outside the configured Greenhouse/Lever boards using
Claude's built-in web search tool. Requires web search to be enabled for the
Anthropic API key used (see console.anthropic.com)."""

from datetime import datetime, timezone

from anthropic import Anthropic

from src.config import get_settings
from src.discovery.normalize import (
    NormalizedJob,
    classify_employment_type,
    classify_location,
    classify_seniority,
    classify_work_authorization,
    guess_remote_type,
)
from src.llm_json import extract_json_array, response_text

SYSTEM_PROMPT = """\
You are a job search assistant. Use web search to find CURRENT, OPEN job postings \
matching the given criteria. Prefer postings on company career pages or major job \
boards. Only include postings you actually found via search with a real URL - never \
invent a posting.

The hard search criteria are:
- Title / role family: the posting should plausibly belong to one of the given target \
  role families (e.g. a listed title, or a close variant of one).
- Location: must be reachable per the given locations/remote preference.
- Employment type: full-time only - exclude internships, contract, temporary, and \
  part-time postings.
- Experience scope: must be appropriate for the given experience level (e.g. entry-level \
  / new grad), not a senior or staff-level role requiring many years of experience.

Do NOT filter or exclude a posting merely because it uses a different technology stack \
than what the candidate currently lists on their resume or in their search configuration \
(for example, do not exclude a Java backend role just because the candidate's background \
is Python/Rust-heavy). Candidate/job skill fit is judged in a later stage of this \
pipeline, not during search - your job here is to maximize recall of postings that fit \
the role family, location, employment type, and experience scope above, regardless of \
the specific technologies mentioned in the posting.

After searching, respond with ONLY a JSON array (no other text, no markdown fences) \
of objects shaped like:
[{"title": str, "company": str, "url": str, "location": str, "description": str, \
"posted_date": str or null}]

description should be a concise summary of the role's requirements and responsibilities \
based on what you found, not the full page text. Return at most 15 results.

posted_date: the posting's ORIGINAL publish/posted date as YYYY-MM-DD, ONLY when you found \
EXPLICIT evidence of it on the actual page - an explicit date shown on the posting, or an \
explicit relative phrase (e.g. "Posted 3 days ago", "30+ days ago") that you resolve against \
TODAY'S DATE given below. If you found no explicit posting-date evidence, set posted_date to \
null - NEVER guess, estimate, infer from search-result freshness, or default to today's date.
"""


def _build_query(preferences: dict, today: str | None = None) -> str:
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"Titles: {', '.join(preferences['title_keywords'])}\n"
        f"Locations: {', '.join(preferences['locations'])}\n"
        f"Remote preference: {preferences['remote_pref']}\n"
        f"Experience level: {preferences['experience_level']}\n"
        f"Today's date (UTC): {today}\n"
    )


def _parse_web_posted_date(value: str | None) -> datetime | None:
    """Conservative: only accepts a clean YYYY-MM-DD string (what the prompt
    asks for) - any other shape, or a missing/null value, returns None rather
    than guessing at a looser parse. No time-of-day is ever available from
    this source, so a date-only value is normalized to that day's UTC
    midnight (00:00:00Z) - documented here as the one normalization rule for
    this source, per src.discovery.normalize's freshness module note."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_web_search_jobs(preferences: dict) -> list[NormalizedJob]:
    client = Anthropic(api_key=get_settings().anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
        messages=[{"role": "user", "content": _build_query(preferences)}],
    )

    raw_jobs = extract_json_array(response_text(response))

    jobs = []
    for raw in raw_jobs:
        url = raw.get("url", "")
        if not url:
            continue
        location = raw.get("location")
        title = raw.get("title", "")
        description = raw.get("description", "")
        work_auth_status, work_auth_reason = classify_work_authorization(f"{title} {description}")
        jobs.append(
            NormalizedJob(
                source="web",
                source_id=url,
                company=raw.get("company", "unknown"),
                title=title,
                location=location,
                remote_type=guess_remote_type(location, title, description),
                url=url,
                description_raw=description,
                # No structured signal available from search results - text-only
                # fallback, same as Greenhouse's employment_type handling.
                employment_type=classify_employment_type(title),
                location_category=classify_location(location),
                seniority=classify_seniority(title, description),
                # Only set when the model reported explicit on-page evidence (see
                # SYSTEM_PROMPT/_parse_web_posted_date) - None (never
                # discovered_at/today) whenever that evidence wasn't found, so an
                # unresolvable web-search job correctly classifies as
                # POST_DATE_UNKNOWN rather than being guessed as recent.
                posted_at=_parse_web_posted_date(raw.get("posted_date")),
                work_auth_status=work_auth_status,
                work_auth_reason=work_auth_reason,
            )
        )
    return jobs
