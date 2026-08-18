import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import respx

from src.discovery.aggregate import _filter
from src.discovery.greenhouse import fetch_greenhouse_jobs
from src.discovery.lever import fetch_lever_jobs
from src.discovery.normalize import (
    AMBIGUOUS_REQUIRES_REVIEW,
    CURRENTLY_ELIGIBLE,
    CURRENTLY_INELIGIBLE,
    POST_DATE_UNKNOWN,
    POSTED_OLD,
    POSTED_RECENTLY,
    NormalizedJob,
    classify_employment_type,
    classify_freshness,
    classify_location,
    classify_seniority,
    classify_work_authorization,
    compute_current_eligibility,
    days_since_posted,
    guess_remote_type,
    is_fresh_enough,
    is_target_role,
    strip_html,
)
from src.discovery.web_search import _build_query, _parse_web_posted_date, fetch_web_search_jobs


def test_strip_html():
    assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_strip_html_handles_escaped_markup():
    # Greenhouse's `content` field returns HTML-escaped HTML.
    assert strip_html("&lt;p&gt;Hello &amp; welcome&lt;/p&gt;") == "Hello & welcome"


def test_guess_remote_type():
    assert guess_remote_type("Remote - US") == "remote"
    assert guess_remote_type("Boston, MA (Hybrid)") == "hybrid"
    assert guess_remote_type("Boston, MA") == "onsite"
    assert guess_remote_type(None) is None


# --- is_target_role -----------------------------------------------------------

def test_is_target_role_positive_examples():
    for title in [
        "Data Engineer",
        "Senior Data Platform Engineer",
        "Data Infrastructure Engineer",
        "Machine Learning Engineer, Capital Underwriting",
        "Applied AI Engineer",
        "AI/ML Platform Engineer",
        "Backend Software Engineer",
        "Software Engineer",
        "Software Engineer II",
        "SDE II",
        "Forward Deployed Engineer, Privy",
        "Forward Deployed Software Engineer",
        "Search Engineer",
        "Retrieval Engineer",
        "NLP Engineer",
        "LLM Engineer",
        "Software Engineer - Infrastructure",
        "Software Engineer - Platform",
        "Analytics Engineer",
        "Data Scientist, Fraud",
        "Quantitative Developer",
        "Quantitative Software Engineer",
        "Solutions Engineer, AI Partnerships",
        "Customer Engineer, AI/Data",
    ]:
        assert is_target_role(title), f"expected match: {title}"


def test_is_target_role_negative_examples():
    for title in [
        "Account Executive, Product Sales",
        "Customer Success Manager",
        "Customer Success Manager (French speaking)",
        "Product Manager, Cash Platform",
        "Engineering Manager, AI Conversation Platform",
        "Legal Counsel",
        "Product Marketing, Data Products",
        "Program Manager, Commercial Solutions",
        "Cloud Security Engineer",
        "Client Platform Security Engineer",
        "Mobile Engineer, Treasury",
        "Android Engineer, Terminal",
        "Frontend Engineer, Expansion",
        "Firmware Engineer",
        "Partner Solutions Architect, AI Partnerships",
        "Global Head of Specialist Solutions Architecture, Billing",
        "ML Engineer Manager, AI Conversation Platform",
        "Data Science Manager, Risk",
    ]:
        assert not is_target_role(title), f"expected no match: {title}"


def test_is_target_role_does_not_reject_seniority_ii_suffix():
    # Seniority is judged separately, not by title filtering.
    assert is_target_role("Software Engineer II")
    assert is_target_role("SDE II")


# --- classify_location ---------------------------------------------------------

def test_classify_location_authoritative_country_code():
    assert classify_location("New York, NY", country_code="US") == "us"
    assert classify_location("London", country_code="GB") == "non_us"


def test_classify_location_office_names():
    assert classify_location("SF, NYC, SEA, CHI", office_names=["US"]) == "us"
    assert classify_location("Tokyo", office_names=["Japan Locations"]) == "non_us"


def test_classify_location_text_fallback():
    assert classify_location("San Francisco, CA") == "us"
    assert classify_location("London") == "non_us"
    assert classify_location("Toronto, Canada") == "non_us"


