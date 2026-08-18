import json

from src.resume.project_library import (
    ProjectFacts,
    RepoEvidence,
    _extract_readme_highlights,
    approve_project_with_bullets,
    build_github_only_record,
    build_master_resume_record,
    classify_eligibility,
    extract_facts,
    generate_bullets,
    is_selectable,
    rebuild_index_from_disk,
    write_library_index,
    write_project_record,
)

_FORBIDDEN_TERMS = (
    "production",
    "production-ready",
    "scalable",
    "large-scale",
    "high-throughput",
    "customer-facing",
    "enterprise",
    "deployed",
)


def _evidence(**overrides) -> RepoEvidence:
    defaults = dict(
        name="java-mvc-project",
        owner="cand",
        repo="java-mvc-project",
        url="https://github.com/cand/java-mvc-project",
        description="A Java Spring MVC application with REST endpoints and PostgreSQL persistence.",
        languages={"Java": 40000, "HTML": 2000},
        topics=["java", "spring-mvc"],
        readme_excerpt="Implements authentication and request handling across the application layers.",
        root_entries=["pom.xml", "src", "tests", "Dockerfile", "README.md"],
        stars=2,
        fetched_at="2026-08-15T00:00:00+00:00",
    )
    defaults.update(overrides)
    return RepoEvidence(**defaults)


# --- extract_facts (evidence -> derived facts, deterministic, no LLM) -----------

def test_extract_facts_detects_technologies_with_provenance():
    facts = extract_facts(_evidence())
    tech_names = {t["technology"] for t in facts.detected_technologies}
    assert "Spring MVC" in tech_names
    assert "REST" in tech_names
    assert "PostgreSQL" in tech_names
    assert "Authentication" in tech_names
    # each detection records which evidence field it came from
    for entry in facts.detected_technologies:
        assert entry["evidence"] in ("description", "README.md", "topics")


def test_extract_facts_never_infers_a_technology_not_literally_present():
    # Java is the primary language, but "Spring" only gets detected because it
    # literally appears in the text - not inferred from the language alone.
    evidence = _evidence(description="A Java command-line tool.", readme_excerpt=None, topics=[])
    facts = extract_facts(evidence)
    tech_names = {t["technology"] for t in facts.detected_technologies}
    assert "Spring" not in tech_names
    assert "Spring MVC" not in tech_names


def test_extract_facts_primary_language_is_the_largest_by_bytes():
    facts = extract_facts(_evidence(languages={"Java": 100, "HTML": 900}))
    assert facts.primary_language == "HTML"


def test_extract_facts_detects_manifests_and_docker_without_claiming_deployment():
    facts = extract_facts(_evidence())
    assert any("Maven" in m for m in facts.detected_manifests)
    assert facts.has_docker_config is True
    assert facts.has_tests_directory is True
    # The dataclass field name itself makes no deployment claim - it only
    # records that a Dockerfile exists, per the explicit "do not infer
    # production usage merely because deployment files exist" requirement.


def test_extract_facts_no_docker_or_tests_when_absent():
    facts = extract_facts(_evidence(root_entries=["README.md", "src"]))
    assert facts.has_docker_config is False
    assert facts.has_tests_directory is False


# --- generate_bullets (deterministic, template-based - no Claude call) ---------

def test_generate_bullets_transcribes_description():
    evidence = _evidence()
    facts = extract_facts(evidence)
    bullets = generate_bullets(evidence, facts)
    assert any("Built a Java Spring MVC application" in b["text"] for b in bullets)


def test_generate_bullets_every_bullet_has_provenance():
    evidence = _evidence()
    facts = extract_facts(evidence)
    bullets = generate_bullets(evidence, facts)
    assert bullets  # sanity: this fixture should produce bullets
    for bullet in bullets:
        assert bullet["evidence"], f"bullet without provenance: {bullet}"


