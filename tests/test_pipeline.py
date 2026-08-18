from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from langgraph.graph import END
from langgraph.types import Command, Send

from src.analysis.ats_agent import ATSResult
from src.db.models import Job, JobAnalysis, ResumeDraft, SearchConfig
from src.discovery.normalize import NormalizedJob
from src.graph.pipeline import (
    _pending_jobs,
    _round_robin_select,
    _split_ineligible,
    analyze_job,
    build_pipeline,
    discover_jobs,
    fan_out_after_discovery,
    resolve_resume_pdf_filename,
    resume_agent_node,
    resume_artifact_path,
    resume_pdf_path,
    select_master_resume,
)
from src.llm_json import LLMResponseError
from src.resume.pdf_render import PdfRenderError
from src.resume.resume_agent import ResumeDecision


# --- resume_artifact_path: deterministic, no-DB-column artifact location -------

def test_resume_artifact_path_is_deterministic():
    assert resume_artifact_path(7, 99) == resume_artifact_path(7, 99)


def test_resume_artifact_path_filename_format():
    p = resume_artifact_path(7, 99)
    assert p.name == "job_7_draft_99.tex"


def test_resume_artifact_path_distinguishes_job_and_draft():
    assert resume_artifact_path(7, 99) != resume_artifact_path(8, 99)
    assert resume_artifact_path(7, 99) != resume_artifact_path(7, 100)


# --- resume_pdf_path: user-facing filename (2026-08-17 naming task) ------------
# Internal .tex naming (resume_artifact_path) is unchanged; resume_pdf_path now
# takes the already-computed display filename (AASAV-SUTHAR-<COMPANY>[-N].pdf,
# see resolve_resume_pdf_filename / src.reporting.tracker.assign_resume_display_names)
# rather than deriving a job/draft-id-based name itself.

def test_resume_pdf_path_is_deterministic():
    assert resume_pdf_path(7, 99, "AASAV-SUTHAR-ACME.pdf") == resume_pdf_path(7, 99, "AASAV-SUTHAR-ACME.pdf")


def test_resume_pdf_path_uses_given_filename_in_pdf_subdir():
    p = resume_pdf_path(7, 99, "AASAV-SUTHAR-ACME.pdf")
    assert p.name == "AASAV-SUTHAR-ACME.pdf"
    assert p.parent.name == "pdf"


def test_resume_pdf_path_follows_artifacts_dir_when_patched(monkeypatch, tmp_path):
    # resume_pdf_path() must re-read RESUME_ARTIFACTS_DIR on every call (not a
    # frozen constant computed at import time) - this is what lets tests (and
    # any future caller) redirect both .tex and .pdf output together.
    monkeypatch.setattr("src.graph.pipeline.RESUME_ARTIFACTS_DIR", tmp_path)
    assert resume_pdf_path(7, 99, "AASAV-SUTHAR-ACME.pdf") == tmp_path / "pdf" / "AASAV-SUTHAR-ACME.pdf"


# --- resolve_resume_pdf_filename: deterministic, locked, full-table-based ------

@patch("src.graph.pipeline.get_session_factory")
def test_resolve_resume_pdf_filename_computes_from_full_table(mock_session_factory):
    mock_session = MagicMock()
    mock_session.query.return_value.join.return_value.with_for_update.return_value.all.return_value = [
        (203, 12, "Acme"), (263, 31, "Acme"),
    ]
    filename = resolve_resume_pdf_filename(mock_session, 263, 31)
    assert filename == "AASAV-SUTHAR-ACME-2.pdf"
    mock_session.query.return_value.join.return_value.with_for_update.assert_called_once()


def test_round_robin_distributes_fairly_across_companies():
    by_company = {
        "greenhouse:stripe": list(range(100)),  # one huge board
        "greenhouse:coinbase": list(range(10)),
        "lever:palantir": list(range(5)),
    }
    selected = _round_robin_select(by_company, cap=9)

    assert len(selected) == 9
    # Stripe's huge board must not consume the entire cap - the other two
    # companies (5+10=15 candidates) should get a fair share too.
    assert selected != list(range(9))


def test_round_robin_respects_cap():
    by_company = {"a": list(range(50)), "b": list(range(50))}
    assert len(_round_robin_select(by_company, cap=7)) == 7


def test_round_robin_handles_fewer_total_candidates_than_cap():
    by_company = {"a": [1, 2], "b": [3]}
    selected = _round_robin_select(by_company, cap=10)
    assert sorted(selected) == [1, 2, 3]


def test_round_robin_single_large_company_still_capped():
    by_company = {"a": list(range(1000))}
    assert len(_round_robin_select(by_company, cap=40)) == 40


def test_round_robin_interleaves_not_drains_first_company():
    # With equal-sized queues, round-robin should alternate, not exhaust one first.
    by_company = {"a": ["a1", "a2", "a3"], "b": ["b1", "b2", "b3"]}
    selected = _round_robin_select(by_company, cap=4)
    # First two picks should be one from each company, not both from "a".
    assert selected[0] != selected[1]
    assert {selected[0], selected[1]} == {"a1", "b1"}


# --- discover_jobs: cross-source canonical-key dedup (2026-08-17 investigation) -
#
# discover_jobs() had ZERO test coverage before this - these tests were added
# alongside the fix for the confirmed cross-source duplication bug (the same
# real posting, found once via web search and later via the native Greenhouse/
# Lever connector, produced two DB rows because (source, source_id) is scoped
# to one source). Discovery/ATS/Resume Agent/round-robin/filtering are all
# UNCHANGED by this fix - only discover_jobs()'s dedup step gained a second,
# additional check.

