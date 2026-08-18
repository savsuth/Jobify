"""LangGraph pipeline: discover_jobs -> analyze_job (fanned out per job) ->
routed by match_score to reject/END, resume_agent, or select_master_resume -> END.

Routing after ATS scoring (search_config.ats_threshold = reject cutoff,
search_config.resume_no_tailor_threshold = no-tailoring ceiling):

    match_score <  ats_threshold                       -> rejected, END
    ats_threshold <= match_score < no_tailor_threshold  -> resume_agent
    match_score >= no_tailor_threshold                  -> select_master_resume (no Claude call)

analyze_job makes this routing decision itself via Command(goto=...) rather
than a separate conditional-edge function reading shared state - the
decision is assembled entirely from that node's own local data, so it never
depends on how LangGraph merges state across concurrent Send branches.

discover_jobs also gates on F-1/OPT eligibility before Claude sees a job:
"ineligible" jobs get a terminal status="ineligible" JobAnalysis row with no
ATS call; "eligible" and "unknown" proceed to analyze_job as normal.

master_raw_latex and github_context are loaded once per run (see
run_pipeline()) and threaded through PipelineState - GitHub is never
refetched per job.

Each node opens its own DB session rather than sharing one: Send()-fanned-out
branches run concurrently on separate threads, and a SQLAlchemy Session
isn't thread-safe.
"""

import operator
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import Command, Send
from sqlalchemy.orm import Session

from src.analysis.ats_agent import score_job
from src.db.models import CandidateProfile, Job, JobAnalysis, ResumeDraft, ResumeSelection, SearchConfig
from src.db.session import get_session_factory
from src.discovery.aggregate import discover_candidates
from src.discovery.normalize import (
    CURRENTLY_ELIGIBLE,
    NormalizedJob,
    canonical_job_identity,
    compute_current_eligibility,
    is_fresh_enough,
)
from src.integrations.github import fetch_github_context
from src.reporting.tracker import assign_resume_display_names
from src.resume.pdf_render import PdfRenderError, compile_tex_to_pdf
from src.resume.resume_agent import decide_and_tailor

# ResumeDraft.tailored_latex is a DB text column only, so this is where the
# .tex/.pdf files actually get written. Filenames are deterministic from
# (job_id, draft_id), so no extra DB column tracks the path.
RESUME_ARTIFACTS_DIR = Path(__file__).resolve().parents[2] / "output" / "resumes"


def resume_pdf_path(job_id: int, draft_id: int, filename: str) -> Path:
    """PDF path for one ResumeDraft. `filename` is the user-facing display
    name (AASAV-SUTHAR-<COMPANY>[-N].pdf, see resolve_resume_pdf_filename) -
    callers compute it, this just joins it onto the artifacts directory.
    Internal .tex naming (resume_artifact_path) is unaffected. Reads
    RESUME_ARTIFACTS_DIR fresh each call rather than a frozen constant, so
    tests can redirect it with a simple monkeypatch."""
    return RESUME_ARTIFACTS_DIR / "pdf" / filename


def resolve_resume_pdf_filename(session: Session, job_id: int, draft_id: int) -> str:
    """This draft's PDF filename, computed from the full current set of
    resume_drafts so two resumes at the same company get stable, distinct
    names. Locks the joined rows for the read (with_for_update) so two
    resume_agent_node calls racing on the same company can't pick the same
    ordinal; released at the next commit/rollback."""
    rows = (
        session.query(ResumeDraft.job_id, ResumeDraft.id, Job.company)
        .join(Job, Job.id == ResumeDraft.job_id)
        .with_for_update()
        .all()
    )
    entries = [(j_id, d_id, company) for j_id, d_id, company in rows]
    mapping = assign_resume_display_names(entries)
    return mapping[(job_id, draft_id)]


def resume_artifact_path(job_id: int, draft_id: int) -> Path:
    """The deterministic file path for one ResumeDraft's artifact. Never
    invented per-call - the same (job_id, draft_id) always produces the same
    path, so this is safe to call from persistence code, backfill scripts,
    and the Excel exporter alike without any of them needing to agree on a
    separately-stored path."""
    return RESUME_ARTIFACTS_DIR / f"job_{job_id}_draft_{draft_id}.tex"


class JobResult(TypedDict):
    job_id: int
    match_score: int
    status: str


class PipelineState(TypedDict, total=False):
    profile: dict
    config: dict
    reject_threshold: int
    no_tailor_threshold: int
    master_raw_latex: str
    github_context: list[dict]
    new_jobs: list[dict]
    pending_resume_jobs: list[dict]
    discovery_stats: dict
    results: Annotated[list[JobResult], operator.add]


