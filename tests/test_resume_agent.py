import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.llm_json import LLMResponseError
from src.resume.resume_agent import (
    _ACTIVE_ZONE_INSTRUCTIONS,
    _SYSTEM_PROMPT_TEMPLATE,
    _brace_balance,
    _build_system_prompt,
    _extract_master_projects,
    _has_sufficient_github_evidence,
    _latex_structurally_valid,
    _verify_github_facts_used,
    build_project_pool,
    decide_and_tailor,
    tailoring_zone,
)

# A two-project master fixture used by the project-pool / scenario tests below -
# mirrors the real resume.tex's macro structure (resumeProjectHeading /
# resumeItemListStart/End / resumeSubHeadingListStart/End) closely enough for
# the deterministic parser to work on it exactly as it would on the real file.
_MULTI_PROJECT_LATEX = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "\\section{Projects}\n"
    "\\resumeSubHeadingListStart\n"
    "\\resumeProjectHeading\n"
    "    {\\textbf{Legacy Dashboard} $|$ \\href{https://github.com/cand/legacy-dashboard}{GitHub}}{}\n"
    "\\resumeItemListStart\n"
    "\\resumeItem{Built a dashboard to visualize legacy metrics using Streamlit and pandas}\n"
    "\\resumeItemListEnd\n"
    "\\resumeProjectHeading\n"
    "    {\\textbf{Java MVC Project} $|$ \\href{https://github.com/cand/java-mvc}{GitHub}}{}\n"
    "\\resumeItemListStart\n"
    "\\resumeItem{Built a Java Spring MVC backend service with REST APIs and a PostgreSQL database}\n"
    "\\resumeItemListEnd\n"
    "\\resumeSubHeadingListEnd\n"
    "\\section{Technical Skills}\n"
    "\\resumeItemListStart\n"
    "\\resumeItem{Languages: Python, Java, PostgreSQL}\n"
    "\\resumeItemListEnd\n"
    "\\end{document}\n"
)

# Points nowhere - load_approved_library_projects() returns [] when index.json
# doesn't exist, so this cleanly isolates build_project_pool() tests below from
# the real data/candidate_projects/ library (which has real approved projects
# in it - see the dedicated project-library integration tests further down for
# those, which point library_dir at controlled fixtures instead).
_EMPTY_LIBRARY_DIR = Path("/nonexistent-test-library-dir")

_VALID_LATEX = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "\\resumeSubHeadingListStart\n"
    "\\resumeItemListStart\n"
    "\\resumeItem{Did a thing}\n"
    "\\resumeItemListEnd\n"
    "\\resumeSubHeadingListEnd\n"
    "\\end{document}\n"
)


def _fake_response(data: dict, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(data))], stop_reason=stop_reason
    )


def _fake_raw_response(raw_text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=raw_text)], stop_reason=stop_reason)


# --- tailoring_zone (deterministic banding) --------------------------------------

def test_tailoring_zone_boundaries():
    assert tailoring_zone(30) == "active"
    assert tailoring_zone(49) == "active"
    assert tailoring_zone(50) == "selective"
    assert tailoring_zone(64) == "selective"


# --- _latex_structurally_valid (cheap deterministic integrity check) -------------

def test_latex_structurally_valid_accepts_well_formed_document():
    assert _latex_structurally_valid(_VALID_LATEX)


def test_latex_structurally_valid_rejects_unbalanced_braces():
    broken = _VALID_LATEX.replace("\\resumeItem{Did a thing}", "\\resumeItem{Did a thing")
    assert not _latex_structurally_valid(broken)


def test_latex_structurally_valid_rejects_missing_begin_document():
    broken = _VALID_LATEX.replace("\\begin{document}\n", "")
    assert not _latex_structurally_valid(broken)


def test_latex_structurally_valid_rejects_duplicate_begin_document():
    broken = _VALID_LATEX.replace("\\begin{document}\n", "\\begin{document}\n\\begin{document}\n")
    assert not _latex_structurally_valid(broken)


def test_latex_structurally_valid_rejects_mismatched_item_list_markers():
    broken = _VALID_LATEX.replace("\\resumeItemListEnd\n", "")
    assert not _latex_structurally_valid(broken)


def test_latex_structurally_valid_rejects_non_document_fragment():
    assert not _latex_structurally_valid("Just some text, not a LaTeX document at all.")


def test_brace_balance():
    assert _brace_balance("{{}}") == 0
    assert _brace_balance("{{}") == 1
    assert _brace_balance("{}}") == -1


# --- decide_and_tailor: active zone (30-49) --------------------------------------

@patch("src.resume.resume_agent.Anthropic")
def test_active_zone_tailors_when_model_finds_improvement(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": True,
            "reasoning": "Reordered projects to lead with the Rust performance work this job emphasizes.",
            "tailored_latex": _VALID_LATEX,
            "summary_of_changes": "Moved Turbovec ahead of TokenPress; reordered skills to lead with Rust.",
            "github_facts_used": [
                {
                    "fact": "Turbovec repo confirms NEON SIMD + rayon usage",
                    "quoted_in_resume": "Did a thing",  # must literally appear in _VALID_LATEX to be kept
                }
            ],
        }
    )

    result = decide_and_tailor(
        job_description="Backend role emphasizing Rust and systems performance.",
        ats_result={"match_score": 35, "hard_requirements_missing": ["Direct Rust production experience"]},
        master_raw_latex=_VALID_LATEX,
        candidate_profile={"skills": {"languages": ["Python", "Rust"]}},
        github_context=[{"project_name": "Turbovec", "languages": {"Rust": 9000}}],
        match_score=35,
    )

    assert result.zone == "active"
    assert result.tailoring_needed is True
    assert result.tailored_latex == _VALID_LATEX
    assert "Turbovec" in result.summary_of_changes
    assert result.github_facts_used == ["Turbovec repo confirms NEON SIMD + rayon usage"]
    mock_client.messages.create.assert_called_once()


@patch("src.resume.resume_agent.Anthropic")
def test_active_zone_still_allows_no_tailoring_when_nothing_legitimate(mock_anthropic_cls):
    # Active zone biases toward searching for improvements, but must not force
    # one when the model genuinely finds nothing legitimate to change.
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": False,
            "reasoning": "Master resume already leads with the most relevant project; no legitimate "
            "improvement found using only supported evidence.",
            "tailored_latex": None,
            "summary_of_changes": None,
            "github_facts_used": [],
        }
    )

    result = decide_and_tailor(
        job_description="...",
        ats_result={"match_score": 42},
        master_raw_latex=_VALID_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=42,
    )

    assert result.zone == "active"
    assert result.tailoring_needed is False
    assert result.tailored_latex is None


# --- decide_and_tailor: selective zone (50-64) -----------------------------------