def test_generate_bullets_lists_only_literally_detected_technologies():
    evidence = _evidence()
    facts = extract_facts(evidence)
    bullets = generate_bullets(evidence, facts)
    tech_bullet = next(b for b in bullets if b["text"].startswith("Used "))
    assert "Spring MVC" in tech_bullet["text"]
    assert "PostgreSQL" in tech_bullet["text"]


def test_generate_bullets_never_introduces_forbidden_maturity_language():
    # No forbidden term appears anywhere in the fixture's evidence text, so
    # none should appear in generated output either - the template has no
    # vocabulary for these words at all.
    evidence = _evidence()
    facts = extract_facts(evidence)
    bullets = generate_bullets(evidence, facts)
    combined = " ".join(b["text"] for b in bullets).lower()
    for term in _FORBIDDEN_TERMS:
        assert term not in combined, f"forbidden term {term!r} leaked into generated bullet: {combined}"


def test_generate_bullets_empty_when_no_description_and_no_technologies():
    evidence = _evidence(description=None, readme_excerpt=None, topics=[], languages={"Python": 10})
    facts = extract_facts(evidence)
    bullets = generate_bullets(evidence, facts)
    assert bullets == []


def test_generate_bullets_deterministic_given_same_input():
    # No randomness/LLM involved - same evidence must always produce the
    # exact same bullets (this is what "canonical bullets" depends on).
    evidence = _evidence()
    facts = extract_facts(evidence)
    assert generate_bullets(evidence, facts) == generate_bullets(evidence, facts)


# --- classify_eligibility: never auto-"approved" for a GitHub-only project ------

def test_classify_eligibility_never_returns_approved():
    state, _ = classify_eligibility(_evidence(), account_username="cand")
    assert state != "approved"
    assert state == "needs_review"


def test_classify_eligibility_excludes_profile_readme_repo():
    evidence = _evidence(name="cand", repo="cand", description=None, readme_excerpt=None, languages={})
    state, reason = classify_eligibility(evidence, account_username="cand")
    assert state == "excluded"
    assert "profile" in reason.lower()


def test_classify_eligibility_excludes_repo_with_no_evidence_at_all():
    evidence = _evidence(description=None, readme_excerpt=None, languages={})
    state, reason = classify_eligibility(evidence, account_username="someone-else")
    assert state == "excluded"
    assert reason


def test_classify_eligibility_needs_review_even_with_strong_evidence():
    state, reason = classify_eligibility(_evidence(), account_username="cand")
    assert state == "needs_review"
    assert reason is None


# --- build_github_only_record: full ingestion of one repo (scenario A/E) -------

def test_build_github_only_record_with_sufficient_evidence_scenario_a_e():
    record = build_github_only_record(_evidence(), account_username="cand")
    assert record.source == "github_only"
    assert record.eligibility_state == "needs_review"
    assert record.resume_bullets_available is True
    assert record.resume_bullets_source == "generated"
    assert record.bullets
    assert record.evidence is not None
    assert record.facts is not None
    # Scenario 13: never selectable despite having bullets (human hasn't approved it).
    assert is_selectable(record) is False


def test_build_github_only_record_insufficient_evidence_scenario_f():
    # Has a language but no description/README/topics - some evidence exists
    # (so it is not "excluded" outright) but not enough to write an honest bullet.
    evidence = _evidence(description=None, readme_excerpt=None, topics=[], languages={"Python": 500})
    record = build_github_only_record(evidence, account_username="cand")
    assert record.eligibility_state == "needs_review"
    assert record.resume_bullets_available is False
    assert record.resume_bullets_source == "none"
    assert record.bullets == []
    assert is_selectable(record) is False


def test_build_github_only_record_excluded_repo_never_selectable_even_if_approved_later():
    evidence = _evidence(name="cand", repo="cand", description=None, readme_excerpt=None, languages={})
    record = build_github_only_record(evidence, account_username="cand")
    assert record.eligibility_state == "excluded"
    assert is_selectable(record) is False
    # Even manually flipping the state to "approved" without real evidence
    # existing is a human error, not a system guarantee - but the point of
    # excluding it here is that ingestion itself never does this automatically.
    record.eligibility_state = "approved"
    assert is_selectable(record) is False  # no bullets exist to approve