_CONFIG = SearchConfig(
    id=1, title_keywords=[], locations=[], remote_pref="any", companies={},
    experience_level="entry_level", technologies=[], ats_threshold=30,
    resume_no_tailor_threshold=65, max_jobs_per_run=40,
    # Freshness gate disabled (None) for this shared fixture - preserves every
    # pre-existing test in this section exactly as before the 2026-08-17
    # freshness-filter addition. Tests that specifically exercise the gate
    # build their own SearchConfig with posted_within_days set - see below.
    posted_within_days=None,
)


def _nj(source, source_id, url, company="palantir", title="Software Engineer", posted_at=None):
    # location="New York, NY" (not bare "NY") - classify_location()'s text-fallback
    # city/state patterns are matched case-sensitively against lowercased text, so
    # a bare two-letter state code like "NY" never actually matches (pre-existing,
    # out-of-scope quirk); the full city name reliably resolves to "us".
    return NormalizedJob(
        source=source, source_id=source_id, company=company, title=title,
        location="New York, NY", remote_type=None, url=url, description_raw="...",
        employment_type="full_time", location_category="us", seniority="entry_level",
        posted_at=posted_at, work_auth_status="eligible", work_auth_reason=None,
    )


def _mock_discover_jobs_session(existing_source_pairs=(), existing_canonical_keys=(), config=_CONFIG, pending_jobs=()):
    """Builds a MagicMock session whose .query() dispatches by the exact
    columns/model discover_jobs() (and the pending-job/pending-resume-job
    helpers it always calls) queries for - everything discover_jobs touches
    end-to-end, so the real function can run against it unmodified."""
    session = MagicMock()

    def query_side_effect(*args):
        # Deliberately uses `is`/len() only, never `==`, on args that may contain
        # SQLAlchemy declarative classes/instrumented attributes - comparing those
        # with `==` builds a real SQL BinaryExpression instead of a bool and raises.
        m = MagicMock()
        if len(args) == 1 and args[0] is SearchConfig:
            m.first.return_value = config
        elif len(args) == 2 and args[0] is Job.source and args[1] is Job.source_id:
            m.all.return_value = list(existing_source_pairs)
        elif len(args) == 1 and args[0] is Job.canonical_key:
            m.all.return_value = [(ck,) for ck in existing_canonical_keys]
        elif len(args) == 1 and args[0] is Job:
            # _pending_jobs(): session.query(Job).outerjoin(...).filter(...)[.filter(...)].limit(...).all()
            # A second .filter() call (the freshness gate, when posted_within_days
            # is not None) resolves to the SAME MagicMock .return_value as the
            # first - configuring it once here covers both the gate-enabled and
            # gate-disabled call shapes.
            m.outerjoin.return_value.filter.return_value.limit.return_value.all.return_value = list(pending_jobs)
        elif len(args) == 2 and args[0] is Job and args[1] is JobAnalysis:
            # _pending_resume_jobs(): .join(...).outerjoin(...).filter(...).all()
            m.join.return_value.outerjoin.return_value.filter.return_value.all.return_value = []
        else:
            m.all.return_value = []
            m.first.return_value = None
        return m

    session.query.side_effect = query_side_effect

    # _persist(): session.flush() must populate job.id, same pattern already
    # used for resume_drafts elsewhere in this file.
    _next_id = iter(range(1000, 2000))

    def _fake_flush():
        for call in session.add.call_args_list:
            obj = call.args[0]
            if isinstance(obj, Job) and obj.id is None:
                obj.id = next(_next_id)

    session.flush.side_effect = _fake_flush
    return session


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.discover_candidates")
def test_discover_jobs_same_source_dedup_still_works(mock_discover, mock_session_factory):
    # G: the ORIGINAL (source, source_id) check must remain unweakened.
    candidate = _nj("lever", "cbe90327-...", "https://jobs.lever.co/palantir/cbe90327-...")
    mock_discover.return_value = ({"lever:palantir": [candidate]}, {})
    session = _mock_discover_jobs_session(existing_source_pairs=[("lever", "cbe90327-...")])
    mock_session_factory.return_value.return_value = session

    result = discover_jobs({})

    assert result["new_jobs"] == []
    assert result["discovery_stats"]["duplicates_removed"] == 1
    assert result["discovery_stats"]["cross_source_duplicates_removed"] == 0


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.discover_candidates")
def test_discover_jobs_cross_source_duplicate_is_not_repersisted(mock_discover, mock_session_factory):
    # A/B: the exact confirmed live bug - a candidate with a NEW (source,
    # source_id) but whose canonical_key already exists (persisted under a
    # DIFFERENT source) must be recognized as already-known, not persisted again.
    url = "https://jobs.lever.co/palantir/cbe90327-3e6e-451c-a54c-1d3cbcef5aeb"
    candidate = _nj("lever", "cbe90327-3e6e-451c-a54c-1d3cbcef5aeb", url)  # native Lever, never seen by (source, source_id)
    mock_discover.return_value = ({"lever:palantir": [candidate]}, {})
    session = _mock_discover_jobs_session(
        existing_source_pairs=[("web", url)],  # originally discovered via web search
        existing_canonical_keys=["lever:palantir:cbe90327-3e6e-451c-a54c-1d3cbcef5aeb"],
    )
    mock_session_factory.return_value.return_value = session

    result = discover_jobs({})

    assert result["new_jobs"] == []
    assert result["discovery_stats"]["duplicates_removed"] == 1
    assert result["discovery_stats"]["cross_source_duplicates_removed"] == 1
    session.add.assert_not_called()


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.discover_candidates")
def test_discover_jobs_same_run_cross_source_duplicate_persisted_only_once(mock_discover, mock_session_factory):
    # H: two DIFFERENT sources discover the SAME posting in the SAME run,
    # before either has ever been persisted - must not create two rows.
    url = "https://jobs.lever.co/palantir/94984771-0704-446c-88c6-91ce748f6d92"
    web_candidate = _nj("web", url, url, company="Palantir Technologies")
    lever_candidate = _nj("lever", "94984771-0704-446c-88c6-91ce748f6d92", url, company="palantir")
    mock_discover.return_value = (
        {"web:search": [web_candidate], "lever:palantir": [lever_candidate]}, {}
    )
    session = _mock_discover_jobs_session()  # nothing pre-existing - both look "new" individually
    mock_session_factory.return_value.return_value = session

    result = discover_jobs({})

    assert len(result["new_jobs"]) == 1
    assert result["discovery_stats"]["cross_source_duplicates_removed"] == 1


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.discover_candidates")
def test_discover_jobs_genuinely_new_job_is_still_persisted(mock_discover, mock_session_factory):
    candidate = _nj("greenhouse", "555", "https://job-boards.greenhouse.io/stripe/jobs/555", company="stripe")
    mock_discover.return_value = ({"greenhouse:stripe": [candidate]}, {})
    session = _mock_discover_jobs_session()
    mock_session_factory.return_value.return_value = session

    result = discover_jobs({})

    assert len(result["new_jobs"]) == 1
    assert result["new_jobs"][0]["company"] == "stripe"
    assert result["discovery_stats"]["duplicates_removed"] == 0
    assert result["discovery_stats"]["cross_source_duplicates_removed"] == 0
    added_job = session.add.call_args_list[0].args[0]
    assert isinstance(added_job, Job)
    assert added_job.canonical_key == "greenhouse:stripe:555"


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.discover_candidates")
def test_discover_jobs_empty_url_falls_back_to_source_id_identity(mock_discover, mock_session_factory):
    # J: a posting with no usable url must not crash, and (source, source_id)
    # remains the working identity check regardless of the degenerate canonical key.
    candidate = _nj("web", "some-unique-id-with-no-url", url="", company="Mystery Co")
    mock_discover.return_value = ({"web:search": [candidate]}, {})
    session = _mock_discover_jobs_session()
    mock_session_factory.return_value.return_value = session

    result = discover_jobs({})

    assert len(result["new_jobs"]) == 1