class JobAnalysisState(TypedDict):
    job: dict
    profile: dict
    reject_threshold: int
    no_tailor_threshold: int
    master_raw_latex: str
    github_context: list[dict]


class ResumeAgentState(TypedDict):
    job: dict
    analysis: dict
    profile: dict
    master_raw_latex: str
    github_context: list[dict]


class SelectMasterState(TypedDict):
    job: dict
    analysis: dict


def _round_robin_select(by_company: dict[str, list[NormalizedJob]], cap: int) -> list[NormalizedJob]:
    """Interleaves across companies (1 job at a time, cycling) instead of draining
    one company's queue before moving to the next - this is what stops a single
    large board (e.g. Stripe's ~380 title-matched postings) from consuming an
    entire run's budget before other configured companies are ever reached."""
    queues = {key: list(jobs) for key, jobs in by_company.items()}
    selected: list[NormalizedJob] = []
    while len(selected) < cap and queues:
        for key in list(queues.keys()):
            if len(selected) >= cap:
                break
            queue = queues[key]
            if queue:
                selected.append(queue.pop(0))
            if not queue:
                del queues[key]
    return selected


def _persist(session: Session, jobs: list[NormalizedJob]) -> list[dict]:
    new_job_dicts = []
    for nj in jobs:
        status, reasons = compute_current_eligibility(nj.title, nj.location, nj.description_raw)
        job = Job(
            **asdict(nj),
            canonical_key=canonical_job_identity(nj.url),
            current_eligibility=status,
            eligibility_reasons=reasons,
        )
        session.add(job)
        session.flush()  # populate job.id
        new_job_dicts.append(
            {
                "id": job.id,
                "title": job.title,
                "company": job.company,
                "description_raw": job.description_raw,
                "work_auth_status": job.work_auth_status,
                "work_auth_reason": job.work_auth_reason,
                "current_eligibility": job.current_eligibility,
                "posted_at": job.posted_at,
            }
        )
    session.commit()
    return new_job_dicts


def _pending_jobs(session: Session, limit: int, posted_within_days: int | None) -> list[dict]:
    """Previously-persisted jobs with no JobAnalysis yet - e.g. left over
    from a run that crashed mid-analysis. Dedup means they'd never be
    treated as "new" again otherwise, so they're re-surfaced here.

    Gated on current_eligibility == CURRENTLY_ELIGIBLE and freshness,
    re-checked every call rather than once at persist time, so a job
    sitting unanalyzed since posting is re-judged against today's cutoff.
    The freshness check runs in SQL so LIMIT lands on the fresh set, and is
    skipped when the gate is disabled.
    """
    query = (
        session.query(Job)
        .outerjoin(JobAnalysis, JobAnalysis.job_id == Job.id)
        .filter(JobAnalysis.id.is_(None), Job.current_eligibility == CURRENTLY_ELIGIBLE)
    )
    if posted_within_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=posted_within_days)
        query = query.filter(Job.posted_at.isnot(None), Job.posted_at >= cutoff)
    rows = query.limit(limit).all()
    return [
        {
            "id": j.id,
            "title": j.title,
            "company": j.company,
            "description_raw": j.description_raw,
            "work_auth_status": j.work_auth_status,
            "work_auth_reason": j.work_auth_reason,
            "current_eligibility": j.current_eligibility,
            "posted_at": j.posted_at,
        }
        for j in rows
    ]


def _pending_resume_jobs(session: Session) -> list[dict]:
    """JobAnalysis rows already scored status="passed" with no resume_selections
    row yet - e.g. left over from a run that crashed between analyze_job and the
    Resume Agent stage. Re-surfaced directly into resume routing WITHOUT
    rescoring: score_job/analyze_job is never re-invoked here, since the existing
    match_score is authoritative and must not be recomputed or overwritten."""
    rows = (
        session.query(Job, JobAnalysis)
        .join(JobAnalysis, JobAnalysis.job_id == Job.id)
        .outerjoin(ResumeSelection, ResumeSelection.job_id == Job.id)
        .filter(JobAnalysis.status == "passed", ResumeSelection.id.is_(None))
        .all()
    )
    return [
        {
            "job": {
                "id": j.id,
                "title": j.title,
                "company": j.company,
                "description_raw": j.description_raw,
            },
            "analysis": {
                "match_score": a.match_score,
                "hard_requirements_met": a.hard_requirements_met,
                "hard_requirements_missing": a.hard_requirements_missing,
                "matched_skills": a.matched_skills,
                "missing_skills": a.missing_skills,
                "reasoning": a.reasoning,
            },
        }
        for j, a in rows
    ]


