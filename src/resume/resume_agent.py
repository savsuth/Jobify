"""Resume Agent: a single decision+generation call for jobs already routed here
by the ATS gate (match_score in [30, 65) - see src/graph/pipeline.py).

Does not re-score fit; the ATS match_score/analysis are read-only context for
a presentation decision: is the master resume already the right
representation for this job, or is there a legitimate, evidence-backed
improvement? One Claude call decides and (if warranted) generates the
tailored LaTeX in the same response, followed by deterministic checks (not a
second LLM call): a LaTeX structural check and a GitHub citation-consistency
check (see _verify_github_facts_used).

CORE PRINCIPLE (added after a live audit found the model rewriting unrelated
project bullets to match the job description): tailor by SELECTING existing
evidence, never by CHANGING what it says. Project selection is flexible;
project facts are not - a same-level synonym swap is the only rewrite
allowed. build_project_pool() computes, once per call in pure Python, the
exact set of projects the model may choose from; a project outside that pool
cannot be selected or described. This is what actually prevents
hallucination: the model is never shown a project it hasn't been
pre-approved to use.

The pool draws from three places: the master resume, live per-run GitHub
context (integrations/github.py), and the Candidate Project Library
(data/candidate_projects/, project_library.py) - only human-approved library
projects are ever visible, loaded read-only via
load_approved_library_projects(), never a live GitHub call at tailoring time.

Routing is deterministic, not left to the model:
- 30-49 ("active"): actively look for a legitimate improvement, though
  tailoring is still never automatic.
- 50-64 ("selective"): default to keeping the master resume; tailor only for
  a specific, meaningful improvement.
(<30 is rejected before reaching here; >=65 bypasses this module and uses
the master resume as-is - both decided by the caller.)

The master resume LaTeX is the protected source of truth; GitHub context may
only confirm or enrich an existing claim, never introduce a new one. The job
description may guide WHAT to emphasize but never HOW SENIOR, HOW OWNED, or
HOW PRODUCTION-GRADE the work sounds (see the "evidence-first rewriting"
rules in the system prompt) - added after an audit found the model copying
job-posting language into project/internship bullets to imply ownership the
candidate doesn't have.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from anthropic import Anthropic

from src.config import get_settings
from src.llm_json import extract_json_object, require_fields, response_text
from src.resume.project_library import LIBRARY_DIR, extract_project_blocks as _extract_master_projects
from src.resume.project_library import load_approved_library_projects

# --- Project pool: deterministic parsing, no Claude/API call -----------------
# _extract_master_projects is project_library.extract_project_blocks,
# relocated there so both modules can share one parser without a circular
# import (project_library.py must not import resume_agent.py).

_SECTION_HEADER_PATTERN = re.compile(r"\\section\{([^{}]+)\}")


def _has_sufficient_github_evidence(repo_ctx: dict) -> bool:
    """Evidence bar a GitHub repo not already in the master resume must clear
    before entering the pool - the model never sees a repo that fails this.
    Requires a real description or README excerpt plus at least one detected
    language; without both, there's nothing trustworthy to build an honest
    bullet from."""
    has_narrative = bool((repo_ctx.get("description") or "").strip()) or bool(
        (repo_ctx.get("readme_excerpt") or "").strip()
    )
    has_languages = bool(repo_ctx.get("languages"))
    return has_narrative and has_languages


def build_project_pool(
    master_raw_latex: str, github_context: list[dict], library_dir: Path = LIBRARY_DIR
) -> list[dict]:
    """The candidate's full selectable project pool for one Resume Agent call.

    Three sources, merged: (1) every master-resume project, always included,
    with GitHub metadata merged in by name match; (2) GitHub repos not
    already in the master resume, included only if they clear
    _has_sufficient_github_evidence; (3) approved Candidate Project Library
    entries (data/candidate_projects/, project_library.py), loaded
    read-only - only human-approved projects with resume-ready bullets ever
    reach this function, filtered upstream by
    load_approved_library_projects(). A library project already represented
    via the master resume is skipped.

    fetch_github_context() currently only returns repos already linked in
    the master resume, so source (2) is empty in practice - kept generic in
    case GitHub discovery is later broadened to surface new repos.
    """
    master_projects = _extract_master_projects(master_raw_latex)
    github_by_name = {
        repo["project_name"].strip().lower(): repo
        for repo in github_context
        if repo.get("project_name")
    }

    pool = []
    matched_names = set()
    for project in master_projects:
        key = project["name"].strip().lower()
        matched_names.add(key)
        github_match = github_by_name.get(key)
        pool.append(
            {
                "name": project["name"],
                "source": "master_resume",
                "owner": project["owner"],
                "repo": project["repo"],
                "existing_bullets": project["existing_bullets"],
                "description": github_match.get("description") if github_match else None,
                "languages": github_match.get("languages") if github_match else {},
                "topics": github_match.get("topics") if github_match else [],
                "readme_excerpt": github_match.get("readme_excerpt") if github_match else None,
            }
        )

    for key, repo in github_by_name.items():
        if key in matched_names or not _has_sufficient_github_evidence(repo):
            continue
        matched_names.add(key)
        pool.append(
            {
                "name": repo.get("project_name"),
                "source": "github_only",
                "owner": repo.get("owner"),
                "repo": repo.get("repo"),
                "existing_bullets": [],
                "description": repo.get("description"),
                "languages": repo.get("languages"),
                "topics": repo.get("topics"),
                "readme_excerpt": repo.get("readme_excerpt"),
            }
        )

    for library_project in load_approved_library_projects(library_dir):
        key = library_project["name"].strip().lower()
        if key in matched_names:
            continue  # already represented via the master resume or live GitHub context above
        matched_names.add(key)
        pool.append(library_project)

    return pool


# --- Prompt --------------------------------------------------------------------

_ACTIVE_ZONE_INSTRUCTIONS = """\
This job scored in the 30-49% ACTIVE TAILORING zone: the master resume has a \
relatively weak match as currently presented. Actively search for a legitimate \
improvement before concluding none exists - examine whether CANDIDATE_PROJECT_POOL \
contains a project that better demonstrates this job's priorities than what's \
currently featured, whether existing skills should be reordered or emphasized \
differently, whether an existing bullet could be rephrased (using only real, \
existing facts, at the SAME level of ownership/completion already stated) to \
better align with this job's stated requirements, and whether less relevant \
content should be de-emphasized. Project selection/replacement/reordering is a \
major, legitimate tailoring mechanism in this zone - promoting a pool project that \
genuinely demonstrates something this job asks for, while de-emphasizing a weaker \
featured one, is a real improvement, not a rewrite. Tailoring is expected to be \
useful more often than not in this zone, but it is NOT automatic: if, after this \
active search, you find no legitimate improvement using only supported evidence, \
tailoring_needed must still be false. Never force a rewrite just because this job \
is in the active zone.

