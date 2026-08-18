# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Implementation Status

**Built**: Discovery (Greenhouse + Lever public APIs, plus Claude web search) → freshness + eligibility filtering → cross-source dedup → ATS scoring → Resume Agent (tailor or use the master resume as-is) → PDF rendering → Excel tracker. See the [README](README.md) for the full pipeline and how to run it.

Discovery is intentionally simple - not the full multi-source ingestion architecture described in "Existing Job Scrapers / Job Data Sources to Research" below. That section is a roadmap (ATS detection, generic career-site crawling, a continuously-updated job index) to build if this outgrows the current per-company-board approach.

**Not built**: application submission, human-review UI, the dashboard.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"  # setup
alembic upgrade head              # apply DB migrations
python scripts/maintenance/seed_config.py     # load config/preferences.yaml into search_config
python scripts/run_pipeline.py    # run one discovery + ATS scoring cycle
uvicorn src.api.main:app --reload # serve read API (GET /jobs, /jobs/{id})
pytest                            # run all tests
pytest tests/test_ats_agent.py -k test_score_job_parses_response  # run a single test
```

## Architecture

- **Source of truth for the candidate is `profile/resume.tex`** (LaTeX, not a hand-maintained YAML/JSON profile). `src/profile/extractor.py` calls Claude to parse it into structured JSON, cached in the single-row `candidate_profile` table and re-extracted only when the file's content hash changes. This choice also sets up future resume tailoring to work by editing the LaTeX directly.
- **`search_config`** (also single-row) holds job-search preferences and `ats_threshold` (currently **40%**); seeded from `config/preferences.yaml` via `scripts/maintenance/seed_config.py`, meant to become dashboard-editable later. Nothing in the pipeline should hardcode the threshold — it's always read from this table.
- **Discovery** (`src/discovery/`) fans out across three independent sources that get normalized into a common `NormalizedJob` shape (`normalize.py`) before persisting: `greenhouse.py` and `lever.py` hit per-company public board APIs (company tokens come from `search_config.companies`), and `web_search.py` uses Claude's built-in web search tool to catch postings outside those two platforms. `aggregate.py` fetches + applies deterministic filters per company (US-only via `classify_location`, full-time-only via `classify_employment_type`, and precise role matching via `normalize.is_target_role` — a curated include/exclude regex set, not loose keyword overlap) before anything reaches expensive ATS scoring.
- **`search_config.max_jobs_per_run`** caps how many NEW jobs get ATS-scored (i.e. cost real Claude calls) in one pipeline invocation. `src/graph/pipeline.py`'s `_round_robin_select()` interleaves across companies (1 job at a time, cycling) rather than draining one company's queue first — this is what stops a single large board (e.g. Stripe's ~380 title-matched postings) from consuming an entire run before other configured companies are ever reached. Company ordering in `preferences.yaml` no longer matters for this reason.
- **The LangGraph pipeline** (`src/graph/pipeline.py`) is the orchestration spine: `discover_jobs` persists new jobs (deduped on `(source, source_id)`, capped at `max_jobs_per_run`), then fans out to a parallel `analyze_job` node per job via LangGraph's `Send` API. Each `analyze_job` call scores the job against the candidate profile (`src/analysis/ats_agent.py`) and applies the pass/reject gate itself — there's no downstream node to route to differently yet, so the "conditional edge" from the CLAUDE.md vision is currently just a status field (`job_analysis.status`) rather than literal graph branching. When Resume/Application agents are added, gate the graph properly: route `passed` jobs to those nodes and `rejected` jobs straight to `END`.
  - **Every graph node opens its own DB session** (`get_session_factory()`) rather than sharing one passed into the graph — `Send()`-fanned-out branches run concurrently on separate threads, and a SQLAlchemy `Session` isn't thread-safe.
  - `discover_jobs` also re-surfaces any previously-persisted `Job` rows with no `JobAnalysis` yet (`_pending_jobs()`) before adding newly-discovered ones — otherwise a job that got persisted but never scored (e.g. the run crashed mid-analysis) would never be picked up again, since dedup means it's no longer "new".
  - `analyze_job` catches exceptions from `score_job()` per-job rather than letting one bad response abort the whole run — a failed job just stays unanalyzed and gets retried by `_pending_jobs()` next run.
  - **F-1/OPT work-authorization eligibility** (`normalize.classify_work_authorization`) is computed at normalize time (same as employment_type/location_category/seniority) and stored on every `Job` row as `work_auth_status` (`eligible` | `ineligible` | `unknown`) + `work_auth_reason`. `_split_ineligible()` in `discover_jobs` routes `ineligible` jobs straight to a terminal `job_analysis.status="ineligible"` row (no Claude call — the candidate can't legally take the role) while `eligible`/`unknown` jobs proceed to `analyze_job` as normal. Deliberately conservative: only fires on unambiguous requirement language ("must be a U.S. citizen", Green Card, ITAR/"US Person", clearance explicitly tied to citizenship) — plain "authorized to work in the US" is never a trigger, and genuinely ambiguous mentions (bare "security clearance", visa sponsorship language) land in `unknown` rather than being guessed. Known limitation: this can only see what's actually in the fetched description text — some employers (e.g. defense contractors) state clearance/citizenship requirements in the application form rather than the job description copy, so `eligible` here means "no disqualifying language found," not "confirmed sponsorship-friendly."
- **Every analyzed job is persisted regardless of outcome** (`job_analysis` table) — this is the beginning of the Application Tracker described below; rejected jobs are kept, not discarded.
- The ATS and profile-extraction prompts explicitly forbid inferring/fabricating candidate skills not present in the source resume — preserve this constraint if you touch those prompts.
- **Always parse Claude responses with `src/llm_json.py`** (`response_text()` + `extract_json_object()`/`extract_json_array()`), never `response.content[0].text` directly and never a bare `json.loads()`. In practice, live runs against real job descriptions hit both failure modes this guards against: the model wrapping JSON in a ` ```json ` fence despite "respond with ONLY JSON", and `content[0]` being a non-text block (e.g. extended thinking) rather than the text block.