def _split_ineligible(session: Session, jobs: list[dict]) -> tuple[list[dict], int]:
    """Immigration-ineligible jobs (explicit citizenship/PR/clearance
    requirement - see normalize.classify_work_authorization) skip Claude
    entirely: the candidate can't legally take the role, so an ATS call on
    skill fit would be wasted. Recorded with status="ineligible" and the
    stored reason, so it's visible and gets a terminal JobAnalysis row
    (_pending_jobs won't re-surface it)."""
    needs_analysis = []
    ineligible_count = 0
    for job in jobs:
        if job.get("work_auth_status") == "ineligible":
            session.add(
                JobAnalysis(
                    job_id=job["id"],
                    match_score=None,
                    matched_skills=[],
                    missing_skills=[],
                    reasoning=job.get("work_auth_reason")
                    or "Immigration/work-authorization eligibility check failed.",
                    status="ineligible",
                )
            )
            ineligible_count += 1
        else:
            needs_analysis.append(job)
    if ineligible_count:
        session.commit()
    return needs_analysis, ineligible_count


def discover_jobs(state: PipelineState) -> PipelineState:
    session = get_session_factory()()
    try:
        config = session.query(SearchConfig).first()
        if config is None:
            raise RuntimeError("search_config is empty - run scripts/maintenance/seed_config.py first")

        pending_jobs = _pending_jobs(session, config.max_jobs_per_run, config.posted_within_days)
        remaining_capacity = max(config.max_jobs_per_run - len(pending_jobs), 0)

        new_jobs: list[dict] = []
        discovery_stats: dict = {}
        if remaining_capacity > 0:
            candidates_by_company, discovery_stats = discover_candidates(config)

            # Dedup against the DB before round-robin selection, so a
            # company's round-robin slots aren't wasted on jobs we already
            # have. Two checks: the original (source, source_id) check, plus
            # canonical_key as an additional signal catching the same
            # posting arriving via a different source (see
            # normalize.canonical_job_identity, migration 0010).
            existing_keys = {
                (source, source_id) for source, source_id in session.query(Job.source, Job.source_id).all()
            }
            existing_canonical_keys = {ck for (ck,) in session.query(Job.canonical_key).all()}
            deduped_by_company: dict[str, list[NormalizedJob]] = {}
            duplicates_removed = 0
            cross_source_duplicates_removed = 0
            seen_canonical_this_run: set[str] = set()
            for key, jobs in candidates_by_company.items():
                fresh = []
                for j in jobs:
                    if (j.source, j.source_id) in existing_keys:
                        duplicates_removed += 1
                        continue
                    ck = canonical_job_identity(j.url)
                    if ck in existing_canonical_keys or ck in seen_canonical_this_run:
                        duplicates_removed += 1
                        cross_source_duplicates_removed += 1
                        continue
                    seen_canonical_this_run.add(ck)
                    fresh.append(j)
                if fresh:
                    deduped_by_company[key] = fresh

            selected = _round_robin_select(deduped_by_company, remaining_capacity)
            persisted = _persist(session, selected)
            # Every newly-persisted job is kept regardless of eligibility -
            # only the eligible, fresh subset proceeds to ATS. "now" is
            # computed once so every job this run uses the same instant.
            now = datetime.now(timezone.utc)
            eligible = [j for j in persisted if j["current_eligibility"] == CURRENTLY_ELIGIBLE]
            new_jobs = [j for j in eligible if is_fresh_enough(j["posted_at"], config.posted_within_days, now)]
            discovery_stats["ineligible_or_ambiguous_excluded"] = len(persisted) - len(eligible)
            discovery_stats["stale_or_unknown_date_excluded"] = len(eligible) - len(new_jobs)

            discovery_stats["duplicates_removed"] = duplicates_removed
            discovery_stats["cross_source_duplicates_removed"] = cross_source_duplicates_removed
            discovery_stats["new_jobs_selected"] = len(selected)
            discovery_stats["companies_covered"] = sorted({j.company for j in selected})

        needs_analysis, ineligible_count = _split_ineligible(session, pending_jobs + new_jobs)
        discovery_stats["immigration_ineligible"] = ineligible_count

        pending_resume_jobs = _pending_resume_jobs(session)
        discovery_stats["pending_resume_jobs"] = len(pending_resume_jobs)

        return {
            "new_jobs": needs_analysis,
            "reject_threshold": config.ats_threshold,
            "no_tailor_threshold": config.resume_no_tailor_threshold,
            "pending_resume_jobs": pending_resume_jobs,
            "discovery_stats": discovery_stats,
        }
    finally:
        session.close()