def test_approved_github_only_project_with_bullets_becomes_selectable():
    # Demonstrates the mechanism a human approval step would use (project
    # replacement readiness, scenario 10) - not wired into the live Resume
    # Agent yet, but the state machine itself is proven here.
    record = build_github_only_record(_evidence(), account_username="cand")
    assert is_selectable(record) is False  # needs_review by default
    record.eligibility_state = "approved"  # simulates the human review step
    assert is_selectable(record) is True


# --- build_master_resume_record: verbatim preservation -------------------------

def test_build_master_resume_record_preserves_bullets_verbatim():
    project = {
        "name": "Turbovec",
        "owner": "savsuth",
        "repo": "Turbovec",
        "existing_bullets": ["Built a Rust vector search index...", "Implemented a NEON SIMD kernel..."],
    }
    record = build_master_resume_record(project)
    assert record.source == "master_resume"
    assert record.eligibility_state == "approved"
    assert record.resume_bullets_source == "master_resume"
    assert [b["text"] for b in record.bullets] == project["existing_bullets"]
    assert is_selectable(record) is True


def test_build_master_resume_record_is_deterministic():
    project = {"name": "TokenPress", "owner": "savsuth", "repo": "Tokenpress", "existing_bullets": ["A bullet."]}
    r1 = build_master_resume_record(project)
    r2 = build_master_resume_record(project)
    assert r1.bullets == r2.bullets
    assert r1.slug == r2.slug


# --- Filesystem persistence (evidence / facts / bullets kept separate) ---------

def test_write_project_record_keeps_evidence_facts_bullets_in_separate_files(tmp_path):
    record = build_github_only_record(_evidence(), account_username="cand")
    project_dir = write_project_record(record, library_dir=tmp_path)

    assert (project_dir / "metadata.json").exists()
    assert (project_dir / "evidence.json").exists()
    assert (project_dir / "facts.json").exists()
    assert (project_dir / "bullets.json").exists()
    assert (project_dir / "resume_bullets.tex").exists()
    assert (project_dir / "README.md").exists()

    evidence_data = json.loads((project_dir / "evidence.json").read_text())
    facts_data = json.loads((project_dir / "facts.json").read_text())
    bullets_data = json.loads((project_dir / "bullets.json").read_text())
    # Three genuinely distinct payloads, not the same blob duplicated three times.
    assert evidence_data != facts_data
    assert facts_data != bullets_data
    assert "detected_technologies" in facts_data
    assert bullets_data == record.bullets


def test_write_project_record_resume_bullets_tex_has_provenance_comments(tmp_path):
    record = build_github_only_record(_evidence(), account_username="cand")
    project_dir = write_project_record(record, library_dir=tmp_path)
    tex = (project_dir / "resume_bullets.tex").read_text()
    assert "needs_review" in tex
    assert "\\resumeItem{" in tex
    assert "% evidence:" in tex


def test_write_project_record_master_resume_bullets_tex_marked_approved(tmp_path):
    project = {"name": "Turbovec", "owner": "savsuth", "repo": "Turbovec", "existing_bullets": ["A real bullet."]}
    record = build_master_resume_record(project)
    project_dir = write_project_record(record, library_dir=tmp_path)
    tex = (project_dir / "resume_bullets.tex").read_text()
    assert "master_resume" in tex
    assert "A real bullet." in tex
    assert "DRAFT" not in tex