AI Job Application Multi-Agent System — Project Idea

I want to build a large AI-powered job application system for my own job search.

The core idea is to have multiple specialized AI agents working together to manage different parts of the job-search and application process.

The system would have a central dashboard where I can monitor and control everything.

Core Idea

The overall workflow I have in mind is:

Find Jobs
    ↓
Understand Job Description
    ↓
Determine Job Fit / ATS Match
    ↓
60%+ Match?
   ├── No → Reject / Ignore
   └── Yes
        ↓
   Tailor Resume
        ↓
   Prepare Application
        ↓
   Review / Approve
        ↓
   Apply
        ↓
   Track Application

A job should only move forward if it meets a minimum 60% match threshold.

The 60% threshold would act as the initial filter before spending resources on resume tailoring and applications.

Agents

Job Discovery Agent

An agent responsible for finding job openings across different sources on the internet.

It would continuously look for relevant positions based on my preferences, such as:
	•	Job title
	•	Location
	•	Remote/hybrid/on-site
	•	Companies
	•	Experience level
	•	Technologies
	•	Other job preferences

It would collect and organize relevant job postings.

Job Analysis / ATS Agent

An agent responsible for understanding each job description and comparing it against my complete candidate profile.

It would identify:
	•	Required skills
	•	Preferred skills
	•	Technologies
	•	Programming languages
	•	Experience requirements
	•	Responsibilities
	•	Qualifications
	•	Important keywords
	•	Overall compatibility

It would produce an ATS/job-match score.

The main rule is:

ATS / Match Score >= 60%
        ↓
     Continue

ATS / Match Score < 60%
        ↓
      Ignore

Only jobs meeting or exceeding the 60% threshold should proceed to the next stage.

The 60% threshold should be configurable from the dashboard later rather than permanently hardcoded.

Resume Agent

An agent responsible for tailoring my resume for individual jobs that pass the 60% threshold.

I would have a master candidate profile containing my actual:
	•	Education
	•	Experience
	•	Projects
	•	Skills
	•	Technologies
	•	Achievements
	•	Other relevant information

The Resume Agent would use this information together with the job description to create a job-specific resume.

The important concept is that the system should never fabricate experience or qualifications.

It should only optimize and present information that already exists in my profile.

Application Agent

An agent responsible for preparing and potentially submitting job applications.

Only jobs that have already passed the 60%+ match threshold should reach this stage.

It would understand the application requirements and use my candidate information and the appropriate tailored resume.

There should be a human-in-the-loop, especially before an application is actually submitted.

Application Tracking

The system would maintain a complete history of:
	•	Jobs discovered
	•	ATS/match scores
	•	Jobs rejected because they were below 60%
	•	Jobs that passed the threshold
	•	Resumes generated
	•	Applications prepared
	•	Applications submitted
	•	Interviews
	•	Rejections
	•	Other application activity

Multi-Agent System

I want the agents to work together rather than operate independently.

Conceptually:

                         Job Application System
                                  |
                              LangGraph
                                  |
          ┌───────────────────────┼───────────────────────┐
          |                       |                       |
          ↓                       ↓                       ↓
   Job Discovery Agent     Job Analysis / ATS      Resume Agent
                                  |
                              60%+ Rule
                             /          \
                           NO            YES
                           |              |
                         STOP             ↓
                                    Resume Agent
                                         |
                                         ↓
                                  Application Agent
                                         |
                                         ↓
                                  Application Tracker

I am considering LangGraph as the framework for the agents/workflow and Claude as the underlying AI model.

Personal Candidate Profile

A major part of the system would be maintaining one authoritative profile about me.

The AI should be able to use my complete background when evaluating jobs and tailoring applications.

The profile would contain my real:
	•	Education
	•	Work experience
	•	Projects
	•	Technical skills
	•	Programming languages
	•	Databases
	•	Cloud experience
	•	AI/ML experience
	•	Other relevant experience

This would act as the source of truth for the entire system.

Dashboard

I want the system to have a central dashboard/control center.

The dashboard would allow me to see things such as:

Jobs Found
      ↓
