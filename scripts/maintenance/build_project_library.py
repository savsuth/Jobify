"""Ingests the candidate's GitHub repositories into the local Candidate
Project Library (data/candidate_projects/) - an offline, human-run step,
not part of the live pipeline.

For each master-resume project, its approved bullets are copied verbatim
(never regenerated). For every other public, non-fork repo under the same
account, real evidence is fetched and - if sufficient - deterministic
draft bullets are generated, always landing in "needs_review": nothing is
ever auto-approved here. Review resume_bullets.tex and, if accurate, flip
metadata.json's "eligibility_state" to "approved" yourself.

Usage: python scripts/maintenance/build_project_library.py
Makes live GitHub API calls (read-only). Does not touch the database, the
master resume, or the LangGraph pipeline.
"""

import httpx

from src.config import RESUME_PATH
from src.integrations.github import fetch_repo_context, fetch_repo_root_contents, list_account_repos
from src.resume.project_library import (
    LIBRARY_DIR,
    RepoEvidence,
    build_github_only_record,
    build_master_resume_record,
    master_resume_projects,
    write_library_index,
    write_project_record,
)
from datetime import datetime, timezone


def main() -> None:
    raw_latex = RESUME_PATH.read_text()
    master_projects = master_resume_projects(raw_latex)
    if not master_projects:
        raise RuntimeError("No projects found in profile/resume.tex - nothing to anchor the account username to.")

    account_username = next((p["owner"] for p in master_projects if p["owner"]), None)
    if not account_username:
        raise RuntimeError("Could not determine a GitHub account username from the master resume's project links.")

    known_repo_keys = {(p["owner"].lower(), p["repo"].lower()) for p in master_projects if p["owner"] and p["repo"]}

    records = [build_master_resume_record(p) for p in master_projects]
    print(f"Master resume projects (approved, bullets preserved verbatim): {len(master_projects)}")
    for p in master_projects:
        print(f"  - {p['name']} ({p['owner']}/{p['repo']})")

    print(f"\nListing public repositories for account '{account_username}' ...")
    with httpx.Client(timeout=30) as client:
        account_repos = list_account_repos(account_username, client)
        print(f"  found {len(account_repos)} public, non-fork, non-archived repositories")

        for repo_data in account_repos:
            key = (account_username.lower(), repo_data["name"].lower())
            if key in known_repo_keys:
                continue  # already ingested as a master-resume project above

            print(f"  fetching evidence for {repo_data['name']} ...")
            ctx = fetch_repo_context(account_username, repo_data["name"], client)
            root_entries = fetch_repo_root_contents(account_username, repo_data["name"], client)

            if ctx is None:
                evidence = RepoEvidence(
                    name=repo_data["name"],
                    owner=account_username,
                    repo=repo_data["name"],
                    url=repo_data.get("html_url", f"https://github.com/{account_username}/{repo_data['name']}"),
                    description=repo_data.get("description"),
                    languages={},
                    topics=[],
                    readme_excerpt=None,
                    root_entries=root_entries,
                    stars=repo_data.get("stargazers_count", 0),
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                evidence = RepoEvidence(
                    name=repo_data["name"],
                    owner=ctx.owner,
                    repo=ctx.repo,
                    url=ctx.url,
                    description=ctx.description,
                    languages=ctx.languages,
                    topics=ctx.topics,
                    readme_excerpt=ctx.readme_excerpt,
                    root_entries=root_entries,
                    stars=ctx.stars,
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                )

            records.append(build_github_only_record(evidence, account_username))

    for record in records:
        write_project_record(record)
    report = write_library_index(records)

    print(f"\nLibrary written to {LIBRARY_DIR}")
    print("\n=== Import report ===")
    print(f"Total repositories inspected: {report['total_repositories_inspected']}")
    print()
    print(f"Approved: {report['approved']}")
    print(f"Needs review: {report['needs_review']}")
    print(f"Excluded: {report['excluded']}")
    print()
    print(f"Resume-ready bullets available: {report['resume_ready_bullets_available']}")
    print(f"Projects requiring manual bullet review: {report['projects_requiring_manual_bullet_review']}")


if __name__ == "__main__":
    main()
