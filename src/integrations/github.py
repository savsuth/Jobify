"""Fetches real, current GitHub repository context for projects linked in
the candidate's master resume - supporting evidence, never a replacement
for the resume/profile.

extract_project_repos() finds every github.com/OWNER/REPO link in the
resume's LaTeX (the profile header's bare github.com/OWNER link is
excluded). fetch_repo_context() pulls real facts for one repo -
description, languages, topics, README excerpt - and returns None (never
guesses) for anything missing, private, or unavailable.

fetch_github_context() is the live pipeline's entry point: called once per
run, never refetched per job, scoped to repos already linked in the resume.

list_account_repos() and fetch_repo_root_contents() are broader-discovery
primitives used by build_project_library.py - offline, human-run, never
called from the live pipeline.
"""

import base64
import re
from dataclasses import asdict, dataclass

import httpx

from src.config import get_settings

GITHUB_API_BASE = "https://api.github.com"

_TEXTBF_PATTERN = re.compile(r"\\textbf\{([^{}]+)\}")
# Requires an owner/repo pair (a bare profile link like github.com/savsuth has
# no second path segment and correctly never matches this).
_REPO_LINK_PATTERN = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)[\s}]")


@dataclass
class ProjectRepo:
    name: str | None  # project's display name from the resume, if determinable
    owner: str
    repo: str
    url: str


@dataclass
class RepoContext:
    owner: str
    repo: str
    url: str
    description: str | None
    languages: dict[str, int]  # language -> bytes, straight from GitHub's API
    topics: list[str]
    readme_excerpt: str | None
    stars: int


def extract_project_repos(raw_latex: str) -> list[ProjectRepo]:
    """Scans line by line so a project's \\textbf{Name} only ever associates
    with a repo link on that same line - avoids misattributing a name to an
    unrelated link elsewhere in the document."""
    seen: dict[tuple[str, str], ProjectRepo] = {}
    for line in raw_latex.splitlines():
        name_match = _TEXTBF_PATTERN.search(line)
        name = name_match.group(1).strip() if name_match else None
        for match in _REPO_LINK_PATTERN.finditer(line):
            owner, repo = match.group(1), match.group(2)
            key = (owner.lower(), repo.lower())
            if key in seen:
                continue
            seen[key] = ProjectRepo(name=name, owner=owner, repo=repo, url=f"https://github.com/{owner}/{repo}")
    return list(seen.values())


def _github_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = get_settings().github_token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_repo_context(owner: str, repo: str, client: httpx.Client) -> RepoContext | None:
    """None on 404/private/unavailable - skipped by the caller, never guessed."""
    headers = _github_headers()

    repo_resp = client.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}", headers=headers)
    if repo_resp.status_code != 200:
        return None
    repo_data = repo_resp.json()

    languages: dict[str, int] = {}
    lang_resp = client.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}/languages", headers=headers)
    if lang_resp.status_code == 200:
        languages = lang_resp.json()

    readme_excerpt = None
    readme_resp = client.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}/readme", headers=headers)
    if readme_resp.status_code == 200:
        content_b64 = readme_resp.json().get("content", "")
        try:
            readme_excerpt = base64.b64decode(content_b64).decode("utf-8", errors="ignore").strip()[:1500]
        except (ValueError, UnicodeDecodeError):
            readme_excerpt = None

    return RepoContext(
        owner=owner,
        repo=repo,
        url=repo_data.get("html_url", f"https://github.com/{owner}/{repo}"),
        description=repo_data.get("description"),
        languages=languages,
        topics=repo_data.get("topics", []),
        readme_excerpt=readme_excerpt,
        stars=repo_data.get("stargazers_count", 0),
    )


def list_account_repos(username: str, client: httpx.Client) -> list[dict]:
    """Lists public, non-fork, non-archived repos under a GitHub account -
    the broader-discovery step for the project library (never used by the
    live pipeline, which only looks at repos already linked in the
    resume). Returns raw GitHub API repo objects unmodified; empty list on
    any non-200 response rather than guessing."""
    resp = client.get(
        f"{GITHUB_API_BASE}/users/{username}/repos",
        headers=_github_headers(),
        params={"per_page": 100, "type": "owner"},
    )
    if resp.status_code != 200:
        return []
    return [r for r in resp.json() if not r.get("fork") and not r.get("archived")]


def fetch_repo_root_contents(owner: str, repo: str, client: httpx.Client) -> list[str]:
    """Top-level entry names (files and directories) in a repo's root - a
    cheap, deterministic evidence signal for manifest/config files (pom.xml,
    package.json, Dockerfile, requirements.txt, a tests/ directory, ...)
    without a full source-tree crawl. Empty list on any non-200 response."""
    resp = client.get(f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/", headers=_github_headers())
    if resp.status_code != 200:
        return []
    return [entry["name"] for entry in resp.json() if isinstance(entry, dict) and entry.get("name")]


def fetch_github_context(raw_latex: str, client: httpx.Client | None = None) -> list[dict]:
    """Extracts every project repo linked in the resume and fetches real context
    for each, once. Repos that are missing/private/unavailable are silently
    skipped (fetch_repo_context already logs nothing to guess with)."""
    repos = extract_project_repos(raw_latex)
    if not repos:
        return []

    owns_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        contexts = []
        for project_repo in repos:
            ctx = fetch_repo_context(project_repo.owner, project_repo.repo, client)
            if ctx is None:
                continue
            contexts.append({"project_name": project_repo.name, **asdict(ctx)})
        return contexts
    finally:
        if owns_client:
            client.close()
