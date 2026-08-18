from types import SimpleNamespace

import pytest

from src.llm_json import LLMResponseError, extract_json_array, extract_json_object, require_fields, response_text


def test_extract_json_object_plain():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_strips_markdown_fence():
    text = '```json\n{"a": 1, "b": [1, 2]}\n```'
    assert extract_json_object(text) == {"a": 1, "b": [1, 2]}


def test_extract_json_object_raises_when_missing():
    with pytest.raises(ValueError):
        extract_json_object("no json here")


def test_extract_json_array_strips_markdown_fence():
    text = 'Here you go:\n```json\n[{"a": 1}]\n```'
    assert extract_json_array(text) == [{"a": 1}]


def test_extract_json_array_empty_when_missing():
    assert extract_json_array("no array here") == []


def test_response_text_skips_leading_thinking_block():
    # content[0] can be a ThinkingBlock (no .text) when extended thinking is used -
    # response_text must filter by type rather than indexing content[0] directly.
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="reasoning about it..."),
            SimpleNamespace(type="text", text='{"a": 1}'),
        ]
    )
    assert response_text(response) == '{"a": 1}'


# --- JSON reliability: llm_json.py unit-level coverage --------------------------
#
# ats_agent.py/resume_agent.py have their own end-to-end regression tests for
# the exact live failure shapes observed 2026-08-16; these pin down the shared
# extraction/validation primitives directly.

def test_extract_json_object_repairs_trailing_comma_before_closing_brace():
    # Python's json module reports this exact case as "Illegal trailing comma
    # before end of object" - a real, observed Claude quirk, safe to strip.
    assert extract_json_object('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}


def test_extract_json_object_repairs_trailing_comma_in_nested_array():
    assert extract_json_object('{"a": [1, 2, 3,], "b": 2}') == {"a": [1, 2, 3], "b": 2}


def test_extract_json_array_repairs_trailing_comma():
    assert extract_json_array('[{"a": 1}, {"a": 2},]') == [{"a": 1}, {"a": 2}]


def test_extract_json_object_unterminated_string_raises_llm_response_error():
    # Genuinely incomplete JSON must never be repaired/guessed at - it must
    # fail loudly with diagnostic context, not silently produce a result.
    with pytest.raises(LLMResponseError, match="malformed/incomplete JSON"):
        extract_json_object('{"a": 1, "b": "unterminated}')


def test_extract_json_object_missing_delimiter_raises_llm_response_error():
    with pytest.raises(LLMResponseError, match="malformed/incomplete JSON"):
        extract_json_object('{"a": 1 "b": 2}')


def test_extract_json_object_diagnostic_includes_stop_reason_and_length():
    with pytest.raises(LLMResponseError, match="stop_reason='max_tokens'") as exc_info:
        extract_json_object('{"a": 1, "b": "unterminated}', stop_reason="max_tokens")
    assert "response length=" in str(exc_info.value)


def test_extract_json_object_no_match_raises_llm_response_error():
    # LLMResponseError is a ValueError subclass, so this is still covered by
    # the pre-existing test_extract_json_object_raises_when_missing above -
    # this pins down the more specific type directly.
    with pytest.raises(LLMResponseError):
        extract_json_object("no json here")


def test_extract_json_array_malformed_raises_llm_response_error():
    with pytest.raises(LLMResponseError, match="malformed/incomplete JSON array"):
        extract_json_array('[{"a": "unterminated}]')


def test_require_fields_passes_when_all_present():
    require_fields({"a": 1, "b": 2}, ["a", "b"], context="Test")  # must not raise


def test_require_fields_raises_listing_all_missing_fields():
    with pytest.raises(LLMResponseError, match=r"Test response missing required field\(s\): \['b', 'c'\]"):
        require_fields({"a": 1}, ["a", "b", "c"], context="Test")


def test_require_fields_does_not_validate_field_values():
    # A pure presence check - a present-but-falsy/None value must not raise.
    require_fields({"a": None, "b": 0, "c": ""}, ["a", "b", "c"], context="Test")