@patch("src.resume.resume_agent.Anthropic")
def test_selective_zone_keeps_master_by_default(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": False,
            "reasoning": "Master resume already represents the candidate's fit well for this job.",
            "tailored_latex": None,
            "summary_of_changes": None,
            "github_facts_used": [],
        }
    )

    result = decide_and_tailor(
        job_description="...",
        ats_result={"match_score": 58},
        master_raw_latex=_VALID_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=58,
    )

    assert result.zone == "selective"
    assert result.tailoring_needed is False
    assert result.tailored_latex is None
    assert result.summary_of_changes is None


@patch("src.resume.resume_agent.Anthropic")
def test_selective_zone_can_still_tailor_for_a_specific_improvement(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": True,
            "reasoning": "Job specifically calls out RAG pipelines; existing bullet rephrased to surface it.",
            "tailored_latex": _VALID_LATEX,
            "summary_of_changes": "Rephrased the Shaligram Infotech RAG bullet to lead with retrieval-augmented generation.",
            "github_facts_used": [],
        }
    )

    result = decide_and_tailor(
        job_description="...",
        ats_result={"match_score": 61},
        master_raw_latex=_VALID_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=61,
    )

    assert result.zone == "selective"
    assert result.tailoring_needed is True
    assert result.tailored_latex == _VALID_LATEX


# --- Guardrails: no fabricated tailoring, no broken LaTeX shipped ----------------

@patch("src.resume.resume_agent.Anthropic")
def test_falls_back_to_master_when_model_claims_tailoring_without_latex(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": True,
            "reasoning": "Would tailor.",
            "tailored_latex": None,
            "summary_of_changes": None,
            "github_facts_used": [],
        }
    )

    result = decide_and_tailor(
        job_description="...",
        ats_result={"match_score": 40},
        master_raw_latex=_VALID_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=40,
    )

    assert result.tailoring_needed is False
    assert result.tailored_latex is None
    assert "falling back to the master resume" in result.reasoning


@patch("src.resume.resume_agent.Anthropic")
def test_falls_back_to_master_when_tailored_latex_fails_structural_check(mock_anthropic_cls):
    broken_latex = _VALID_LATEX.replace("\\resumeItemListEnd\n", "")  # unbalanced list markers

    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": True,
            "reasoning": "Reordered projects.",
            "tailored_latex": broken_latex,
            "summary_of_changes": "Reordered.",
            "github_facts_used": [],
        }
    )

    result = decide_and_tailor(
        job_description="...",
        ats_result={"match_score": 45},
        master_raw_latex=_VALID_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=45,
    )

    assert result.tailoring_needed is False
    assert result.tailored_latex is None
    assert result.summary_of_changes is None
    assert "structural check" in result.reasoning


@patch("src.resume.resume_agent.Anthropic")
def test_missing_github_facts_used_falls_back_to_empty_list(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": False,
            "reasoning": "No change needed.",
            "tailored_latex": None,
            "summary_of_changes": None,
            # github_facts_used omitted entirely
        }
    )

    result = decide_and_tailor(
        job_description="...",
        ats_result={"match_score": 55},
        master_raw_latex=_VALID_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=55,
    )

    assert result.github_facts_used == []


# --- Single-call guarantee: no second ATS call, no revise loop ------------------

@patch("src.resume.resume_agent.Anthropic")
def test_exactly_one_claude_call_regardless_of_outcome(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": True,
            "reasoning": "...",
            "tailored_latex": _VALID_LATEX.replace("\\resumeItemListEnd\n", ""),  # will fail structural check
            "summary_of_changes": "...",
            "github_facts_used": [],
        }
    )

    decide_and_tailor(
        job_description="...",
        ats_result={"match_score": 33},
        master_raw_latex=_VALID_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=33,
    )

    # Even when the structural check fails and the agent falls back to the
    # master resume, it must not retry/revise with a second call.
    assert mock_client.messages.create.call_count == 1


@patch("src.resume.resume_agent.Anthropic")
def test_uses_active_zone_instructions_in_system_prompt(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {"tailoring_needed": False, "reasoning": "...", "tailored_latex": None, "summary_of_changes": None}
    )

    decide_and_tailor(
        job_description="...",
        ats_result={"match_score": 33},
        master_raw_latex=_VALID_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=33,
    )

    _, kwargs = mock_client.messages.create.call_args
    assert kwargs["model"] == "claude-sonnet-5"
    assert "ACTIVE TAILORING" in kwargs["system"]
    assert "SELECTIVE TAILORING" not in kwargs["system"]


@patch("src.resume.resume_agent.Anthropic")
def test_uses_selective_zone_instructions_in_system_prompt(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {"tailoring_needed": False, "reasoning": "...", "tailored_latex": None, "summary_of_changes": None}
    )

    decide_and_tailor(
        job_description="...",
        ats_result={"match_score": 60},
        master_raw_latex=_VALID_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=60,
    )

    _, kwargs = mock_client.messages.create.call_args
    assert "SELECTIVE TAILORING" in kwargs["system"]
    assert "ACTIVE TAILORING" not in kwargs["system"]


# --- Citation consistency (_verify_github_facts_used) ----------------------------
#
# A live audit (2026-08-14) found job 252's self-reported github_facts_used
# claimed a "40%+ more accurate during peak hours" fact was used, but that text
# never appeared anywhere in job 252's own tailored_latex - it belonged to a
# different job's output. These tests reconstruct that exact failure mode and
# confirm the fix (requiring a verbatim "quoted_in_resume" match) catches it.

def test_verify_github_facts_used_drops_fact_not_actually_quoted_in_this_output():
    # Reconstructs job 252's exact defect: a claimed fact whose "quoted_in_resume"
    # text does not appear in THIS job's tailored_latex must not be recorded.
    raw = [
        {
            "fact": "Traffic-Aware Routing System README states the system is "
            "'40%+ more accurate during peak hours'",
            "quoted_in_resume": "40%+ more accurate predictions during peak-hour traffic",
        }
    ]
    tailored_latex = _VALID_LATEX  # does not contain that phrase anywhere
    assert _verify_github_facts_used(raw, tailored_latex) == []


def test_verify_github_facts_used_keeps_fact_that_is_genuinely_quoted():
    raw = [{"fact": "Turbovec compresses to 2 bits/dim", "quoted_in_resume": "Did a thing"}]
    assert _verify_github_facts_used(raw, _VALID_LATEX) == ["Turbovec compresses to 2 bits/dim"]


def test_verify_github_facts_used_keeps_only_the_verified_subset():
    # Mixed batch: one real citation, one that doesn't match this output - only
    # the verified one survives, matching item 7's requirement exactly.
    raw = [
        {"fact": "real fact", "quoted_in_resume": "Did a thing"},
        {"fact": "unverifiable fact", "quoted_in_resume": "text that is not in the resume"},
    ]
    assert _verify_github_facts_used(raw, _VALID_LATEX) == ["real fact"]


