"""CSV parsing for ingestion source files.

Handles structural concerns only: file readability, required columns
present, no duplicate header names, and every data row having exactly as
many cells as the header (no silent short/long-row coercion). Preserves raw
text field values exactly as read — no stripping, no coercion. Field-level
and cross-row content validation happens in validation.py.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .errors import ValidationError, ValidationErrorCode
from .records import RawCommunityRow, RawFacilityRow, RawPatientRow

COMMUNITY_COLUMNS = ("community_code", "community_name", "full_address", "longitude", "latitude")
FACILITY_COLUMNS = (
    "facility_code",
    "facility_name",
    "facility_type",
    "full_address",
    "longitude",
    "latitude",
)
PATIENT_COLUMNS = (
    "patient_id",
    "name",
    "illness",
    "diagnosis_date",
    "address_text",
    "community_name",
    "diagnosing_facility_code",
)
PATIENT_OPTIONAL_COLUMNS = ("sex", "age_years")


def _file_error(source: str, message: str) -> ValidationError:
    return ValidationError(ValidationErrorCode.FILE_ERROR, source, None, None, message)


def _read_rows(
    path: Path, required_columns: tuple[str, ...], source: str
) -> tuple[list[tuple[int, dict[str, str]]], list[ValidationError]]:
    """Read a CSV file into (row_number, row_dict) pairs.

    A well-formed row is included in the returned list. A malformed row
    (wrong cell count) is reported as an error and excluded, but does not
    prevent other rows in the same file from being read. File-level problems
    (missing/unreadable file, decode errors, missing/duplicate header
    columns) return no rows at all, since nothing in the file can be trusted.
    """
    try:
        with path.open(newline="", encoding="utf-8") as csv_file:
            raw_rows = list(csv.reader(csv_file))
    except FileNotFoundError:
        return [], [_file_error(source, "file not found")]
    except IsADirectoryError:
        return [], [_file_error(source, "expected a file, found a directory")]
    except UnicodeDecodeError:
        return [], [_file_error(source, "could not decode file as UTF-8")]
    except csv.Error as exc:
        return [], [_file_error(source, f"CSV parsing failed: {exc}")]
    except OSError as exc:
        return [], [_file_error(source, f"could not read file: {exc.strerror or exc}")]

    if not raw_rows:
        return [], [
            ValidationError(ValidationErrorCode.MISSING_HEADER, source, None, None, "file is empty")
        ]

    header, *data_rows = raw_rows

    duplicate_columns = sorted({name for name in header if header.count(name) > 1})
    if duplicate_columns:
        return [], [
            ValidationError(
                ValidationErrorCode.DUPLICATE_HEADER,
                source,
                None,
                None,
                f"duplicate column name(s) in header: {', '.join(duplicate_columns)}",
            )
        ]

    missing = [column for column in required_columns if column not in header]
    if missing:
        return [], [
            ValidationError(
                ValidationErrorCode.MISSING_HEADER,
                source,
                None,
                None,
                f"missing required column(s): {', '.join(missing)}",
            )
        ]

    expected_width = len(header)
    rows: list[tuple[int, dict[str, str]]] = []
    errors: list[ValidationError] = []
    for row_number, cells in enumerate(data_rows, start=2):
        if len(cells) != expected_width:
            shape = "short" if len(cells) < expected_width else "long"
            errors.append(
                ValidationError(
                    ValidationErrorCode.MALFORMED_ROW,
                    source,
                    row_number,
                    None,
                    f"row has {len(cells)} cell(s), expected {expected_width} ({shape} row)",
                )
            )
            continue
        rows.append((row_number, dict(zip(header, cells))))

    return rows, errors


def parse_communities_csv(path: Path) -> tuple[list[RawCommunityRow], list[ValidationError]]:
    """Parse communities.csv into raw rows. Row numbers start at 2 (after the header)."""
    source = path.name
    rows, errors = _read_rows(path, COMMUNITY_COLUMNS, source)
    parsed = [
        RawCommunityRow(
            row_number=row_number,
            community_code=row["community_code"],
            community_name=row["community_name"],
            full_address=row["full_address"],
            longitude=row["longitude"],
            latitude=row["latitude"],
        )
        for row_number, row in rows
    ]
    return parsed, errors


def parse_facilities_csv(path: Path) -> tuple[list[RawFacilityRow], list[ValidationError]]:
    """Parse healthcare_facilities.csv into raw rows."""
    source = path.name
    rows, errors = _read_rows(path, FACILITY_COLUMNS, source)
    parsed = [
        RawFacilityRow(
            row_number=row_number,
            facility_code=row["facility_code"],
            facility_name=row["facility_name"],
            facility_type=row["facility_type"],
            full_address=row["full_address"],
            longitude=row["longitude"],
            latitude=row["latitude"],
        )
        for row_number, row in rows
    ]
    return parsed, errors


def parse_patients_csv(path: Path) -> tuple[list[RawPatientRow], list[ValidationError]]:
    """Parse patients.csv into raw rows."""
    source = path.name
    rows, errors = _read_rows(path, PATIENT_COLUMNS, source)
    parsed = [
        RawPatientRow(
            row_number=row_number,
            patient_id=row["patient_id"],
            name=row["name"],
            illness=row["illness"],
            diagnosis_date=row["diagnosis_date"],
            address_text=row["address_text"],
            community_name=row["community_name"],
            diagnosing_facility_code=row["diagnosing_facility_code"],
            sex=row.get("sex", ""),
            age_years=row.get("age_years", ""),
        )
        for row_number, row in rows
    ]
    return parsed, errors
