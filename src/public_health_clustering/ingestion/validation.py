"""Row-level and cross-row validation for parsed CSV ingestion data.

Validation is presence/format/range checking only. It does not look up
communities or facilities against each other or against the database —
community matching and facility fallback resolution are later milestones.

Identifier normalization: patient_id, facility_code, and community_code are
external identifiers used for joining/deduplication, so leading/trailing
whitespace is stripped before comparison and before the value is stored on
the Validated* record — this is the value that will later be inserted, so
duplicate detection uses that same normalized value. Every other text field
(names, addresses, community_name) is schema-authoritative source text and
is preserved and compared exactly as sourced, with no normalization.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date

from .errors import ValidationError, ValidationErrorCode
from .records import (
    RawCommunityRow,
    RawFacilityRow,
    RawPatientRow,
    ValidatedCommunity,
    ValidatedFacility,
    ValidatedPatient,
)

_DATE_SHAPE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _require(
    value: str, *, source: str, row_number: int, field: str, errors: list[ValidationError]
) -> bool:
    if not value.strip():
        errors.append(
            ValidationError(
                ValidationErrorCode.REQUIRED_FIELD,
                source,
                row_number,
                field,
                "required field is missing or blank",
            )
        )
        return False
    return True


def _parse_coordinate(
    raw: str,
    *,
    source: str,
    row_number: int,
    field: str,
    minimum: float,
    maximum: float,
    errors: list[ValidationError],
) -> float | None:
    text = raw.strip()
    if not text:
        errors.append(
            ValidationError(
                ValidationErrorCode.REQUIRED_FIELD,
                source,
                row_number,
                field,
                "required field is missing or blank",
            )
        )
        return None
    try:
        value = float(text)
    except ValueError:
        errors.append(
            ValidationError(
                ValidationErrorCode.INVALID_NUMBER,
                source,
                row_number,
                field,
                f"not a valid number: {raw!r}",
            )
        )
        return None
    if not math.isfinite(value):
        errors.append(
            ValidationError(
                ValidationErrorCode.INVALID_NUMBER,
                source,
                row_number,
                field,
                f"not a finite number: {raw!r}",
            )
        )
        return None
    if not (minimum <= value <= maximum):
        errors.append(
            ValidationError(
                ValidationErrorCode.OUT_OF_RANGE,
                source,
                row_number,
                field,
                f"out of range [{minimum}, {maximum}]: {value}",
            )
        )
        return None
    return value


def validate_communities(
    rows: list[RawCommunityRow], source: str = "communities.csv"
) -> tuple[list[ValidatedCommunity], list[ValidationError]]:
    errors: list[ValidationError] = []
    valid: list[ValidatedCommunity] = []

    code_counts = Counter(row.community_code.strip() for row in rows if row.community_code.strip())
    # community_name is schema-authoritative source text (not a normalized
    # identifier): compare, and later insert, it exactly as sourced.
    name_counts = Counter(row.community_name for row in rows if row.community_name.strip())

    for row in rows:
        row_errors: list[ValidationError] = []

        code_ok = _require(
            row.community_code,
            source=source,
            row_number=row.row_number,
            field="community_code",
            errors=row_errors,
        )
        normalized_code = row.community_code.strip()
        if code_ok and code_counts[normalized_code] > 1:
            row_errors.append(
                ValidationError(
                    ValidationErrorCode.DUPLICATE_ID,
                    source,
                    row.row_number,
                    "community_code",
                    f"duplicate community_code: {normalized_code!r}",
                )
            )

        name_ok = _require(
            row.community_name,
            source=source,
            row_number=row.row_number,
            field="community_name",
            errors=row_errors,
        )
        if name_ok and name_counts[row.community_name] > 1:
            row_errors.append(
                ValidationError(
                    ValidationErrorCode.DUPLICATE_ID,
                    source,
                    row.row_number,
                    "community_name",
                    f"duplicate community_name: {row.community_name!r}",
                )
            )

        _require(
            row.full_address,
            source=source,
            row_number=row.row_number,
            field="full_address",
            errors=row_errors,
        )

        longitude = _parse_coordinate(
            row.longitude,
            source=source,
            row_number=row.row_number,
            field="longitude",
            minimum=-180,
            maximum=180,
            errors=row_errors,
        )
        latitude = _parse_coordinate(
            row.latitude,
            source=source,
            row_number=row.row_number,
            field="latitude",
            minimum=-90,
            maximum=90,
            errors=row_errors,
        )

        if row_errors:
            errors.extend(row_errors)
            continue

        assert longitude is not None and latitude is not None
        valid.append(
            ValidatedCommunity(
                row_number=row.row_number,
                community_code=normalized_code,
                community_name=row.community_name,
                full_address=row.full_address,
                longitude=longitude,
                latitude=latitude,
            )
        )

    return valid, errors


def validate_facilities(
    rows: list[RawFacilityRow], source: str = "healthcare_facilities.csv"
) -> tuple[list[ValidatedFacility], list[ValidationError]]:
    errors: list[ValidationError] = []
    valid: list[ValidatedFacility] = []

    code_counts = Counter(row.facility_code.strip() for row in rows if row.facility_code.strip())

    for row in rows:
        row_errors: list[ValidationError] = []

        code_ok = _require(
            row.facility_code,
            source=source,
            row_number=row.row_number,
            field="facility_code",
            errors=row_errors,
        )
        normalized_code = row.facility_code.strip()
        if code_ok and code_counts[normalized_code] > 1:
            row_errors.append(
                ValidationError(
                    ValidationErrorCode.DUPLICATE_ID,
                    source,
                    row.row_number,
                    "facility_code",
                    f"duplicate facility_code: {normalized_code!r}",
                )
            )

        _require(
            row.facility_name,
            source=source,
            row_number=row.row_number,
            field="facility_name",
            errors=row_errors,
        )
        _require(
            row.full_address,
            source=source,
            row_number=row.row_number,
            field="full_address",
            errors=row_errors,
        )

        longitude = _parse_coordinate(
            row.longitude,
            source=source,
            row_number=row.row_number,
            field="longitude",
            minimum=-180,
            maximum=180,
            errors=row_errors,
        )
        latitude = _parse_coordinate(
            row.latitude,
            source=source,
            row_number=row.row_number,
            field="latitude",
            minimum=-90,
            maximum=90,
            errors=row_errors,
        )

        if row_errors:
            errors.extend(row_errors)
            continue

        assert longitude is not None and latitude is not None
        valid.append(
            ValidatedFacility(
                row_number=row.row_number,
                facility_code=normalized_code,
                facility_name=row.facility_name,
                facility_type=row.facility_type,
                full_address=row.full_address,
                longitude=longitude,
                latitude=latitude,
            )
        )

    return valid, errors


def validate_patients(
    rows: list[RawPatientRow], source: str = "patients.csv"
) -> tuple[list[ValidatedPatient], list[ValidationError]]:
    errors: list[ValidationError] = []
    valid: list[ValidatedPatient] = []

    id_counts = Counter(row.patient_id.strip() for row in rows if row.patient_id.strip())

    for row in rows:
        row_errors: list[ValidationError] = []

        id_ok = _require(
            row.patient_id,
            source=source,
            row_number=row.row_number,
            field="patient_id",
            errors=row_errors,
        )
        normalized_id = row.patient_id.strip()
        if id_ok and id_counts[normalized_id] > 1:
            row_errors.append(
                ValidationError(
                    ValidationErrorCode.DUPLICATE_ID,
                    source,
                    row.row_number,
                    "patient_id",
                    "duplicate patient_id",
                )
            )

        _require(
            row.name, source=source, row_number=row.row_number, field="name", errors=row_errors
        )
        _require(
            row.illness,
            source=source,
            row_number=row.row_number,
            field="illness",
            errors=row_errors,
        )

        diagnosis_date: date | None = None
        raw_date = row.diagnosis_date.strip()
        if not raw_date:
            row_errors.append(
                ValidationError(
                    ValidationErrorCode.REQUIRED_FIELD,
                    source,
                    row.row_number,
                    "diagnosis_date",
                    "required field is missing or blank",
                )
            )
        elif not _DATE_SHAPE_PATTERN.match(raw_date):
            row_errors.append(
                ValidationError(
                    ValidationErrorCode.INVALID_DATE,
                    source,
                    row.row_number,
                    "diagnosis_date",
                    "invalid date format, expected exactly YYYY-MM-DD",
                )
            )
        else:
            try:
                diagnosis_date = date.fromisoformat(raw_date)
            except ValueError:
                row_errors.append(
                    ValidationError(
                        ValidationErrorCode.INVALID_DATE,
                        source,
                        row.row_number,
                        "diagnosis_date",
                        "not a valid calendar date",
                    )
                )

        # Presence only — format/content beyond that, and resolving this
        # against known facilities, happen in a later milestone.

        if row_errors:
            errors.extend(row_errors)
            continue

        assert diagnosis_date is not None
        valid.append(
            ValidatedPatient(
                row_number=row.row_number,
                patient_id=normalized_id,
                name=row.name,
                illness=row.illness,
                diagnosis_date=diagnosis_date,
                address_text=row.address_text,
                community_name=row.community_name,
                diagnosing_facility_code=row.diagnosing_facility_code,
            )
        )

    return valid, errors
