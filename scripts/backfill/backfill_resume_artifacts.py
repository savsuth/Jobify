"""One-time (and idempotent) backfill: writes a .tex artifact file for
every existing resume_drafts row that doesn't already have one on disk.
Content is exactly the stored tailored_latex - never regenerated, never
altered, no Claude call. Safe to re-run: existing correct files are left
untouched (content re-verified, not blindly overwritten), and
resume_agent_node writes new drafts' artifacts automatically going
forward - this only needs to run again if a file is somehow lost.

Usage: python scripts/backfill/backfill_resume_artifacts.py
"""

from sqlalchemy import text

from src.db.session import get_session_factory
from src.graph.pipeline import RESUME_ARTIFACTS_DIR, resume_artifact_path


def main() -> None:
    session = get_session_factory()()
    try:
        rows = session.execute(text("SELECT id, job_id, tailored_latex FROM resume_drafts ORDER BY id")).fetchall()
    finally:
        session.close()

    RESUME_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    written, already_correct, mismatched_rewritten = 0, 0, 0
    for draft_id, job_id, tailored_latex in rows:
        path = resume_artifact_path(job_id, draft_id)
        if path.exists():
            if path.read_text() == tailored_latex:
                already_correct += 1
                continue
            mismatched_rewritten += 1
        else:
            written += 1
        path.write_text(tailored_latex)

    print(f"resume_drafts rows: {len(rows)}")
    print(f"  already correct on disk: {already_correct}")
    print(f"  newly written: {written}")
    print(f"  mismatched content, rewritten to match DB: {mismatched_rewritten}")

    # Verify: every draft has a file, every file matches DB content exactly.
    all_files = {p.name for p in RESUME_ARTIFACTS_DIR.glob("*.tex")}
    expected_files = {resume_artifact_path(job_id, draft_id).name for draft_id, job_id, _ in rows}
    missing = expected_files - all_files
    orphans = all_files - expected_files
    print(f"Verification: {len(expected_files)} expected, {len(all_files)} on disk, "
          f"{len(missing)} missing, {len(orphans)} orphaned")
    if missing:
        print("  MISSING:", missing)
    if orphans:
        print("  ORPHANED:", orphans)


if __name__ == "__main__":
    main()