# --- discover_jobs: posting-date freshness gate (2026-08-17 Discovery enhancement)
#
# Freshness is a SEPARATE, additional condition alongside current_eligibility
# (never a replacement for US/full-time/title/seniority/canonical-dedup, all
# of which are exercised unchanged by the tests above) - see
# src.discovery.normalize.is_fresh_enough/classify_freshness and the module
# docstring in src/graph/pipeline.py for why this lives here (persist-time
# gate) rather than inside aggregate.py's _filter().

_NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)


def _config(posted_within_days):
    return SearchConfig(
        id=1, title_keywords=[], locations=[], remote_pref="any", companies={},
        experience_level="entry_level", technologies=[], ats_threshold=30,
        resume_no_tailor_threshold=65, max_jobs_per_run=40,
        posted_within_days=posted_within_days,
    )


@patch("src.graph.pipeline.datetime")
@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.discover_candidates")
def test_discover_jobs_old_job_excluded_from_new_jobs_when_gate_enabled(mock_discover, mock_session_factory, mock_dt):
    # L: an old job must not reach ATS (new_jobs) when the freshness gate is
    # enabled, even though it passes every other filter (US/full-time/role/
    # non-senior/eligible/not-a-duplicate).
    mock_dt.now.return_value = _NOW
    old_candidate = _nj(
        "greenhouse", "999", "https://job-boards.greenhouse.io/stripe/jobs/999",
        company="stripe", posted_at=_NOW - timedelta(days=40),
    )
    mock_discover.return_value = ({"greenhouse:stripe": [old_candidate]}, {})
    session = _mock_discover_jobs_session(config=_config(posted_within_days=7))
    mock_session_factory.return_value.return_value = session

    result = discover_jobs({})

    assert result["new_jobs"] == []
    assert result["discovery_stats"]["stale_or_unknown_date_excluded"] == 1
    # The job IS still persisted (historical/audit visibility preserved) -
    # only ATS routing is blocked, mirroring current_eligibility's pattern.
    session.add.assert_called_once()
    added_job = session.add.call_args_list[0].args[0]
    assert isinstance(added_job, Job)


@patch("src.graph.pipeline.datetime")
@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.discover_candidates")
def test_discover_jobs_unknown_date_job_excluded_when_gate_enabled(mock_discover, mock_session_factory, mock_dt):
    # M: a job with NO posting date must not be silently treated as recent -
    # excluded from new_jobs while the gate is enabled, same as an old one.
    mock_dt.now.return_value = _NOW
    unknown_date_candidate = _nj(
        "greenhouse", "998", "https://job-boards.greenhouse.io/stripe/jobs/998",
        company="stripe", posted_at=None,
    )
    mock_discover.return_value = ({"greenhouse:stripe": [unknown_date_candidate]}, {})
    session = _mock_discover_jobs_session(config=_config(posted_within_days=7))
    mock_session_factory.return_value.return_value = session

    result = discover_jobs({})

    assert result["new_jobs"] == []
    assert result["discovery_stats"]["stale_or_unknown_date_excluded"] == 1


