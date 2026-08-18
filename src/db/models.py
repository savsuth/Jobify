from datetime import datetime, timezone

from sqlalchemy import JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CandidateProfile(Base):
    """Single-row table: the parsed candidate profile derived from profile/resume.tex."""

    __tablename__ = "candidate_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_latex: Mapped[str]
    raw_latex_hash: Mapped[str]
    structured_json: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SearchConfig(Base):
    """Single-row table seeded from config/preferences.yaml; editable later via dashboard."""

    __tablename__ = "search_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_keywords: Mapped[list] = mapped_column(JSON)
    locations: Mapped[list] = mapped_column(JSON)
    remote_pref: Mapped[str]
    companies: Mapped[dict] = mapped_column(JSON)
    experience_level: Mapped[str]
    technologies: Mapped[list] = mapped_column(JSON)
    # Reject cutoff for the ATS gate; jobs below this never reach the
    # Resume Agent. Was 40 (pass/fail); now 30, since 30-64 routes there.
    ats_threshold: Mapped[int] = mapped_column(default=30)
    # At or above this score, the master resume is used unmodified, no
    # Claude call. Between ats_threshold and this, the Resume Agent decides.
    resume_no_tailor_threshold: Mapped[int] = mapped_column(default=65)
    max_jobs_per_run: Mapped[int] = mapped_column(default=40)
    # A job must be posted within this many days to enter the fresh pool
    # (normalize.is_fresh_enough). NULL disables the gate entirely.
    posted_within_days: Mapped[int | None] = mapped_column(default=7)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_jobs_source_source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str]  # greenhouse | lever | web
    source_id: Mapped[str]
    # Source-independent identity (normalize.canonical_job_identity), an
    # additional signal alongside (source, source_id), never a replacement.
    # Lets discover_jobs() recognize the same posting found via two
    # different sources. Deliberately no unique constraint: real historical
    # duplicate pairs already exist with diverging downstream data on both
    # sides (migration 0010) - merging them is a separate, human decision.
    canonical_key: Mapped[str]
    company: Mapped[str]
    title: Mapped[str]
    location: Mapped[str | None]
    remote_type: Mapped[str | None]
    url: Mapped[str]
    description_raw: Mapped[str]
    employment_type: Mapped[str] = mapped_column(default="unknown")
    location_category: Mapped[str] = mapped_column(default="location_unknown")
    seniority: Mapped[str] = mapped_column(default="unknown")
    posted_at: Mapped[datetime | None]  # from the source, when it provides one - never fabricated
    # F-1/OPT eligibility. "unknown" default reflects rows persisted before
    # this classifier existed - never evaluated, not assumed eligible.
    work_auth_status: Mapped[str] = mapped_column(default="unknown")  # eligible | ineligible | unknown
    work_auth_reason: Mapped[str | None]
    discovered_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Re-evaluation against CURRENT Discovery rules
    # (normalize.compute_current_eligibility) - independent of
    # location_category/seniority above, which reflect whatever filter
    # version was active at persist time and can be stale. This is the
    # gate ATS/Resume processing must respect (migration 0011).
    current_eligibility: Mapped[str]
    eligibility_reasons: Mapped[list] = mapped_column(JSON, default=list)
    eligibility_evaluated_at: Mapped[datetime] = mapped_column(default=utcnow)

    analysis: Mapped["JobAnalysis"] = relationship(back_populates="job", uselist=False)


class JobAnalysis(Base):
    __tablename__ = "job_analysis"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    # Null for status="ineligible": those jobs skip Claude entirely (the
    # candidate can't legally take the role, so scoring skill fit would be
    # wasted spend) rather than getting a real ATS score.
    match_score: Mapped[int | None]
    # Which of the job's mandatory requirements the holistic judgment identified
    # as met/missing - observability into the score, not a separate formula.
    # Empty on historical rows scored before this field existed (not re-scored).
    hard_requirements_met: Mapped[list] = mapped_column(JSON, default=list)
    hard_requirements_missing: Mapped[list] = mapped_column(JSON, default=list)
    matched_skills: Mapped[list] = mapped_column(JSON)
    missing_skills: Mapped[list] = mapped_column(JSON)
    reasoning: Mapped[str]
    status: Mapped[str]  # rejected | passed | ineligible
    analyzed_at: Mapped[datetime] = mapped_column(default=utcnow)

    job: Mapped["Job"] = relationship(back_populates="analysis")


class ResumeDraft(Base):
    """Version history for tailored resumes. Only written when the Resume Agent
    actually tailors a job (score 30-64 and tailoring_needed=True) - a
    no-tailoring outcome never creates a row here (see ResumeSelection)."""

    __tablename__ = "resume_drafts"
    __table_args__ = (UniqueConstraint("job_id", "version", name="uq_resume_drafts_job_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    version: Mapped[int]
    tailored_latex: Mapped[str]
    summary_of_changes: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ResumeSelection(Base):
    """One row per job that reached the Resume Agent stage (score >= ats_threshold):
    which resume (master or a specific tailored version) was selected, and why.
    tailoring_zone records which of the deterministic 30-49/50-64/65+ bands drove
    the decision, for observability into whether the zones behave as intended."""

    __tablename__ = "resume_selections"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    resume_source: Mapped[str]  # master | tailored
    tailoring_needed: Mapped[bool]
    tailoring_zone: Mapped[str]  # active (30-49) | selective (50-64) | no_tailor_required (65+)
    reasoning: Mapped[str]
    # Null when resume_source="master". Points at the ResumeDraft actually used -
    # not necessarily version 1, if this job is ever re-tailored later.
    selected_version_id: Mapped[int | None] = mapped_column(ForeignKey("resume_drafts.id"))
    # Self-reported GitHub facts the model says it used, already verified
    # against tailored_latex (_verify_github_facts_used). Empty for "master".
    github_context_used: Mapped[dict] = mapped_column(JSON, default=dict)
    # Raw GitHub context actually supplied to the model (repo descriptions/
    # languages/README excerpts), independent of its self-report above -
    # lets a later audit check a claim without a live GitHub call. Empty for "master".
    github_context_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    # Structured project-selection audit trail: which pool projects were
    # selected/removed/reordered and why - the model's own structured
    # self-report (resume_agent.ResumeDecision). Empty for "master".
    project_selection: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
