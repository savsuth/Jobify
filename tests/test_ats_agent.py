import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.analysis.ats_agent import score_job
from src.llm_json import LLMResponseError


def _fake_response(data: dict, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(data))], stop_reason=stop_reason
    )


def _fake_raw_response(raw_text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=raw_text)], stop_reason=stop_reason)


@patch("src.analysis.ats_agent.Anthropic")
def test_score_job_parses_response(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "match_score": 72,
            "hard_requirements_met": ["3+ years Python experience"],
            "hard_requirements_missing": [],
            "matched_skills": ["Python", "PostgreSQL"],
            "missing_skills": ["Kubernetes"],
            "reasoning": "Strong backend overlap, no k8s experience listed.",
        }
    )

    result = score_job(
        job_description="Backend role needing Python, PostgreSQL, Kubernetes.",
        candidate_profile={"skills": {"languages": ["Python"], "databases": ["PostgreSQL"]}},
    )

    assert result.match_score == 72
    assert result.hard_requirements_met == ["3+ years Python experience"]
    assert result.hard_requirements_missing == []
    assert result.matched_skills == ["Python", "PostgreSQL"]
    assert result.missing_skills == ["Kubernetes"]
    assert "backend overlap" in result.reasoning

    mock_client.messages.create.assert_called_once()
    _, kwargs = mock_client.messages.create.call_args
    assert kwargs["model"] == "claude-sonnet-5"


@patch("src.analysis.ats_agent.Anthropic")
def test_score_job_missing_new_fields_falls_back_to_empty_list(mock_anthropic_cls):
    # If the model omits the new fields, don't fail the whole job over it -
    # only match_score/matched_skills/missing_skills/reasoning are still required.
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "match_score": 50,
            "matched_skills": ["Python"],
            "missing_skills": [],
            "reasoning": "Partial fit.",
        }
    )

    result = score_job(job_description="...", candidate_profile={})

    assert result.hard_requirements_met == []
    assert result.hard_requirements_missing == []


# --- Regression tests: hard vs. preferred requirement scenarios ----------------
#
# These are plumbing/parsing tests (the Anthropic call is mocked) - they confirm
# score_job correctly threads hard-requirement data through to ATSResult for each
# shape of response the real model is expected to produce, not that the live
# model reasons correctly (that can't be unit tested).

@patch("src.analysis.ats_agent.Anthropic")
def test_missing_hard_requirement_reflected_in_result(mock_anthropic_cls):
    """A job with a clear, unmet hard requirement (5+ years experience) should
    surface that gap in hard_requirements_missing and a low score, with the
    reasoning explicitly naming it as consequential."""
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "match_score": 18,
            "hard_requirements_met": ["Python proficiency"],
            "hard_requirements_missing": ["5+ years professional software engineering experience"],
            "matched_skills": ["SQL", "Git"],
            "missing_skills": ["Distributed systems experience"],
            "reasoning": (
                "The candidate has Python and SQL fundamentals, but the role explicitly "
                "requires 5+ years of professional software engineering experience and the "
                "candidate has only ~10 months. This hard requirement gap is disqualifying "
                "for a senior-level position, far outweighing the minor missing distributed "
                "systems exposure."
            ),
        }
    )

    result = score_job(job_description="...", candidate_profile={})

    assert result.match_score == 18
    assert "5+ years professional software engineering experience" in result.hard_requirements_missing
    assert "Python proficiency" in result.hard_requirements_met
    assert "hard requirement" in result.reasoning.lower()


@patch("src.analysis.ats_agent.Anthropic")
def test_hard_requirements_met_but_preferred_quals_missing(mock_anthropic_cls):
    """All hard requirements satisfied, several preferred/nice-to-have quals
    missing - score should stay reasonably high since only non-mandatory gaps
    exist, and hard_requirements_missing should be empty."""
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "match_score": 78,
            "hard_requirements_met": ["Bachelor's in CS", "2+ years Python"],
            "hard_requirements_missing": [],
            "matched_skills": ["Python", "PostgreSQL", "Docker"],
            "missing_skills": ["Experience with Snowflake", "dbt", "Kafka"],
            "reasoning": (
                "The candidate meets both hard requirements (CS degree, 2+ years Python) "
                "and has strong overlap on core tools (Python, PostgreSQL, Docker). Several "
                "preferred qualifications (Snowflake, dbt, Kafka) are missing, but these are "
                "explicitly listed as nice-to-haves rather than mandatory, so the overall fit "
                "remains strong."
            ),
        }
    )

    result = score_job(job_description="...", candidate_profile={})

    assert result.hard_requirements_missing == []
    assert len(result.hard_requirements_met) == 2
    assert result.match_score >= 70
    assert "Snowflake" in " ".join(result.missing_skills)