ATS < 60% → Rejected

ATS ≥ 60%
      ↓
Strong Matches
      ↓
Resumes Generated
      ↓
Applications Ready
      ↓
Applications Submitted
      ↓
Interviews
      ↓
Rejections

I would also be able to control the system from the dashboard, including:
	•	Job-search preferences
	•	Minimum ATS/match threshold
	•	Application settings
	•	Approval requirements
	•	Agent activity
	•	Application status

The 60% threshold should be a configurable setting so I can change it later if I decide that 70%, 75%, etc. produces better results.

Overall Vision

The final system I have in mind is essentially:

                  PERSONAL JOB SEARCH AI
                           |
                           ↓
                    Discover Jobs
                           |
                           ↓
                     Analyze JD
                           |
                           ↓
                  Calculate Match/ATS
                           |
                    ┌──────┴──────┐
                    ↓             ↓
                  <60%          ≥60%
                    ↓             ↓
                  Ignore      Tailor Resume
                                  |
                                  ↓
                           Prepare Application
                                  |
                                  ↓
                             Human Review
                                  |
                                  ↓
                                Apply
                                  |
                                  ↓
                              Track

The goal is to create a system that can continuously discover relevant jobs, evaluate them against my background, automatically filter out anything below a 60% match, tailor applications for qualified opportunities, help me apply, and maintain the entire application pipeline in one place.

I want this to be a substantial AI + backend + multi-agent systems project, rather than a simple chatbot or resume generator.

# Existing Job Scrapers / Job Data Sources to Research

I want to research existing job-scraping and job-data platforms as inspiration
for building my own continuously updated job-market index.

## 1. Apify
- Apify platform / Actors
- LinkedIn Jobs scrapers
- Indeed scrapers
- Google Jobs scrapers
- Glassdoor scrapers
- Generic website/job-board scrapers
- Career-site-specific scrapers
- Useful for understanding how scraping can be scheduled and exposed through APIs.

## 2. Bright Data
- Jobs Scraper
- Job datasets
- LinkedIn job data
- Indeed job data
- Glassdoor job data
- Other public job sources
- Useful for understanding large-scale commercial job-data collection.

## 3. Google Jobs / Google for Jobs
- Google Jobs search results
- JobPosting structured data
- Company career pages indexed by Google
- Useful as a job-discovery layer rather than relying on a single job board.

## 4. Greenhouse
- Greenhouse Job Board API
- Public company job boards
- Direct company job postings
- Useful because many companies expose structured job data through Greenhouse.

## 5. Ashby
- Ashby Public Job Posting API
- Company-specific job boards
- Direct company job postings
- Useful as another structured ATS source.

## 6. Lever
- Lever job postings
- Public career/job pages
- Lever API / public job data
- Useful for discovering jobs directly from company career infrastructure.

## 7. Workday
- Workday-powered company career sites
- Public job-search pages
- Useful because many large companies use Workday for recruiting.

## 8. SmartRecruiters
- Public job boards
- Company career pages
- Job posting APIs / structured endpoints where available.

## 9. iCIMS
- Company career sites
- Public job postings
- Enterprise recruiting infrastructure.

## 10. Job aggregators
Research platforms such as:
- Indeed
- LinkedIn Jobs
- ZipRecruiter
- Glassdoor
- Monster
- CareerBuilder
- Dice
- Wellfound
- SimplyHired

These should be considered discovery/data sources rather than the only
source of truth.

## 11. Generic Career-Site Crawling
Research how to automatically:
- Discover `/careers`
- Discover `/jobs`
- Detect job pages
- Detect ATS providers
- Extract JobPosting JSON-LD
- Extract structured job information
- Handle JavaScript-rendered pages
- Use Playwright when necessary
- Respect robots.txt, terms, rate limits, and applicable laws

## 12. Search-Engine-Based Discovery
Research using search engines to discover:
- Company career pages
- Individual job pages
- JobPosting structured data
- New companies not yet present in the database

Example concept:

"software engineer" + "careers" + "Boston"

The search engine would be used for discovery, not as the permanent database.

---

# Main Idea

I do NOT want to simply depend on one commercial scraper.

The goal is to investigate these systems and design a multi-source
job-ingestion architecture:

Company Discovery
        ↓
Career Page Discovery
        ↓
ATS Detection
        ↓
Source-Specific Connector
        ↓
Generic Career-Site Crawler
        ↓
Job Extraction
        ↓
Normalization
        ↓
Deduplication
        ↓
PostgreSQL
        ↓
Continuously Updated Job Index

The objective is to maximize coverage of publicly accessible job postings
rather than claiming that the system can literally retrieve 100% of jobs
on the internet.

The job index should continuously track:
- New jobs
- Updated jobs
- Removed/closed jobs
- Companies
- Job sources
- Source/ATS
- First-seen date
- Last-seen date
- Job URL
- External job ID
- Duplicate relationships

After this ingestion layer, the AI system can use Claude + LangGraph for:
- Job analysis
- Candidate/job matching
- 60% minimum match filtering
- Resume tailoring
- Application preparation
- Human approval
- Application execution