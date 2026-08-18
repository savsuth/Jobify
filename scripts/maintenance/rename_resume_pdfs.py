"""Renames resume PDFs from the internal job_<id>_draft_<id>.pdf naming to
the user-facing AASAV-SUTHAR-<COMPANY>[-N].pdf naming. Idempotent.

Pure rename/copy - no pdflatex, no Claude call, no content change. For
every resume_drafts row: copies the old-named PDF to its new name (via
assign_resume_display_names, the same function resume_agent_node uses),
then verifies the copy is byte-identical and still one page. Old files
move (not delete) into output/resumes/pdf/_pre_rename_backup/ only once
every row has verified.

Does not touch the database.
"""

import hashlib
import shutil
from pathlib import Path

from sqlalchemy import text

from src.db.session import get_session_factory
from src.graph.pipeline import RESUME_ARTIFACTS_DIR, resume_pdf_path
from src.reporting.tracker import assign_resume_display_names
from src.resume.pdf_render import _pdf_page_count

OLD_PDF_DIR = RESUME_ARTIFACTS_DIR / "pdf"
BACKUP_DIR = OLD_PDF_DIR / "_pre_rename_backup"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    session = get_session_factory()()
    try:
        rows = session.execute(text(
            "SELECT resume_drafts.id AS draft_id, resume_drafts.job_id AS job_id, jobs.company AS company "
            "FROM resume_drafts JOIN jobs ON jobs.id = resume_drafts.job_id ORDER BY resume_drafts.id"
        )).fetchall()
    finally:
        session.close()

    filename_by_draft = assign_resume_display_names([(job_id, draft_id, company) for draft_id, job_id, company in rows])

    planned = []  # (draft_id, job_id, company, old_path, new_path)
    skipped_no_old_pdf = []
    for draft_id, job_id, company in rows:
        old_path = OLD_PDF_DIR / f"job_{job_id}_draft_{draft_id}.pdf"
        new_filename = filename_by_draft[(job_id, draft_id)]
        new_path = resume_pdf_path(job_id, draft_id, new_filename)
        if not old_path.exists():
            skipped_no_old_pdf.append((job_id, draft_id))
            continue
        planned.append((draft_id, job_id, company, old_path, new_path))

    print(f"resume_drafts rows: {len(rows)}")
    print(f"old-named PDFs found (to be renamed): {len(planned)}")
    print(f"skipped (no old-named PDF on disk - pending/failed compile): {len(skipped_no_old_pdf)}")
    if skipped_no_old_pdf:
        print("  SKIPPED:", skipped_no_old_pdf)

    # Phase 1: copy + verify every one, without touching any old file yet.
    renamed, failures = [], []
    for draft_id, job_id, company, old_path, new_path in planned:
        old_hash = _sha256(old_path)
        old_pages = _pdf_page_count(old_path)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_path, new_path)
        new_hash = _sha256(new_path)
        new_pages = _pdf_page_count(new_path)
        if new_hash != old_hash:
            failures.append((job_id, draft_id, f"content hash mismatch after copy ({old_path.name} -> {new_path.name})"))
            new_path.unlink(missing_ok=True)
            continue
        if new_pages != 1 or old_pages != 1:
            failures.append((job_id, draft_id, f"page count not exactly 1 (old={old_pages}, new={new_pages})"))
            new_path.unlink(missing_ok=True)
            continue
        renamed.append((draft_id, job_id, company, old_path, new_path))

    print(f"\nCopied + verified (content-identical, one page): {len(renamed)}")
    print(f"Failures (left untouched, old PDF still primary): {len(failures)}")
    for job_id, draft_id, reason in failures:
        print(f"  job {job_id} draft {draft_id}: {reason}")

    if failures:
        print("\nAborting before archiving old files - fix the failures above and re-run.")
        return

    # Phase 2: only now, move (never delete) every successfully-verified old
    # file into a backup subdirectory, so output/resumes/pdf/ contains only
    # the new user-facing names going forward.
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for draft_id, job_id, company, old_path, new_path in renamed:
        shutil.move(str(old_path), str(BACKUP_DIR / old_path.name))

    print(f"\nArchived {len(renamed)} old-named PDF(s) to {BACKUP_DIR}")

    # Final verification pass, reading state back from disk (not trusting
    # this script's own in-memory bookkeeping).
    remaining_old_named = [p.name for p in OLD_PDF_DIR.glob("job_*_draft_*.pdf")]
    missing_new = [(job_id, draft_id) for draft_id, job_id, company, old_path, new_path in renamed if not new_path.exists()]
    print(f"Old-named PDFs remaining in primary pdf/ dir: {len(remaining_old_named)} (must be 0)")
    print(f"New-named PDFs missing after archive step: {len(missing_new)} (must be 0)")
    if remaining_old_named:
        print("  REMAINING OLD-NAMED:", remaining_old_named)
    if missing_new:
        print("  MISSING NEW:", missing_new)


if __name__ == "__main__":
    main()