def test_verify_github_facts_used_drops_legacy_bare_string_entries():
    # Old output shape (list[str], pre-fix) can't be verified at all - must be
    # dropped rather than trusted, not crash.
    assert _verify_github_facts_used(["a bare string fact"], _VALID_LATEX) == []


def test_verify_github_facts_used_empty_when_no_tailored_latex():
    assert _verify_github_facts_used([{"fact": "x", "quoted_in_resume": "y"}], None) == []


def test_verify_github_facts_used_handles_missing_keys_gracefully():
    assert _verify_github_facts_used([{"fact": "x"}], _VALID_LATEX) == []
    assert _verify_github_facts_used([{"quoted_in_resume": "Did a thing"}], _VALID_LATEX) == []


@patch("src.resume.resume_agent.Anthropic")
def test_decide_and_tailor_end_to_end_drops_unverified_citation_job_252_scenario(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": True,
            "reasoning": "Enriched the routing project with verified GitHub README details.",
            "tailored_latex": _VALID_LATEX,
            "summary_of_changes": "Enriched the routing bullet with a peak-hour accuracy figure.",
            "github_facts_used": [
                {
                    "fact": "README states the system is '40%+ more accurate during peak hours'",
                    "quoted_in_resume": "40%+ more accurate predictions during peak-hour traffic",
                }
            ],
        }
    )

    result = decide_and_tailor(
        job_description="...",
        ats_result={"match_score": 46},
        master_raw_latex=_VALID_LATEX,
        candidate_profile={},
        github_context=[{"project_name": "Traffic-Aware Routing System"}],
        match_score=46,
    )

    # Tailoring itself still succeeds (structurally valid draft) - only the
    # unverifiable citation is stripped from the recorded audit trail.
    assert result.tailoring_needed is True
    assert result.github_facts_used == []


# --- Regression tests: prompt content for the failure patterns found in jobs
# 256, 257, 234, 250, 251, 252 (live audit, 2026-08-14) -----------------------------
#
# These are content/regression guards, not behavioral tests - they confirm the
# specific instructions this fix added are actually present in the prompt sent
# to the model, so a future edit can't silently drop them. Whether the live
# model actually complies can only be confirmed by a future live-run audit
# (explicitly out of scope here - no live calls).

def test_prompt_states_evidence_first_rewriting_principle():
    prompt = _build_system_prompt("active")
    assert "EVIDENCE-FIRST REWRITING" in prompt
    assert "must NEVER tell you" in prompt
    assert "HOW SENIOR" in prompt


def test_prompt_forbids_prototyped_to_built_deployed_upgrade():
    # Regression for jobs 250 ("Independently prototyped...end-to-end") and 251
    # ("Built an end-to-end GPT-3.5-based FAQ assistant..." replacing "Prototyped").
    prompt = _SYSTEM_PROMPT_TEMPLATE
    assert "FORBIDDEN SEMANTIC UPGRADES" in prompt
    assert '"Prototyped"' in prompt
    assert "Built" in prompt and "Deployed" in prompt and "Owned" in prompt


def test_prompt_forbids_project_to_production_service_upgrade():
    # Regression for job 226 ("production-style integration", "backend service
    # integration") and 252 ("production-ready traffic routing system").
    prompt = _SYSTEM_PROMPT_TEMPLATE
    assert "production system" in prompt
    assert "customer-facing" in prompt and "client delivery" in prompt


def test_prompt_forbids_copying_job_postings_ownership_vocabulary():
    # Regression for jobs 256 and 257: both tailored resumes directly reused
    # the Palantir JD's own phrases ("own end-to-end execution", "full stack of
    # execution", "deploying AI agents") to describe a personal project and a
    # "Prototyped" internship bullet.
    prompt = _SYSTEM_PROMPT_TEMPLATE
    assert "DO NOT COPY THE JOB POSTING" in prompt
    assert "own end-to-end execution" in prompt
    assert "full stack of execution" in prompt
    assert "deploying AI agents" in prompt


def test_prompt_states_project_professional_boundary():
    # Regression for jobs 256/257 (project -> "owned the full stack"/"deployed")
    # and 234 (project -> "owning the project end-to-end from prototype to
    # packaged binding").
    prompt = _SYSTEM_PROMPT_TEMPLATE
    assert "PROJECT / PROFESSIONAL BOUNDARY" in prompt
    assert "personal projects" in prompt
    assert "production services" in prompt


def test_prompt_retains_scale_and_maturity_qualifier_restriction():
    # The original (pre-existing) restriction from the first live-run audit -
    # confirms it wasn't lost while rewriting the rest of the prompt.
    prompt = _SYSTEM_PROMPT_TEMPLATE
    for term in ("scalable", "at scale", "high-throughput", "large-scale", "production-style", "throughput"):
        assert term in prompt, f"expected {term!r} to still be listed in the prompt"


def test_active_zone_instructions_reinforce_evidence_first_discipline():
    # All six named failing jobs (256, 257, 234, 250, 251, 252) were active-zone
    # (30-49%) - the active-zone instructions specifically warn that a low score
    # is not license to reach for unsupported ownership/production wording.
    assert "evidence-first discipline" in _ACTIVE_ZONE_INSTRUCTIONS.lower()
    assert "forbidden semantic upgrade" in _ACTIVE_ZONE_INSTRUCTIONS.lower()


def test_prompt_output_contract_requires_verbatim_quoted_citation():
    # The new github_facts_used shape (item 6/7): each entry must include an
    # exact, verbatim "quoted_in_resume" substring, not just a free-text claim.
    prompt = _SYSTEM_PROMPT_TEMPLATE
    assert "quoted_in_resume" in prompt
    assert "EXACT, VERBATIM" in prompt


# --- Project pool: deterministic parsing (no Claude/API call) -------------------

def test_extract_master_projects_parses_names_repos_and_bullets():
    projects = _extract_master_projects(_MULTI_PROJECT_LATEX)
    assert [p["name"] for p in projects] == ["Legacy Dashboard", "Java MVC Project"]
    java_project = projects[1]
    assert java_project["owner"] == "cand"
    assert java_project["repo"] == "java-mvc"
    assert java_project["existing_bullets"] == [
        "Built a Java Spring MVC backend service with REST APIs and a PostgreSQL database"
    ]


def test_extract_master_projects_against_real_resume():
    raw_latex = open("profile/resume.tex").read()
    projects = _extract_master_projects(raw_latex)
    assert {p["name"] for p in projects} == {
        "TokenPress",
        "Turbovec",
        "Traffic-Aware Routing System",
        "Derivatives Pricing Dashboard",
    }
    assert all(len(p["existing_bullets"]) == 3 for p in projects)