def test_classify_location_multi_location_with_us_option():
    # A US option among several counts as reachable, per the candidate's actual ask.
    assert classify_location("New York, San Francisco, Seattle, or Remote (US/Canada)") == "us"


def test_classify_location_ambiguous_stays_unknown():
    assert classify_location("N/A") == "location_unknown"
    assert classify_location(None) == "location_unknown"
    assert classify_location("Planet Earth") == "location_unknown"


# --- classify_employment_type ---------------------------------------------------

def test_classify_employment_type_lever_commitment():
    assert classify_employment_type("Backend Engineer", commitment="Permanent") == "full_time"
    assert classify_employment_type("Backend Engineer", commitment="Full Time Contractor") == "contract"
    assert classify_employment_type("Backend Engineer", commitment="Short Term") == "temporary"


def test_classify_employment_type_title_heuristic():
    assert classify_employment_type("Software Engineer Intern (Winter 2027)") == "internship"
    assert classify_employment_type("Backend Engineer, Contractor") == "contract"
    assert classify_employment_type("Backend Engineer") == "unknown"


def test_classify_employment_type_never_assumes_full_time():
    # Greenhouse gives no structured signal - must stay "unknown", not "full_time".
    assert classify_employment_type("Backend Engineer, Core Technology") == "unknown"


# --- classify_seniority ---------------------------------------------------------
#
# Title-based senior/entry-level checks run first (see normalize.py's module note
# on why title-only, not description text) - these tests cover the new pre-ATS
# seniority gate (regression items 1-5 from the seniority-filter fix). The
# description-based years/entry-level fallback below is the original,
# pre-existing logic - unchanged, still tested with an empty/no-signal title to
# prove it still behaves exactly as before.

def test_classify_seniority_senior_title_variants():
    # Regression 1-3: Senior/Staff/Principal/Lead must all classify as "senior",
    # regardless of what (if anything) the description says.
    assert classify_seniority("Senior Software Engineer", "") == "senior"
    assert classify_seniority("Sr. Software Engineer", "") == "senior"
    assert classify_seniority("Staff Software Engineer", "") == "senior"
    assert classify_seniority("Principal Software Engineer", "") == "senior"
    assert classify_seniority("Lead Software Engineer", "") == "senior"
    # Real examples from the live run that motivated this fix.
    assert classify_seniority("Senior Software Engineer - Enterprise AI", "") == "senior"
    assert classify_seniority("Senior Software Engineer, Developer Infrastructure", "") == "senior"


def test_classify_seniority_senior_title_overrides_entry_level_description_text():
    # Title is the authoritative, employer-declared signal - an explicit
    # "Senior" title wins even if description boilerplate mentions "new grad"
    # elsewhere (e.g. a generic early-career benefits blurb).
    assert classify_seniority("Senior Software Engineer", "New grad friendly benefits package.") == "senior"


def test_classify_seniority_new_grad_and_entry_level_titles_retained():
    # Regression 4-5: New Grad / entry-level / junior titles must NOT be
    # classified "senior" - they stay eligible for ATS.
    assert classify_seniority("Software Engineer, New Grad", "") == "entry_level"
    assert classify_seniority("Software Engineer - Entry Level", "") == "entry_level"
    assert classify_seniority("Junior Software Engineer", "") == "entry_level"
    assert classify_seniority("Jr. Software Engineer", "") == "entry_level"


def test_classify_seniority_plain_title_does_not_trigger_senior():
    # A title with no seniority signal at all must not be misclassified.
    assert classify_seniority("Software Engineer", "") != "senior"
    assert classify_seniority("Forward Deployed Software Engineer", "") != "senior"


def test_classify_seniority_description_fallback_unchanged():
    # Original description-based logic (no title signal) - must behave exactly
    # as before this fix.
    assert classify_seniority("", "This role is great for a new grad / entry level candidate.") == "entry_level"
    assert classify_seniority("", "Requires 1+ years of experience.") == "0-1"
    assert classify_seniority("", "Requires 2+ years of experience.") == "1-2"
    assert classify_seniority("", "Requires 3+ years of experience.") == "2-3"
    assert classify_seniority("", "Requires 5+ years of experience.") == "3-5"
    assert classify_seniority("", "Requires 8+ years of experience.") == "5+"
    assert classify_seniority("", "No years mentioned at all.") == "unknown"