@patch("src.analysis.ats_agent.Anthropic")
def test_strong_technical_overlap_but_missing_role_specific_requirement(mock_anthropic_cls):
    """Candidate has broad, strong technical skills but lacks one hard,
    role-specific requirement (e.g. a specific domain/platform experience) -
    should be reflected as a hard-requirement gap, not buried among generic
    missing_skills, and should materially depress the score despite the
    otherwise-strong technical overlap."""
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "match_score": 35,
            "hard_requirements_met": ["Strong systems programming background"],
            "hard_requirements_missing": ["Direct experience with Palantir Foundry/Gotham platform"],
            "matched_skills": ["Rust", "Python", "Performance optimization", "Distributed systems concepts"],
            "missing_skills": [],
            "reasoning": (
                "The candidate has strong, broad systems-programming experience (Rust, "
                "performance-critical code, distributed systems concepts) that aligns well "
                "with the role's general technical bar. However, the posting requires direct "
                "experience with Palantir's Foundry/Gotham platform as a hard requirement, "
                "which the candidate's profile does not show, and this role-specific gap is "
                "highly consequential despite the otherwise strong technical overlap."
            ),
        }
    )

    result = score_job(job_description="...", candidate_profile={})

    assert "Palantir Foundry/Gotham platform" in " ".join(result.hard_requirements_missing)
    assert len(result.matched_skills) >= 3  # strong general technical overlap still captured
    assert result.match_score < 50  # hard requirement gap materially depresses the score


# --- JSON reliability regression tests -----------------------------------------
#
# A live production run (2026-08-16) hit 4 real ATS failures: 3 trailing-comma
# JSON syntax errors and 1 bare KeyError on a missing "reasoning" field. These
# tests pin down the exact observed failure shapes so a regression is caught
# deterministically, without weakening what's required or guessing at repairs
# for genuinely broken responses. See src/llm_json.py's docstring.

@patch("src.analysis.ats_agent.Anthropic")
def test_score_job_trailing_comma_before_closing_brace_is_safely_repaired(mock_anthropic_cls):
    # Exact observed live shape: a valid response with one stray trailing comma
    # right before the final '}' - Python's json module reports this as "Illegal
    # trailing comma before end of object". Must parse successfully, not fail.
    raw = (
        '{"match_score": 45, "hard_requirements_met": [], "hard_requirements_missing": [], '
        '"matched_skills": ["Python"], "missing_skills": ["Go"], "reasoning": "Partial fit.",}'
    )
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_raw_response(raw)

    result = score_job(job_description="...", candidate_profile={})

    assert result.match_score == 45
    assert result.reasoning == "Partial fit."


@patch("src.analysis.ats_agent.Anthropic")
def test_score_job_missing_reasoning_field_raises_llm_response_error_not_keyerror(mock_anthropic_cls):
    # Exact observed live shape (job_id=287): valid JSON, but "reasoning" absent.
    # Must be a clear, diagnosable LLMResponseError - never a bare KeyError.
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {"match_score": 40, "matched_skills": ["Python"], "missing_skills": ["Go"]}
    )

    with pytest.raises(LLMResponseError, match="reasoning"):
        score_job(job_description="...", candidate_profile={})


@patch("src.analysis.ats_agent.Anthropic")
def test_score_job_unterminated_string_raises_llm_response_error(mock_anthropic_cls):
    # Exact observed live shape: JSON cut off mid-string. Must NOT be repaired
    # or guessed at - must fail loudly and cleanly.
    # Includes a trailing '}' (mirroring the live shape, where a '}' from later,
    # unrelated content still lets the extraction regex find a candidate match)
    # so this exercises the "matched but json.loads() fails" path specifically,
    # not the separate "no '{...}' span found at all" path.
    raw = '{"match_score": 40, "matched_skills": ["Python"], "missing_skills": [], "reasoning": "Partial fit}'
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_raw_response(raw, stop_reason="max_tokens")

    with pytest.raises(LLMResponseError, match="malformed/incomplete JSON"):
        score_job(job_description="...", candidate_profile={})


@patch("src.analysis.ats_agent.Anthropic")
def test_score_job_missing_delimiter_raises_llm_response_error(mock_anthropic_cls):
    # Exact observed live shape: "Expecting ',' delimiter" - a genuinely broken
    # object structure, distinct from the safe trailing-comma case.
    raw = '{"match_score": 40 "matched_skills": ["Python"], "missing_skills": [], "reasoning": "x"}'
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_raw_response(raw)

    with pytest.raises(LLMResponseError, match="malformed/incomplete JSON"):
        score_job(job_description="...", candidate_profile={})