def test_has_sufficient_github_evidence_requires_narrative_and_languages():
    assert _has_sufficient_github_evidence({"description": "A thing", "languages": {"Python": 100}})
    assert _has_sufficient_github_evidence({"readme_excerpt": "A thing", "languages": {"Go": 100}})
    assert not _has_sufficient_github_evidence({"description": "", "readme_excerpt": "", "languages": {"Go": 100}})
    assert not _has_sufficient_github_evidence({"description": "A thing", "languages": {}})
    assert not _has_sufficient_github_evidence({})


def test_build_project_pool_always_includes_all_master_projects():
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context=[], library_dir=_EMPTY_LIBRARY_DIR)
    assert {p["name"] for p in pool} == {"Legacy Dashboard", "Java MVC Project"}
    assert all(p["source"] == "master_resume" for p in pool)


def test_build_project_pool_merges_github_metadata_by_name():
    github_context = [
        {
            "project_name": "Java MVC Project",
            "owner": "cand",
            "repo": "java-mvc",
            "description": "A layered Spring MVC backend",
            "languages": {"Java": 5000},
            "topics": ["java", "spring", "mvc"],
            "readme_excerpt": "A Java Spring MVC backend with REST endpoints.",
        }
    ]
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context, library_dir=_EMPTY_LIBRARY_DIR)
    java_entry = next(p for p in pool if p["name"] == "Java MVC Project")
    assert java_entry["source"] == "master_resume"  # already in master - not duplicated as github_only
    assert java_entry["description"] == "A layered Spring MVC backend"
    assert java_entry["languages"] == {"Java": 5000}
    # existing_bullets must still be the real master bullets, not replaced by GitHub data.
    assert java_entry["existing_bullets"] == [
        "Built a Java Spring MVC backend service with REST APIs and a PostgreSQL database"
    ]


def test_build_project_pool_adds_github_only_project_with_sufficient_evidence():
    github_context = [
        {
            "project_name": "Kafka Streaming Service",
            "owner": "cand",
            "repo": "kafka-streaming",
            "description": "A real-time event processing service using Kafka and Go",
            "languages": {"Go": 8000},
            "topics": ["kafka", "go", "streaming"],
            "readme_excerpt": "Consumes and processes events from Kafka topics in Go.",
        }
    ]
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context, library_dir=_EMPTY_LIBRARY_DIR)
    names = {p["name"] for p in pool}
    assert names == {"Legacy Dashboard", "Java MVC Project", "Kafka Streaming Service"}
    kafka_entry = next(p for p in pool if p["name"] == "Kafka Streaming Service")
    assert kafka_entry["source"] == "github_only"
    assert kafka_entry["existing_bullets"] == []  # never fabricated - only real metadata is present


def test_build_project_pool_excludes_github_repo_with_insufficient_evidence():
    # No description, no README excerpt - nothing trustworthy to build a bullet
    # from, so this repo must never reach the model at all (scenario F).
    github_context = [
        {
            "project_name": "Abandoned Experiment",
            "owner": "cand",
            "repo": "abandoned-experiment",
            "description": None,
            "languages": {"Python": 10},
            "topics": [],
            "readme_excerpt": None,
        }
    ]
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context, library_dir=_EMPTY_LIBRARY_DIR)
    assert {p["name"] for p in pool} == {"Legacy Dashboard", "Java MVC Project"}
    assert "Abandoned Experiment" not in {p["name"] for p in pool}


def test_build_project_pool_excludes_github_repo_with_no_languages():
    github_context = [
        {
            "project_name": "Docs Only Repo",
            "owner": "cand",
            "repo": "docs-only",
            "description": "A collection of notes",
            "languages": {},
            "topics": [],
            "readme_excerpt": None,
        }
    ]
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context, library_dir=_EMPTY_LIBRARY_DIR)
    assert "Docs Only Repo" not in {p["name"] for p in pool}


def test_build_project_pool_never_contains_a_project_not_in_master_or_github():
    # Structural guarantee behind "prevent project-library hallucination": the
    # pool can only ever be built from these two sources.
    github_context = [
        {
            "project_name": "Real Repo",
            "description": "x",
            "languages": {"Python": 1},
            "readme_excerpt": None,
            "topics": [],
        }
    ]
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context, library_dir=_EMPTY_LIBRARY_DIR)
    allowed_names = {"Legacy Dashboard", "Java MVC Project", "Real Repo"}
    assert {p["name"] for p in pool} <= allowed_names


# --- Structural safety: section-preservation and duplicate-project checks -------

def test_latex_structurally_valid_backward_compatible_without_master_arg():
    # Existing single-arg call sites keep working unchanged.
    assert _latex_structurally_valid(_VALID_LATEX)


def test_latex_structurally_valid_rejects_dropped_section():
    master = _MULTI_PROJECT_LATEX
    dropped_section = master.replace("\\section{Technical Skills}\n", "")
    assert not _latex_structurally_valid(dropped_section, master)


def test_latex_structurally_valid_accepts_all_master_sections_present():
    # Reordered/edited content is fine as long as every master section header
    # still exists somewhere in the output.
    reordered = _MULTI_PROJECT_LATEX.replace("Legacy Dashboard", "Legacy Dashboard (deprioritized)")
    assert _latex_structurally_valid(reordered, _MULTI_PROJECT_LATEX)


def test_latex_structurally_valid_rejects_duplicate_project_names():
    duplicated = _MULTI_PROJECT_LATEX.replace("Java MVC Project", "Legacy Dashboard")
    assert not _latex_structurally_valid(duplicated, _MULTI_PROJECT_LATEX)


def test_latex_structurally_valid_allows_fewer_projects_than_master():
    # Legitimate project removal/replacement must not be penalized - only
    # internal consistency and section presence matter, not project count.
    one_project_only = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{Projects}\n"
        "\\resumeSubHeadingListStart\n"
        "\\resumeProjectHeading\n"
        "    {\\textbf{Java MVC Project} $|$ \\href{https://github.com/cand/java-mvc}{GitHub}}{}\n"
        "\\resumeItemListStart\n"
        "\\resumeItem{Built a Java Spring MVC backend service with REST APIs and a PostgreSQL database}\n"
        "\\resumeItemListEnd\n"
        "\\resumeSubHeadingListEnd\n"
        "\\section{Technical Skills}\n"
        "\\resumeItemListStart\n"
        "\\resumeItem{Languages: Python, Java, PostgreSQL}\n"
        "\\resumeItemListEnd\n"
        "\\end{document}\n"
    )
    assert _latex_structurally_valid(one_project_only, _MULTI_PROJECT_LATEX)


# --- Scenario tests (section 16) -------------------------------------------------

