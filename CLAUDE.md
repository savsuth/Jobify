# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Implementation Status

**Built**: Discovery (Greenhouse + Lever public APIs, plus Claude web search) → freshness + eligibility filtering → cross-source dedup → ATS scoring → Resume Agent (tailor-or-use-master-as-is, evidence-bound) → PDF rendering → Excel tracker. See the [README](README.md) for the product-level pipeline description and how to run it.

**Not built**: application submission, human-review UI, the dashboard.

The original pre-implementation brainstorm/spec lives in [docs/VISION.md](docs/VISION.md) for historical context — it describes a fixed 60% threshold and an Application Agent that don't reflect the current, more granular design below. Treat it as a roadmap of ideas (multi-source ingestion, ATS detection, generic career-site crawling, a continuously-updated job index), not a current spec.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"  # setup
alembic upgrade head                          # apply DB migrations
python scripts/maintenance/seed_config.py     # load config/preferences.yaml into search_config
python scripts/run_pipeline.py                # run one discovery + ATS scoring + resume-tailoring cycle
python scripts/reporting/build_job_tracker.py # rebuild output/job_tracker.xlsx from current DB state
uvicorn src.api.main:app --reload             # serve read API (GET /jobs, /jobs/{id})
pytest                                        # run all tests
pytest tests/test_ats_agent.py -k test_score_job_parses_response  # run a single test
```

## Architecture

- **Source of truth for the candidate is `profile/resume.tex`** (LaTeX, not a hand-maintained YAML/JSON profile). `src/profile/extractor.py` calls Claude to parse it into structured JSON, cached in the single-row `candidate_profile` table and re-extracted only when the file's content hash changes.
- **`search_config`** (also single-row) holds job-search preferences plus two ATS thresholds — `ats_threshold` (reject cutoff) and `resume_no_tailor_threshold` (no-tailoring ceiling), currently **30 / 65**; seeded from `config/preferences.yaml` via `scripts/maintenance/seed_config.py`, meant to become dashboard-editable later. Nothing in the pipeline should hardcode either value — both are always read from this table.
- **Discovery** (`src/discovery/`) fans out across three independent sources normalized into a common `NormalizedJob` shape (`normalize.py`) before persisting: `greenhouse.py` and `lever.py` hit per-company public board APIs (company tokens come from `search_config.companies`), and `web_search.py` uses Claude's built-in web search tool to catch postings outside those two platforms. `aggregate.py` fetches + applies deterministic filters per company (US-only via `classify_location`, full-time-only via `classify_employment_type`, precise role matching via `normalize.is_target_role` — a curated include/exclude regex set, not loose keyword overlap) before anything reaches expensive ATS scoring.
- **`search_config.max_jobs_per_run`** caps how many NEW jobs get ATS-scored (i.e. cost real Claude calls) in one pipeline invocation. `src/graph/pipeline.py`'s `_round_robin_select()` interleaves across companies (1 job at a time, cycling) rather than draining one company's queue first — this stops a single large board from consuming an entire run before other configured companies are reached. Company ordering in `preferences.yaml` no longer matters for this reason.
- **The LangGraph pipeline** (`src/graph/pipeline.py`) is the orchestration spine: `discover_jobs` persists new jobs (deduped on `(source, source_id)`, capped at `max_jobs_per_run`), then fans out to a parallel `analyze_job` node per job via LangGraph's `Send` API. Each `analyze_job` call scores the job (`src/analysis/ats_agent.py`) and routes itself via `Command(goto=...)` — not a separate conditional-edge function reading shared state, so the decision never depends on how LangGraph merges state across concurrent `Send` branches:
  - `match_score < ats_threshold` → `rejected`, `END`
  - `ats_threshold <= match_score < resume_no_tailor_threshold` → `resume_agent` node (`src/resume/resume_agent.py`, `decide_and_tailor`)
  - `match_score >= resume_no_tailor_threshold` → `select_master_resume` (no Claude call — master resume used as-is)
  - **Every graph node opens its own DB session** (`get_session_factory()`) rather than sharing one passed into the graph — `Send()`-fanned-out branches run concurrently on separate threads, and a SQLAlchemy `Session` isn't thread-safe.
  - `discover_jobs` also re-surfaces any previously-persisted `Job`/`JobAnalysis` rows left unfinished by a prior run (crash mid-analysis, no resume decision yet) before adding newly-discovered ones — dedup means a job that's already persisted is no longer "new," so without this it would never be picked up again.
  - `analyze_job` catches exceptions from `score_job()` per-job rather than letting one bad response abort the whole run — a failed job just stays unanalyzed and gets retried next run.
  - **F-1/OPT work-authorization eligibility** (`normalize.classify_work_authorization`) is computed at normalize time (same as employment_type/location_category/seniority) and stored on every `Job` row as `work_auth_status` (`eligible` | `ineligible` | `unknown`) + `work_auth_reason`. `discover_jobs` routes `ineligible` jobs straight to a terminal `job_analysis.status="ineligible"` row (no Claude call) while `eligible`/`unknown` jobs proceed to `analyze_job` as normal. Deliberately conservative: only fires on unambiguous requirement language ("must be a U.S. citizen", Green Card, ITAR/"US Person", clearance explicitly tied to citizenship) — plain "authorized to work in the US" is never a trigger, and genuinely ambiguous mentions (bare "security clearance", visa sponsorship language) land in `unknown` rather than being guessed. Known limitation: this can only see what's in the fetched description text — some employers state clearance/citizenship requirements in the application form rather than the job description copy, so `eligible` means "no disqualifying language found," not "confirmed sponsorship-friendly."
- **Resume Agent** (`src/resume/resume_agent.py`, `decide_and_tailor`) makes one decision+generation call: is the master resume already the right fit, or is there a legitimate, evidence-backed improvement? It does not re-score fit — the ATS `match_score`/analysis are read-only context. Tailoring works by **selecting** existing evidence, never by **changing** what it says — `build_project_pool()` computes, once per call in pure Python, the exact set of projects the model may choose from (master resume + live GitHub context via `src/integrations/github.py` + human-approved entries in the Candidate Project Library, `src/resume/project_library.py`); a project outside that pool cannot be selected or described. This is what prevents hallucination: the model is never shown a project it hasn't been pre-approved to use. Followed by deterministic (non-LLM) checks: a LaTeX structural check and a GitHub citation-consistency check.
- **PDF rendering** (`src/resume/pdf_render.py`) compiles the resulting `.tex` to a one-page PDF via `pdflatex` (BasicTeX/MacTeX) — a rendering step only, never a second content pass or Claude call; enforces the page limit with a byte-level page-count check on the compiled PDF.
- **Every analyzed job is persisted regardless of outcome** (`job_analysis` table) — the beginning of the Application Tracker; rejected jobs are kept, not discarded. `scripts/reporting/build_job_tracker.py` rebuilds `output/job_tracker.xlsx` read-only from current DB state (sheets: CURRENT TARGETS, JOBS, REVIEW, DUPLICATES, RESUMES, SUMMARY).
- The ATS and profile-extraction prompts explicitly forbid inferring/fabricating candidate skills not present in the source resume — preserve this constraint if you touch those prompts. Same principle governs the Resume Agent's project-pool approach above.
- **Always parse Claude responses with `src/llm_json.py`** (`response_text()` + `extract_json_object()`/`extract_json_array()`), never `response.content[0].text` directly and never a bare `json.loads()`. Live runs against real job descriptions hit both failure modes this guards against: the model wrapping JSON in a ` ```json ` fence despite "respond with ONLY JSON," and `content[0]` being a non-text block (e.g. extended thinking) rather than the text block.