@patch("src.graph.pipeline.datetime")
@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.discover_candidates")
def test_discover_jobs_recent_job_still_reaches_new_jobs_when_gate_enabled(mock_discover, mock_session_factory, mock_dt):
    mock_dt.now.return_value = _NOW
    recent_candidate = _nj(
        "greenhouse", "997", "https://job-boards.greenhouse.io/stripe/jobs/997",
        company="stripe", posted_at=_NOW - timedelta(days=1),
    )
    mock_discover.return_value = ({"greenhouse:stripe": [recent_candidate]}, {})
    session = _mock_discover_jobs_session(config=_config(posted_within_days=7))
    mock_session_factory.return_value.return_value = session

    result = discover_jobs({})

    assert len(result["new_jobs"]) == 1
    assert result["discovery_stats"]["stale_or_unknown_date_excluded"] == 0


@patch("src.graph.pipeline.datetime")
@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.discover_candidates")
def test_discover_jobs_configurable_window_7_vs_14_days(mock_discover, mock_session_factory, mock_dt):
    # N: the exact same 10-day-old candidate is excluded under a 7-day window
    # and included under a 14-day window - proves the window is genuinely
    # read from config, not hardcoded.
    mock_dt.now.return_value = _NOW
    ten_days_old = _nj(
        "greenhouse", "996", "https://job-boards.greenhouse.io/stripe/jobs/996",
        company="stripe", posted_at=_NOW - timedelta(days=10),
    )

    mock_discover.return_value = ({"greenhouse:stripe": [ten_days_old]}, {})
    session7 = _mock_discover_jobs_session(config=_config(posted_within_days=7))
    mock_session_factory.return_value.return_value = session7
    assert discover_jobs({})["new_jobs"] == []

    mock_discover.return_value = ({"greenhouse:stripe": [ten_days_old]}, {})
    session14 = _mock_discover_jobs_session(config=_config(posted_within_days=14))
    mock_session_factory.return_value.return_value = session14
    assert len(discover_jobs({})["new_jobs"]) == 1


@patch("src.graph.pipeline.datetime")
@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.discover_candidates")
def test_discover_jobs_disabled_gate_restores_prior_behavior(mock_discover, mock_session_factory, mock_dt):
    # O: posted_within_days=None -> old AND unknown-date candidates both still
    # reach new_jobs, exactly matching pre-freshness-filter behavior.
    mock_dt.now.return_value = _NOW
    old_candidate = _nj(
        "greenhouse", "995", "https://job-boards.greenhouse.io/stripe/jobs/995",
        company="stripe", posted_at=_NOW - timedelta(days=400),
    )
    unknown_candidate = _nj(
        "greenhouse", "994", "https://job-boards.greenhouse.io/coinbase/jobs/994",
        company="coinbase", posted_at=None,
    )
    mock_discover.return_value = (
        {"greenhouse:stripe": [old_candidate], "greenhouse:coinbase": [unknown_candidate]}, {}
    )
    session = _mock_discover_jobs_session(config=_config(posted_within_days=None))
    mock_session_factory.return_value.return_value = session

    result = discover_jobs({})

    assert len(result["new_jobs"]) == 2
    assert result["discovery_stats"]["stale_or_unknown_date_excluded"] == 0


@patch("src.graph.pipeline.datetime")
@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.discover_candidates")
def test_discover_jobs_freshness_combines_with_existing_eligibility_filters(mock_discover, mock_session_factory, mock_dt):
    # K: a candidate that fails an EXISTING rule (senior title) AND freshness
    # must be excluded (for the pre-existing reason, current_eligibility);
    # a candidate that passes existing rules but fails only freshness must
    # still be excluded (proves the two gates combine with AND, neither one
    # compensating for the other).
    mock_dt.now.return_value = _NOW
    senior_and_old = _nj(
        "greenhouse", "993", "https://job-boards.greenhouse.io/stripe/jobs/993",
        company="stripe", title="Senior Software Engineer", posted_at=_NOW - timedelta(days=40),
    )
    eligible_but_old = _nj(
        "greenhouse", "992", "https://job-boards.greenhouse.io/stripe/jobs/992",
        company="stripe", title="Software Engineer", posted_at=_NOW - timedelta(days=40),
    )
    mock_discover.return_value = (
        {"greenhouse:stripe": [senior_and_old, eligible_but_old]}, {}
    )
    session = _mock_discover_jobs_session(config=_config(posted_within_days=7))
    mock_session_factory.return_value.return_value = session

    result = discover_jobs({})

    assert result["new_jobs"] == []
    # Both persisted (historical visibility) but neither reaches ATS.
    assert session.add.call_count == 2


# --- _pending_jobs: freshness re-checked on every retry, not just at persist ----

def test_pending_jobs_excludes_stale_posting_when_gate_enabled():
    session = MagicMock()
    session.query.return_value.outerjoin.return_value.filter.return_value.filter.return_value.limit.return_value.all.return_value = []
    _pending_jobs(session, limit=40, posted_within_days=7)
    # The freshness gate must add a SECOND .filter() call (Job.posted_at
    # cutoff) chained after the existing eligibility .filter() - not replace
    # it, not skip it.
    assert session.query.return_value.outerjoin.return_value.filter.return_value.filter.called