def test_classify_seniority_government_title_not_treated_as_senior():
    # Regression 6: government-facing wording alone must never trigger the
    # senior-tier classification - only an explicit seniority-tier word does.
    assert classify_seniority("Forward Deployed Software Engineer - US Government", "") != "senior"
    assert classify_seniority("Forward Deployed Infrastructure Engineer, New Grad - US Government", "") == (
        "entry_level"
    )
    # But a title that is BOTH senior-tier AND government-facing must still be
    # rejected - for being senior, not for being government-related.
    assert classify_seniority("Senior Software Engineer - US Government", "") == "senior"


def test_seniority_and_work_authorization_classify_independently():
    # Regression 7: adding the seniority gate must not change F-1/OPT logic at
    # all - both classifiers run on the same posting and reach their own,
    # independent conclusions.
    title = "Forward Deployed Software Engineer - US Government"
    non_citizen_description = "Entry-level role embedded with government customers."
    citizen_required_description = "Must be a U.S. citizen to apply for this role."

    assert classify_seniority(title, non_citizen_description) != "senior"
    assert classify_work_authorization(f"{title} {non_citizen_description}") == ("eligible", None)

    # Same (non-senior) title, but this posting's description requires
    # citizenship - work_auth correctly flags it while seniority still doesn't
    # reject it for being government-related.
    assert classify_seniority(title, citizen_required_description) != "senior"
    status, reason = classify_work_authorization(f"{title} {citizen_required_description}")
    assert status == "ineligible"
    assert "citizenship" in reason.lower()


# --- aggregate._filter: seniority as a pre-ATS gate ------------------------------

def _nj(title: str, seniority: str, **overrides) -> NormalizedJob:
    defaults = dict(
        source="greenhouse",
        source_id=title,
        company="acme",
        title=title,
        location="Remote - US",
        remote_type="remote",
        url="https://acme.com/jobs/1",
        description_raw="...",
        employment_type="full_time",
        location_category="us",
        seniority=seniority,
        posted_at=None,
        work_auth_status="eligible",
        work_auth_reason=None,
    )
    defaults.update(overrides)
    return NormalizedJob(**defaults)


def test_filter_rejects_senior_titled_jobs_before_ats():
    stats = {
        "raw_fetched": 0,
        "non_us_rejected": 0,
        "non_full_time_rejected": 0,
        "title_rejected": 0,
        "senior_rejected": 0,
        "eligible": 0,
    }
    jobs = [
        _nj("Senior Software Engineer", "senior"),
        _nj("Staff Software Engineer", "senior"),
        _nj("Principal Software Engineer", "senior"),
        _nj("Lead Software Engineer", "senior"),
        _nj("Software Engineer, New Grad", "entry_level"),
    ]
    eligible = _filter(jobs, stats)

    assert [j.title for j in eligible] == ["Software Engineer, New Grad"]
    assert stats["senior_rejected"] == 4
    assert stats["eligible"] == 1


def test_filter_retains_unknown_seniority_jobs():
    # "unknown" must not be treated as a rejection - preserve current behavior.
    stats = {
        "raw_fetched": 0,
        "non_us_rejected": 0,
        "non_full_time_rejected": 0,
        "title_rejected": 0,
        "senior_rejected": 0,
        "eligible": 0,
    }
    jobs = [_nj("Software Engineer", "unknown")]
    eligible = _filter(jobs, stats)

    assert [j.title for j in eligible] == ["Software Engineer"]
    assert stats["senior_rejected"] == 0
    assert stats["eligible"] == 1


def test_filter_does_not_reject_government_role_for_being_government_related():
    # Regression 6: a non-senior, government-facing role must pass the
    # seniority gate - only work_auth logic (untouched here) governs eligibility
    # for citizenship/clearance requirements.
    stats = {
        "raw_fetched": 0,
        "non_us_rejected": 0,
        "non_full_time_rejected": 0,
        "title_rejected": 0,
        "senior_rejected": 0,
        "eligible": 0,
    }
    jobs = [_nj("Forward Deployed Software Engineer - US Government", "unknown")]
    eligible = _filter(jobs, stats)

    assert [j.title for j in eligible] == ["Forward Deployed Software Engineer - US Government"]
    assert stats["senior_rejected"] == 0


