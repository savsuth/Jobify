"""Regression tests for canonical_job_identity() - the cross-source job
identity fix (2026-08-17 investigation: web search + native Greenhouse/Lever
connectors independently discovering the same posting produced 7 real
duplicate pairs in the DB). Pure-function tests for the normalization/matching
rules; see tests/test_pipeline.py for the discover_jobs() dedup-wiring tests
(same-run and cross-run behavior, existing (source, source_id) preservation).
"""

from src.discovery.normalize import canonical_job_identity


# --- A/B: same exact URL, different (hypothetical) discovery source ------------
# canonical_job_identity() only ever takes a url - "discovered via web vs
# discovered via Lever" is proven equivalent at the pipeline level (same URL in,
# same key out, regardless of which connector produced the NormalizedJob), but
# is worth pinning down directly here too since it's the core guarantee.

def test_lever_url_produces_stable_key_regardless_of_how_it_was_found():
    url = "https://jobs.lever.co/palantir/cbe90327-3e6e-451c-a54c-1d3cbcef5aeb"
    # Simulates: web search found this exact URL; the native Lever connector
    # later found the identical URL for the same still-open posting.
    assert canonical_job_identity(url) == canonical_job_identity(url)
    assert canonical_job_identity(url) == "lever:palantir:cbe90327-3e6e-451c-a54c-1d3cbcef5aeb"


def test_greenhouse_url_produces_stable_key_regardless_of_how_it_was_found():
    url = "https://job-boards.greenhouse.io/airtable/jobs/8409376002"
    assert canonical_job_identity(url) == canonical_job_identity(url)
    assert canonical_job_identity(url) == "greenhouse:airtable:8409376002"


def test_greenhouse_legacy_boards_subdomain_also_recognized():
    # Some Greenhouse-hosted boards use boards.greenhouse.io rather than the
    # newer job-boards.greenhouse.io - both must resolve to the same identity.
    a = canonical_job_identity("https://boards.greenhouse.io/stripe/jobs/12345")
    b = canonical_job_identity("https://job-boards.greenhouse.io/stripe/jobs/12345")
    assert a == b == "greenhouse:stripe:12345"


# --- C: equivalent normalized URL variants --------------------------------------

def test_generic_url_ignores_tracking_query_params():
    a = canonical_job_identity("https://www.builtinsf.com/job/some-role/123")
    b = canonical_job_identity(
        "https://www.builtinsf.com/job/some-role/123?utm_source=linkedin&utm_campaign=x"
    )
    assert a == b


def test_generic_url_ignores_trailing_slash():
    a = canonical_job_identity("https://www.builtinsf.com/job/some-role/123")
    b = canonical_job_identity("https://www.builtinsf.com/job/some-role/123/")
    assert a == b


def test_generic_url_normalizes_scheme_and_host_case():
    a = canonical_job_identity("https://WWW.BuiltInSF.com/job/some-role/123")
    b = canonical_job_identity("https://www.builtinsf.com/job/some-role/123")
    assert a == b


def test_lever_uuid_case_insensitive():
    a = canonical_job_identity("https://jobs.lever.co/palantir/CBE90327-3E6E-451C-A54C-1D3CBCEF5AEB")
    b = canonical_job_identity("https://jobs.lever.co/palantir/cbe90327-3e6e-451c-a54c-1d3cbcef5aeb")
    assert a == b


def test_generic_url_preserves_a_real_distinguishing_query_param():
    # Only known tracking params are stripped - a real distinguishing query
    # param (e.g. a job ID in the query string) must NOT be discarded.
    a = canonical_job_identity("https://example.com/careers?job_id=111")
    b = canonical_job_identity("https://example.com/careers?job_id=222")
    assert a != b