def fan_out_after_discovery(state: PipelineState):
    """Fans newly-discovered/pending-ATS jobs into analyze_job, and separately
    fans crash-recovered pending-resume jobs (already scored, missing only a
    resume_selections row) directly into resume_agent/select_master_resume -
    bypassing analyze_job entirely so their existing match_score is never
    recomputed."""
    sends = [
        Send(
            "analyze_job",
            {
                "job": job,
                "profile": state["profile"],
                "reject_threshold": state["reject_threshold"],
                "no_tailor_threshold": state["no_tailor_threshold"],
                "master_raw_latex": state["master_raw_latex"],
                "github_context": state["github_context"],
            },
        )
        for job in state["new_jobs"]
    ]

    for pending in state.get("pending_resume_jobs", []):
        job, analysis = pending["job"], pending["analysis"]
        if analysis["match_score"] >= state["no_tailor_threshold"]:
            sends.append(Send("select_master_resume", {"job": job, "analysis": analysis}))
        else:
            sends.append(
                Send(
                    "resume_agent",
                    {
                        "job": job,
                        "analysis": analysis,
                        "profile": state["profile"],
                        "master_raw_latex": state["master_raw_latex"],
                        "github_context": state["github_context"],
                    },
                )
            )

    if not sends:
        return END
    return sends


def analyze_job(state: JobAnalysisState) -> Command:
    job = state["job"]
    try:
        result = score_job(job["description_raw"], state["profile"])
    except Exception as exc:
        # Don't let one bad response (e.g. a truncated/malformed model reply) abort
        # every other in-flight analysis in this run - leave this job unanalyzed so
        # _pending_jobs() picks it up and retries it on the next run.
        print(f"  [warn] job_id={job['id']} ({job['company']} - {job['title']}) failed to score: {exc}")
        return Command(goto=END, update={"results": []})

    status = "passed" if result.match_score >= state["reject_threshold"] else "rejected"

    session = get_session_factory()()
    try:
        session.add(
            JobAnalysis(
                job_id=job["id"],
                match_score=result.match_score,
                hard_requirements_met=result.hard_requirements_met,
                hard_requirements_missing=result.hard_requirements_missing,
                matched_skills=result.matched_skills,
                missing_skills=result.missing_skills,
                reasoning=result.reasoning,
                status=status,
            )
        )
        session.commit()
    finally:
        session.close()

    job_result: JobResult = {"job_id": job["id"], "match_score": result.match_score, "status": status}

    if status == "rejected":
        return Command(goto=END, update={"results": [job_result]})

    analysis_payload = {
        "match_score": result.match_score,
        "hard_requirements_met": result.hard_requirements_met,
        "hard_requirements_missing": result.hard_requirements_missing,
        "matched_skills": result.matched_skills,
        "missing_skills": result.missing_skills,
        "reasoning": result.reasoning,
    }

    if result.match_score >= state["no_tailor_threshold"]:
        return Command(
            goto=Send("select_master_resume", {"job": job, "analysis": analysis_payload}),
            update={"results": [job_result]},
        )

    return Command(
        goto=Send(
            "resume_agent",
            {
                "job": job,
                "analysis": analysis_payload,
                "profile": state["profile"],
                "master_raw_latex": state["master_raw_latex"],
                "github_context": state["github_context"],
            },
        ),
        update={"results": [job_result]},
    )


def select_master_resume(state: SelectMasterState) -> PipelineState:
    """Deterministic, no Claude call: match_score >= no_tailor_threshold means
    the master resume is already considered sufficiently strong."""
    job, analysis = state["job"], state["analysis"]
    session = get_session_factory()()
    try:
        session.add(
            ResumeSelection(
                job_id=job["id"],
                resume_source="master",
                tailoring_needed=False,
                tailoring_zone="no_tailor_required",
                reasoning=(
                    f"ATS match_score {analysis['match_score']}% is at or above the "
                    "no-tailoring threshold - master resume already representative."
                ),
                selected_version_id=None,
                github_context_used={},
                github_context_snapshot=[],
                project_selection={},
            )
        )
        session.commit()
    finally:
        session.close()
    return {}