def test_write_library_index_report_counts(tmp_path):
    approved = build_master_resume_record(
        {"name": "Turbovec", "owner": "savsuth", "repo": "Turbovec", "existing_bullets": ["x"]}
    )
    needs_review_with_bullets = build_github_only_record(_evidence(), account_username="cand")
    excluded = build_github_only_record(
        _evidence(name="cand", repo="cand", description=None, readme_excerpt=None, languages={}),
        account_username="cand",
    )
    records = [approved, needs_review_with_bullets, excluded]

    report = write_library_index(records, library_dir=tmp_path)

    assert report["total_repositories_inspected"] == 3
    assert report["approved"] == 1
    assert report["needs_review"] == 1
    assert report["excluded"] == 1
    assert report["resume_ready_bullets_available"] == 2  # approved + the needs_review one with bullets
    assert report["projects_requiring_manual_bullet_review"] == 1  # only the needs_review one with bullets

    index = json.loads((tmp_path / "index.json").read_text())
    assert len(index) == 3
    selectable_flags = {row["name"]: row["selectable"] for row in index}
    assert selectable_flags["Turbovec"] is True
    assert selectable_flags["java-mvc-project"] is False  # needs_review, not yet approved
    assert selectable_flags["cand"] is False  # excluded


# --- approve_project_with_bullets: the human-approval write path ---------------

def test_approve_project_with_bullets_promotes_to_approved(tmp_path):
    record = build_github_only_record(_evidence(), account_username="cand")
    write_project_record(record, library_dir=tmp_path)
    assert is_selectable(record) is False  # starts needs_review

    human_bullets = [
        "Architected a Java Spring MVC application with layered controllers and PostgreSQL persistence.",
        "Validated functionality through 120+ JUnit test cases with CI automated via GitHub Actions.",
    ]
    metadata = approve_project_with_bullets(record.slug, human_bullets, library_dir=tmp_path)

    assert metadata["eligibility_state"] == "approved"
    assert metadata["resume_bullets_source"] == "human_provided"
    assert metadata["resume_bullets_available"] is True


def test_approve_project_with_bullets_overwrites_bullets_json_with_provenance(tmp_path):
    record = build_github_only_record(_evidence(), account_username="cand")
    write_project_record(record, library_dir=tmp_path)

    human_bullets = ["A precise, human-written bullet about the real project."]
    approve_project_with_bullets(record.slug, human_bullets, library_dir=tmp_path)

    bullets = json.loads((tmp_path / record.slug / "bullets.json").read_text())
    assert bullets == [{"text": human_bullets[0], "evidence": ["human_provided"]}]

    tex = (tmp_path / record.slug / "resume_bullets.tex").read_text()
    assert "human_provided" in tex
    assert "eligibility_state=approved" in tex
    assert "DRAFT" not in tex
    assert human_bullets[0] in tex


def test_approve_project_with_bullets_does_not_touch_evidence_or_facts(tmp_path):
    record = build_github_only_record(_evidence(), account_username="cand")
    write_project_record(record, library_dir=tmp_path)
    evidence_before = (tmp_path / record.slug / "evidence.json").read_text()
    facts_before = (tmp_path / record.slug / "facts.json").read_text()

    approve_project_with_bullets(record.slug, ["A human bullet."], library_dir=tmp_path)

    assert (tmp_path / record.slug / "evidence.json").read_text() == evidence_before
    assert (tmp_path / record.slug / "facts.json").read_text() == facts_before