def _ats_result(**overrides) -> dict:
    base = {
        "match_score": 40,
        "hard_requirements_met": [],
        "hard_requirements_missing": [],
        "matched_skills": [],
        "missing_skills": [],
        "reasoning": "...",
    }
    base.update(overrides)
    return base


# Scenario A: obvious project replacement - Java/MVC job, weak project featured,
# a genuinely relevant existing project available in the pool.
@patch("src.resume.resume_agent.Anthropic")
def test_scenario_a_project_replacement(mock_anthropic_cls):
    tailored = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{Projects}\n"
        "\\resumeSubHeadingListStart\n"
        "\\resumeProjectHeading\n"
        "    {\\textbf{Java MVC Project} $|$ \\href{https://github.com/cand/java-mvc}{GitHub}}{}\n"
        "\\resumeItemListStart\n"
        "\\resumeItem{Built a Java Spring MVC backend service with REST APIs and a PostgreSQL database}\n"
        "\\resumeItemListEnd\n"
        "\\resumeSubHeadingListEnd\n"
        "\\section{Technical Skills}\n"
        "\\resumeItemListStart\n"
        "\\resumeItem{Languages: Java, Python, PostgreSQL}\n"
        "\\resumeItemListEnd\n"
        "\\end{document}\n"
    )
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": True,
            "reasoning": "Job requires Java/MVC/backend work; Java MVC Project is direct existing evidence.",
            "selected_projects": [
                {
                    "name": "Java MVC Project",
                    "source": "master_resume",
                    "repo": "java-mvc",
                    "why": "Replaced Legacy Dashboard because Java MVC Project provides direct existing "
                    "evidence for the job's Java/MVC requirement.",
                }
            ],
            "removed_or_deemphasized_projects": [
                {"name": "Legacy Dashboard", "why": "Not relevant to Java/MVC/backend requirements."}
            ],
            "reordered_skills": ["Languages"],
            "tailored_latex": tailored,
            "summary_of_changes": "Replaced Legacy Dashboard with Java MVC Project; reordered Languages.",
            "github_facts_used": [],
        }
    )

    result = decide_and_tailor(
        job_description="We need a backend engineer strong in Java, MVC frameworks, and REST APIs.",
        ats_result=_ats_result(match_score=40, matched_skills=["Java"]),
        master_raw_latex=_MULTI_PROJECT_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=40,
    )

    assert result.tailoring_needed is True
    assert result.selected_projects[0]["name"] == "Java MVC Project"
    assert result.removed_or_deemphasized_projects[0]["name"] == "Legacy Dashboard"
    assert "Java MVC Project" in result.tailored_latex
    assert "Legacy Dashboard" not in result.tailored_latex
    # The original bullet must be preserved verbatim, not rewritten.
    assert "Built a Java Spring MVC backend service with REST APIs and a PostgreSQL database" in result.tailored_latex


# Scenario B: no matching project (Kafka/Go) - the pool structurally cannot
# offer what doesn't exist, so nothing can be invented for it.
def test_scenario_b_no_matching_project_pool_has_no_kafka_go_entry():
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context=[], library_dir=_EMPTY_LIBRARY_DIR)
    pool_text = json.dumps(pool).lower()
    assert "kafka" not in pool_text
    assert not any(p["name"].lower() == "go" for p in pool)


@patch("src.resume.resume_agent.Anthropic")
def test_scenario_b_no_matching_project_declines_rather_than_rewriting(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": False,
            "reasoning": "No Kafka or Go evidence exists in the project pool; rewriting an unrelated "
            "project to sound like Kafka/Go would fabricate experience, so no legitimate tailoring exists.",
            "tailored_latex": None,
            "summary_of_changes": None,
            "github_facts_used": [],
        }
    )

    result = decide_and_tailor(
        job_description="Looking for a Go engineer with Kafka streaming experience.",
        ats_result=_ats_result(match_score=32, missing_skills=["Kafka", "Go"]),
        master_raw_latex=_MULTI_PROJECT_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=32,
    )

    assert result.tailoring_needed is False
    assert result.tailored_latex is None
    assert result.selected_projects == []


# Scenario C: keyword-only temptation ("production-scale distributed service")
# must not upgrade a prototype - this is prompt-level (no code can force model
# compliance), so this is a regression guard on the prompt text itself.
def test_scenario_c_prompt_forbids_production_scale_service_upgrade():
    prompt = _SYSTEM_PROMPT_TEMPLATE
    assert "production" in prompt
    assert '"service"' in prompt
    assert "FORBIDDEN SEMANTIC UPGRADES" in prompt


# Scenario D: skill ordering only - Java and PostgreSQL already exist, no new
# skills invented.
@patch("src.resume.resume_agent.Anthropic")
def test_scenario_d_skill_reorder_only_no_new_skills(mock_anthropic_cls):
    tailored = _MULTI_PROJECT_LATEX.replace(
        "\\resumeItem{Languages: Python, Java, PostgreSQL}",
        "\\resumeItem{Languages: Java, PostgreSQL, Python}",
    )
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": True,
            "reasoning": "Job explicitly requests Java and PostgreSQL, both already in the candidate's "
            "existing skills - reordering surfaces them first.",
            "selected_projects": [],
            "removed_or_deemphasized_projects": [],
            "reordered_skills": ["Languages: Java and PostgreSQL moved earlier"],
            "tailored_latex": tailored,
            "summary_of_changes": "Reordered Languages to lead with Java and PostgreSQL.",
            "github_facts_used": [],
        }
    )

    result = decide_and_tailor(
        job_description="Requires Java and PostgreSQL experience.",
        ats_result=_ats_result(match_score=55, matched_skills=["Java", "PostgreSQL"]),
        master_raw_latex=_MULTI_PROJECT_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=55,
    )

    assert result.tailoring_needed is True
    assert result.reordered_skills
    assert result.selected_projects == []
    assert result.removed_or_deemphasized_projects == []
    # Same three skills, no invented fourth one.
    assert "Java, PostgreSQL, Python" in result.tailored_latex