# --- gh_jid: Greenhouse's query-param job ID (Stripe-style), not a tracking param
#
# Caught via a read-only backfill dry-run against the real database before this
# fix was ever applied: gh_jid was initially (wrongly) listed in
# _TRACKING_QUERY_PARAMS. Stripe's absolute_url for EVERY posting is
# "https://stripe.com/jobs/search?gh_jid={id}" - no per-job path, no company
# slug anywhere in the URL. Stripping gh_jid as "tracking" would have collapsed
# all ~125 distinct Stripe postings in the live DB onto one canonical identity.

def test_gh_jid_style_url_produces_distinct_keys_per_job():
    a = canonical_job_identity("https://stripe.com/jobs/search?gh_jid=8114738")
    b = canonical_job_identity("https://stripe.com/jobs/search?gh_jid=8060387")
    assert a != b
    assert a == "greenhouse:jid:8114738"


def test_gh_jid_style_url_stable_across_rediscovery():
    # A + B, this URL shape specifically: same URL found twice (e.g. web
    # search, then the native Greenhouse connector re-fetching Stripe's board)
    # must produce the identical key both times.
    url = "https://stripe.com/jobs/search?gh_jid=8114738"
    assert canonical_job_identity(url) == canonical_job_identity(url)


def test_gh_jid_survives_alongside_other_query_params():
    a = canonical_job_identity("https://stripe.com/jobs/search?gh_jid=8114738")
    b = canonical_job_identity("https://stripe.com/jobs/search?utm_source=x&gh_jid=8114738")
    assert a == b


# --- D/E/F: must remain separate (conservative - no fuzzy matching) ------------

def test_same_company_same_title_different_lever_posting_ids_stay_separate():
    # Two distinct Lever postings for the literal same company+title combo
    # (e.g. different locations) - Greenhouse/Lever legitimately expose these
    # as separate postings; canonical identity must not collapse them.
    a = canonical_job_identity("https://jobs.lever.co/palantir/11111111-1111-1111-1111-111111111111")
    b = canonical_job_identity("https://jobs.lever.co/palantir/22222222-2222-2222-2222-222222222222")
    assert a != b


def test_same_company_similar_title_different_ats_id_stays_separate():
    a = canonical_job_identity("https://job-boards.greenhouse.io/stripe/jobs/1000")
    b = canonical_job_identity("https://job-boards.greenhouse.io/stripe/jobs/1001")
    assert a != b


def test_different_companies_similar_titles_stay_separate():
    a = canonical_job_identity("https://jobs.lever.co/palantir/33333333-3333-3333-3333-333333333333")
    b = canonical_job_identity("https://jobs.lever.co/robinhood/33333333-3333-3333-3333-333333333333")
    assert a != b  # company slug differs even though the (contrived) UUID matches


# --- I: coincidental cross-company native-ID collision must not merge ----------

def test_canonical_identity_does_not_merge_a_coincidental_cross_company_id_collision():
    # A real Greenhouse numeric ID is globally unique in practice, but this
    # pins down the defense-in-depth: the company slug is part of the key, so
    # even an identical native ID under two different company slugs stays
    # separate - never merged purely on the numeric ID.
    a = canonical_job_identity("https://job-boards.greenhouse.io/companya/jobs/999999")
    b = canonical_job_identity("https://job-boards.greenhouse.io/companyb/jobs/999999")
    assert a != b


# --- J: no canonical URL / empty url never crashes, never collides silently ----

def test_empty_url_does_not_raise_and_produces_a_key():
    # discover_jobs() always ALSO checks (source, source_id) first - this only
    # confirms the canonical-key layer degrades harmlessly rather than raising
    # when a posting has no usable url. (An empty path normalizes to "/", same
    # as any real URL with no path component - not a special empty-string case.)
    assert canonical_job_identity("") == "url:/"
    assert canonical_job_identity(None) == "url:/"


def test_two_jobs_with_empty_url_share_the_same_degenerate_key():
    # Expected and safe: with no url, canonical_key alone can't distinguish
    # them - this is exactly why discover_jobs() checks (source, source_id)
    # FIRST and independently, never relying on canonical_key alone.
    assert canonical_job_identity("") == canonical_job_identity(None)
