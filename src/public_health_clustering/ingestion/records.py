"""Raw and validated row records for CSV ingestion.

Raw*Row records hold exactly the text read from each CSV cell, with no type
coercion. Validated* records hold the same data after it has passed
validation, with fields coerced to their schema-aligned Python types.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class RawCommunityRow:
    row_number: int
    community_code: str
    community_name: str
    full_address: str
    longitude: str
    latitude: str


@dataclass(frozen=True)
class RawFacilityRow:
    row_number: int
    facility_code: str
    facility_name: str
    facility_type: str
    full_address: str
    longitude: str
    latitude: str


@dataclass(frozen=True)
class RawPatientRow:
    row_number: int
    patient_id: str
    name: str
    illness: str
    diagnosis_date: str
    address_text: str
    community_name: str
    diagnosing_facility_code: str
    sex: str = ""
    age_years: str = ""


@dataclass(frozen=True)
class ValidatedCommunity:
    row_number: int
    community_code: str
    community_name: str
    full_address: str
    longitude: float
    latitude: float


@dataclass(frozen=True)
class ValidatedFacility:
    row_number: int
    facility_code: str
    facility_name: str
    facility_type: str
    full_address: str
    longitude: float
    latitude: float


@dataclass(frozen=True)
class ValidatedPatient:
    row_number: int
    patient_id: str
    name: str
    illness: str
    diagnosis_date: date
    address_text: str
    community_name: str
    # Presence/format-validated only; not yet resolved against the database.
    diagnosing_facility_code: str
    sex: str | None = None
    age_years: int | None = None
