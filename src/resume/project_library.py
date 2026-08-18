"""Candidate Project Library: a local, deterministic evidence-ingestion layer
that sits between raw GitHub data and the Resume Agent.

The Resume Agent's project pool only ever contains projects already in the
master resume, since its own GitHub discovery is scoped to repos already
linked in resume.tex. Real candidate projects that exist on GitHub but were
never written up as resume bullets are ingested here instead - once,
offline, human-reviewed - rather than asking the Resume Agent to improvise a
description on every job call.

Three deliberately separate layers per project, never collapsed into one
blob (see ProjectRecord/write_project_record):
  1. RepoEvidence - raw fetched facts (description, languages, topics,
     README excerpt, root listing). Nothing derived or interpreted.
  2. ProjectFacts - facts deterministically derived from that evidence
     (technology keywords literally present, manifest/config files found).
  3. bullets - resume-ready text generated from those facts, each with
     explicit provenance (which evidence field supports it).

Eligibility is never auto-"approved" for a GitHub-only project (see
classify_eligibility) - ingestion only assigns "needs_review" or "excluded";
a human promotes to "approved" by editing metadata.json. Existing
master-resume projects are the exception: already human-approved by virtue
of being on the live resume, so ingestion marks them "approved" directly and
preserves their existing bullets verbatim.

Nothing here makes a live Claude call - bullet generation is template-based,
not LLM-based, to avoid asking a model to improvise marketing copy.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

LIBRARY_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "candidate_projects"

# --- LaTeX project-block parsing -------------------------------------------------
# Lives here so both this module and resume_agent.py can share one parser
# without a circular import - resume_agent.py imports this function, never
# the reverse.

_PROJECT_BLOCK_PATTERN = re.compile(
    r"\\resumeProjectHeading(.*?)(?=\\resumeProjectHeading|\\resumeSubHeadingListEnd)", re.DOTALL
)
_PROJECT_NAME_PATTERN = re.compile(r"\\textbf\{([^{}]+)\}")
_PROJECT_BULLET_PATTERN = re.compile(r"\\resumeItem\{(.*?)\}")
_PROJECT_REPO_LINK_PATTERN = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)[\s}]")


def extract_project_blocks(raw_latex: str) -> list[dict]:
    """Deterministically parses \\resumeProjectHeading blocks (works on the
    master resume, and is reused on generated tailored output to check for
    duplicate project names - see resume_agent._latex_structurally_valid).
    Pure text parsing, no Claude call."""
    projects = []
    for block in _PROJECT_BLOCK_PATTERN.findall(raw_latex):
        name_match = _PROJECT_NAME_PATTERN.search(block)
        if not name_match:
            continue
        repo_match = _PROJECT_REPO_LINK_PATTERN.search(block)
        projects.append(
            {
                "name": name_match.group(1).strip(),
                "owner": repo_match.group(1) if repo_match else None,
                "repo": repo_match.group(2) if repo_match else None,
                "existing_bullets": _PROJECT_BULLET_PATTERN.findall(block),
            }
        )
    return projects

# Technology keywords the fact-extractor looks for verbatim
# (case-insensitive, word-boundary) - never inferred from an adjacent
# technology, only recorded if the exact term appears in the evidence.
_TECH_KEYWORDS = [
    "Spring MVC", "Spring Boot", "Spring", "MVC", "REST", "RESTful", "GraphQL", "gRPC",
    "React", "Next.js", "Node.js", "Express", "Django", "Flask", "FastAPI",
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "SQLite",
    "Docker", "Kubernetes", "Socket.io", "WebSocket", "Prisma", "Supabase",
    "OAuth", "JWT", "Authentication", "Observer pattern",
    "Streamlit", "TensorFlow", "PyTorch", "Sentence-BERT", "CNN", "TF-IDF",
    "PyQt6", "PyQt", "Anthropic", "Claude", "Spotify", "Threading",
    "CLI", "GUI",
]
_TECH_KEYWORD_PATTERNS = [(kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)) for kw in _TECH_KEYWORDS]

# Matches a markdown list item - extracts a third "how it works" bullet
# directly from the README's structure. Purely mechanical: never
# summarizes or infers meaning, only truncates long items.
_README_LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.+)$", re.MULTILINE)
_MARKDOWN_EMPHASIS_PATTERN = re.compile(r"[`*_]")

_DOCKER_FILES = {"dockerfile", "docker-compose.yml", "docker-compose.yaml"}
_TEST_DIR_NAMES = {"test", "tests", "__tests__", "spec"}
_BUILD_MANIFESTS = {
    "pom.xml": "Maven build manifest (Java)",
    "build.gradle": "Gradle build manifest (Java/Kotlin)",
    "build.gradle.kts": "Gradle build manifest (Kotlin DSL)",
    "package.json": "npm/Node.js package manifest",
    "requirements.txt": "Python pip requirements",
    "pyproject.toml": "Python packaging manifest",
    "setup.py": "Python packaging script",
    "cargo.toml": "Rust Cargo manifest",
    "go.mod": "Go module manifest",
}

_LEADING_ARTICLE_PATTERN = re.compile(r"^(A|An|The)\s+")


@dataclass
class RepoEvidence:
    """Layer 1: raw fetched facts. Nothing derived or interpreted here."""

    name: str
    owner: str
    repo: str
    url: str
    description: str | None
    languages: dict[str, int]
    topics: list[str]
    readme_excerpt: str | None
    root_entries: list[str]
    stars: int
    fetched_at: str


@dataclass
class ProjectFacts:
    """Layer 2: deterministically derived from RepoEvidence - still no
    invention, only what's literally present is recorded, each with which
    evidence field it came from."""

    primary_language: str | None
    detected_technologies: list[dict]  # [{"technology": str, "evidence": str}]
    detected_manifests: list[str]  # human-readable manifest descriptions
    has_docker_config: bool  # a Dockerfile/docker-compose file exists - NOT proof of production deployment
    has_tests_directory: bool


@dataclass
class ProjectRecord:
    name: str
    slug: str
    source: str  # "master_resume" | "github_only"
    owner: str | None
    repo: str | None
    url: str | None
    eligibility_state: str  # "approved" | "needs_review" | "excluded"
    exclusion_reason: str | None
    resume_bullets_source: str  # "master_resume" | "generated" | "none"
    resume_bullets_available: bool
    evidence: RepoEvidence | None = None
    facts: ProjectFacts | None = None
    bullets: list[dict] = field(default_factory=list)  # [{"text": str, "evidence": [str]}]


def is_selectable(record: ProjectRecord) -> bool:
    """The exact rule from the eligibility-states spec: only an approved
    project WITH available bullets may ever be selected by the Resume Agent.
    needs_review and excluded are never selectable, regardless of whether
    bullets happen to exist in draft form."""
    return record.eligibility_state == "approved" and record.resume_bullets_available


# --- Layer 2: deterministic fact extraction --------------------------------------


def extract_facts(evidence: RepoEvidence) -> ProjectFacts:
    """Pure function, no I/O: derives ProjectFacts from already-fetched
    RepoEvidence. Every detected technology records which evidence field it
    was literally found in - nothing is inferred from an adjacent/related
    technology (e.g. "Spring" is never assumed just because the primary
    language is Java)."""
    primary_language = max(evidence.languages, key=evidence.languages.get) if evidence.languages else None

    detected: list[dict] = []
    seen_tech = set()
    text_sources = [
        ("description", evidence.description or ""),
        ("README.md", evidence.readme_excerpt or ""),
        ("topics", " ".join(evidence.topics)),
    ]
    for source_name, text in text_sources:
        for tech, pattern in _TECH_KEYWORD_PATTERNS:
            if tech.lower() in seen_tech:
                continue
            if pattern.search(text):
                detected.append({"technology": tech, "evidence": source_name})
                seen_tech.add(tech.lower())

    root_lower = {entry.lower() for entry in evidence.root_entries}
    manifests = [desc for fname, desc in _BUILD_MANIFESTS.items() if fname in root_lower]
    has_docker = bool(root_lower & _DOCKER_FILES)
    has_tests = bool(root_lower & _TEST_DIR_NAMES)

    return ProjectFacts(
        primary_language=primary_language,
        detected_technologies=detected,
        detected_manifests=manifests,
        has_docker_config=has_docker,
        has_tests_directory=has_tests,
    )


# --- Layer 3: deterministic (template-based, no Claude call) bullet generation ---


def _bullet_from_description(description: str) -> dict:
    """Transcribes the repo's own description into resume-bullet form -
    deliberately mechanical rather than "smoothed over" marketing prose, per
    the explicit "evidence extraction, not marketing copy" requirement. Only
    normalizes a leading article; never rewrites, embellishes, or infers
    beyond the literal text."""
    match = _LEADING_ARTICLE_PATTERN.match(description)
    if match:
        article = match.group(1).lower()
        rest = description[match.end() :]
        text = f"Built {article} {rest}"
    else:
        text = f"Built: {description}"
    return {"text": text, "evidence": ["description"]}


def _extract_readme_highlights(readme_excerpt: str | None, max_items: int = 3, max_item_length: int = 90) -> list[str]:
    """Pulls markdown list items (bulleted or numbered) directly out of the
    README, in document order - purely mechanical extraction of text that's
    already there, not summarization. Strips markdown emphasis markers
    (`/*/_) and truncates an overlong item at a word boundary; never expands,
    rewords, or infers beyond what the item's own text says."""
    if not readme_excerpt:
        return []
    items: list[str] = []
    for match in _README_LIST_ITEM_PATTERN.finditer(readme_excerpt):
        text = _MARKDOWN_EMPHASIS_PATTERN.sub("", match.group(1)).strip()
        if not text:
            continue
        if len(text) > max_item_length:
            text = text[:max_item_length].rsplit(" ", 1)[0] + "..."
        items.append(text)
        if len(items) >= max_items:
            break
    return items


def generate_bullets(evidence: RepoEvidence, facts: ProjectFacts) -> list[dict]:
    """Deterministic, template-based - never a Claude call. Each bullet is
    either a normalized transcription of the repo's description, a plain
    listing of detected technologies, or a direct extraction of
    README-listed steps/features, each carrying which evidence field
    supports it. Maturity words ("production", "scalable", "deployed", ...)
    are not template vocabulary, so they cannot appear in the output."""
    bullets: list[dict] = []

    if evidence.description and len(evidence.description.strip()) >= 15:
        bullets.append(_bullet_from_description(evidence.description.strip()))

    if facts.detected_technologies:
        tech_names = [t["technology"] for t in facts.detected_technologies]
        tech_sources = sorted({t["evidence"] for t in facts.detected_technologies})
        bullets.append(
            {
                "text": f"Used {', '.join(tech_names)} in the implementation.",
                "evidence": tech_sources,
            }
        )

    highlights = _extract_readme_highlights(evidence.readme_excerpt)
    if highlights:
        joined = "; ".join(highlights)
        suffix = "" if joined.endswith("...") else "."  # avoid a redundant "...." artifact
        bullets.append(
            {
                "text": f"Implements: {joined}{suffix}",
                "evidence": ["README.md"],
            }
        )

    return bullets


# --- Eligibility classification --------------------------------------------------


def classify_eligibility(evidence: RepoEvidence, account_username: str) -> tuple[str, str | None]:
    """Returns (state, exclusion_reason). Never returns "approved" - that's
    a human decision made via metadata.json. Only "excluded" (transparently
    not a project: the account's own profile-README repo, or no evidence at
    all) or "needs_review" are assigned automatically, regardless of
    evidence strength."""
    if evidence.repo.strip().lower() == account_username.strip().lower():
        return "excluded", "This is the account's GitHub profile README repository, not a project."
    if not evidence.description and not evidence.readme_excerpt and not evidence.languages:
        return "excluded", "No description, README, or language data available - no evidence to work from."
    return "needs_review", None


# --- Orchestration: one repo -> one ProjectRecord (pure, given evidence) --------


def build_master_resume_record(project: dict) -> ProjectRecord:
    """A project already in the master resume - already human-approved by
    virtue of being on the live resume. Bullets are copied verbatim, never
    regenerated."""
    slug = _slugify(project["name"])
    return ProjectRecord(
        name=project["name"],
        slug=slug,
        source="master_resume",
        owner=project["owner"],
        repo=project["repo"],
        url=f"https://github.com/{project['owner']}/{project['repo']}" if project["owner"] else None,
        eligibility_state="approved",
        exclusion_reason=None,
        resume_bullets_source="master_resume",
        resume_bullets_available=True,
        evidence=None,
        facts=None,
        bullets=[{"text": b, "evidence": ["profile/resume.tex"]} for b in project["existing_bullets"]],
    )


def build_github_only_record(evidence: RepoEvidence, account_username: str) -> ProjectRecord:
    """A repo not currently in the master resume - the new capability this
    module adds. Evidence -> facts -> bullets, each layer stored separately;
    eligibility never auto-approved (see classify_eligibility)."""
    state, reason = classify_eligibility(evidence, account_username)
    slug = _slugify(evidence.name)

    if state == "excluded":
        return ProjectRecord(
            name=evidence.name,
            slug=slug,
            source="github_only",
            owner=evidence.owner,
            repo=evidence.repo,
            url=evidence.url,
            eligibility_state="excluded",
            exclusion_reason=reason,
            resume_bullets_source="none",
            resume_bullets_available=False,
            evidence=evidence,
            facts=None,
            bullets=[],
        )

    facts = extract_facts(evidence)
    bullets = generate_bullets(evidence, facts)
    bullets_available = bool(bullets)

    return ProjectRecord(
        name=evidence.name,
        slug=slug,
        source="github_only",
        owner=evidence.owner,
        repo=evidence.repo,
        url=evidence.url,
        eligibility_state="needs_review",
        exclusion_reason=None,
        resume_bullets_source="generated" if bullets_available else "none",
        resume_bullets_available=bullets_available,
        evidence=evidence,
        facts=facts,
        bullets=bullets,
    )


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


# --- Filesystem persistence -------------------------------------------------------


def _resume_bullets_tex(record: ProjectRecord) -> str:
    header = (
        f"% {record.name} - resume_bullets_source={record.resume_bullets_source}\n"
        f"% eligibility_state={record.eligibility_state}"
        + (" (DRAFT - human review required before use)" if record.eligibility_state != "approved" else "")
        + "\n"
    )
    if not record.bullets:
        return header + "% No bullets available - insufficient evidence.\n"
    lines = [header]
    for bullet in record.bullets:
        lines.append(f"\\resumeItem{{{bullet['text']}}}  % evidence: {', '.join(bullet['evidence'])}\n")
    return "".join(lines)


def write_project_record(record: ProjectRecord, library_dir: Path = LIBRARY_DIR) -> Path:
    """Writes one project's five files, keeping evidence/facts/bullets in
    separate files by design (see module docstring) rather than one blob."""
    project_dir = library_dir / record.slug
    project_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "name": record.name,
        "slug": record.slug,
        "source": record.source,
        "owner": record.owner,
        "repo": record.repo,
        "url": record.url,
        "eligibility_state": record.eligibility_state,
        "exclusion_reason": record.exclusion_reason,
        "resume_bullets_source": record.resume_bullets_source,
        "resume_bullets_available": record.resume_bullets_available,
        # Duplicated from evidence.json so the Resume Agent can build its
        # pool from metadata.json + bullets.json alone, without reading
        # evidence.json/README.md at prompt-build time.
        "description": record.evidence.description if record.evidence else None,
        "languages": list(record.evidence.languages.keys()) if record.evidence else [],
        "topics": record.evidence.topics if record.evidence else [],
        "stars": record.evidence.stars if record.evidence else None,
    }
    (project_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

    if record.evidence:
        (project_dir / "evidence.json").write_text(json.dumps(asdict(record.evidence), indent=2))
        (project_dir / "README.md").write_text(record.evidence.readme_excerpt or "(no README available)")

    if record.facts:
        (project_dir / "facts.json").write_text(json.dumps(asdict(record.facts), indent=2))

    (project_dir / "bullets.json").write_text(json.dumps(record.bullets, indent=2))
    (project_dir / "resume_bullets.tex").write_text(_resume_bullets_tex(record))

    return project_dir


def write_library_index(records: list[ProjectRecord], library_dir: Path = LIBRARY_DIR) -> dict:
    """Writes index.json (one row per project, for scanning without opening
    every directory) and returns the summary report dict."""
    index = [
        {
            "name": r.name,
            "slug": r.slug,
            "source": r.source,
            "eligibility_state": r.eligibility_state,
            "resume_bullets_available": r.resume_bullets_available,
            "selectable": is_selectable(r),
        }
        for r in records
    ]
    library_dir.mkdir(parents=True, exist_ok=True)
    (library_dir / "index.json").write_text(json.dumps(index, indent=2))

    report = {
        "total_repositories_inspected": len(records),
        "approved": sum(1 for r in records if r.eligibility_state == "approved"),
        "needs_review": sum(1 for r in records if r.eligibility_state == "needs_review"),
        "excluded": sum(1 for r in records if r.eligibility_state == "excluded"),
        "resume_ready_bullets_available": sum(1 for r in records if r.resume_bullets_available),
        "projects_requiring_manual_bullet_review": sum(
            1 for r in records if r.eligibility_state == "needs_review" and r.resume_bullets_available
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (library_dir / "report.json").write_text(json.dumps(report, indent=2))
    return report


def approve_project_with_bullets(slug: str, bullet_texts: list[str], library_dir: Path = LIBRARY_DIR) -> dict:
    """The human-approval write path: overwrites a project's bullets with
    human-provided text and promotes it to eligibility_state="approved". Not
    re-verified against evidence.json the way generate_bullets() output is -
    a human asserting a fact about their own work differs fundamentally from
    a model inferring one. The only way a github_only project becomes
    "approved"; leaves evidence.json/facts.json untouched. Returns the
    updated metadata dict."""
    project_dir = library_dir / slug
    metadata_path = project_dir / "metadata.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"No project record found for slug {slug!r} in {library_dir}")
    metadata = json.loads(metadata_path.read_text())

    bullets = [{"text": text, "evidence": ["human_provided"]} for text in bullet_texts]
    metadata["resume_bullets_source"] = "human_provided"
    metadata["resume_bullets_available"] = True
    metadata["eligibility_state"] = "approved"
    metadata["exclusion_reason"] = None
    metadata_path.write_text(json.dumps(metadata, indent=2))
    (project_dir / "bullets.json").write_text(json.dumps(bullets, indent=2))

    header = (
        f"% {metadata['name']} - resume_bullets_source=human_provided\n"
        f"% eligibility_state=approved (human-reviewed and approved)\n"
    )
    lines = [header] + [f"\\resumeItem{{{b['text']}}}  % evidence: human_provided\n" for b in bullets]
    (project_dir / "resume_bullets.tex").write_text("".join(lines))

    return metadata


def rebuild_index_from_disk(library_dir: Path = LIBRARY_DIR) -> dict:
    """Regenerates index.json/report.json by reading every project's
    metadata.json off disk - used after a local edit (e.g.
    approve_project_with_bullets) so the index reflects reality without
    re-running the live GitHub ingestion. Returns the report dict."""
    rows = []
    for metadata_path in sorted(library_dir.glob("*/metadata.json")):
        rows.append(json.loads(metadata_path.read_text()))

    index = [
        {
            "name": m["name"],
            "slug": m["slug"],
            "source": m["source"],
            "eligibility_state": m["eligibility_state"],
            "resume_bullets_available": m["resume_bullets_available"],
            "selectable": m["eligibility_state"] == "approved" and m["resume_bullets_available"],
        }
        for m in rows
    ]
    (library_dir / "index.json").write_text(json.dumps(index, indent=2))

    report = {
        "total_repositories_inspected": len(rows),
        "approved": sum(1 for m in rows if m["eligibility_state"] == "approved"),
        "needs_review": sum(1 for m in rows if m["eligibility_state"] == "needs_review"),
        "excluded": sum(1 for m in rows if m["eligibility_state"] == "excluded"),
        "resume_ready_bullets_available": sum(1 for m in rows if m["resume_bullets_available"]),
        "projects_requiring_manual_bullet_review": sum(
            1 for m in rows if m["eligibility_state"] == "needs_review" and m["resume_bullets_available"]
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (library_dir / "report.json").write_text(json.dumps(report, indent=2))
    return report


def master_resume_projects(master_raw_latex: str) -> list[dict]:
    """Named entry point for scripts/maintenance/build_project_library.py - a thin,
    readable alias for extract_project_blocks() at the one ingestion call site
    that means specifically "the master resume's projects"."""
    return extract_project_blocks(master_raw_latex)


# --- Resume Agent integration: read-only, filesystem-only loader ---------------


def load_approved_library_projects(library_dir: Path = LIBRARY_DIR) -> list[dict]:
    """Loads only approved, bullet-ready projects from the local Candidate
    Project Library for the Resume Agent's project pool - read-only,
    filesystem-only, no live GitHub/Claude call.

    Reads index.json once, then only metadata.json/bullets.json per
    approved project - never evidence.json or README.md, so the prompt
    never gets padded with full repository text.

    index.json's "selectable" field (eligibility_state=="approved" and
    resume_bullets_available) is the single source of truth for this
    filter - never re-derived here, so there is exactly one place that
    decides what counts as selectable.

    Read-only at tailoring time; library maintenance (ingestion, approval)
    is a separate offline operation
    (scripts/maintenance/build_project_library.py,
    approve_project_with_bullets())."""
    index_path = library_dir / "index.json"
    if not index_path.exists():
        return []
    index = json.loads(index_path.read_text())

    projects = []
    for row in index:
        if not row.get("selectable"):
            continue
        project_dir = library_dir / row["slug"]
        metadata_path = project_dir / "metadata.json"
        bullets_path = project_dir / "bullets.json"
        if not metadata_path.exists() or not bullets_path.exists():
            continue  # inconsistent library state - skip rather than guess
        metadata = json.loads(metadata_path.read_text())
        bullets = json.loads(bullets_path.read_text())
        if not bullets:
            continue  # defensive re-check: never trust "selectable" blindly

        projects.append(
            {
                "name": metadata["name"],
                "source": "candidate_project_library",
                "owner": metadata.get("owner"),
                "repo": metadata.get("repo"),
                "existing_bullets": [b["text"] for b in bullets],
                "description": metadata.get("description"),
                "languages": metadata.get("languages", []),
                "topics": metadata.get("topics", []),
            }
        )
    return projects
