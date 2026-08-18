"""Scores a job description against the candidate's structured profile.

Score/reasoning must be grounded strictly in what's present in the profile -
the agent is explicitly told not to credit the candidate with skills or
experience they haven't actually demonstrated.

match_score remains a single holistic 0-100 LLM judgment call - NOT a computed
or weighted formula, and NOT a literal percentage of requirements matched. The
hard_requirements_met/missing fields make that judgment more observable (which
mandatory requirements drove the number) without turning it into arithmetic.
"""

import json
from dataclasses import dataclass

from anthropic import Anthropic

from src.config import get_settings
from src.llm_json import extract_json_object, require_fields, response_text

SYSTEM_PROMPT = """\
You are an ATS (applicant tracking system) matching agent. Compare a job description \
against a candidate's structured profile and produce a holistic match score - your \
overall judgment call, not a computed or weighted formula.

Rules:
- Only credit the candidate with skills, experience, education, or qualifications that \
  are explicitly present in their profile. Never infer or assume capabilities from \
  adjacent technologies, similar-sounding tools, or general competence - if it isn't \
  stated, it isn't credited.
- First, identify which of the job's requirements are HARD requirements (explicitly \
  mandatory - "required", "must have", a minimum years-of-experience, a required degree \
  or clearance, etc.) versus PREFERRED requirements (explicitly optional - "preferred", \
  "nice to have", "a plus", "bonus"). If the posting doesn't clearly distinguish them, \
  use your judgment about what's plainly core to the role versus supplementary.
- hard_requirements_met: at most 5 hard requirements the candidate's profile actually satisfies.
- hard_requirements_missing: at most 5 hard requirements the candidate's profile does not satisfy.
- matched_skills: at most 6 other requirements/skills (preferred quals, relevant tools, \
  etc.) the profile satisfies - don't repeat items already in hard_requirements_met.
- missing_skills: at most 6 other requirements/skills the profile does not satisfy - \
  don't repeat items already in hard_requirements_missing.
- match_score is 0-100: your holistic judgment of how well the candidate's real \
  background fits this SPECIFIC job's requirements (not how good the candidate is in \
  general). It is not a percentage of requirements matched and not derived from any \
  formula or category weights. A missing hard requirement is far more consequential \
  than a missing preferred one and should weigh heavily in your judgment, materially \
  pulling the score down - but don't treat every requirement as equally important.
- reasoning: 2-4 sentences that reference specific requirements from the job description \
  and specific facts from the candidate's profile. Give a genuinely mandatory requirement \
  more attention than a minor preferred one. Explicitly call out any gap that is clearly \
  disqualifying or highly consequential (especially a missing hard requirement). Never \
  describe the score as mathematically calculated, formula-derived, or weighted by category \
  - it's your holistic judgment.

Respond with ONLY a JSON object, no other text:
{"match_score": int, "hard_requirements_met": [str], "hard_requirements_missing": [str], \
"matched_skills": [str], "missing_skills": [str], "reasoning": str}
"""


@dataclass
class ATSResult:
    match_score: int
    hard_requirements_met: list[str]
    hard_requirements_missing: list[str]
    matched_skills: list[str]
    missing_skills: list[str]
    reasoning: str


def score_job(job_description: str, candidate_profile: dict) -> ATSResult:
    client = Anthropic(api_key=get_settings().anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=2560,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"CANDIDATE PROFILE:\n{json.dumps(candidate_profile, indent=2)}\n\n"
                    f"JOB DESCRIPTION:\n{job_description}"
                ),
            }
        ],
    )
    data = extract_json_object(response_text(response), stop_reason=getattr(response, "stop_reason", None))
    # A live run (2026-08-16) hit a bare KeyError when "reasoning" was absent from an
    # otherwise-valid response - require_fields turns that into one clear, diagnosable
    # error listing every missing required field, without weakening the requirement.
    require_fields(data, ["match_score", "matched_skills", "missing_skills", "reasoning"], context="ATS")
    return ATSResult(
        match_score=data["match_score"],
        # New fields fall back to [] if the model omits them - don't fail the
        # whole job over an enhancement field the way we would for the core ones.
        hard_requirements_met=data.get("hard_requirements_met", []),
        hard_requirements_missing=data.get("hard_requirements_missing", []),
        matched_skills=data["matched_skills"],
        missing_skills=data["missing_skills"],
        reasoning=data["reasoning"],
    )