# Scenario E: GitHub-only project with sufficient evidence, not yet on the
# master resume - it may be selected using only its trusted evidence.
@patch("src.resume.resume_agent.Anthropic")
def test_scenario_e_github_only_project_with_sufficient_evidence_may_be_selected(mock_anthropic_cls):
    github_context = [
        {
            "project_name": "Kafka Streaming Service",
            "owner": "cand",
            "repo": "kafka-streaming",
            "description": "A real-time event processing service using Kafka and Go",
            "languages": {"Go": 8000},
            "topics": ["kafka", "go"],
            "readme_excerpt": "Consumes and processes events from Kafka topics in Go.",
        }
    ]
    tailored = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\section{Projects}\n"
        "\\resumeSubHeadingListStart\n"
        "\\resumeProjectHeading\n"
        "    {\\textbf{Kafka Streaming Service} $|$ \\href{https://github.com/cand/kafka-streaming}{GitHub}}{}\n"
        "\\resumeItemListStart\n"
        "\\resumeItem{Built a real-time event processing service using Kafka and Go}\n"
        "\\resumeItemListEnd\n"
        "\\resumeSubHeadingListEnd\n"
        "\\section{Technical Skills}\n"
        "\\resumeItemListStart\n"
        "\\resumeItem{Languages: Go, Python, Java, PostgreSQL}\n"
        "\\resumeItemListEnd\n"
        "\\end{document}\n"
    )
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": True,
            "reasoning": "The GitHub-only Kafka Streaming Service project is verified evidence directly "
            "matching the job's Kafka/Go requirement and is more relevant than the currently featured projects.",
            "selected_projects": [
                {
                    "name": "Kafka Streaming Service",
                    "source": "github_only",
                    "repo": "kafka-streaming",
                    "why": "Directly demonstrates Kafka and Go, which the job requires and no existing "
                    "featured project shows.",
                }
            ],
            "removed_or_deemphasized_projects": [
                {"name": "Legacy Dashboard", "why": "Less relevant."},
                {"name": "Java MVC Project", "why": "Less relevant to this Kafka/Go role."},
            ],
            "reordered_skills": ["Languages"],
            "tailored_latex": tailored,
            "summary_of_changes": "Added Kafka Streaming Service (GitHub-only, sufficient evidence).",
            "github_facts_used": [
                {
                    "fact": "Kafka Streaming Service README describes a real-time event processing "
                    "service using Kafka and Go",
                    "quoted_in_resume": "Built a real-time event processing service using Kafka and Go",
                }
            ],
        }
    )

    result = decide_and_tailor(
        job_description="Looking for a Go engineer with Kafka streaming experience.",
        ats_result=_ats_result(match_score=45),
        master_raw_latex=_MULTI_PROJECT_LATEX,
        candidate_profile={},
        github_context=github_context,
        match_score=45,
    )

    assert result.tailoring_needed is True
    assert result.selected_projects[0]["source"] == "github_only"
    assert "Kafka Streaming Service" in result.tailored_latex
    assert result.github_facts_used == [
        "Kafka Streaming Service README describes a real-time event processing service using Kafka and Go"
    ]


# Scenario F: repository exists but evidence is insufficient - must never be
# added, enforced deterministically by build_project_pool (already covered
# above), confirmed again here at the pool level for this exact scenario.
def test_scenario_f_insufficient_github_evidence_project_not_added_to_pool():
    github_context = [
        {
            "project_name": "Sparse Repo",
            "owner": "cand",
            "repo": "sparse-repo",
            "description": None,
            "languages": {},
            "topics": [],
            "readme_excerpt": None,
        }
    ]
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context, library_dir=_EMPTY_LIBRARY_DIR)
    assert "Sparse Repo" not in {p["name"] for p in pool}


# Scenario G: regression protection for jobs 256, 257, 234, 250, 251, 252 -
# confirms the project-selection rewrite didn't drop any prior safety rule.
def test_scenario_g_prior_failure_regressions_still_present_in_prompt():
    prompt = _SYSTEM_PROMPT_TEMPLATE
    assert "own end-to-end execution" in prompt
    assert "full stack of execution" in prompt
    assert "deploying AI agents" in prompt
    assert '"Prototyped"' in prompt
    assert "PROJECT / PROFESSIONAL BOUNDARY" in prompt
    assert "EVIDENCE-FIRST REWRITING" in prompt


def test_prompt_states_core_selection_over_rewriting_principle():
    # The central philosophy shift this change introduces.
    prompt = _SYSTEM_PROMPT_TEMPLATE
    assert "CORE PRINCIPLE" in prompt
    assert "Project selection is flexible" in prompt
    assert "Project FACTS are protected" in prompt
    assert "TAILORING PRIORITY ORDER" in prompt
    assert "Do NOT start by rewriting bullets" in prompt


def test_prompt_states_decision_sequence_questions():
    prompt = _SYSTEM_PROMPT_TEMPLATE
    assert "DECISION SEQUENCE" in prompt
    for fragment in (
        "What does this job genuinely prioritize",
        "What candidate evidence already exists",
        "Is the CURRENT project/skill selection",
        "Can the resume be improved by SELECTING or REORDERING",
        "should it replace a weaker one",
        "without changing the factual meaning",
    ):
        assert fragment in prompt


def test_prompt_forbids_inventing_skills_from_job_description():
    prompt = _SYSTEM_PROMPT_TEMPLATE
    assert "SKILLS RULES" in prompt
    assert "never because the job description asks for it" in prompt
    assert "Spring" in prompt and "React" in prompt  # the adjacent-technology inference examples


def test_prompt_output_contract_includes_project_selection_fields():
    prompt = _SYSTEM_PROMPT_TEMPLATE
    assert "selected_projects" in prompt
    assert "removed_or_deemphasized_projects" in prompt
    assert "reordered_skills" in prompt
    assert "describes a selection" in prompt or "describes a rewrite" in prompt


def test_active_zone_mentions_project_replacement_as_major_mechanism():
    assert "major, legitimate tailoring mechanism" in _ACTIVE_ZONE_INSTRUCTIONS


# --- Candidate Project Library integration --------------------------------------
#
# These build a small, controlled fixture library (via project_library.py's own
# tested functions - the same code the real ingestion uses) rather than
# constructing raw JSON by hand, so the fixtures stay representative of the real
# on-disk schema. All fixture libraries live under tmp_path - the real
# data/candidate_projects/ library is never touched by these tests.

from src.resume.project_library import (  # noqa: E402
    build_github_only_record,
    write_library_index,
    write_project_record,
)


def _library_repo_evidence(name: str, **overrides):
    from src.resume.project_library import RepoEvidence

    defaults = dict(
        name=name,
        owner="savsuth",
        repo=name,
        url=f"https://github.com/savsuth/{name}",
        description=f"A Java Spring MVC application with REST endpoints and PostgreSQL persistence ({name}).",
        languages={"Java": 30000},
        topics=[],
        readme_excerpt="Implements MVC controllers, REST endpoints, and PostgreSQL persistence.",
        root_entries=["pom.xml"],
        stars=0,
        fetched_at="2026-08-16T00:00:00+00:00",
    )
    defaults.update(overrides)
    return RepoEvidence(**defaults)