def test_approve_project_with_bullets_raises_for_unknown_slug(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        approve_project_with_bullets("does-not-exist", ["x"], library_dir=tmp_path)


# --- rebuild_index_from_disk ----------------------------------------------------

def test_rebuild_index_from_disk_reflects_approval(tmp_path):
    approved = build_master_resume_record(
        {"name": "Turbovec", "owner": "savsuth", "repo": "Turbovec", "existing_bullets": ["x"]}
    )
    needs_review = build_github_only_record(_evidence(), account_username="cand")
    write_project_record(approved, library_dir=tmp_path)
    write_project_record(needs_review, library_dir=tmp_path)
    write_library_index([approved, needs_review], library_dir=tmp_path)

    report_before = json.loads((tmp_path / "report.json").read_text())
    assert report_before["approved"] == 1
    assert report_before["needs_review"] == 1

    approve_project_with_bullets(needs_review.slug, ["Human-approved bullet."], library_dir=tmp_path)
    report_after = rebuild_index_from_disk(library_dir=tmp_path)

    assert report_after["approved"] == 2
    assert report_after["needs_review"] == 0
    index = json.loads((tmp_path / "index.json").read_text())
    selectable_flags = {row["slug"]: row["selectable"] for row in index}
    assert selectable_flags[needs_review.slug] is True


# --- _extract_readme_highlights / third bullet from README structure -----------

def test_extract_readme_highlights_bulleted_list():
    readme = (
        "# My Project\n\n"
        "## How it works\n"
        "- Fetches data from a public API within a configurable radius.\n"
        "- Retrieves a photo for the selected item from a third-party service.\n"
        "- Sends the data to an AI agent, which returns a short observation.\n"
    )
    highlights = _extract_readme_highlights(readme)
    assert highlights == [
        "Fetches data from a public API within a configurable radius.",
        "Retrieves a photo for the selected item from a third-party service.",
        "Sends the data to an AI agent, which returns a short observation.",
    ]


def test_extract_readme_highlights_numbered_list():
    readme = "I implemented four architectures:\n1. Autoencoder (baseline)\n2. CBDNet\n3. PRIDNet\n4. RIDNet\n"
    highlights = _extract_readme_highlights(readme)
    assert highlights == ["Autoencoder (baseline)", "CBDNet", "PRIDNet"]  # capped at max_items=3


def test_extract_readme_highlights_strips_markdown_emphasis():
    readme = "- **Sentence-BERT** lyric embeddings for semantic similarity\n"
    highlights = _extract_readme_highlights(readme)
    assert highlights == ["Sentence-BERT lyric embeddings for semantic similarity"]


def test_extract_readme_highlights_truncates_long_items_without_inventing_text():
    long_item = "- " + "a" * 200 + " end\n"
    highlights = _extract_readme_highlights(long_item, max_item_length=50)
    assert len(highlights) == 1
    assert highlights[0].endswith("...")
    assert len(highlights[0]) <= 54  # 50 + len("...")
    # Every character before the ellipsis is a literal prefix of the source text.
    assert long_item.strip("- \n").startswith(highlights[0].rstrip("."))


def test_extract_readme_highlights_empty_when_no_list_present():
    assert _extract_readme_highlights("Just a plain paragraph with no lists at all.") == []
    assert _extract_readme_highlights(None) == []
    assert _extract_readme_highlights("") == []


def test_generate_bullets_produces_three_bullets_when_readme_has_a_list():
    # This is the fix for "every project should have three bullets" - when a
    # description, at least one detected technology, and a README list are
    # all present, exactly three distinct, evidence-grounded bullets result.
    evidence = _evidence(
        readme_excerpt=(
            "## Features\n"
            "- Natural-language search\n"
            "- Seed track recommendation\n"
            "- Hybrid scoring: embeddings + metadata\n"
        )
    )
    facts = extract_facts(evidence)
    bullets = generate_bullets(evidence, facts)
    assert len(bullets) == 3
    assert bullets[2]["text"].startswith("Implements:")
    assert bullets[2]["evidence"] == ["README.md"]


def test_generate_bullets_third_bullet_never_invents_beyond_readme_text():
    evidence = _evidence(
        description=None,
        readme_excerpt="- Combines Sentence-BERT embeddings with TF-IDF metadata scoring\n- Optional Spotify re-ranking\n",
        topics=[],
    )
    facts = extract_facts(evidence)
    bullets = generate_bullets(evidence, facts)
    implements_bullet = next(b for b in bullets if b["text"].startswith("Implements:"))
    assert "Combines Sentence-BERT embeddings with TF-IDF metadata scoring" in implements_bullet["text"]
    assert "Optional Spotify re-ranking" in implements_bullet["text"]
    # No forbidden maturity/scope language introduced by the extraction itself.
    combined = " ".join(b["text"] for b in bullets).lower()
    for term in _FORBIDDEN_TERMS:
        assert term not in combined
