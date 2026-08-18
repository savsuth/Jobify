"""Runs one full discovery + ATS analysis cycle and prints a summary.

Usage: python scripts/run_pipeline.py
Requires: DB migrated (alembic upgrade head), search_config seeded
(scripts/maintenance/seed_config.py), and profile/resume.tex filled in with your real resume.
"""

from src.db.session import get_session_factory
from src.graph.pipeline import run_pipeline
from src.profile.extractor import extract_profile


def main() -> None:
    """Runs one end-to-end pipeline cycle and prints a summary to stdout.

    Steps, in order:
    1. extract_profile() - re-parses resume.tex only if its content hash
       changed since the last run (cached otherwise, no Claude call).
    2. run_pipeline() - the full LangGraph cycle: discover_jobs ->
       analyze_job (ATS, fanned out per job) -> resume_agent or
       select_master_resume for passed jobs. Every stage persists to the DB
       as it goes; this function only reads back the final state to print
       a summary.
    3. Prints discovery filter-stage stats, then each analyzed job's ATS
       status/score, highest-first. Resume Agent outcomes are not printed
       here (query resume_selections/resume_drafts directly).
    """
    session = get_session_factory()()
    try:
        print("Extracting candidate profile from profile/resume.tex ...")
        extract_profile(session)

        print("Running discovery + ATS pipeline ...")
        final_state = run_pipeline(session)
        results = final_state.get("results", [])
        stats = final_state.get("discovery_stats", {})

        print()
        print("=== Discovery ===")
        print(f"  raw jobs fetched:        {stats.get('raw_fetched', 0)}")
        print(f"  rejected (non-US):       {stats.get('non_us_rejected', 0)}")
        print(f"  rejected (non-full-time):{stats.get('non_full_time_rejected', 0)}")
        print(f"  rejected (off-target title): {stats.get('title_rejected', 0)}")
        print(f"  rejected (senior/staff/principal/lead): {stats.get('senior_rejected', 0)}")
        print(f"  eligible after filters:  {stats.get('eligible', 0)}")
        print(f"  duplicates removed:      {stats.get('duplicates_removed', 0)}")
        print(f"    (of which cross-source: {stats.get('cross_source_duplicates_removed', 0)})")
        print(f"  new jobs selected:       {stats.get('new_jobs_selected', 0)}")
        print(f"  companies covered:       {stats.get('companies_covered', [])}")
        print(f"  immigration-ineligible (skipped Claude): {stats.get('immigration_ineligible', 0)}")

        if not results:
            print("\nNo new jobs analyzed this run.")
            return

        passed = [r for r in results if r["status"] == "passed"]
        rejected = [r for r in results if r["status"] == "rejected"]

        print(f"\n=== ATS analysis: {len(results)} job(s), {len(passed)} passed, {len(rejected)} rejected ===\n")
        for r in sorted(results, key=lambda r: r["match_score"], reverse=True):
            print(f"  [{r['status']:>8}] {r['match_score']:>3}%  job_id={r['job_id']}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