def test_pending_jobs_skips_freshness_filter_when_gate_disabled():
    session = MagicMock()
    session.query.return_value.outerjoin.return_value.filter.return_value.limit.return_value.all.return_value = []
    _pending_jobs(session, limit=40, posted_within_days=None)
    # Only the ONE pre-existing .filter() call - no second freshness filter
    # call when the gate is disabled.
    assert not session.query.return_value.outerjoin.return_value.filter.return_value.filter.called


# --- analyze_job routing: reject / resume_agent / select_master_resume ----------
#
# analyze_job now returns a Command that both persists the JobAnalysis row and
# dynamically routes each Send-fanned branch to its own next hop, using only its
# own local computation (never reading back shared/merged graph state) - see
# pipeline.py's module docstring for why. reject_threshold/no_tailor_threshold
# are never hardcoded here - these tests confirm the gate reads them from state.

def _job_state(reject_threshold: int, no_tailor_threshold: int = 65) -> dict:
    return {
        "job": {"id": 1, "company": "acme", "title": "Backend Engineer", "description_raw": "..."},
        "profile": {},
        "reject_threshold": reject_threshold,
        "no_tailor_threshold": no_tailor_threshold,
        "master_raw_latex": "\\documentclass{article}...",
        "github_context": [],
    }


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.score_job")
def test_analyze_job_rejects_just_below_threshold(mock_score_job, mock_session_factory):
    mock_score_job.return_value = ATSResult(
        match_score=29,
        hard_requirements_met=[],
        hard_requirements_missing=["Python"],
        matched_skills=[],
        missing_skills=["Python"],
        reasoning="not quite",
    )
    mock_session_factory.return_value.return_value = MagicMock()

    command = analyze_job(_job_state(reject_threshold=30))

    assert isinstance(command, Command)
    assert command.update["results"][0]["status"] == "rejected"
    assert command.goto == END


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.score_job")
def test_analyze_job_gate_is_not_hardcoded_to_any_fixed_value(mock_score_job, mock_session_factory):
    # Same score, different configured reject_threshold -> different outcomes.
    mock_score_job.return_value = ATSResult(
        match_score=35,
        hard_requirements_met=[],
        hard_requirements_missing=[],
        matched_skills=[],
        missing_skills=[],
        reasoning="mid",
    )
    mock_session_factory.return_value.return_value = MagicMock()

    assert analyze_job(_job_state(reject_threshold=40)).update["results"][0]["status"] == "rejected"
    assert analyze_job(_job_state(reject_threshold=30)).update["results"][0]["status"] == "passed"


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.score_job")
def test_analyze_job_at_reject_threshold_routes_to_resume_agent(mock_score_job, mock_session_factory):
    mock_score_job.return_value = ATSResult(
        match_score=30,
        hard_requirements_met=[],
        hard_requirements_missing=[],
        matched_skills=["Python"],
        missing_skills=[],
        reasoning="weak but eligible",
    )
    mock_session_factory.return_value.return_value = MagicMock()

    command = analyze_job(_job_state(reject_threshold=30, no_tailor_threshold=65))

    assert command.update["results"][0]["status"] == "passed"
    assert isinstance(command.goto, Send)
    assert command.goto.node == "resume_agent"


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.score_job")
def test_analyze_job_mid_score_routes_to_resume_agent_with_full_payload(mock_score_job, mock_session_factory):
    mock_score_job.return_value = ATSResult(
        match_score=45,
        hard_requirements_met=["x"],
        hard_requirements_missing=["y"],
        matched_skills=["Python"],
        missing_skills=["Go"],
        reasoning="mid fit",
    )
    mock_session_factory.return_value.return_value = MagicMock()

    command = analyze_job(_job_state(reject_threshold=30, no_tailor_threshold=65))

    assert command.goto.node == "resume_agent"
    payload = command.goto.arg
    assert payload["job"]["id"] == 1
    assert payload["analysis"]["match_score"] == 45
    assert payload["analysis"]["hard_requirements_missing"] == ["y"]
    assert payload["master_raw_latex"] == "\\documentclass{article}..."
    assert payload["github_context"] == []


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.score_job")
def test_analyze_job_high_score_routes_to_select_master_resume(mock_score_job, mock_session_factory):
    mock_score_job.return_value = ATSResult(
        match_score=70,
        hard_requirements_met=[],
        hard_requirements_missing=[],
        matched_skills=[],
        missing_skills=[],
        reasoning="strong fit",
    )
    mock_session_factory.return_value.return_value = MagicMock()

    command = analyze_job(_job_state(reject_threshold=30, no_tailor_threshold=65))

    assert command.goto.node == "select_master_resume"
    assert command.goto.arg["job"]["id"] == 1
    assert command.goto.arg["analysis"]["match_score"] == 70


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.score_job")
def test_analyze_job_at_no_tailor_threshold_boundary_routes_to_select_master_resume(
    mock_score_job, mock_session_factory
):
    mock_score_job.return_value = ATSResult(
        match_score=65,
        hard_requirements_met=[],
        hard_requirements_missing=[],
        matched_skills=[],
        missing_skills=[],
        reasoning="exactly at ceiling",
    )
    mock_session_factory.return_value.return_value = MagicMock()

    command = analyze_job(_job_state(reject_threshold=30, no_tailor_threshold=65))

    assert command.goto.node == "select_master_resume"


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.score_job")
def test_analyze_job_score_exception_routes_to_end_without_crashing_other_branches(
    mock_score_job, mock_session_factory
):
    mock_score_job.side_effect = ValueError("malformed model response")
    mock_session_factory.return_value.return_value = MagicMock()

    command = analyze_job(_job_state(reject_threshold=30))

    assert command.goto == END
    assert command.update["results"] == []


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.score_job")
def test_analyze_job_malformed_json_never_persists_job_analysis(mock_score_job, mock_session_factory):
    # A real production failure shape (2026-08-16 live run, jobs 267/277/285/287) -
    # score_job() raising LLMResponseError must isolate exactly like any other
    # exception: no job_analysis row, no crash, job left for next-run retry.
    mock_score_job.side_effect = LLMResponseError("Model response contains malformed/incomplete JSON")
    mock_session = MagicMock()
    mock_session_factory.return_value.return_value = mock_session

    command = analyze_job(_job_state(reject_threshold=30))

    assert command.goto == END
    assert command.update["results"] == []
    mock_session.add.assert_not_called()
    mock_session.commit.assert_not_called()