This is exactly the zone where evidence-first discipline matters most: a low \
match score is never a license to describe existing work as more senior, owned, \
or production-grade than it actually was. If the only way to "improve" a bullet \
for this job would require a forbidden semantic upgrade (see below) or borrowing \
the job posting's own language about what the role involves, that is NOT a \
legitimate improvement - leave that specific content unchanged (reordering/\
de-emphasis/project replacement alone may still be legitimate) rather than \
reaching for stronger wording than the evidence supports.\
"""

_SELECTIVE_ZONE_INSTRUCTIONS = """\
This job scored in the 50-64% SELECTIVE TAILORING zone: the master resume already \
has a moderate match. First determine whether meaningful tailoring is actually \
beneficial before making any change. Default to keeping the master resume unchanged \
(tailoring_needed=false) unless you identify a specific, concrete improvement that \
would meaningfully strengthen the candidate's presentation for this particular job. \
Project replacement/reordering can still qualify as that "specific, concrete \
improvement" if it produces a meaningful relevance gain - it is not exempt from \
the same default-to-false bar everything else in this zone is held to. Do not \
tailor merely because the job falls in this range.\
"""

_SYSTEM_PROMPT_TEMPLATE = """\
You are a Resume Tailoring Agent. This job has already passed ATS screening - the \
match_score and analysis you're given are read-only context, not something to \
recompute. Your job is a presentation decision: can the candidate's master resume \
be more effectively presented for this specific job using only their real, \
existing, verifiable background?

CORE PRINCIPLE: tailor by SELECTING the strongest existing evidence, not by \
CHANGING the meaning of existing evidence. Project selection is flexible - which \
existing project you feature, promote, reorder, or drop is an open choice. \
Project FACTS are protected - what a selected project's bullets say about what \
the candidate actually did must never change beyond a strict same-level synonym \
swap.

DECISION SEQUENCE - reason through these in order:
1. What does this job genuinely prioritize (technologies, domain, responsibilities)?
2. What candidate evidence already exists (MASTER_RESUME, STRUCTURED CANDIDATE \
PROFILE, or CANDIDATE_PROJECT_POOL) that addresses those priorities?
3. Is the CURRENT project/skill selection in the master resume hiding more \
relevant evidence that already exists elsewhere in the pool?
4. Can the resume be improved by SELECTING or REORDERING existing evidence, \
rather than rewriting it?
5. If a stronger project exists in CANDIDATE_PROJECT_POOL than what's currently \
featured, should it replace a weaker one?
6. Can all of this be done without changing the factual meaning of any evidence \
(ownership, completion, deployment/production status, scale)?
If the honest answer to 4-6 is yes, tailoring_needed=true. If reaching "yes" would \
require inventing, upgrading, or borrowing job-posting language instead, \
tailoring_needed=false.

TAILORING PRIORITY ORDER - always prefer earlier options over later ones, and stop \
as soon as an earlier option solves the problem:
1. Select the most relevant existing projects from CANDIDATE_PROJECT_POOL
2. Replace/de-emphasize a weaker featured project with a more relevant, verified \
pool project
3. Reorder projects
4. Reorder technical skill categories/items
5. Only if 1-4 aren't enough: a strictly same-level wording change to an existing \
bullet
6. Only if directly relevant: cite verified GitHub evidence to support an existing \
claim
Do NOT start by rewriting bullets. If project or skill selection alone can solve \
the tailoring problem, that is the answer - do not also rewrite content on top of it.

CANDIDATE_PROJECT_POOL (a separate structured list in the user message) is every \
project you are allowed to select from. Three kinds of entries:
- source="master_resume": already in the master resume, with its real \
"existing_bullets" - pre-verified true content. You may keep these bullets \
verbatim (the default), reorder them, drop the whole project, or rephrase a \
bullet within the strict same-level limits below. Never invent a bullet for one \
of these beyond a rephrase of an existing_bullets entry.
- source="github_only": a GitHub repository with enough evidence to be eligible \
(already checked deterministically before you ever saw it - you are not being \
asked to judge sufficiency) but NOT currently in the master resume. If you select \
one, its resume bullets must be built ONLY from its own supplied description/\
topics/languages/readme_excerpt fields - never invent an accomplishment, metric, \
or capability beyond what those fields literally support. If those fields don't \
give you enough to write an honest, specific bullet, do not add the project - \
leaving it out is always safe; a vague or embellished bullet is not.
- source="candidate_project_library": a real candidate project from the local, \
human-reviewed Candidate Project Library - selectable ONLY because a human \
already reviewed and approved it, and it already carries its own real, \
human-finalized "existing_bullets". Treat these bullets EXACTLY like a \
master_resume project's: reuse them verbatim as the default, never invent a new \
description for this project, and never rewrite them to sound more impressive - \
the same forbidden-upgrade and evidence-first rules below apply identically to \
every source in this pool. Selecting a library project is a pure presentation \
decision (which of the candidate's real, already-approved projects to feature), \
never an opportunity to add new claims.
A project not present in CANDIDATE_PROJECT_POOL does not exist for tailoring \
purposes - never reference, select, or describe a project that isn't in this \
list, no matter what the job description asks for.

PROJECT REPLACEMENT EXAMPLE: if the job asks for Java/MVC/backend work and \
CANDIDATE_PROJECT_POOL contains a project whose languages/description genuinely \
show Java/MVC/backend work, you may de-emphasize or drop a currently-featured but \
less relevant project (e.g. a finance-visualization dashboard) and promote the \
Java/MVC project instead - using ONLY that project's own existing_bullets or \
evidence-backed description, never rewriting it to sound more impressive than its \
own evidence supports.

EVIDENCE-FIRST REWRITING - read this before anything else: the job description may \
tell you WHAT to emphasize (which existing skills/projects are actually relevant). \
It must NEVER tell you HOW SENIOR, HOW OWNED, or HOW PRODUCTION-GRADE the \
candidate's work was. Every factual claim, and every implied level of ownership, \
scope, or maturity in your output, must be independently traceable to the master \
resume, the structured profile, or an exact, verifiable GitHub fact - never to the \
job posting's own language about what the ROLE involves.

FORBIDDEN SEMANTIC UPGRADES - never do these, no matter how well they would align \
with the job:
- "Prototyped" -> "Built" / "Implemented" / "Deployed" / "Shipped" / "Owned"
- "Built" / "Implemented" / "Designed" -> "Owned" / "Operated" / "Ran in production"
- a personal/academic project -> a "production system" or "service"
- a personal/academic project -> "customer-facing" or "client delivery" work
- an experiment or prototype -> a "production deployment"
- exploratory/learning work -> "professional ownership" or "end-to-end ownership"
If the master resume says "Prototyped" or "Designed," the tailored version must \
still say "Prototyped" or "Designed" (or a strict same-level synonym, e.g. \
"Engineered" for "Built") for that same piece of work - never a verb implying \
greater completion, ownership, or production status, regardless of how well a \
stronger verb would match the job description.

DO NOT COPY THE JOB POSTING'S OWN VOCABULARY. Distinctive phrases describing what \
the ROLE demands - "own end-to-end execution," "full stack of execution," \
"deploying AI agents," "delivery," "production," "production-grade," \
"customer-facing," "large-scale," "high-throughput," and similar - describe the \
JOB, not the CANDIDATE. Never insert this language into a project or experience \
bullet unless the underlying claim is independently supported by the master resume \
or GitHub evidence, completely independent of whether the job description happens \
to use similar words.

PROJECT / PROFESSIONAL BOUNDARY: personal projects (anything under "Projects" in \
the master resume) must remain described as projects - never reframed as \
production services, deployed systems, or client-facing deliverables. Internship/\
employment work (anything under "Experience") must remain described at the same \
scope and completion level the master resume states - never upgraded to imply \
broader ownership, deployment, or client-facing responsibility than what's stated. \
A job description using ownership or production language is never, by itself, a \
reason to describe the candidate's own work that way.

SKILLS RULES: you may reorder existing skill categories/items and move relevant \
existing technologies earlier. You may add a skill/technology to Technical Skills \
ONLY if it is independently supported by trusted evidence (STRUCTURED CANDIDATE \
PROFILE, an existing_bullets entry, or a verified GitHub fact) - never because the \
job description asks for it, and never by inferring a framework/tool from an \
adjacent or related technology (e.g. do not add "Spring" just because "Java" is \
listed, do not add "React" because "JavaScript" is listed). If a requested \
technology is neither in the candidate's existing skills nor demonstrated by a \
pool project, leave it out entirely - do not invent it and do not imply it through \
a nearby project's placement.

The master resume LaTeX (MASTER_RESUME) is the primary, protected source of truth. \
GitHub repository context (GITHUB_CONTEXT), when provided, is supporting evidence \
only - use it to confirm or enrich an existing project's description, never to \
introduce a claim the master resume doesn't already make.

Absolute rules:
- Every claim in a tailored resume must be traceable to the master resume, the \
  structured candidate profile, CANDIDATE_PROJECT_POOL, or the provided GitHub \
  context. Never invent technologies, experience, responsibilities, metrics, \
  achievements, project capabilities, employment history, certifications, \
  education, or any other unsupported factual claim, no matter how plausible it \
  sounds.
- Allowed changes when tailoring, per the TAILORING PRIORITY ORDER above: select/\
  replace/reorder existing projects from CANDIDATE_PROJECT_POOL, reorder existing \
  skills, de-emphasize or remove less relevant existing content, and only then \
  same-level rephrasing. Rephrasing must preserve the exact ownership/completion \
  level already stated (see forbidden upgrades above) - nothing else is allowed.
- Never add a scale, performance, or maturity qualifier - "scalable," "at scale," \
  "high-throughput," "large-scale," "production," "production-style," "service," \
  "throughput," or similar - unless that exact claim is already stated in the \
  source bullet or an explicitly cited, verified GitHub fact.
- Do not run, reference, or imply a second ATS/fit score. The given match_score is \
  final and already decided this job proceeds - ATS ANALYSIS tells you WHICH \
  evidence is relevant, not how strong the final presentation should sound.
- If you tailor, tailored_latex must be the FULL resulting LaTeX document (same \
  preamble and macros as the master, not a diff or fragment) - it must remain a \
  complete, compilable document. Every \\section{{}} present in MASTER_RESUME must \
  still be present in your output, and no two projects may share the same name.

{zone_instructions}

Respond with ONLY a JSON object, no other text:
{{"tailoring_needed": bool, "reasoning": str,
"selected_projects": [{{"name": str, "source": "master_resume" or "github_only" or \
"candidate_project_library", "repo": str or null, "why": str}}],
"removed_or_deemphasized_projects": [{{"name": str, "why": str}}],
"reordered_skills": [str],
"tailored_latex": str or null, "summary_of_changes": str or null,
"github_facts_used": [{{"fact": str, "quoted_in_resume": str}}]}}

reasoning: 2-4 sentences explaining the decision, referencing specific job \
requirements and specific resume content. selected_projects: every project \
actually featured in tailored_latex, explaining WHY it was selected - e.g. \
"Replaced Derivatives Pricing Dashboard with Java MVC Project because the latter \
provides direct existing evidence for the job's Java/MVC requirement" describes a \
selection; "Tailored the project to emphasize Java/MVC" describes a rewrite and is \
not an acceptable answer. removed_or_deemphasized_projects: any project present in \
MASTER_RESUME no longer featured (or clearly de-emphasized) in tailored_latex, and \
why. reordered_skills: which skill categories/items were reordered and why. \
selected_projects/removed_or_deemphasized_projects/reordered_skills/\
tailored_latex/summary_of_changes must all be empty/null when tailoring_needed is \
false. github_facts_used: for each GitHub fact you actually relied on, "fact" is \
the specific fact from GITHUB_CONTEXT, and "quoted_in_resume" must be an EXACT, \
VERBATIM substring copied from your own tailored_latex showing where that fact was \
used - do not report a fact you did not actually incorporate into the text, and do \
not paraphrase "quoted_in_resume" (it will be checked programmatically against \
your own output).
"""


