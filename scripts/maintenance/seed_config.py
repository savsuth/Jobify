"""Seed (or update) the single search_config row from config/preferences.yaml."""

from src.config import load_preferences
from src.db.models import SearchConfig
from src.db.session import get_session_factory


def main() -> None:
    """Upserts the single search_config row from config/preferences.yaml.

    search_config is a singleton table (one row total). If it doesn't
    exist yet, creates it from preferences.yaml (falling back to hardcoded
    defaults for ats_threshold/resume_no_tailor_threshold/max_jobs_per_run
    if absent from the YAML). If it already exists, overwrites every field
    with the current preferences.yaml contents - re-run this any time
    preferences.yaml changes, since the pipeline reads search_config,
    never the YAML directly.
    """
    prefs = load_preferences()
    session = get_session_factory()()
    try:
        row = session.query(SearchConfig).first()
        if row is None:
            row = SearchConfig(
                title_keywords=prefs["title_keywords"],
                locations=prefs["locations"],
                remote_pref=prefs["remote_pref"],
                companies=prefs["companies"],
                experience_level=prefs["experience_level"],
                technologies=prefs["technologies"],
                ats_threshold=prefs.get("ats_threshold", 30),
                resume_no_tailor_threshold=prefs.get("resume_no_tailor_threshold", 65),
                max_jobs_per_run=prefs.get("max_jobs_per_run", 40),
                posted_within_days=prefs.get("posted_within_days", 7),
            )
            session.add(row)
            print("Created search_config from preferences.yaml")
        else:
            row.title_keywords = prefs["title_keywords"]
            row.locations = prefs["locations"]
            row.remote_pref = prefs["remote_pref"]
            row.companies = prefs["companies"]
            row.experience_level = prefs["experience_level"]
            row.technologies = prefs["technologies"]
            row.ats_threshold = prefs.get("ats_threshold", 30)
            row.resume_no_tailor_threshold = prefs.get("resume_no_tailor_threshold", 65)
            row.max_jobs_per_run = prefs.get("max_jobs_per_run", 40)
            row.posted_within_days = prefs.get("posted_within_days", 7)
            print("Updated search_config from preferences.yaml")
        session.commit()
    finally:
        session.close()


if __name__ == "__main__":
    main()
