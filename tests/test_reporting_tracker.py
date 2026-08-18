"""Tests for src/reporting/tracker.py - the Excel job tracker's pure,
deterministic logic (processing status mapping, duplicate-group representative
selection). Extracted from a one-off script during the 2026-08-17
historical-cleanup task specifically so the representative-selection fix
(confirmed bug: lowest-job-ID tie-break hid an available tailored resume -
see job pairs 212/292 and 203/263) has real regression coverage.
"""

from src.reporting.tracker import (
    ATS_INELIGIBLE,
    ATS_PASSED_RESUME_PENDING,
    ATS_PENDING,
    ATS_REJECTED,
    RESUME_COMPLETED,
    RESUME_DECLINED,
    RESUME_NOT_REQUIRED,
    assign_resume_display_names,
    group_by_canonical_key,
    normalize_company_for_filename,
    processing_status,
    resolve_representatives,
    resume_display_filename,
    select_representative,
)


# --- processing_status ----------------------------------------------------------

def test_status_no_job_analysis_is_ats_pending():
    assert processing_status(None, None, None, None) == ATS_PENDING


def test_status_rejected():
    assert processing_status("rejected", None, None, None) == ATS_REJECTED


def test_status_ineligible():
    assert processing_status("ineligible", None, None, None) == ATS_INELIGIBLE


def test_status_passed_no_resume_selection_yet():
    assert processing_status("passed", None, None, None) == ATS_PASSED_RESUME_PENDING


def test_status_passed_no_tailor_required_zone():
    assert processing_status("passed", "master", "no_tailor_required", False) == RESUME_NOT_REQUIRED


def test_status_passed_declined_in_active_or_selective_zone():
    assert processing_status("passed", "master", "active", False) == RESUME_DECLINED
    assert processing_status("passed", "master", "selective", False) == RESUME_DECLINED


def test_status_passed_tailored():
    assert processing_status("passed", "tailored", "active", True) == RESUME_COMPLETED


# --- select_representative / resolve_representatives ----------------------------

def _rec(id, draft_id=None, resume_source=None, ats_status=None):
    return {"id": id, "draft_id": draft_id, "resume_source": resume_source, "ats_status": ats_status,
            "canonical_key": "shared"}


def test_representative_prefers_tailored_draft_over_lower_id():
    # The exact confirmed bug: job 212 (lower id, master only) vs job 292
    # (higher id, has a real tailored draft) - 292 must win.
    records = [
        _rec(212, draft_id=None, resume_source="master", ats_status="passed"),
        _rec(292, draft_id=41, resume_source="tailored", ats_status="passed"),
    ]
    winner, reason = select_representative(records)
    assert winner["id"] == 292
    assert reason == "has tailored resume draft"


def test_representative_second_confirmed_case_203_263():
    records = [
        _rec(203, draft_id=None, resume_source="master", ats_status="passed"),
        _rec(263, draft_id=31, resume_source="tailored", ats_status="passed"),
    ]
    winner, reason = select_representative(records)
    assert winner["id"] == 263
    assert reason == "has tailored resume draft"


def test_representative_both_have_drafts_lowest_id_tiebreaks():
    records = [
        _rec(124, draft_id=4, resume_source="tailored", ats_status="passed"),
        _rec(171, draft_id=5, resume_source="tailored", ats_status="passed"),
    ]
    winner, reason = select_representative(records)
    assert winner["id"] == 124
    assert reason == "has tailored resume draft"


def test_representative_neither_has_a_draft_prefers_resume_selection_over_analysis_only():
    records = [
        _rec(220, draft_id=None, resume_source="master", ats_status="passed"),
        _rec(289, draft_id=None, resume_source=None, ats_status="passed"),  # no resume_selections row at all
    ]
    winner, reason = select_representative(records)
    assert winner["id"] == 220
    assert reason == "has resume selection"


def test_representative_both_master_only_lowest_id_tiebreaks():
    records = [
        _rec(115, draft_id=None, resume_source="master", ats_status="passed"),
        _rec(259, draft_id=None, resume_source="master", ats_status="passed"),
    ]
    winner, reason = select_representative(records)
    assert winner["id"] == 115
    assert reason == "has resume selection"


def test_representative_neither_side_has_any_downstream_data():
    records = [
        _rec(10, ats_status=None),
        _rec(20, ats_status=None),
    ]
    winner, reason = select_representative(records)
    assert winner["id"] == 10  # lowest id
    assert reason == "lowest job ID (no downstream data on any side)"


def test_group_by_canonical_key():
    records = [
        {"id": 1, "canonical_key": "a"},
        {"id": 2, "canonical_key": "a"},
        {"id": 3, "canonical_key": "b"},
    ]
    groups = group_by_canonical_key(records)
    assert set(groups.keys()) == {"a", "b"}
    assert len(groups["a"]) == 2
    assert len(groups["b"]) == 1


