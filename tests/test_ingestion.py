"""Unit tests for Milestone 3.1 CSV parsing and validation."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from public_health_clustering.ingestion import (
    ValidationErrorCode,
    load_communities,
    load_facilities,
    load_patients,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "data" / "sample"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def test_load_communities_sample_data_is_valid() -> None:
    valid, errors = load_communities(SAMPLE_DIR / "communities.csv")

    assert errors == []
    assert len(valid) == 5
    riverside = next(c for c in valid if c.community_code == "COMM-001")
    assert riverside.community_name == "Riverside Heights"
    assert riverside.longitude == -98.55
    assert riverside.latitude == 39.55


def test_load_facilities_sample_data_is_valid() -> None:
    valid, errors = load_facilities(SAMPLE_DIR / "healthcare_facilities.csv")

    assert errors == []
    assert len(valid) == 4
    central = next(f for f in valid if f.facility_code == "FAC-002")
    assert central.facility_name == "Central Hospital"
    assert central.facility_type == "HOSPITAL"


def test_load_patients_sample_data_is_valid() -> None:
    valid, errors = load_patients(SAMPLE_DIR / "patients.csv")

    assert errors == []
    assert len(valid) == 16

    community_match = next(p for p in valid if p.patient_id == "PAT-001")
    assert community_match.diagnosis_date == date(2026, 9, 10)
    assert community_match.community_name == "Riverside Heights"
    assert community_match.diagnosing_facility_code == ""

    facility_fallback = next(p for p in valid if p.patient_id == "PAT-004")
    assert facility_fallback.community_name == ""
    assert facility_fallback.diagnosing_facility_code == "FAC-002"


def test_duplicate_patient_id_is_rejected() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "patients_duplicate_id.csv")

    assert valid == []
    assert len(errors) == 2
    assert all(e.field == "patient_id" for e in errors)
    assert all(e.code == ValidationErrorCode.DUPLICATE_ID for e in errors)


def test_invalid_diagnosis_date_is_rejected() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "patients_invalid_date.csv")

    assert valid == []
    assert len(errors) == 2
    assert all(e.field == "diagnosis_date" for e in errors)
    assert all(e.code == ValidationErrorCode.INVALID_DATE for e in errors)


def test_missing_required_patient_fields_are_rejected() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "patients_missing_required_fields.csv")

    assert valid == []
    fields = {e.field for e in errors}
    assert fields == {"patient_id", "name", "illness", "diagnosis_date"}
    assert len(errors) == 4
    assert all(e.code == ValidationErrorCode.REQUIRED_FIELD for e in errors)


def test_free_text_diagnosing_facility_code_is_preserved_without_format_check() -> None:
    # diagnosing_facility_code is TEXT in the schema and is not cross-checked
    # against healthcare_facilities in Milestone 3.1 — any non-blank text is
    # accepted and preserved exactly.
    valid, errors = load_patients(FIXTURES_DIR / "patients_free_text_facility_code.csv")

    assert errors == []
    assert len(valid) == 1
    assert valid[0].diagnosing_facility_code == "bad code with spaces"


def test_blank_diagnosing_facility_code_is_not_an_error() -> None:
    valid, errors = load_patients(SAMPLE_DIR / "patients.csv")
    assert errors == []
    assert any(p.diagnosing_facility_code == "" for p in valid)


def test_optional_demographics_are_normalized_and_validated(tmp_path: Path) -> None:
    csv_path = tmp_path / "demographics.csv"
    csv_path.write_text(
        "patient_id,name,illness,diagnosis_date,address_text,community_name,diagnosing_facility_code,sex,age_years\n"
        "D-1,Case 1,Measles,2026-09-10,,,,女,8\n"
        "D-2,Case 2,Measles,2026-09-10,,,,M,121\n",
        encoding="utf-8",
    )
    valid, errors = load_patients(csv_path)
    assert len(valid) == 1
    assert valid[0].sex == "FEMALE"
    assert valid[0].age_years == 8
    assert len(errors) == 1
    assert errors[0].field == "age_years"


def test_invalid_community_coordinates_are_rejected() -> None:
    valid, errors = load_communities(FIXTURES_DIR / "communities_invalid_coordinates.csv")

    assert valid == []
    assert len(errors) == 3
    fields = {e.field for e in errors}
    assert fields == {"longitude", "latitude"}


def test_invalid_facility_coordinates_are_rejected() -> None:
    valid, errors = load_facilities(FIXTURES_DIR / "facilities_invalid_coordinates.csv")

    assert valid == []
    assert len(errors) == 2
    fields = {e.field for e in errors}
    assert fields == {"longitude", "latitude"}


def test_duplicate_facility_code_is_rejected() -> None:
    valid, errors = load_facilities(FIXTURES_DIR / "facilities_duplicate_code.csv")

    assert valid == []
    assert len(errors) == 2
    assert all(e.field == "facility_code" for e in errors)
    assert all(e.code == ValidationErrorCode.DUPLICATE_ID for e in errors)


def test_missing_required_column_is_reported_as_a_structural_error() -> None:
    valid, errors = load_communities(FIXTURES_DIR / "communities_missing_column.csv")

    assert valid == []
    assert len(errors) == 1
    assert errors[0].row_number is None
    assert errors[0].code == ValidationErrorCode.MISSING_HEADER
    assert "latitude" in errors[0].message


def test_nan_and_infinite_coordinates_are_rejected_as_invalid_numbers() -> None:
    valid, errors = load_communities(FIXTURES_DIR / "communities_nan_infinity_coordinates.csv")

    assert valid == []
    assert len(errors) == 3
    assert all(e.code == ValidationErrorCode.INVALID_NUMBER for e in errors)


def test_exact_coordinate_boundaries_are_valid() -> None:
    valid, errors = load_communities(FIXTURES_DIR / "communities_boundary_coordinates.csv")

    assert errors == []
    assert len(valid) == 2
    west = next(c for c in valid if c.community_code == "COMM-BOUND-001")
    east = next(c for c in valid if c.community_code == "COMM-BOUND-002")
    assert (west.longitude, west.latitude) == (-180.0, -90.0)
    assert (east.longitude, east.latitude) == (180.0, 90.0)


def test_patient_id_whitespace_is_normalized() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "patients_identifier_whitespace.csv")

    assert errors == []
    assert len(valid) == 1
    assert valid[0].patient_id == "PAT-WS-001"


def test_facility_code_whitespace_is_normalized() -> None:
    valid, errors = load_facilities(FIXTURES_DIR / "facilities_identifier_whitespace.csv")

    assert errors == []
    assert len(valid) == 1
    assert valid[0].facility_code == "FAC-WS-001"


def test_community_code_whitespace_is_normalized() -> None:
    valid, errors = load_communities(FIXTURES_DIR / "communities_identifier_whitespace.csv")

    assert errors == []
    assert len(valid) == 1
    assert valid[0].community_code == "COMM-WS-001"


def test_identifier_duplicate_matching_is_case_sensitive() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "patients_case_sensitive_ids.csv")

    assert errors == []
    assert len(valid) == 2
    assert {p.patient_id for p in valid} == {"PAT-CASE-001", "pat-case-001"}


def test_multiple_blank_identifiers_are_each_reported_and_not_flagged_as_duplicates() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "patients_multiple_blank_ids.csv")

    assert valid == []
    assert len(errors) == 2
    assert all(e.field == "patient_id" for e in errors)
    assert all(e.code == ValidationErrorCode.REQUIRED_FIELD for e in errors)
    assert all(e.code != ValidationErrorCode.DUPLICATE_ID for e in errors)


def test_quoted_whitespace_in_non_identifier_field_is_preserved_exactly() -> None:
    valid, errors = load_communities(FIXTURES_DIR / "communities_quoted_whitespace.csv")

    assert errors == []
    assert len(valid) == 1
    assert valid[0].full_address == "  100 Main St, Cedarbrook  "


def test_embedded_comma_and_newline_in_quoted_field_are_preserved() -> None:
    valid, errors = load_communities(FIXTURES_DIR / "communities_embedded_newline.csv")

    assert errors == []
    assert len(valid) == 1
    assert valid[0].full_address == "100 Main St\nSuite 2, Cedarbrook"
    assert valid[0].row_number == 2


def test_optional_blank_fields_are_valid() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "patients_optional_blanks.csv")

    assert errors == []
    assert len(valid) == 1
    assert valid[0].address_text == ""
    assert valid[0].community_name == ""
    assert valid[0].diagnosing_facility_code == ""


def test_short_row_is_detected_as_malformed_not_silently_padded() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "patients_short_row.csv")

    assert valid == []
    assert len(errors) == 1
    assert errors[0].code == ValidationErrorCode.MALFORMED_ROW
    assert errors[0].row_number == 2
    assert "short" in errors[0].message


def test_long_row_is_detected_as_malformed_not_silently_truncated() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "patients_long_row.csv")

    assert valid == []
    assert len(errors) == 1
    assert errors[0].code == ValidationErrorCode.MALFORMED_ROW
    assert errors[0].row_number == 2
    assert "long" in errors[0].message


def test_duplicate_header_names_are_detected() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "patients_duplicate_header.csv")

    assert valid == []
    assert len(errors) == 1
    assert errors[0].code == ValidationErrorCode.DUPLICATE_HEADER
    assert errors[0].row_number is None
    assert "patient_id" in errors[0].message


def test_missing_file_is_a_structured_file_error() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "does_not_exist.csv")

    assert valid == []
    assert len(errors) == 1
    assert errors[0].code == ValidationErrorCode.FILE_ERROR
    assert errors[0].row_number is None


def test_compact_and_week_date_shapes_are_rejected() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "patients_date_shape_violations.csv")

    assert valid == []
    assert len(errors) == 2
    assert all(e.code == ValidationErrorCode.INVALID_DATE for e in errors)


def test_one_valid_row_mixed_with_invalid_rows() -> None:
    valid, errors = load_patients(FIXTURES_DIR / "patients_mixed_valid_invalid.csv")

    assert len(valid) == 1
    assert valid[0].patient_id == "PAT-MIX-001"
    assert len(errors) == 2
    assert [e.row_number for e in errors] == [3, 4]
    assert errors[0].field == "name"
    assert errors[1].field == "diagnosis_date"


def test_error_ordering_is_deterministic() -> None:
    _, errors_first = load_patients(FIXTURES_DIR / "patients_mixed_valid_invalid.csv")
    _, errors_second = load_patients(FIXTURES_DIR / "patients_mixed_valid_invalid.csv")

    assert errors_first == errors_second
    row_numbers = [e.row_number for e in errors_first]
    assert row_numbers == sorted(row_numbers)


def test_patient_related_error_messages_do_not_echo_raw_field_values() -> None:
    _, dup_errors = load_patients(FIXTURES_DIR / "patients_duplicate_id.csv")
    assert all("PAT-DUP-001" not in e.message for e in dup_errors)

    _, date_errors = load_patients(FIXTURES_DIR / "patients_invalid_date.csv")
    assert all("2026-13-40" not in e.message and "not-a-date" not in e.message for e in date_errors)
