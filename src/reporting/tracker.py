"""Data and pure logic behind the Excel job tracker.

fetch_records() reads the current DB state into plain dicts; everything else
here is deterministic logic (status labels, duplicate-group representative
selection, resume PDF filenames) kept separate from openpyxl so it's easy to
unit test.
"""

import re
from datetime import datetime, timezone

from sqlalchemy import text

from src.db.session import get_session_factory
from src.discovery.normalize import classify_freshness, days_since_posted

ATS_PENDING = "ATS PENDING"
ATS_REJECTED = "ATS REJECTED"
ATS_INELIGIBLE = "ATS INELIGIBLE"
ATS_PASSED_RESUME_PENDING = "ATS PASSED — RESUME PENDING"
RESUME_NOT_REQUIRED = "RESUME NOT REQUIRED"
RESUME_DECLINED = "RESUME DECLINED"
RESUME_COMPLETED = "RESUME COMPLETED"


def processing_status(
    ats_status: str | None,
    resume_source: str | None,
    tailoring_zone: str | None,
    tailoring_needed: bool | None,
) -> str:
    """Maps a job's ATS/resume-selection fields to one human-readable status.

    ats_status is job_analysis.status, or None if it hasn't been scored yet.
    The rest come from resume_selections, or None if that row doesn't exist.
    """
    if ats_status is None:
        return ATS_PENDING
    if ats_status == "rejected":
        return ATS_REJECTED
    if ats_status == "ineligible":
        return ATS_INELIGIBLE
    if resume_source is None:
        return ATS_PASSED_RESUME_PENDING
    if tailoring_zone == "no_tailor_required":
        return RESUME_NOT_REQUIRED
    if tailoring_needed is False:
        return RESUME_DECLINED
    return RESUME_COMPLETED


# Duplicate postings (same canonical_key) need one representative row in the
# report. We used to just pick the lowest job ID, which could hide a real
# tailored resume behind a duplicate that only ever got the master resume
# (see jobs 212/292 and 203/263) - so a tailored draft now outranks a bare
# resume selection, which outranks ATS-only, with lowest ID as the tiebreak.

def representative_sort_key(has_draft: bool, has_selection: bool, has_analysis: bool, job_id: int) -> tuple[int, int]:
    """Lower tuple sorts first, i.e. is the better representative."""
    if has_draft:
        tier = 0
    elif has_selection:
        tier = 1
    elif has_analysis:
        tier = 2
    else:
        tier = 3
    return (tier, job_id)


_TIER_REASON = {
    0: "has tailored resume draft",
    1: "has resume selection",
    2: "has ATS analysis",
    3: "lowest job ID (no downstream data on any side)",
}


def select_representative(records: list[dict]) -> tuple[dict, str]:
    """Picks the representative among 2+ records sharing one canonical_key.

    Each record needs "id", "draft_id", "resume_source", "ats_status".
    Returns (representative_record, reason).
    """
    ranked = sorted(
        records,
        key=lambda r: representative_sort_key(
            r.get("draft_id") is not None, r.get("resume_source") is not None,
            r.get("ats_status") is not None, r["id"],
        ),
    )
    winner = ranked[0]
    tier = representative_sort_key(
        winner.get("draft_id") is not None, winner.get("resume_source") is not None,
        winner.get("ats_status") is not None, winner["id"],
    )[0]
    return winner, _TIER_REASON[tier]


def group_by_canonical_key(records: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for rec in records:
        groups.setdefault(rec["canonical_key"], []).append(rec)
    return groups


def resolve_representatives(records: list[dict]) -> tuple[set[int], dict[str, tuple[int, str]]]:
    """Returns (representative_job_ids, {canonical_key: (representative_id, reason)}).

    Single-record groups (no duplication) just keep their one record, with no
    reason entry.
    """
    groups = group_by_canonical_key(records)
    representative_ids: set[int] = set()
    reasons: dict[str, tuple[int, str]] = {}
    for ck, recs in groups.items():
        if len(recs) == 1:
            representative_ids.add(recs[0]["id"])
            continue
        winner, reason = select_representative(recs)
        representative_ids.add(winner["id"])
        reasons[ck] = (winner["id"], reason)
    return representative_ids, reasons


# User-facing resume PDF filenames: AASAV-SUTHAR-<COMPANY>[-N].pdf. Internal
# artifacts (.tex, DB rows) keep their job_<id>_draft_<id> identity - this is
# purely the display name shown to the candidate and linked from Excel.

CANDIDATE_FILENAME_PREFIX = "AASAV-SUTHAR"

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Z0-9-]+")
_WHITESPACE_RUN = re.compile(r"\s+")
_REPEATED_HYPHENS = re.compile(r"-{2,}")


def normalize_company_for_filename(company: str) -> str:
    """"Palantir Technologies" -> "PALANTIR-TECHNOLOGIES", "Squarespace, Inc." -> "SQUARESPACE-INC"."""
    s = _WHITESPACE_RUN.sub("-", company.strip().upper())
    s = _UNSAFE_FILENAME_CHARS.sub("", s)
    s = _REPEATED_HYPHENS.sub("-", s)
    return s.strip("-")