@dataclass
class ResumeDecision:
    zone: str  # "active" (30-49) | "selective" (50-64)
    tailoring_needed: bool
    reasoning: str
    tailored_latex: str | None
    summary_of_changes: str | None
    github_facts_used: list[str]
    selected_projects: list[dict]
    removed_or_deemphasized_projects: list[dict]
    reordered_skills: list[str]


def tailoring_zone(match_score: int) -> str:
    """Deterministic 30-49 ("active") vs 50-64 ("selective") split - the model
    never buckets its own score. Assumes the caller only invokes the Resume
    Agent for scores in [30, 65); <30 and >=65 are decided before this point."""
    return "active" if match_score < 50 else "selective"


def _brace_balance(latex: str) -> int:
    return latex.count("{") - latex.count("}")


def _latex_structurally_valid(tailored_latex: str, master_raw_latex: str | None = None) -> bool:
    """Cheap, deterministic internal-consistency check, not a semantic
    validator or second LLM call. Confirms the tailored document isn't
    obviously broken (unbalanced braces, mismatched environments, a dropped
    section, a duplicated project name). Deliberately ignores project
    *count* against the master, since dropping/replacing a less-relevant
    project is legitimate.

    master_raw_latex is optional; when given, also confirms every master
    \\section{} survives in the output."""
    if not tailored_latex.strip().startswith("\\documentclass"):
        return False
    if _brace_balance(tailored_latex) != 0:
        return False
    if tailored_latex.count("\\begin{document}") != 1 or tailored_latex.count("\\end{document}") != 1:
        return False
    for start_marker, end_marker in (
        ("\\resumeItemListStart", "\\resumeItemListEnd"),
        ("\\resumeSubHeadingListStart", "\\resumeSubHeadingListEnd"),
    ):
        if tailored_latex.count(start_marker) != tailored_latex.count(end_marker):
            return False

    if master_raw_latex is not None:
        master_sections = set(_SECTION_HEADER_PATTERN.findall(master_raw_latex))
        tailored_sections = set(_SECTION_HEADER_PATTERN.findall(tailored_latex))
        if not master_sections.issubset(tailored_sections):
            return False

    project_names = [p["name"] for p in _extract_master_projects(tailored_latex)]
    if len(project_names) != len(set(project_names)):
        return False

    return True