# --- fan_out_after_discovery: new jobs + crash-safe pending-resume jobs ---------

def _base_pipeline_state(**overrides) -> dict:
    state = {
        "profile": {},
        "reject_threshold": 30,
        "no_tailor_threshold": 65,
        "master_raw_latex": "\\documentclass{article}...",
        "github_context": [],
        "new_jobs": [],
        "pending_resume_jobs": [],
    }
    state.update(overrides)
    return state


def test_fan_out_after_discovery_returns_end_when_nothing_to_do():
    assert fan_out_after_discovery(_base_pipeline_state()) == END


def test_fan_out_after_discovery_sends_new_jobs_to_analyze_job():
    state = _base_pipeline_state(new_jobs=[{"id": 1, "title": "x", "company": "y", "description_raw": "z"}])
    sends = fan_out_after_discovery(state)
    assert len(sends) == 1
    assert sends[0].node == "analyze_job"
    assert sends[0].arg["job"]["id"] == 1
    assert sends[0].arg["reject_threshold"] == 30
    assert sends[0].arg["no_tailor_threshold"] == 65


def test_fan_out_after_discovery_routes_pending_resume_jobs_by_score_without_rescoring():
    # Pending-resume jobs already have a real match_score from a prior run - they
    # must be routed directly, never sent back through analyze_job/score_job.
    state = _base_pipeline_state(
        pending_resume_jobs=[
            {"job": {"id": 10, "title": "a", "description_raw": "..."}, "analysis": {"match_score": 72}},
            {"job": {"id": 11, "title": "b", "description_raw": "..."}, "analysis": {"match_score": 40}},
        ]
    )
    sends = fan_out_after_discovery(state)
    assert len(sends) == 2
    nodes_hit = {s.node for s in sends}
    assert "analyze_job" not in nodes_hit
    by_node = {s.node: s for s in sends}
    assert by_node["select_master_resume"].arg["job"]["id"] == 10
    assert by_node["resume_agent"].arg["job"]["id"] == 11
    assert by_node["resume_agent"].arg["master_raw_latex"] == "\\documentclass{article}..."


def test_fan_out_after_discovery_combines_new_and_pending_resume_jobs():
    state = _base_pipeline_state(
        new_jobs=[{"id": 1, "title": "x", "company": "y", "description_raw": "z"}],
        pending_resume_jobs=[{"job": {"id": 10, "description_raw": "..."}, "analysis": {"match_score": 72}}],
    )
    sends = fan_out_after_discovery(state)
    nodes = sorted(s.node for s in sends)
    assert nodes == ["analyze_job", "select_master_resume"]


# --- select_master_resume: deterministic, no Claude call ------------------------

@patch("src.graph.pipeline.get_session_factory")
def test_select_master_resume_persists_deterministic_selection(mock_session_factory):
    mock_session = MagicMock()
    mock_session_factory.return_value.return_value = mock_session

    result = select_master_resume({"job": {"id": 5}, "analysis": {"match_score": 80}})

    assert result == {}
    mock_session.add.assert_called_once()
    selection = mock_session.add.call_args[0][0]
    assert selection.job_id == 5
    assert selection.resume_source == "master"
    assert selection.tailoring_needed is False
    assert selection.tailoring_zone == "no_tailor_required"
    assert selection.selected_version_id is None
    assert "80%" in selection.reasoning
    # Deterministic bypass never fetches/shows GitHub context or a project pool -
    # both audit fields must default to empty, not be left unset.
    assert selection.github_context_snapshot == []
    assert selection.project_selection == {}
    mock_session.commit.assert_called_once()


# --- resume_agent_node: single Resume Agent call, version history, no fake draft

