"""Robust text/JSON extraction from Claude responses.

Even when a prompt says "respond with ONLY JSON, no other text", the model
sometimes wraps the answer in a ```json ... ``` fence anyway - extract the
JSON object/array from whatever text came back rather than assuming it's
the entire string. (The greedy first-'{'-to-last-'}' regex already skips
over fence markers on its own, since those contain no brace characters.)

A live production run (2026-08-16) surfaced two distinct real failure
patterns, both handled deliberately rather than left as raw exceptions:

1. "Illegal trailing comma before end of object/array" - a genuine,
   observed Claude JSON quirk. A pure syntax artifact that can never
   change what any field's value says, so it's safe to strip and retry
   exactly once (_parse_with_trailing_comma_retry). This is the ONLY
   normalization this module performs, not a general JSON repair.
2. Genuinely malformed/incomplete JSON (e.g. "Unterminated string") -
   never repaired or guessed at. Raises LLMResponseError with diagnostic
   context (response length, stop_reason, a text snippet), propagating
   like any other exception into the caller's per-job isolation (see
   analyze_job/resume_agent_node in pipeline.py). A missing required
   field is a separate failure mode - see require_fields() - and is
   likewise never silently defaulted.
"""

import json
import re

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


class LLMResponseError(ValueError):
    """A Claude response could not be turned into usable structured data -
    no JSON found, malformed/incomplete JSON, or a required field missing.
    Always a ValueError subclass, so existing except-clause handling (e.g.
    the pipeline's per-job isolation) is unaffected; callers can catch this
    specifically to distinguish "bad LLM response" from other errors."""


def response_text(response) -> str:
    """Concatenates all text blocks in a Messages API response.

    response.content[0] is NOT reliably a text block - e.g. an extended-thinking
    block can come first - so filter by type rather than indexing.
    """
    return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")


def _diagnostic_suffix(text: str, stop_reason: str | None) -> str:
    snippet = text[:200] + ("..." if len(text) > 200 else "")
    return f" [response length={len(text)} chars, stop_reason={stop_reason!r}, response starts: {snippet!r}]"


def _parse_with_trailing_comma_retry(matched_text: str) -> object:
    """Parses matched_text as JSON. On a trailing-comma JSONDecodeError,
    retries once with trailing commas stripped (module docstring, case 1);
    any other error, or a second failure after retry, propagates unchanged
    so the caller can raise a diagnosable LLMResponseError."""
    try:
        return json.loads(matched_text)
    except json.JSONDecodeError as exc:
        if not exc.msg.startswith("Illegal trailing comma"):
            raise
        repaired = _TRAILING_COMMA_RE.sub(r"\1", matched_text)
        try:
            result = json.loads(repaired)
        except json.JSONDecodeError:
            raise exc from None  # repair didn't help - surface the original error
        print("[llm_json] auto-repaired a stray trailing comma in an otherwise-valid Claude JSON response")
        return result


def extract_json_object(text: str, stop_reason: str | None = None) -> dict:
    """stop_reason is optional (pass response.stop_reason for richer
    diagnostics on failure - e.g. "max_tokens" directly confirms output
    truncation); omitting it does not change parsing behavior."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise LLMResponseError(f"No JSON object found in model response.{_diagnostic_suffix(text, stop_reason)}")
    try:
        return _parse_with_trailing_comma_retry(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMResponseError(
            f"Model response contains malformed/incomplete JSON "
            f"({exc.msg} at line {exc.lineno} column {exc.colno})."
            f"{_diagnostic_suffix(text, stop_reason)}"
        ) from exc


def extract_json_array(text: str) -> list:
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        return _parse_with_trailing_comma_retry(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMResponseError(
            f"Model response contains malformed/incomplete JSON array "
            f"({exc.msg} at line {exc.lineno} column {exc.colno})."
        ) from exc


def require_fields(data: dict, fields: list[str], context: str) -> None:
    """Raises one clear LLMResponseError listing every missing required
    field at once, instead of a raw KeyError for whichever field a caller
    happens to access first. A pure presence check - doesn't read,
    validate, or alter values, and doesn't affect fields a caller has left
    optional (those keep using dict.get() with a fallback)."""
    missing = [f for f in fields if f not in data]
    if missing:
        raise LLMResponseError(f"{context} response missing required field(s): {missing}")
