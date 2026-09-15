"""Database insertion for validated CSV records (Milestone 3.2).

Idempotency policy
-------------------
Each entity is looked up by its natural external identifier before writing:

- communities   -> community_name   (communities.community_name is the
                   schema's actual UNIQUE key; community_code is a CSV-only
                   staging label with no corresponding database column, so it
                   cannot be used as a lookup key here — see note below)
- facilities    -> facility_code    (healthcare_facilities.facility_code,
                   UNIQUE in the schema)
- patients      -> patient_id       (patients.patient_id, the primary key)

For each record:
  - no existing row for that key  -> INSERT
  - existing row, fields identical -> no write, outcome UNCHANGED
  - existing row, fields differ    -> UPDATE the existing row in place,
                                       outcome UPDATED

This is a deliberate "last write wins, in place" policy rather than
rejecting changed re-imports as conflicts: it keeps repeated imports of a
corrected CSV simple and side-effect-free (no duplicate rows, no manual
conflict resolution step), which matches how this dataset is expected to be
used during development. It is applied identically to all three entity
types.

Note on community_code: sql/schema.sql has no community_code column on
communities — only community_name is unique there. Milestone 3.1's CSVs use
community_code purely as a human-readable label for the sample dataset. This
importer therefore keys communities on community_name, the schema's real
natural key, and does not persist community_code anywhere.

Transaction policy
-------------------
Each row is written inside its own SAVEPOINT (Session.begin_nested()). If a
row fails at the database level (e.g. an unexpected constraint violation),
only that row's savepoint is rolled back and the row is recorded with
outcome FAILED — the rest of the batch, and any rows already written in the
same call, are unaffected. These functions flush but never commit: the
caller owns the outer transaction and decides when (or whether) to commit.

Foreign-key resolution: a patient's diagnosing_facility_code is resolved
against healthcare_facilities.facility_code already visible in this session
(pre-existing rows or rows just imported by import_facilities in the same
transaction). A non-blank code that does not resolve is treated as a
row-level failure (outcome FAILED) rather than silently inserting the
patient with a NULL facility reference — resolving it is intentionally not
treated as "facility fallback" location resolution (a later milestone); it
is just populating the patients.diagnosing_facility_id foreign key.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models import CaseEvent, Community, DataBatch, HealthcareFacility, Patient
from .records import ValidatedCommunity, ValidatedFacility, ValidatedPatient


class ImportAction(enum.StrEnum):
    INSERTED = "INSERTED"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ImportOutcome:
    """The result of importing a single record, keyed by its external identifier."""

    external_id: str
    action: ImportAction
    message: str | None = None


@dataclass(frozen=True)
class ImportBatchResult:
    outcomes: list[ImportOutcome]

    @property
    def inserted(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ImportAction.INSERTED)

    @property
    def updated(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ImportAction.UPDATED)

    @property
    def unchanged(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ImportAction.UNCHANGED)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ImportAction.FAILED)


@dataclass(frozen=True)
class DatasetImportResult:
    communities: ImportBatchResult
    facilities: ImportBatchResult
    patients: ImportBatchResult


def _upsert_community(session: Session, record: ValidatedCommunity) -> ImportOutcome:
    try:
        with session.begin_nested():
            existing = session.execute(
                select(Community).where(Community.community_name == record.community_name)
            ).scalar_one_or_none()

            if existing is None:
                session.add(
                    Community(
                        community_name=record.community_name,
                        full_address=record.full_address,
                        longitude=record.longitude,
                        latitude=record.latitude,
                    )
                )
                session.flush()
                return ImportOutcome(record.community_name, ImportAction.INSERTED)

            changed = (
                existing.full_address != record.full_address
                or existing.longitude != record.longitude
                or existing.latitude != record.latitude
            )
            if not changed:
                return ImportOutcome(record.community_name, ImportAction.UNCHANGED)

            existing.full_address = record.full_address
            existing.longitude = record.longitude
            existing.latitude = record.latitude
            session.flush()
            return ImportOutcome(record.community_name, ImportAction.UPDATED)
    except SQLAlchemyError as exc:
        return ImportOutcome(record.community_name, ImportAction.FAILED, str(exc))


def import_communities(
    session: Session, records: Sequence[ValidatedCommunity]
) -> ImportBatchResult:
    return ImportBatchResult([_upsert_community(session, record) for record in records])


def _upsert_facility(session: Session, record: ValidatedFacility) -> ImportOutcome:
    facility_type = record.facility_type or None
    try:
        with session.begin_nested():
            existing = session.execute(
                select(HealthcareFacility).where(
                    HealthcareFacility.facility_code == record.facility_code
                )
            ).scalar_one_or_none()

            if existing is None:
                session.add(
                    HealthcareFacility(
                        facility_code=record.facility_code,
                        facility_name=record.facility_name,
                        facility_type=facility_type,
                        full_address=record.full_address,
                        longitude=record.longitude,
                        latitude=record.latitude,
                    )
                )
                session.flush()
                return ImportOutcome(record.facility_code, ImportAction.INSERTED)

            changed = (
                existing.facility_name != record.facility_name
                or existing.facility_type != facility_type
                or existing.full_address != record.full_address
                or existing.longitude != record.longitude
                or existing.latitude != record.latitude
            )
            if not changed:
                return ImportOutcome(record.facility_code, ImportAction.UNCHANGED)

            existing.facility_name = record.facility_name
            existing.facility_type = facility_type
            existing.full_address = record.full_address
            existing.longitude = record.longitude
            existing.latitude = record.latitude
            session.flush()
            return ImportOutcome(record.facility_code, ImportAction.UPDATED)
    except SQLAlchemyError as exc:
        return ImportOutcome(record.facility_code, ImportAction.FAILED, str(exc))


def import_facilities(
    session: Session, records: Sequence[ValidatedFacility]
) -> ImportBatchResult:
    return ImportBatchResult([_upsert_facility(session, record) for record in records])


def _resolve_facility_id(session: Session, facility_code: str) -> tuple[int | None, bool]:
    """Look up a facility_id by facility_code. Returns (facility_id, resolved).

    resolved is False only when facility_code is non-blank but matches no
    known facility — a blank code always resolves successfully to None.
    """
    if not facility_code:
        return None, True
    facility_id = session.execute(
        select(HealthcareFacility.facility_id).where(
            HealthcareFacility.facility_code == facility_code
        )
    ).scalar_one_or_none()
    return facility_id, facility_id is not None


def _upsert_patient(session: Session, record: ValidatedPatient) -> ImportOutcome:
    facility_id, resolved = _resolve_facility_id(session, record.diagnosing_facility_code)
    if not resolved:
        return ImportOutcome(
            record.patient_id,
            ImportAction.FAILED,
            f"diagnosing_facility_code {record.diagnosing_facility_code!r} "
            "does not match any known facility",
        )

    address_text = record.address_text or None
    community_name = record.community_name or None

    try:
        with session.begin_nested():
            existing = session.execute(
                select(Patient).where(Patient.patient_id == record.patient_id)
            ).scalar_one_or_none()

            if existing is None:
                session.add(
                    Patient(
                        patient_id=record.patient_id,
                        name=record.name,
                        illness=record.illness,
                        diagnosis_date=record.diagnosis_date,
                        address_text=address_text,
                        community_name=community_name,
                        diagnosing_facility_id=facility_id,
                    )
                )
                session.flush()
                return ImportOutcome(record.patient_id, ImportAction.INSERTED)

            changed = (
                existing.name != record.name
                or existing.illness != record.illness
                or existing.diagnosis_date != record.diagnosis_date
                or existing.address_text != address_text
                or existing.community_name != community_name
                or existing.diagnosing_facility_id != facility_id
            )
            if not changed:
                return ImportOutcome(record.patient_id, ImportAction.UNCHANGED)

            existing.name = record.name
            existing.illness = record.illness
            existing.diagnosis_date = record.diagnosis_date
            existing.address_text = address_text
            existing.community_name = community_name
            existing.diagnosing_facility_id = facility_id
            session.flush()
            return ImportOutcome(record.patient_id, ImportAction.UPDATED)
    except SQLAlchemyError as exc:
        return ImportOutcome(record.patient_id, ImportAction.FAILED, str(exc))


def import_patients(
    session: Session,
    records: Sequence[ValidatedPatient],
    *,
    batch: DataBatch | None = None,
) -> ImportBatchResult:
    outcomes: list[ImportOutcome] = []
    for record in records:
        outcome = _upsert_patient(session, record)
        if batch is not None and outcome.action != ImportAction.FAILED:
            event = session.scalar(select(CaseEvent).where(
                CaseEvent.batch_id == batch.batch_id,
                CaseEvent.source_case_id == record.patient_id,
            ))
            if event is None:
                session.add(CaseEvent(
                    batch_id=batch.batch_id, patient_id=record.patient_id,
                    source_case_id=record.patient_id, illness=record.illness,
                    diagnosis_date=record.diagnosis_date,
                ))
                session.flush()
            else:
                event.illness = record.illness
                event.diagnosis_date = record.diagnosis_date
        outcomes.append(outcome)
    return ImportBatchResult(outcomes)


def import_dataset(
    session: Session,
    communities: Sequence[ValidatedCommunity],
    facilities: Sequence[ValidatedFacility],
    patients: Sequence[ValidatedPatient],
    *,
    batch: DataBatch | None = None,
) -> DatasetImportResult:
    """Import communities, then facilities, then patients, in that order.

    Order matters: patient rows resolve diagnosing_facility_code against
    facility rows that must already be visible in this session, so
    facilities must be imported (and flushed) first. Does not commit —
    the caller owns the transaction.
    """
    community_result = import_communities(session, communities)
    facility_result = import_facilities(session, facilities)
    patient_result = import_patients(session, patients, batch=batch)
    return DatasetImportResult(community_result, facility_result, patient_result)