def resume_agent_node(state: ResumeAgentState) -> PipelineState:
    job, analysis = state["job"], state["analysis"]
    try:
        decision = decide_and_tailor(
            job_description=job["description_raw"],
            ats_result=analysis,
            master_raw_latex=state["master_raw_latex"],
            candidate_profile=state["profile"],
            github_context=state["github_context"],
            match_score=analysis["match_score"],
        )
    except Exception as exc:
        # Same resilience pattern as analyze_job: leave this job without a
        # resume_selections row so _pending_resume_jobs() retries it next run,
        # rather than aborting every other in-flight branch.
        print(f"  [warn] job_id={job['id']} ({job.get('company', '?')} - {job.get('title', '?')}) Resume Agent failed: {exc}")
        return {}

    session = get_session_factory()()
    try:
        selected_version_id = None
        github_context_used: dict = {}
        project_selection: dict = {}
        if decision.tailoring_needed:
            draft = ResumeDraft(
                job_id=job["id"],
                version=1,
                tailored_latex=decision.tailored_latex,
                summary_of_changes=decision.summary_of_changes,
            )
            session.add(draft)
            session.flush()  # populate draft.id
            selected_version_id = draft.id
            # Write the artifact file - exact same content as tailored_latex,
            # never regenerated. Best-effort: a filesystem failure here must
            # not lose the already-valid DB row; the file can be rebuilt
            # from it later.
            try:
                RESUME_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
                resume_artifact_path(job["id"], draft.id).write_text(decision.tailored_latex)
            except OSError as exc:
                print(f"  [warn] job_id={job['id']} draft_id={draft.id} failed to write resume artifact file: {exc}")
            # Compile the just-written .tex into a one-page PDF - a pure
            # rendering step, no content change. Best-effort like the .tex
            # write above: a compile failure must never lose the DB row or
            # the .tex.
            try:
                pdf_filename = resolve_resume_pdf_filename(session, job["id"], draft.id)
                compile_tex_to_pdf(
                    resume_artifact_path(job["id"], draft.id), resume_pdf_path(job["id"], draft.id, pdf_filename)
                )
            except PdfRenderError as exc:
                print(f"  [warn] job_id={job['id']} draft_id={draft.id} failed to compile resume PDF: {exc}")
            github_context_used = {"facts_used": decision.github_facts_used}
            project_selection = {
                "selected_projects": decision.selected_projects,
                "removed_or_deemphasized_projects": decision.removed_or_deemphasized_projects,
                "reordered_skills": decision.reordered_skills,
            }

        session.add(
            ResumeSelection(
                job_id=job["id"],
                resume_source="tailored" if decision.tailoring_needed else "master",
                tailoring_needed=decision.tailoring_needed,
                tailoring_zone=decision.zone,
                reasoning=decision.reasoning,
                selected_version_id=selected_version_id,
                github_context_used=github_context_used,
                # Raw context actually supplied to the model, independent of
                # its self-report, so a later audit can check a claimed fact
                # against the real evidence (see _verify_github_facts_used).
                github_context_snapshot=state["github_context"],
                project_selection=project_selection,
            )
        )
        session.commit()
    finally:
        session.close()

    return {}


def build_pipeline():
    graph = StateGraph(PipelineState)
    graph.add_node("discover_jobs", discover_jobs)
    graph.add_node("analyze_job", analyze_job)
    graph.add_node("resume_agent", resume_agent_node)
    graph.add_node("select_master_resume", select_master_resume)
    graph.set_entry_point("discover_jobs")
    graph.add_conditional_edges(
        "discover_jobs",
        fan_out_after_discovery,
        ["analyze_job", "resume_agent", "select_master_resume", END],
    )
    # analyze_job routes itself dynamically via Command(goto=...) - no static
    # edge declared for it; resume_agent/select_master_resume are the current
    # terminal stages (no Application Agent yet), so both edge straight to END.
    graph.add_edge("resume_agent", END)
    graph.add_edge("select_master_resume", END)

    return graph.compile()


def run_pipeline(session: Session) -> dict:
    """Returns the final graph state: {"results": [...], "discovery_stats": {...}}."""
    profile_row = session.query(CandidateProfile).first()
    if profile_row is None:
        raise RuntimeError("candidate_profile is empty - profile extraction hasn't run yet")

    master_raw_latex = profile_row.raw_latex
    # Fetched exactly once per run - never refetched per job (see
    # fan_out_after_discovery/analyze_job, which thread this same value through).
    github_context = fetch_github_context(master_raw_latex)

    app = build_pipeline()
    return app.invoke(
        {
            "profile": profile_row.structured_json,
            "master_raw_latex": master_raw_latex,
            "github_context": github_context,
            "results": [],
        }
    )