def test_resolve_representatives_full_dataset_matches_both_confirmed_fixes():
    records = [
        _rec(212, draft_id=None, resume_source="master", ats_status="passed"),
        _rec(292, draft_id=41, resume_source="tailored", ats_status="passed"),
        {**_rec(115, resume_source="master", ats_status="passed"), "canonical_key": "grp2"},
        {**_rec(259, resume_source="master", ats_status="passed"), "canonical_key": "grp2"},
        {**_rec(999, resume_source="master", ats_status="passed"), "canonical_key": "solo"},  # no duplication
    ]
    # give the 212/292 pair their own shared key distinct from "shared" default collision
    for r in records[:2]:
        r["canonical_key"] = "grp1"

    representative_ids, reasons = resolve_representatives(records)
    assert 292 in representative_ids and 212 not in representative_ids
    assert 115 in representative_ids and 259 not in representative_ids
    assert 999 in representative_ids  # solo record always kept, no reason entry
    assert "solo" not in reasons  # single-record groups produce no reason entry
    assert reasons["grp1"] == (292, "has tailored resume draft")


# --- normalize_company_for_filename / resume_display_filename -------------------

def test_normalize_company_simple_two_word():
    assert normalize_company_for_filename("Palantir Technologies") == "PALANTIR-TECHNOLOGIES"


def test_normalize_company_three_word():
    assert normalize_company_for_filename("The Home Depot") == "THE-HOME-DEPOT"


def test_normalize_company_single_word():
    assert normalize_company_for_filename("Figma") == "FIGMA"


def test_normalize_company_removes_unsafe_chars_not_hyphenates_them():
    # Comma/period are unsafe filesystem chars - removed outright, not turned
    # into an extra hyphen (that would produce a double hyphen).
    assert normalize_company_for_filename("Squarespace, Inc.") == "SQUARESPACE-INC"


def test_normalize_company_ampersand_removed():
    assert normalize_company_for_filename("AT&T") == "ATT"


def test_normalize_company_collapses_repeated_hyphens():
    assert normalize_company_for_filename("Foo -- Bar") == "FOO-BAR"


def test_normalize_company_strips_leading_trailing_hyphens():
    assert normalize_company_for_filename("  -Acme-  ") == "ACME"


def test_resume_display_filename_no_suffix_for_first():
    assert resume_display_filename("Figma", 1) == "AASAV-SUTHAR-FIGMA.pdf"


def test_resume_display_filename_examples_from_spec():
    assert resume_display_filename("Optiver", 1) == "AASAV-SUTHAR-OPTIVER.pdf"
    assert resume_display_filename("Databricks", 1) == "AASAV-SUTHAR-DATABRICKS.pdf"
    assert resume_display_filename("AppLovin", 1) == "AASAV-SUTHAR-APPLOVIN.pdf"
    assert resume_display_filename("Palantir Technologies", 1) == "AASAV-SUTHAR-PALANTIR-TECHNOLOGIES.pdf"
    assert resume_display_filename("The Home Depot", 1) == "AASAV-SUTHAR-THE-HOME-DEPOT.pdf"


def test_resume_display_filename_suffix_for_second_and_third():
    assert resume_display_filename("Figma", 2) == "AASAV-SUTHAR-FIGMA-2.pdf"
    assert resume_display_filename("Figma", 3) == "AASAV-SUTHAR-FIGMA-3.pdf"


# --- assign_resume_display_names -------------------------------------------------

def test_assign_display_names_single_company_no_suffix():
    mapping = assign_resume_display_names([(101, 1, "Figma")])
    assert mapping[(101, 1)] == "AASAV-SUTHAR-FIGMA.pdf"


def test_assign_display_names_two_different_companies_no_collision():
    mapping = assign_resume_display_names([(101, 1, "Figma"), (102, 2, "Optiver")])
    assert mapping[(101, 1)] == "AASAV-SUTHAR-FIGMA.pdf"
    assert mapping[(102, 2)] == "AASAV-SUTHAR-OPTIVER.pdf"


def test_assign_display_names_same_company_deterministic_suffix_by_job_id_order():
    # Lower job_id (203) gets no suffix; higher job_id (263) gets -2 -
    # regardless of the order entries are passed in.
    mapping = assign_resume_display_names([(263, 31, "Acme"), (203, 12, "Acme")])
    assert mapping[(203, 12)] == "AASAV-SUTHAR-ACME.pdf"
    assert mapping[(263, 31)] == "AASAV-SUTHAR-ACME-2.pdf"


def test_assign_display_names_three_drafts_same_company():
    mapping = assign_resume_display_names([(10, 1, "Acme"), (20, 2, "Acme"), (30, 3, "Acme")])
    assert mapping[(10, 1)] == "AASAV-SUTHAR-ACME.pdf"
    assert mapping[(20, 2)] == "AASAV-SUTHAR-ACME-2.pdf"
    assert mapping[(30, 3)] == "AASAV-SUTHAR-ACME-3.pdf"


def test_assign_display_names_stable_across_reruns_with_same_input():
    entries = [(263, 31, "Acme"), (203, 12, "Acme"), (500, 99, "Figma")]
    first = assign_resume_display_names(entries)
    second = assign_resume_display_names(list(reversed(entries)))
    assert first == second


def test_assign_display_names_company_name_variants_normalize_to_same_group():
    # Same normalized company spelled two different ways in the DB (unlikely
    # but not impossible) still groups into one suffix sequence.
    mapping = assign_resume_display_names([(10, 1, "Acme Inc."), (20, 2, "ACME INC")])
    assert mapping[(10, 1)] == "AASAV-SUTHAR-ACME-INC.pdf"
    assert mapping[(20, 2)] == "AASAV-SUTHAR-ACME-INC-2.pdf"