def test_filter_rejects_senior_government_role_for_being_senior_not_government():
    stats = {
        "raw_fetched": 0,
        "non_us_rejected": 0,
        "non_full_time_rejected": 0,
        "title_rejected": 0,
        "senior_rejected": 0,
        "eligible": 0,
    }
    jobs = [_nj("Senior Software Engineer - US Government", "senior")]
    eligible = _filter(jobs, stats)

    assert eligible == []
    assert stats["senior_rejected"] == 1


# --- classify_work_authorization (F-1/OPT eligibility) -------------------------

def test_classify_work_authorization_eligible_by_default():
    # No immigration-related language at all - default is eligible, not unknown.
    assert classify_work_authorization("Backend Engineer building payment APIs in Python.") == (
        "eligible",
        None,
    )


def test_classify_work_authorization_general_work_auth_is_not_a_trigger():
    # Explicitly required by the user: plain "authorized to work" language must
    # NOT cause a rejection - OPT genuinely provides work authorization.
    status, _ = classify_work_authorization(
        "Candidates must be authorized to work in the United States."
    )
    assert status == "eligible"


def test_classify_work_authorization_citizenship_required():
    status, reason = classify_work_authorization("You must be a U.S. citizen to apply for this role.")
    assert status == "ineligible"
    assert "citizenship" in reason.lower()


def test_classify_work_authorization_citizenship_required_phrasing_variants():
    for text in [
        "U.S. citizenship is required for this position.",
        "This role is open to U.S. citizens only.",
    ]:
        status, _ = classify_work_authorization(text)
        assert status == "ineligible", text


def test_classify_work_authorization_green_card_required():
    status, reason = classify_work_authorization("Applicants must currently hold a green card.")
    assert status == "ineligible"
    assert "green card" in reason.lower()


def test_classify_work_authorization_export_control():
    status, reason = classify_work_authorization(
        "This role is ITAR-restricted and requires the candidate to qualify as a U.S. person."
    )
    assert status == "ineligible"


def test_classify_work_authorization_clearance_tied_to_citizenship():
    status, reason = classify_work_authorization(
        "This position requires an active security clearance; candidates must be U.S. citizens."
    )
    assert status == "ineligible"
    assert "clearance" in reason.lower()


def test_classify_work_authorization_bare_clearance_mention_is_unknown():
    # Clearance mentioned but no explicit citizenship language - don't guess.
    status, reason = classify_work_authorization(
        "This position requires an active security clearance."
    )
    assert status == "unknown"
    assert "clearance" in reason.lower()


def test_classify_work_authorization_sponsorship_mention_is_unknown():
    status, reason = classify_work_authorization(
        "We are unable to provide visa sponsorship for this role."
    )
    assert status == "unknown"


def test_classify_work_authorization_escape_hatch_alternative():
    # Citizenship mentioned alongside a work-authorization alternative that OPT
    # would satisfy - don't hard-reject, but still flag for a human look.
    status, reason = classify_work_authorization(
        "Must be a U.S. citizen, permanent resident, or authorized to work in the US."
    )
    assert status == "unknown"


def test_classify_work_authorization_does_not_false_positive_on_eeo_boilerplate():
    # Standard EEO non-discrimination language mentions "citizenship status" in
    # the OPPOSITE sense of a requirement - must not trigger a rejection.
    status, _ = classify_work_authorization(
        "We do not discriminate on the basis of race, color, religion, sex, "
        "national origin, disability, protected veteran status, or citizenship status."
    )
    assert status == "eligible"


# --- web_search._build_query ----------------------------------------------------

_PREFERENCES = {
    "title_keywords": ["Data Engineer", "Software Engineer"],
    "locations": ["United States"],
    "remote_pref": "any",
    "experience_level": "entry-level (intern / new grad, graduating December 2026)",
}


def test_build_query_does_not_include_technologies_line():
    # Discovery must not filter by tech stack - only role family, location,
    # employment type, and experience scope are hard search criteria.
    query = _build_query(_PREFERENCES)
    assert "Technologies" not in query


def test_build_query_includes_role_location_and_experience_criteria():
    query = _build_query(_PREFERENCES)
    assert "Data Engineer" in query
    assert "United States" in query
    assert "entry-level" in query