def _verify_github_facts_used(raw_facts_used: list, tailored_latex: str | None) -> list[str]:
    """Citation-consistency check: the model self-reports which GitHub facts it
    used, but a live audit (job 252, 2026-08-14) found a self-reported fact
    that didn't actually appear in that job's own resume - the citation
    belonged to a different job's output. Each entry must include an exact
    "quoted_in_resume" substring that genuinely appears in THIS job's
    tailored_latex; anything that fails is silently dropped."""
    if not tailored_latex:
        return []
    verified = []
    for entry in raw_facts_used or []:
        if not isinstance(entry, dict):
            continue  # malformed entry (e.g. a bare string) - not verifiable, drop it
        fact = entry.get("fact")
        quoted = entry.get("quoted_in_resume")
        if fact and quoted and quoted in tailored_latex:
            verified.append(fact)
    return verified


def _build_system_prompt(zone: str) -> str:
    zone_instructions = _ACTIVE_ZONE_INSTRUCTIONS if zone == "active" else _SELECTIVE_ZONE_INSTRUCTIONS
    return _SYSTEM_PROMPT_TEMPLATE.format(zone_instructions=zone_instructions)


def _build_user_content(
    job_description: str,
    ats_result: dict,
    master_raw_latex: str,
    candidate_profile: dict,
    github_context: list[dict],
    project_pool: list[dict],
) -> str:
    return (
        f"JOB DESCRIPTION:\n{job_description}\n\n"
        f"ATS ANALYSIS (already decided this job proceeds - do not recompute):\n"
        f"{json.dumps(ats_result, indent=2)}\n\n"
        f"MASTER_RESUME (protected source of truth, full LaTeX):\n{master_raw_latex}\n\n"
        f"STRUCTURED CANDIDATE PROFILE:\n{json.dumps(candidate_profile, indent=2)}\n\n"
        f"CANDIDATE_PROJECT_POOL (every project you are allowed to select from - "
        f"see CANDIDATE_PROJECT_POOL rules in the system prompt):\n"
        f"{json.dumps(project_pool, indent=2)}\n\n"
        f"GITHUB_CONTEXT (raw fetched repo data, supporting evidence only, may be empty):\n"
        f"{json.dumps(github_context, indent=2)}\n"
    )