def _fixture_library(tmp_path, approved: bool = True, with_bullets: bool = True, excluded: bool = False):
    """Builds one project ('Virtual-calendar' by default) in a fixture library
    under tmp_path with a controllable eligibility state, using the real
    project_library ingestion functions (never hand-crafted JSON)."""
    evidence = _library_repo_evidence("Virtual-calendar")
    if excluded:
        evidence = _library_repo_evidence("savsuth", owner="savsuth", repo="savsuth")  # matches account username
    record = build_github_only_record(evidence, account_username="savsuth")
    if approved and not excluded:
        record.eligibility_state = "approved"
        if not with_bullets:
            record.bullets = []
            record.resume_bullets_available = False
            record.resume_bullets_source = "none"
    write_project_record(record, library_dir=tmp_path)
    write_library_index([record], library_dir=tmp_path)
    return tmp_path, record


# Test A: approved GitHub-only project is visible in the pool.
def test_library_test_a_approved_project_visible_in_pool(tmp_path):
    library_dir, record = _fixture_library(tmp_path, approved=True, with_bullets=True)
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context=[], library_dir=library_dir)

    library_entries = [p for p in pool if p["source"] == "candidate_project_library"]
    assert len(library_entries) == 1
    assert library_entries[0]["name"] == "Virtual-calendar"
    assert library_entries[0]["existing_bullets"] == [b["text"] for b in record.bullets]


# Test B: needs_review project is hidden from the pool.
def test_library_test_b_needs_review_project_hidden(tmp_path):
    library_dir, _ = _fixture_library(tmp_path, approved=False)  # stays needs_review (default)
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context=[], library_dir=library_dir)

    assert not [p for p in pool if p["source"] == "candidate_project_library"]
    assert "Virtual-calendar" not in {p["name"] for p in pool}


# Test C: excluded project is hidden from the pool.
def test_library_test_c_excluded_project_hidden(tmp_path):
    library_dir, _ = _fixture_library(tmp_path, excluded=True)
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context=[], library_dir=library_dir)

    assert not [p for p in pool if p["source"] == "candidate_project_library"]


# Test D: approved but no resume-ready bullets -> hidden (never selectable).
def test_library_test_d_approved_without_bullets_hidden(tmp_path):
    library_dir, _ = _fixture_library(tmp_path, approved=True, with_bullets=False)
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context=[], library_dir=library_dir)

    assert not [p for p in pool if p["source"] == "candidate_project_library"]


# Test E: master resume projects remain correctly represented alongside the library.
def test_library_test_e_master_projects_still_correct_alongside_library(tmp_path):
    library_dir, _ = _fixture_library(tmp_path, approved=True, with_bullets=True)
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context=[], library_dir=library_dir)

    master_entries = {p["name"]: p for p in pool if p["source"] == "master_resume"}
    assert set(master_entries) == {"Legacy Dashboard", "Java MVC Project"}
    assert master_entries["Java MVC Project"]["existing_bullets"] == [
        "Built a Java Spring MVC backend service with REST APIs and a PostgreSQL database"
    ]


# Test F: project-replacement payload - a Java/MVC job's prompt contains enough
# to select the library project and its canonical bullets. No live Claude call.
@patch("src.resume.resume_agent.Anthropic")
def test_library_test_f_replacement_payload_reaches_the_prompt(mock_anthropic_cls, tmp_path):
    library_dir, record = _fixture_library(tmp_path, approved=True, with_bullets=True)
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": True,
            "reasoning": "Virtual-calendar is direct existing evidence for the job's Java/MVC requirement.",
            "selected_projects": [
                {
                    "name": "Virtual-calendar",
                    "source": "candidate_project_library",
                    "repo": "Virtual-calendar",
                    "why": "Direct existing Java/MVC evidence.",
                }
            ],
            "removed_or_deemphasized_projects": [
                {"name": "Legacy Dashboard", "why": "Less relevant to the requested Java/MVC stack."}
            ],
            "reordered_skills": [],
            "tailored_latex": _MULTI_PROJECT_LATEX,
            "summary_of_changes": "Replaced Legacy Dashboard with the approved Virtual-calendar library project.",
            "github_facts_used": [],
        }
    )

    result = decide_and_tailor(
        job_description="We need a backend engineer strong in Java, MVC frameworks, and REST APIs.",
        ats_result=_ats_result(match_score=40, matched_skills=["Java"]),
        master_raw_latex=_MULTI_PROJECT_LATEX,
        candidate_profile={},
        github_context=[],
        match_score=40,
        library_dir=library_dir,
    )

    _, kwargs = mock_client.messages.create.call_args
    prompt_content = kwargs["messages"][0]["content"]
    assert "Virtual-calendar" in prompt_content
    assert "candidate_project_library" in prompt_content
    assert record.bullets[0]["text"] in prompt_content  # canonical bullet reached the prompt verbatim

    assert result.selected_projects[0]["name"] == "Virtual-calendar"
    assert result.selected_projects[0]["source"] == "candidate_project_library"
    assert result.removed_or_deemphasized_projects[0]["name"] == "Legacy Dashboard"


# Test G: canonical bullets are passed through unchanged (byte-for-byte).
def test_library_test_g_canonical_bullets_unchanged(tmp_path):
    library_dir, record = _fixture_library(tmp_path, approved=True, with_bullets=True)
    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context=[], library_dir=library_dir)

    library_entry = next(p for p in pool if p["source"] == "candidate_project_library")
    on_disk_bullets = json.loads((library_dir / record.slug / "bullets.json").read_text())
    assert library_entry["existing_bullets"] == [b["text"] for b in on_disk_bullets]


# Test H: no unapproved project leakage - mixed library, only the approved one
# reaches the pool or the prompt.
def test_library_test_h_no_unapproved_leakage_mixed_library(tmp_path):
    approved_evidence = _library_repo_evidence("Virtual-calendar")
    approved_record = build_github_only_record(approved_evidence, account_username="savsuth")
    approved_record.eligibility_state = "approved"

    needs_review_evidence = _library_repo_evidence("Discord-Clone")
    needs_review_record = build_github_only_record(needs_review_evidence, account_username="savsuth")
    # left as the default needs_review state

    excluded_evidence = _library_repo_evidence("savsuth", owner="savsuth", repo="savsuth")
    excluded_record = build_github_only_record(excluded_evidence, account_username="savsuth")

    for record in (approved_record, needs_review_record, excluded_record):
        write_project_record(record, library_dir=tmp_path)
    write_library_index([approved_record, needs_review_record, excluded_record], library_dir=tmp_path)

    pool = build_project_pool(_MULTI_PROJECT_LATEX, github_context=[], library_dir=tmp_path)
    library_names = {p["name"] for p in pool if p["source"] == "candidate_project_library"}
    assert library_names == {"Virtual-calendar"}
    assert "Discord-Clone" not in {p["name"] for p in pool}
    assert "savsuth" not in {p["name"] for p in pool}