def test_build_query_ignores_technologies_key_if_present():
    # Even if a caller still passes a "technologies" key (e.g. stale config),
    # _build_query must not read or surface it.
    preferences_with_stale_key = {**_PREFERENCES, "technologies": ["Python", "Rust"]}
    query = _build_query(preferences_with_stale_key)
    assert "Technologies" not in query
    assert "Python" not in query
    assert "Rust" not in query


@respx.mock
def test_fetch_greenhouse_jobs():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 123,
                        "title": "Backend Engineer",
                        "location": {"name": "Remote"},
                        "absolute_url": "https://acme.com/jobs/123",
                        "content": "<p>Build things with Python. Requires 3+ years experience.</p>",
                        "offices": [{"name": "US"}],
                        "first_published": "2026-07-22T13:15:53-04:00",
                    }
                ]
            },
        )
    )
    jobs = fetch_greenhouse_jobs("acme")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "greenhouse"
    assert job.source_id == "123"
    assert job.remote_type == "remote"
    assert "Python" in job.description_raw
    assert job.location_category == "us"
    assert job.employment_type == "unknown"  # Greenhouse exposes no structured signal
    assert job.seniority == "2-3"
    assert job.posted_at == datetime.fromisoformat("2026-07-22T13:15:53-04:00")
    assert job.work_auth_status == "eligible"
    assert job.work_auth_reason is None


@respx.mock
def test_fetch_greenhouse_jobs_flags_citizenship_requirement():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 124,
                        "title": "Backend Engineer - Defense",
                        "location": {"name": "Remote"},
                        "absolute_url": "https://acme.com/jobs/124",
                        "content": "<p>Must be a U.S. citizen to apply for this role.</p>",
                    }
                ]
            },
        )
    )
    jobs = fetch_greenhouse_jobs("acme")
    assert jobs[0].work_auth_status == "ineligible"
    assert "citizenship" in jobs[0].work_auth_reason.lower()


@respx.mock
def test_fetch_lever_jobs():
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "abc",
                    "text": "Backend Engineer",
                    "categories": {"location": "Remote", "commitment": "Permanent"},
                    "country": "US",
                    "workplaceType": "remote",
                    "hostedUrl": "https://jobs.lever.co/acme/abc",
                    "description": "<p>Build things with Go. Requires 5+ years experience.</p>",
                    "createdAt": 1700000000000,
                }
            ],
        )
    )
    jobs = fetch_lever_jobs("acme")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "lever"
    assert job.source_id == "abc"
    assert "Go" in job.description_raw
    assert job.location_category == "us"
    assert job.employment_type == "full_time"
    assert job.remote_type == "remote"
    assert job.seniority == "3-5"
    assert job.posted_at == datetime.fromtimestamp(1700000000000 / 1000, tz=timezone.utc)
    assert job.work_auth_status == "eligible"
    assert job.work_auth_reason is None


# --- compute_current_eligibility (2026-08-17 historical-cleanup task) ----------
#
# Re-evaluates a job against CURRENT Discovery rules only - see the function's
# own docstring for why "current rules are the source of truth" means this
# must mirror _filter()'s exact 4 rejection criteria (non_us / senior /
# off-target-title / explicit non-full-time), never inventing a 5th, while
# ALSO distinguishing genuinely uncertain signals (location_unknown) as
# AMBIGUOUS_REQUIRES_REVIEW rather than silently eligible.

def test_eligibility_clearly_eligible_job():
    status, reasons = compute_current_eligibility(
        "Software Engineer, New Grad", "New York, NY", "Entry level role for new grads.",
    )
    assert status == CURRENTLY_ELIGIBLE
    assert reasons == []


def test_eligibility_non_us_location_is_ineligible():
    # Exact confirmed live case: job 9, "Backend/API Engineer, Money as a
    # Service" @ stripe, location "United Kingdom".
    status, reasons = compute_current_eligibility(
        "Backend/API Engineer, Money as a Service", "United Kingdom", "...",
    )
    assert status == CURRENTLY_INELIGIBLE
    assert "non-US location" in reasons


