"""Parses profile/resume.tex into a structured candidate profile via Claude.

The LaTeX source is the single source of truth. Extraction is idempotent: it
only re-calls the model when the file's content hash has changed since the
last run, and it never invents information not present in the source.
"""

import hashlib

from anthropic import Anthropic
from sqlalchemy.orm import Session

from src.config import RESUME_PATH, get_settings
from src.db.models import CandidateProfile
from src.llm_json import extract_json_object, response_text

EXTRACTION_SYSTEM_PROMPT = """\
You extract a candidate's background from the LaTeX source of their resume into \
structured JSON. Only include information that is explicitly present in the source \
text. Do not infer, embellish, or add skills/experience that aren't stated. If a \
field has no data, use an empty list or empty string.

Respond with ONLY a JSON object with this shape, no other text:
{
  "education": [{"institution": str, "degree": str, "field": str, "graduation": str}],
  "experience": [{"company": str, "title": str, "start": str, "end": str, "bullets": [str]}],
  "projects": [{"name": str, "description": str, "technologies": [str]}],
  "skills": {"languages": [str], "frameworks": [str], "databases": [str], "cloud": [str], "other": [str]},
  "achievements": [str]
}
"""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_profile(session: Session, force: bool = False) -> CandidateProfile:
    raw_latex = RESUME_PATH.read_text()
    latex_hash = _hash(raw_latex)

    existing = session.query(CandidateProfile).first()
    if existing is not None and existing.raw_latex_hash == latex_hash and not force:
        return existing

    client = Anthropic(api_key=get_settings().anthropic_api_key)
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4096,
        system=EXTRACTION_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": raw_latex}],
    )
    structured = extract_json_object(response_text(response))

    if existing is None:
        existing = CandidateProfile(
            raw_latex=raw_latex,
            raw_latex_hash=latex_hash,
            structured_json=structured,
        )
        session.add(existing)
    else:
        existing.raw_latex = raw_latex
        existing.raw_latex_hash = latex_hash
        existing.structured_json = structured

    session.commit()
    return existing