# Test I: prior live-audit regressions (jobs 256/257/234/250/251/252) still pass
# with the library integration in place - re-verified explicitly here (the
# original regression tests earlier in this file are untouched and still run).
def test_library_test_i_prior_regression_prompt_rules_still_present():
    prompt = _SYSTEM_PROMPT_TEMPLATE
    assert "FORBIDDEN SEMANTIC UPGRADES" in prompt
    assert "own end-to-end execution" in prompt
    assert "full stack of execution" in prompt
    assert "deploying AI agents" in prompt
    assert "PROJECT / PROFESSIONAL BOUNDARY" in prompt
    # And the new library rule applies the SAME forbidden-upgrade rules to
    # library-sourced projects, not a separate/weaker standard.
    assert 'source="candidate_project_library"' in prompt
    assert "the same forbidden-upgrade and evidence-first rules below apply identically" in prompt


# --- JSON reliability regression tests -----------------------------------------
#
# A live production run (2026-08-16) hit 4 real Resume Agent failures: 3
# "Unterminated string" and 1 "Expecting ',' delimiter" - all genuinely
# malformed/incomplete JSON, not repaired here (see src/llm_json.py's
# docstring on why only the trailing-comma case is safe to auto-repair).
# These pin the exact observed shapes down deterministically.

@patch("src.resume.resume_agent.Anthropic")
def test_decide_and_tailor_trailing_comma_before_closing_brace_is_safely_repaired(mock_anthropic_cls):
    raw = (
        '{"tailoring_needed": false, "reasoning": "Master resume already representative.", '
        '"selected_projects": [], "removed_or_deemphasized_projects": [], "reordered_skills": [], '
        '"tailored_latex": null, "summary_of_changes": null, "github_facts_used": [],}'
    )
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_raw_response(raw)

    decision = decide_and_tailor(
        job_description="...", ats_result={"match_score": 55}, master_raw_latex=_VALID_LATEX,
        candidate_profile={}, github_context=[], match_score=55,
    )

    assert decision.tailoring_needed is False
    assert decision.reasoning == "Master resume already representative."


@patch("src.resume.resume_agent.Anthropic")
def test_decide_and_tailor_missing_tailoring_needed_raises_llm_response_error(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response({"reasoning": "..."})

    with pytest.raises(LLMResponseError, match="tailoring_needed"):
        decide_and_tailor(
            job_description="...", ats_result={"match_score": 40}, master_raw_latex=_VALID_LATEX,
            candidate_profile={}, github_context=[], match_score=40,
        )


@patch("src.resume.resume_agent.Anthropic")
def test_decide_and_tailor_missing_reasoning_raises_llm_response_error(mock_anthropic_cls):
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response({"tailoring_needed": False})

    with pytest.raises(LLMResponseError, match="reasoning"):
        decide_and_tailor(
            job_description="...", ats_result={"match_score": 40}, master_raw_latex=_VALID_LATEX,
            candidate_profile={}, github_context=[], match_score=40,
        )


@patch("src.resume.resume_agent.Anthropic")
def test_decide_and_tailor_unterminated_string_raises_llm_response_error(mock_anthropic_cls):
    # Exact observed live shape (jobs 289/290/295): cut off mid-string, well
    # before any realistic max_tokens ceiling - must fail loudly, never be
    # silently repaired or turned into a fabricated decision.
    # Trailing '}' mirrors the live shape (a '}' from later content still lets
    # the extraction regex find a candidate match) - exercises the "matched but
    # json.loads() fails" path, not the separate "no span found" path.
    raw = '{"tailoring_needed": true, "reasoning": "The job requires Java and this candidate has}'
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_raw_response(raw, stop_reason="max_tokens")

    with pytest.raises(LLMResponseError, match="malformed/incomplete JSON"):
        decide_and_tailor(
            job_description="...", ats_result={"match_score": 40}, master_raw_latex=_VALID_LATEX,
            candidate_profile={}, github_context=[], match_score=40,
        )


@patch("src.resume.resume_agent.Anthropic")
def test_decide_and_tailor_missing_delimiter_raises_llm_response_error(mock_anthropic_cls):
    # Exact observed live shape (job_id=294): "Expecting ',' delimiter".
    raw = '{"tailoring_needed": true "reasoning": "x", "tailored_latex": null}'
    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_raw_response(raw)

    with pytest.raises(LLMResponseError, match="malformed/incomplete JSON"):
        decide_and_tailor(
            job_description="...", ats_result={"match_score": 40}, master_raw_latex=_VALID_LATEX,
            candidate_profile={}, github_context=[], match_score=40,
        )


@patch("src.resume.resume_agent.Anthropic")
def test_decide_and_tailor_parses_correctly_with_long_brace_heavy_latex_content(mock_anthropic_cls):
    # A real tailored_latex payload is thousands of characters of brace-heavy
    # LaTeX (\resumeItem{...}, \textbf{...}, etc.) - confirms the greedy
    # first-'{'-to-last-'}' extraction and JSON parsing handle a realistically
    # large, heavily-nested string value correctly end-to-end, not just the
    # short fixtures used elsewhere in this file.
    long_latex = (
        "\\documentclass{article}\n\\begin{document}\n\\resumeSubHeadingListStart\n"
        + "".join(
            f"\\resumeProjectHeading\n    {{\\textbf{{Project {i}}} $|$ "
            f"\\href{{https://github.com/cand/project-{i}}}{{GitHub}}}}{{}}\n"
            f"\\resumeItemListStart\n\\resumeItem{{Built project {i} using Python, "
            f"React, and PostgreSQL with a {{nested}} {{brace}} heavy description "
            f"covering roughly a hundred characters of realistic bullet text}}\n"
            f"\\resumeItemListEnd\n"
            for i in range(15)
        )
        + "\\resumeSubHeadingListEnd\n\\end{document}\n"
    )
    assert len(long_latex) > 3000  # comparable in scale to the live failures' error offsets

    mock_client = mock_anthropic_cls.return_value
    mock_client.messages.create.return_value = _fake_response(
        {
            "tailoring_needed": True,
            "reasoning": "Replaced weaker projects with stronger, more relevant existing evidence.",
            "selected_projects": [{"name": f"Project {i}", "source": "master_resume", "repo": None, "why": "x"} for i in range(15)],
            "removed_or_deemphasized_projects": [],
            "reordered_skills": [],
            "tailored_latex": long_latex,
            "summary_of_changes": "Reordered and replaced projects for relevance.",
            "github_facts_used": [],
        }
    )

    decision = decide_and_tailor(
        job_description="...", ats_result={"match_score": 40}, master_raw_latex=_VALID_LATEX,
        candidate_profile={}, github_context=[], match_score=40,
    )

    assert decision.tailoring_needed is True
    assert decision.tailored_latex == long_latex
    assert len(decision.selected_projects) == 15