def test_eligibility_senior_title_is_ineligible():
    # Exact confirmed live case: job 213, "Staff Data Scientist, Security".
    status, reasons = compute_current_eligibility("Staff Data Scientist, Security", "n/a", "...")
    assert status == CURRENTLY_INELIGIBLE
    assert "senior/staff/principal/lead title" in reasons


def test_eligibility_wrong_role_family_is_ineligible():
    status, reasons = compute_current_eligibility("Customer Success Manager", "New York, NY", "...")
    assert status == CURRENTLY_INELIGIBLE
    assert "wrong role family" in reasons


def test_eligibility_non_full_time_is_ineligible():
    status, reasons = compute_current_eligibility("Software Engineering Intern", "New York, NY", "...")
    assert status == CURRENTLY_INELIGIBLE
    assert any("non-full-time" in r for r in reasons)


def test_eligibility_multiple_violations_flagged_together():
    # Exact confirmed live case: job 5, "Android BSP Engineer" @ stripe,
    # Taipei, Taiwan - both wrong role family AND non-US.
    status, reasons = compute_current_eligibility("Android BSP Engineer", "Taipei, Taiwan", "...")
    assert status == CURRENTLY_INELIGIBLE
    assert "non-US location" in reasons
    assert "wrong role family" in reasons
    assert "multiple violations" in reasons


def test_eligibility_ambiguous_location_requires_review():
    status, reasons = compute_current_eligibility("Software Engineer", "N/A", "...")
    assert status == AMBIGUOUS_REQUIRES_REVIEW
    assert "insufficient location evidence / ambiguous location" in reasons


def test_eligibility_unknown_employment_type_does_not_block_eligibility():
    # Critical regression: without a source-provided commitment field (true
    # for every Greenhouse job), classify_employment_type(title) can ONLY
    # ever return "unknown" or an explicit non-full-time type - never
    # positively "full_time". Treating "unknown" as blocking would mark
    # nearly the entire Greenhouse-sourced pool ambiguous, which is not a
    # real signal and directly contradicts current _filter() behavior (which
    # never rejects "unknown" employment type either). This was caught by a
    # pre-apply dry run that showed 0/304 jobs reaching CURRENTLY_ELIGIBLE.
    status, reasons = compute_current_eligibility("Software Engineer, New Grad", "New York, NY", "...")
    assert status == CURRENTLY_ELIGIBLE


def test_eligibility_years_of_experience_alone_never_blocks():
    # current _filter() only rejects on the literal seniority value "senior"
    # (title-driven) - a description mentioning "5+ years" with a plain,
    # non-senior title must NOT be treated as ineligible; that would be
    # inventing a new filtering criterion beyond what _filter() enforces today.
    status, reasons = compute_current_eligibility(
        "Software Engineer", "New York, NY", "Looking for a candidate with 5+ years of experience.",
    )
    assert status == CURRENTLY_ELIGIBLE


def test_eligibility_toronto_robinhood_wallet_case():
    # Exact confirmed live case (jobs 188/232): live Greenhouse re-fetch
    # showed the posting's OWN location.name is "Toronto, Canada" - conclusive,
    # not ambiguous - even though the original persisted row was stored "us"
    # (Greenhouse's offices[] field returned Robinhood's generic company
    # office roster, not this posting's actual location).
    status, reasons = compute_current_eligibility(
        "Senior Software Engineer, Wallet", "Toronto, Canada", "...",
    )
    assert status == CURRENTLY_INELIGIBLE
    assert "non-US location" in reasons


# --- Posting-date freshness (2026-08-17 Discovery enhancement) -----------------
#
# posted_at was already stored per-job (migration 0003, Greenhouse
# first_published / Lever createdAt) but nothing consumed it as a filter -
# these tests cover the new classify_freshness/is_fresh_enough/
# days_since_posted gate added on top of that already-real data, plus the new
# evidence-only web-search date extraction.

_NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def test_freshness_posted_today_is_recent():
    # A: job posted today -> POSTED_RECENTLY.
    posted = _NOW - timedelta(hours=2)
    assert classify_freshness(posted, posted_within_days=7, now=_NOW) == POSTED_RECENTLY