@patch("src.graph.pipeline.compile_tex_to_pdf")
@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.decide_and_tailor")
def test_resume_agent_node_persists_tailored_draft_and_selection(
    mock_decide, mock_session_factory, mock_compile_pdf, tmp_path, monkeypatch
):
    # Redirect the resume-artifact directory to a temp dir - this test must
    # never write into the real output/resumes/ directory. compile_tex_to_pdf
    # is mocked - a real pdflatex invocation has no place in a fast unit test,
    # and the placeholder tailored_latex below isn't valid LaTeX anyway.
    monkeypatch.setattr("src.graph.pipeline.RESUME_ARTIFACTS_DIR", tmp_path)
    mock_session = MagicMock()

    def _fake_flush():
        for call in mock_session.add.call_args_list:
            obj = call.args[0]
            if isinstance(obj, ResumeDraft):
                obj.id = 99

    mock_session.flush.side_effect = _fake_flush
    mock_session.query.return_value.join.return_value.with_for_update.return_value.all.return_value = [
        (7, 99, "acme"),
    ]
    mock_session_factory.return_value.return_value = mock_session

    mock_decide.return_value = ResumeDecision(
        zone="active",
        tailoring_needed=True,
        reasoning="Reordered projects to emphasize Rust work.",
        tailored_latex="\\documentclass{article}...tailored...",
        summary_of_changes="Reordered projects; led with Turbovec.",
        github_facts_used=["Turbovec repo confirms Rust/NEON usage"],
        selected_projects=[{"name": "Turbovec", "source": "master_resume", "repo": "Turbovec", "why": "Rust match"}],
        removed_or_deemphasized_projects=[{"name": "TokenPress", "why": "less relevant"}],
        reordered_skills=["Rust Ecosystem"],
    )

    fake_github_context = [
        {
            "project_name": "Turbovec",
            "owner": "savsuth",
            "repo": "Turbovec",
            "description": "Rust vector search index",
            "languages": {"Rust": 90.0, "Python": 10.0},
            "topics": [],
            "readme_excerpt": "TurboQuant-based compression...",
        }
    ]

    result = resume_agent_node(
        {
            "job": {"id": 7, "title": "Backend Engineer", "company": "acme", "description_raw": "..."},
            "analysis": {"match_score": 40},
            "profile": {},
            "master_raw_latex": "\\documentclass{article}...",
            "github_context": fake_github_context,
        }
    )

    assert result == {}
    assert mock_session.add.call_count == 2  # ResumeDraft (version history) + ResumeSelection
    draft_obj, selection_obj = (call.args[0] for call in mock_session.add.call_args_list)

    assert isinstance(draft_obj, ResumeDraft)
    assert draft_obj.job_id == 7
    assert draft_obj.version == 1
    assert draft_obj.tailored_latex == "\\documentclass{article}...tailored..."

    assert selection_obj.job_id == 7
    assert selection_obj.resume_source == "tailored"
    assert selection_obj.tailoring_needed is True
    assert selection_obj.tailoring_zone == "active"
    assert selection_obj.selected_version_id == 99  # points at the ResumeDraft just flushed
    assert selection_obj.project_selection["selected_projects"][0]["name"] == "Turbovec"
    assert selection_obj.project_selection["removed_or_deemphasized_projects"][0]["name"] == "TokenPress"
    assert selection_obj.project_selection["reordered_skills"] == ["Rust Ecosystem"]
    # The raw context actually supplied to the model must be persisted verbatim,
    # independent of the model's own self-reported github_facts_used.
    assert selection_obj.github_context_snapshot == fake_github_context
    mock_session.commit.assert_called_once()

    # The artifact file must be written automatically, with EXACT tailored_latex
    # content - never regenerated/altered.
    artifact = tmp_path / "job_7_draft_99.tex"
    assert artifact.exists()
    assert artifact.read_text() == "\\documentclass{article}...tailored..."

    # PDF compilation must be invoked on that exact .tex file, targeting the
    # user-facing AASAV-SUTHAR-<COMPANY>.pdf name (2026-08-17 naming task).
    mock_compile_pdf.assert_called_once_with(tmp_path / "job_7_draft_99.tex", tmp_path / "pdf" / "AASAV-SUTHAR-ACME.pdf")


@patch("src.graph.pipeline.compile_tex_to_pdf")
@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.decide_and_tailor")
def test_resume_agent_node_survives_pdf_compilation_failure_without_losing_draft(
    mock_decide, mock_session_factory, mock_compile_pdf, tmp_path, monkeypatch
):
    # A PDF compilation failure (missing pdflatex, bad LaTeX, >1 page, etc.)
    # must never lose the already-valid DB draft/selection or the .tex file -
    # the .tex/DB remain the source of truth; the PDF can be regenerated later.
    monkeypatch.setattr("src.graph.pipeline.RESUME_ARTIFACTS_DIR", tmp_path)
    mock_compile_pdf.side_effect = PdfRenderError("pdflatex not found")
    mock_session = MagicMock()

    def _fake_flush():
        for call in mock_session.add.call_args_list:
            obj = call.args[0]
            if isinstance(obj, ResumeDraft):
                obj.id = 55

    mock_session.flush.side_effect = _fake_flush
    mock_session.query.return_value.join.return_value.with_for_update.return_value.all.return_value = [
        (9, 55, "y"),
    ]
    mock_session_factory.return_value.return_value = mock_session
    mock_decide.return_value = ResumeDecision(
        zone="active", tailoring_needed=True, reasoning="Tailored.",
        tailored_latex="\\documentclass{article}...", summary_of_changes="Changes.",
        github_facts_used=[], selected_projects=[], removed_or_deemphasized_projects=[], reordered_skills=[],
    )

    result = resume_agent_node({
        "job": {"id": 9, "title": "x", "company": "y", "description_raw": "z"},
        "analysis": {"match_score": 40}, "profile": {},
        "master_raw_latex": "...", "github_context": [],
    })

    assert result == {}
    # The DB draft and selection must still be persisted despite the PDF failure.
    assert mock_session.add.call_count == 2
    mock_session.commit.assert_called_once()
    # The .tex file must still exist.
    assert (tmp_path / "job_9_draft_55.tex").exists()
    # No PDF must exist - never a fake/placeholder file.
    assert not (tmp_path / "pdf" / "AASAV-SUTHAR-Y.pdf").exists()


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.decide_and_tailor")
def test_resume_agent_node_no_tailoring_needed_selects_master_without_fake_draft(mock_decide, mock_session_factory):
    mock_session = MagicMock()
    mock_session_factory.return_value.return_value = mock_session

    mock_decide.return_value = ResumeDecision(
        zone="selective",
        tailoring_needed=False,
        reasoning="Master resume already representative for this job.",
        tailored_latex=None,
        summary_of_changes=None,
        github_facts_used=[],
        selected_projects=[],
        removed_or_deemphasized_projects=[],
        reordered_skills=[],
    )

    fake_github_context = [
        {
            "project_name": "TokenPress",
            "owner": "savsuth",
            "repo": "Tokenpress",
            "description": "Compression library",
            "languages": {"Python": 100.0},
            "topics": [],
            "readme_excerpt": "...",
        }
    ]

    result = resume_agent_node(
        {
            "job": {"id": 9, "title": "x", "company": "y", "description_raw": "z"},
            "analysis": {"match_score": 60},
            "profile": {},
            "master_raw_latex": "...",
            "github_context": fake_github_context,
        }
    )

    assert result == {}
    # No-tailoring must never create a ResumeDraft row - only the selection.
    mock_session.add.assert_called_once()
    selection = mock_session.add.call_args[0][0]
    assert not isinstance(selection, ResumeDraft)
    assert selection.job_id == 9
    assert selection.resume_source == "master"
    assert selection.tailoring_needed is False
    assert selection.tailoring_zone == "selective"
    assert selection.selected_version_id is None
    assert selection.project_selection == {}
    # Even when the Resume Agent declines to tailor, the GitHub context it was
    # actually shown must still be recorded for audit - this is distinct from
    # select_master_resume's deterministic bypass, which never fetches context.
    assert selection.github_context_snapshot == fake_github_context


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.decide_and_tailor")
def test_resume_agent_node_survives_decision_failure_without_persisting_or_retrying(
    mock_decide, mock_session_factory
):
    # Mirrors analyze_job's resilience pattern: one bad response must not persist
    # a partial/broken row - the job is simply left for _pending_resume_jobs() to
    # retry next run, and there is no in-node revise loop.
    mock_decide.side_effect = ValueError("malformed model response")

    result = resume_agent_node(
        {
            "job": {"id": 8, "title": "x", "company": "y", "description_raw": "z"},
            "analysis": {"match_score": 40},
            "profile": {},
            "master_raw_latex": "...",
            "github_context": [],
        }
    )

    assert result == {}
    mock_session_factory.assert_not_called()
    mock_decide.assert_called_once()  # exactly one attempt, no retry