def decide_and_tailor(
    job_description: str,
    ats_result: dict,
    master_raw_latex: str,
    candidate_profile: dict,
    github_context: list[dict],
    match_score: int,
    library_dir: Path = LIBRARY_DIR,
) -> ResumeDecision:
    zone = tailoring_zone(match_score)
    project_pool = build_project_pool(master_raw_latex, github_context, library_dir)

    client = Anthropic(api_key=get_settings().anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=8192,
        system=_build_system_prompt(zone),
        messages=[
            {
                "role": "user",
                "content": _build_user_content(
                    job_description, ats_result, master_raw_latex, candidate_profile, github_context, project_pool
                ),
            }
        ],
    )
    data = extract_json_object(response_text(response), stop_reason=getattr(response, "stop_reason", None))
    # Only tailoring_needed/reasoning are hard-required here; the other
    # fields are nullable when tailoring_needed=False or covered by the
    # fallback-to-master path below. See llm_json.require_fields.
    require_fields(data, ["tailoring_needed", "reasoning"], context="Resume Agent")

    tailoring_needed = bool(data["tailoring_needed"])
    reasoning = data["reasoning"]
    tailored_latex = data.get("tailored_latex")
    summary_of_changes = data.get("summary_of_changes")
    github_facts_used = _verify_github_facts_used(data.get("github_facts_used", []), tailored_latex)
    selected_projects = data.get("selected_projects", []) if tailoring_needed else []
    removed_or_deemphasized_projects = data.get("removed_or_deemphasized_projects", []) if tailoring_needed else []
    reordered_skills = data.get("reordered_skills", []) if tailoring_needed else []

    if tailoring_needed and not tailored_latex:
        # Model claimed tailoring_needed=true without providing the LaTeX -
        # don't fabricate one ourselves, fall back to the protected master.
        reasoning = (
            f"{reasoning} [Resume Agent note: model reported tailoring_needed=true "
            "without providing tailored_latex - falling back to the master resume.]"
        )
        tailoring_needed = False
        tailored_latex = None
        summary_of_changes = None
        github_facts_used = []
        selected_projects = []
        removed_or_deemphasized_projects = []
        reordered_skills = []
    elif tailoring_needed and not _latex_structurally_valid(tailored_latex, master_raw_latex):
        # No revise loop - a structurally broken draft is discarded outright
        # and the job falls back to the master resume, not retried.
        reasoning = (
            f"{reasoning} [Resume Agent note: tailored draft failed the LaTeX "
            "structural check - falling back to the master resume rather than "
            "risking a broken document.]"
        )
        tailoring_needed = False
        tailored_latex = None
        summary_of_changes = None
        github_facts_used = []
        selected_projects = []
        removed_or_deemphasized_projects = []
        reordered_skills = []

    return ResumeDecision(
        zone=zone,
        tailoring_needed=tailoring_needed,
        reasoning=reasoning,
        tailored_latex=tailored_latex,
        summary_of_changes=summary_of_changes,
        github_facts_used=github_facts_used,
        selected_projects=selected_projects,
        removed_or_deemphasized_projects=removed_or_deemphasized_projects,
        reordered_skills=reordered_skills,
    )
