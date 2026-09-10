"""CSV parsing, validation, and database import for source data (Milestone 3).

This package parses and validates the three sample CSV formats (communities,
healthcare_facilities, patients) and can insert/update already-validated
records into the database (Milestone 3.2). It does not resolve patient
locations, match communities against patients, fall back to facility
coordinates, or run clustering — those are later milestones.
"""

from __future__ import annotations

from pathlib import Path

from .errors import ValidationError, ValidationErrorCode
from .importer import (
    DatasetImportResult,
    ImportAction,
    ImportBatchResult,
    ImportOutcome,
    import_communities,
    import_dataset,
    import_facilities,
    import_patients,
)
from .parsing import parse_communities_csv, parse_facilities_csv, parse_patients_csv
from .records import (
    RawCommunityRow,
    RawFacilityRow,
    RawPatientRow,
    ValidatedCommunity,
    ValidatedFacility,
    ValidatedPatient,
)
from .validation import validate_communities, validate_facilities, validate_patients


def _sort_key(error: ValidationError) -> tuple[int, int]:
    # File-level errors (row_number is None) sort first; otherwise ascending by row.
    return (0 if error.row_number is None else 1, error.row_number or 0)


def load_communities(path: Path) -> tuple[list[ValidatedCommunity], list[ValidationError]]:
    """Parse and validate a communities CSV file in one step."""
    rows, parse_errors = parse_communities_csv(path)
    valid, validation_errors = validate_communities(rows)
    return valid, sorted(parse_errors + validation_errors, key=_sort_key)


def load_facilities(path: Path) -> tuple[list[ValidatedFacility], list[ValidationError]]:
    """Parse and validate a healthcare_facilities CSV file in one step."""
    rows, parse_errors = parse_facilities_csv(path)
    valid, validation_errors = validate_facilities(rows)
    return valid, sorted(parse_errors + validation_errors, key=_sort_key)


def load_patients(path: Path) -> tuple[list[ValidatedPatient], list[ValidationError]]:
    """Parse and validate a patients CSV file in one step."""
    rows, parse_errors = parse_patients_csv(path)
    valid, validation_errors = validate_patients(rows)
    return valid, sorted(parse_errors + validation_errors, key=_sort_key)


__all__ = [
    "ValidationError",
    "ValidationErrorCode",
    "RawCommunityRow",
    "RawFacilityRow",
    "RawPatientRow",
    "ValidatedCommunity",
    "ValidatedFacility",
    "ValidatedPatient",
    "parse_communities_csv",
    "parse_facilities_csv",
    "parse_patients_csv",
    "validate_communities",
    "validate_facilities",
    "validate_patients",
    "load_communities",
    "load_facilities",
    "load_patients",
    "ImportAction",
    "ImportOutcome",
    "ImportBatchResult",
    "DatasetImportResult",
    "import_communities",
    "import_facilities",
    "import_patients",
    "import_dataset",
]