def test_freshness_exact_boundary_is_recent_inclusive():
    # B: posted EXACTLY posted_within_days ago is the documented inclusive
    # boundary - counts as POSTED_RECENTLY, not POSTED_OLD.
    posted = _NOW - timedelta(days=7)
    assert classify_freshness(posted, posted_within_days=7, now=_NOW) == POSTED_RECENTLY


def test_freshness_one_second_past_boundary_is_old():
    posted = _NOW - timedelta(days=7, seconds=1)
    assert classify_freshness(posted, posted_within_days=7, now=_NOW) == POSTED_OLD


def test_freshness_older_than_window_is_old():
    # C: clearly older than 7 days -> POSTED_OLD.
    posted = _NOW - timedelta(days=29)  # ~July 20 relative to an Aug 17 "now"
    assert classify_freshness(posted, posted_within_days=7, now=_NOW) == POSTED_OLD


def test_freshness_missing_posted_at_is_unknown():
    # D: no posting date at all -> POST_DATE_UNKNOWN, never guessed.
    assert classify_freshness(None, posted_within_days=7, now=_NOW) == POST_DATE_UNKNOWN


def test_freshness_never_derived_from_discovered_at():
    # E: classify_freshness has no discovered_at parameter at all - passing an
    # OLD posted_at must classify as POSTED_OLD regardless of how recently the
    # job was actually discovered/ingested (discovered_at is irrelevant here
    # by construction - this test documents that, not just asserts a value).
    old_posted_at = _NOW - timedelta(days=60)
    discovered_at = _NOW  # ingested/discovered right now - must not matter
    assert classify_freshness(old_posted_at, posted_within_days=7, now=discovered_at) == POSTED_OLD


def test_freshness_timezone_normalization_non_utc_offset():
    # F: a posted_at with a non-UTC offset must normalize correctly - 2026-08-17
    # 09:00-05:00 is 14:00 UTC, which is AFTER a 12:00 UTC "now", so still recent.
    posted_minus5 = datetime(2026, 8, 17, 9, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert classify_freshness(posted_minus5, posted_within_days=7, now=_NOW) == POSTED_RECENTLY


def test_freshness_naive_datetime_treated_as_utc():
    # F: a naive (no tzinfo) posted_at is treated as UTC, not machine-local time.
    naive_posted = datetime(2026, 8, 17, 10, 0, 0)  # no tzinfo
    assert classify_freshness(naive_posted, posted_within_days=7, now=_NOW) == POSTED_RECENTLY


def test_freshness_naive_now_treated_as_utc():
    naive_now = datetime(2026, 8, 17, 12, 0, 0)
    posted = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
    assert classify_freshness(posted, posted_within_days=7, now=naive_now) == POSTED_RECENTLY


def test_is_fresh_enough_recent_true():
    assert is_fresh_enough(_NOW - timedelta(days=1), posted_within_days=7, now=_NOW) is True


def test_is_fresh_enough_old_false():
    assert is_fresh_enough(_NOW - timedelta(days=30), posted_within_days=7, now=_NOW) is False


def test_is_fresh_enough_unknown_date_false_when_enabled():
    # M: an unknown-date job must NOT pass the fresh-operational-pool gate
    # merely because the gate exists - unknown is never silently "recent".
    assert is_fresh_enough(None, posted_within_days=7, now=_NOW) is False


def test_is_fresh_enough_disabled_gate_restores_prior_behavior():
    # O: posted_within_days=None disables the gate entirely - every job
    # passes, including an old one and an unknown-date one (matches
    # pre-freshness-filter behavior exactly).
    assert is_fresh_enough(_NOW - timedelta(days=400), posted_within_days=None, now=_NOW) is True
    assert is_fresh_enough(None, posted_within_days=None, now=_NOW) is True


def test_is_fresh_enough_configurable_window_7_vs_14_days():
    # N: a job 10 days old fails a 7-day window but passes a 14-day window -
    # proves the window is genuinely configurable, not hardcoded.
    posted = _NOW - timedelta(days=10)
    assert is_fresh_enough(posted, posted_within_days=7, now=_NOW) is False
    assert is_fresh_enough(posted, posted_within_days=14, now=_NOW) is True


def test_days_since_posted_known_date():
    assert days_since_posted(_NOW - timedelta(days=3), now=_NOW) == 3


def test_days_since_posted_unknown_is_none():
    assert days_since_posted(None, now=_NOW) is None


# --- Greenhouse/Lever posting-date parsing feeding classify_freshness ----------
# (G/H) - fetch_greenhouse_jobs/fetch_lever_jobs already assert posted_at
# parsing directly (see test_fetch_greenhouse_jobs/test_fetch_lever_jobs
# above) - these confirm that parsed value flows correctly into
# classify_freshness end to end.

@respx.mock
def test_greenhouse_posted_at_feeds_freshness_classification():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(
            200,
            json={"jobs": [{
                "id": 1, "title": "Backend Engineer", "location": {"name": "Remote"},
                "absolute_url": "https://acme.com/jobs/1", "content": "<p>...</p>",
                "first_published": (_NOW - timedelta(days=2)).isoformat(),
            }]},
        )
    )
    job = fetch_greenhouse_jobs("acme")[0]
    assert classify_freshness(job.posted_at, posted_within_days=7, now=_NOW) == POSTED_RECENTLY