def resume_display_filename(company: str, ordinal: int) -> str:
    """ordinal 1 has no suffix; ordinal 2+ appends -N, for a second resume at the same company."""
    base = f"{CANDIDATE_FILENAME_PREFIX}-{normalize_company_for_filename(company)}"
    if ordinal > 1:
        base = f"{base}-{ordinal}"
    return f"{base}.pdf"


def assign_resume_display_names(entries: list[tuple[int, int, str]]) -> dict[tuple[int, int], str]:
    """Assigns each (job_id, draft_id) its display filename.

    Pass the FULL current set of (job_id, draft_id, company) entries, not a
    partial one - the ordinal for each company is assigned by sorting
    (job_id, draft_id) ascending, so a partial view would shuffle existing
    filenames as new drafts show up.
    """
    ordered = sorted(entries, key=lambda e: (e[0], e[1]))
    counts: dict[str, int] = {}
    result: dict[tuple[int, int], str] = {}
    for job_id, draft_id, company in ordered:
        key = normalize_company_for_filename(company)
        counts[key] = counts.get(key, 0) + 1
        result[(job_id, draft_id)] = resume_display_filename(company, counts[key])
    return result


def fetch_records():
    """Reads every job (plus its ATS/resume state) from the DB into plain dicts.

    Returns (records, db_counts, posted_within_days). Read-only. Freshness is
    computed here against "now", not stored - a job posted 6 days ago should
    read as stale a day later without anyone re-running a backfill.
    """
    from src.graph.pipeline import resume_artifact_path, resume_pdf_path

    session_factory = get_session_factory()
    with session_factory() as session:
        rows = session.execute(text("""
            SELECT
                j.id, j.company, j.title, j.location, j.remote_type, j.employment_type,
                j.url, j.source, j.source_id, j.canonical_key, j.discovered_at, j.posted_at,
                j.current_eligibility, j.eligibility_reasons, j.description_raw,
                a.match_score, a.status AS ats_status,
                rs.resume_source, rs.tailoring_needed, rs.tailoring_zone,
                rs.reasoning AS rs_reasoning, rs.project_selection,
                rd.id AS draft_id, rd.created_at AS draft_created_at, rd.summary_of_changes
            FROM jobs j
            LEFT JOIN job_analysis a ON a.job_id = j.id
            LEFT JOIN resume_selections rs ON rs.job_id = j.id
            LEFT JOIN resume_drafts rd ON rd.id = rs.selected_version_id
            ORDER BY j.id
        """)).fetchall()
        db_counts = {
            "jobs": session.execute(text("SELECT COUNT(*) FROM jobs")).scalar(),
            "job_analysis": session.execute(text("SELECT COUNT(*) FROM job_analysis")).scalar(),
            "resume_selections": session.execute(text("SELECT COUNT(*) FROM resume_selections")).scalar(),
            "resume_drafts": session.execute(text("SELECT COUNT(*) FROM resume_drafts")).scalar(),
        }
        posted_within_days = session.execute(text("SELECT posted_within_days FROM search_config LIMIT 1")).scalar()

    # Computed once over every draft so two resumes at the same company get
    # stable, non-colliding filenames (see assign_resume_display_names).
    pdf_filename_by_draft = assign_resume_display_names(
        [(r.id, r.draft_id, r.company) for r in rows if r.draft_id is not None]
    )

    records = []
    for r in rows:
        # The .tex stays on disk for reproducibility, but the PDF is the
        # link Excel actually shows - and only once it exists on disk (a
        # compile can fail or still be pending).
        fname = fpath = flink = tex_path = None
        if r.draft_id is not None:
            tex_path = resume_artifact_path(r.id, r.draft_id)
            pdf_filename = pdf_filename_by_draft[(r.id, r.draft_id)]
            p = resume_pdf_path(r.id, r.draft_id, pdf_filename)
            if p.exists():
                fname, fpath, flink = p.name, str(p), f"file://{p}"
        status = processing_status(r.ats_status, r.resume_source, r.tailoring_zone, r.tailoring_needed)
        now = datetime.now(timezone.utc)
        # classify_freshness needs a real window; None means the gate is
        # disabled, which is an operational concern, not a display one - fall
        # back to the documented default so the column stays populated.
        freshness_status = classify_freshness(r.posted_at, posted_within_days or 7, now)
        records.append({
            "id": r.id, "company": r.company, "title": r.title, "location": r.location,
            "remote_type": r.remote_type, "employment_type": r.employment_type, "url": r.url,
            "source": r.source, "source_id": r.source_id, "canonical_key": r.canonical_key,
            "discovered_at": r.discovered_at, "posted_at": r.posted_at,
            "freshness_status": freshness_status, "days_since_posted": days_since_posted(r.posted_at, now),
            "current_eligibility": r.current_eligibility,
            "eligibility_reasons": r.eligibility_reasons or [], "description_raw": r.description_raw,
            "ats_score": r.match_score, "ats_status": r.ats_status,
            "resume_source": r.resume_source, "tailoring_needed": r.tailoring_needed,
            "tailoring_zone": r.tailoring_zone, "draft_id": r.draft_id,
            "draft_created_at": r.draft_created_at, "summary_of_changes": r.summary_of_changes,
            "rs_reasoning": r.rs_reasoning, "project_selection": r.project_selection, "status": status,
            "resume_file_name": fname, "resume_file_path": fpath, "resume_link": flink,
            "resume_tex_path": str(tex_path) if tex_path else None,
        })
    return records, db_counts, posted_within_days
