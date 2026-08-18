"""One-time (and idempotent) backfill: compiles a one-page PDF for every
existing resume_drafts row whose .tex artifact exists but has no PDF yet (or
whose PDF is stale relative to the .tex). Compiles output/resumes/*.tex -
never regenerates content, never calls Claude. Safe to re-run: an existing,
already-correct PDF is left untouched.

Usage: python scripts/backfill/backfill_resume_pdfs.py
Prerequisite: scripts/backfill/backfill_resume_artifacts.py (the .tex files must exist first).
"""

from sqlalchemy import text

from src.db.session import get_session_factory
from src.graph.pipeline import resume_artifact_path, resume_pdf_path
from src.reporting.tracker import assign_resume_display_names
from src.resume.pdf_render import PdfRenderError, compile_tex_to_pdf


def main() -> None:
    session = get_session_factory()()
    try:
        rows = session.execute(text(
            "SELECT resume_drafts.id AS draft_id, resume_drafts.job_id AS job_id, jobs.company AS company "
            "FROM resume_drafts JOIN jobs ON jobs.id = resume_drafts.job_id ORDER BY resume_drafts.id"
        )).fetchall()
    finally:
        session.close()

    # Same filename scheme as resolve_resume_pdf_filename() in pipeline.py -
    # computed over the full current set so it stays stable across runs.
    filename_by_draft = assign_resume_display_names([(job_id, draft_id, company) for draft_id, job_id, company in rows])

    attempted, already_ok, compiled, failures = 0, 0, 0, []
    for draft_id, job_id, _company in rows:
        tex_path = resume_artifact_path(job_id, draft_id)
        pdf_path = resume_pdf_path(job_id, draft_id, filename_by_draft[(job_id, draft_id)])

        if not tex_path.exists():
            failures.append((job_id, draft_id, "no .tex artifact found - run backfill_resume_artifacts.py first"))
            continue

        if pdf_path.exists() and pdf_path.stat().st_mtime >= tex_path.stat().st_mtime:
            already_ok += 1
            continue

        attempted += 1
        try:
            compile_tex_to_pdf(tex_path, pdf_path)
            compiled += 1
        except PdfRenderError as exc:
            failures.append((job_id, draft_id, str(exc)[:200]))

    print(f"resume_drafts rows: {len(rows)}")
    print(f"already up to date: {already_ok}")
    print(f"attempted: {attempted}")
    print(f"newly compiled: {compiled}")
    print(f"failures: {len(failures)}")
    for job_id, draft_id, reason in failures:
        print(f"  job {job_id} draft {draft_id}: {reason}")

    # Verify: every draft with a .tex has a corresponding one-page PDF, no orphans.
    pdf_dir = resume_pdf_path(0, 0, "_probe.pdf").parent
    all_pdfs = {p.name for p in pdf_dir.glob("*.pdf")}
    expected_pdfs = {filename_by_draft[(job_id, draft_id)] for draft_id, job_id, _company in rows
                      if resume_artifact_path(job_id, draft_id).exists()}
    missing = expected_pdfs - all_pdfs
    orphans = all_pdfs - expected_pdfs
    print(f"\nVerification: {len(expected_pdfs)} expected, {len(all_pdfs)} on disk, "
          f"{len(missing)} missing, {len(orphans)} orphaned")
    if missing:
        print("  MISSING:", missing)
    if orphans:
        print("  ORPHANED:", orphans)


if __name__ == "__main__":
    main()