@patch("src.graph.pipeline.get_session_factory")
@patch("src.graph.pipeline.decide_and_tailor")
def test_resume_agent_node_malformed_json_never_persists_selection_or_draft(
    mock_decide, mock_session_factory
):
    # Real production failure shape (2026-08-16 live run, jobs 289/290/294/295) -
    # decide_and_tailor() raising LLMResponseError must isolate exactly like any
    # other exception: no resume_selections row, no resume_drafts row, no crash.
    mock_decide.side_effect = LLMResponseError("Model response contains malformed/incomplete JSON")

    result = resume_agent_node(
        {
            "job": {"id": 8, "title": "x", "company": "y", "description_raw": "z"},
            "analysis": {"match_score": 40},
            "profile": {},
            "master_raw_latex": "...",
            "github_context": [],
        }
    )

    assert result == {}
    mock_session_factory.assert_not_called()  # no DB session touched at all - no ResumeSelection, no ResumeDraft
    mock_decide.assert_called_once()


# --- graph wiring -----------------------------------------------------------------

def test_build_pipeline_compiles_with_all_expected_nodes():
    app = build_pipeline()
    node_names = set(app.get_graph().nodes.keys())
    assert {"discover_jobs", "analyze_job", "resume_agent", "select_master_resume"} <= node_names


# --- F-1/OPT eligibility gate -----------------------------------------------
#
# Immigration-ineligible jobs must never reach Claude - _split_ineligible is
# what enforces that split before the fan-out to analyze_job.

def test_split_ineligible_records_directly_without_claude():
    session = MagicMock()
    jobs = [
        {"id": 1, "title": "Backend Engineer", "company": "acme", "description_raw": "...",
         "work_auth_status": "ineligible", "work_auth_reason": "Posting explicitly requires US citizenship"},
        {"id": 2, "title": "Data Engineer", "company": "acme", "description_raw": "...",
         "work_auth_status": "eligible", "work_auth_reason": None},
        {"id": 3, "title": "ML Engineer", "company": "acme", "description_raw": "...",
         "work_auth_status": "unknown", "work_auth_reason": "Mentions visa sponsorship limitations"},
    ]

    needs_analysis, ineligible_count = _split_ineligible(session, jobs)

    assert ineligible_count == 1
    # Eligible AND unknown both still need real ATS analysis - only "ineligible" is skipped.
    assert [j["id"] for j in needs_analysis] == [2, 3]

    session.add.assert_called_once()
    added_analysis = session.add.call_args[0][0]
    assert added_analysis.job_id == 1
    assert added_analysis.status == "ineligible"
    assert added_analysis.match_score is None
    assert "citizenship" in added_analysis.reasoning.lower()
    session.commit.assert_called_once()


def test_split_ineligible_no_commit_when_nothing_ineligible():
    session = MagicMock()
    jobs = [
        {"id": 1, "title": "Data Engineer", "company": "acme", "description_raw": "...",
         "work_auth_status": "eligible", "work_auth_reason": None},
    ]

    needs_analysis, ineligible_count = _split_ineligible(session, jobs)

    assert ineligible_count == 0
    assert len(needs_analysis) == 1
    session.add.assert_not_called()
    session.commit.assert_not_called()