@respx.mock
def test_lever_posted_at_feeds_freshness_classification():
    stale_ms = int((_NOW - timedelta(days=40)).timestamp() * 1000)
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(
            200,
            json=[{
                "id": "abc", "text": "Backend Engineer", "categories": {"location": "Remote"},
                "country": "US", "hostedUrl": "https://jobs.lever.co/acme/abc",
                "description": "<p>...</p>", "createdAt": stale_ms,
            }],
        )
    )
    job = fetch_lever_jobs("acme")[0]
    assert classify_freshness(job.posted_at, posted_within_days=7, now=_NOW) == POSTED_OLD


# --- Web-search posting-date extraction (evidence-only, never fabricated) ------

def _ws_response(data: list):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(data))], stop_reason="end_turn")


def test_parse_web_posted_date_valid_iso_date():
    assert _parse_web_posted_date("2026-08-10") == datetime(2026, 8, 10, tzinfo=timezone.utc)


def test_parse_web_posted_date_missing_is_none():
    assert _parse_web_posted_date(None) is None


def test_parse_web_posted_date_garbage_is_none_not_guessed():
    assert _parse_web_posted_date("3 days ago") is None
    assert _parse_web_posted_date("recently") is None
    assert _parse_web_posted_date("") is None


@patch("src.discovery.web_search.Anthropic")
def test_fetch_web_search_jobs_uses_explicit_posted_date_evidence(mock_anthropic_cls):
    # I: the source DOES provide trustworthy date evidence (the model reported
    # an explicit posted_date) - it must be parsed into posted_at.
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _ws_response([{
        "title": "Backend Engineer", "company": "Acme", "url": "https://acme.com/jobs/1",
        "location": "Remote", "description": "...", "posted_date": "2026-08-15",
    }])
    jobs = fetch_web_search_jobs({
        "title_keywords": ["Backend Engineer"], "locations": ["United States"],
        "remote_pref": "any", "experience_level": "entry-level",
    })
    assert jobs[0].posted_at == datetime(2026, 8, 15, tzinfo=timezone.utc)


@patch("src.discovery.web_search.Anthropic")
def test_fetch_web_search_jobs_unknown_date_stays_none_not_discovered_at(mock_anthropic_cls):
    # J: no posted_date evidence (field absent) -> posted_at stays None, is
    # NEVER substituted with discovered_at or "now" at fetch time.
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _ws_response([{
        "title": "Backend Engineer", "company": "Acme", "url": "https://acme.com/jobs/2",
        "location": "Remote", "description": "...",
        # no "posted_date" key at all
    }])
    jobs = fetch_web_search_jobs({
        "title_keywords": ["Backend Engineer"], "locations": ["United States"],
        "remote_pref": "any", "experience_level": "entry-level",
    })
    assert jobs[0].posted_at is None
    assert classify_freshness(jobs[0].posted_at, posted_within_days=7, now=_NOW) == POST_DATE_UNKNOWN


def test_build_query_includes_todays_date_for_relative_date_resolution():
    query = _build_query({
        "title_keywords": ["Backend Engineer"], "locations": ["United States"],
        "remote_pref": "any", "experience_level": "entry-level",
    }, today="2026-08-17")
    assert "2026-08-17" in query
