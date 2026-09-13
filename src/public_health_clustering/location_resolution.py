"""Patient location resolution (Milestone 4).

Computes and writes each patient's current row in patient_locations. Does
not geocode, fuzzy-match, cluster, or map — it only chooses between two
already-known point sources (a matched community's location, or the
diagnosing facility's location as a proxy) and records that choice with
full provenance.

Resolution priority, computed fresh for each patient on every run
--------------------------------------------------------------------
1. Exact equality: patients.community_name == communities.community_name
   -> COMMUNITY_REFERENCE, is_proxy=False, community_id set.
2. Otherwise, if the patient has a diagnosing facility (already resolved to
   healthcare_facilities.facility_id by the Milestone 3.2 importer):
   -> FACILITY_PROXY, is_proxy=True, facility_id set.
3. Otherwise: unresolved.

patients.address_text and patients.community_name are only ever read here,
never written.

Idempotency, staleness, and history
-------------------------------------
Each run recomputes the desired resolution from current reference data and
reconciles it against the patient's existing current row, if any. Two
things make an existing current row stale, each handled distinctly:

1. The chosen source changed (different reference, or reference type, e.g.
   a community now matches where only a facility did before) — detected by
   comparing location_source + community_id + facility_id.
2. The same reference is still selected, but its own coordinates were
   corrected in place (the community's or facility's row was updated, same
   ID) — detected by comparing the *current* reference geometry against the
   geometry already stored on the existing patient_location row via
   PostGIS ST_Equals, not by comparing identity keys alone.

Outcomes:

- no existing current row, desired found       -> insert it (RESOLVED)
- existing current row matches desired exactly
  (same source/reference AND same geometry)    -> no write (UNCHANGED)
- existing current row's source/reference OR
  its reference's geometry differs from desired -> insert the new row,
  mark the old row is_current=False (UPDATED). Never updates the old row's
  geometry in place — a stale row is superseded, not rewritten, so full
  history (including what was believed true at the time) is preserved.
- desired is unresolved                        -> if a current row exists,
  mark it is_current=False (UNRESOLVED with a write) so
  current_patient_spatial_points stops exposing this patient; if no
  current row exists, this is a pure no-op (UNRESOLVED, no write).

Concurrency
------------
Each patient's reconciliation acquires a `SELECT ... FOR UPDATE` row lock
on that patient's row in `patients` (not a table-level lock) before reading
or writing anything for them, serializing concurrent resolution attempts
for the *same* patient without blocking resolution of other patients. The
partial unique index on patient_locations (one is_current=TRUE row per
patient) remains the actual final invariant regardless — the lock exists to
avoid two concurrent transactions racing into a real constraint violation
(reported as a misleading FAILED outcome) under ordinary same-patient
contention, not to replace that invariant.

Transaction boundary — read this before assuming more than it says
-----------------------------------------------------------------------
Each patient is resolved inside its own SAVEPOINT (`Session.begin_nested()`),
mirroring ingestion/importer.py: if resolving one patient raises, only that
savepoint is rolled back and the batch continues. The "downgrade old row +
insert new row" pair for a single patient is atomic *as SQL statements* —
if the second half fails, the first half is rolled back too, since both
happened inside the same savepoint.

However, `Session.flush()` flushes the *entire* session's pending state, not
just the current patient's changes. If the caller passes in a session that
already has other, unrelated pending changes when calling
`resolve_patient_locations`, those changes can be swept into one of these
per-patient flushes and, if that patient's savepoint is then rolled back,
rolled back along with it. This module does not attempt to isolate
unrelated caller changes from a failing patient's savepoint — it isolates
only the SQL this module itself issues while resolving one patient at a
time. Call it with a session that has no unrelated uncommitted work if that
distinction matters to the caller.

These functions flush but never commit — the caller owns the transaction.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import Community, HealthcareFacility, LocationSource, Patient, PatientLocation


class ResolutionAction(enum.StrEnum):
    RESOLVED = "RESOLVED"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    UNRESOLVED = "UNRESOLVED"
    NOT_FOUND = "NOT_FOUND"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ResolutionOutcome:
    """The result of resolving a single patient's current location."""

    patient_id: str
    action: ResolutionAction
    location_source: LocationSource | None = None
    message: str | None = None


@dataclass(frozen=True)
class ResolutionBatchResult:
    outcomes: list[ResolutionOutcome]

    @property
    def resolved(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ResolutionAction.RESOLVED)

    @property
    def updated(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ResolutionAction.UPDATED)

    @property
    def unchanged(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ResolutionAction.UNCHANGED)

    @property
    def unresolved(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ResolutionAction.UNRESOLVED)

    @property
    def not_found(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ResolutionAction.NOT_FOUND)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.action == ResolutionAction.FAILED)


@dataclass(frozen=True)
class _DesiredLocation:
    source: LocationSource
    is_proxy: bool
    community_id: int | None
    facility_id: int | None
    location: object


def _compute_desired_location(session: Session, patient: Patient) -> _DesiredLocation | None:
    if patient.community_name:
        community = session.execute(
            select(Community).where(Community.community_name == patient.community_name)
        ).scalar_one_or_none()
        if community is not None:
            return _DesiredLocation(
                source=LocationSource.COMMUNITY_REFERENCE,
                is_proxy=False,
                community_id=community.community_id,
                facility_id=None,
                location=community.location,
            )

    if patient.diagnosing_facility_id is not None:
        facility = session.get(HealthcareFacility, patient.diagnosing_facility_id)
        if facility is not None:
            return _DesiredLocation(
                source=LocationSource.FACILITY_PROXY,
                is_proxy=True,
                community_id=None,
                facility_id=facility.facility_id,
                location=facility.location,
            )

    return None


def _reference_geometry_changed(
    session: Session, existing: PatientLocation, desired: _DesiredLocation
) -> bool:
    """Compare the *current* reference geometry against what's already
    stored on the existing row, using PostGIS rather than comparing Python
    geometry objects (whose equality is not reliable across separate
    fetches)."""
    if desired.source == LocationSource.COMMUNITY_REFERENCE:
        reference_table = Community
        reference_id_column = Community.community_id
        reference_id = desired.community_id
    else:
        reference_table = HealthcareFacility
        reference_id_column = HealthcareFacility.facility_id
        reference_id = desired.facility_id

    existing_wkt = session.execute(
        select(func.ST_AsText(PatientLocation.location)).where(
            PatientLocation.patient_location_id == existing.patient_location_id
        )
    ).scalar_one()
    reference_wkt = session.execute(
        select(func.ST_AsText(reference_table.location)).where(
            reference_id_column == reference_id
        )
    ).scalar_one()
    return existing_wkt != reference_wkt


def _matches(session: Session, existing: PatientLocation, desired: _DesiredLocation) -> bool:
    if (
        existing.location_source != desired.source
        or existing.community_id != desired.community_id
        or existing.facility_id != desired.facility_id
    ):
        return False
    return not _reference_geometry_changed(session, existing, desired)


def _get_current_location(session: Session, patient_id: str) -> PatientLocation | None:
    return session.execute(
        select(PatientLocation).where(
            PatientLocation.patient_id == patient_id, PatientLocation.is_current.is_(True)
        )
    ).scalar_one_or_none()


def _resolve_patient(session: Session, patient: Patient) -> ResolutionOutcome:
    try:
        with session.begin_nested():
            # Row-level lock (not table-level): serializes concurrent
            # resolution attempts for this one patient. Refresh afterward
            # so a stale in-memory copy of this patient can't drive the
            # decision below once the lock is held.
            session.execute(
                select(Patient.patient_id)
                .where(Patient.patient_id == patient.patient_id)
                .with_for_update()
            )
            session.refresh(patient)

            existing_current = _get_current_location(session, patient.patient_id)
            desired = _compute_desired_location(session, patient)

            if desired is None:
                if existing_current is not None:
                    existing_current.is_current = False
                    session.flush()
                return ResolutionOutcome(patient.patient_id, ResolutionAction.UNRESOLVED)

            if existing_current is not None and _matches(session, existing_current, desired):
                return ResolutionOutcome(
                    patient.patient_id, ResolutionAction.UNCHANGED, desired.source
                )

            # Two explicit flushes, in this order, so the partial unique
            # index (one is_current=TRUE row per patient) is never violated
            # even transiently: the old current row is downgraded and
            # flushed before the new current row is added.
            if existing_current is not None:
                existing_current.is_current = False
                session.flush()

            session.add(
                PatientLocation(
                    patient_id=patient.patient_id,
                    location=desired.location,
                    location_source=desired.source,
                    community_id=desired.community_id,
                    facility_id=desired.facility_id,
                    is_proxy=desired.is_proxy,
                    is_current=True,
                )
            )
            session.flush()

            action = (
                ResolutionAction.UPDATED
                if existing_current is not None
                else ResolutionAction.RESOLVED
            )
            return ResolutionOutcome(patient.patient_id, action, desired.source)
    except SQLAlchemyError as exc:
        return ResolutionOutcome(patient.patient_id, ResolutionAction.FAILED, None, str(exc))


def resolve_patient_locations(
    session: Session, patient_ids: Sequence[str] | None = None
) -> ResolutionBatchResult:
    """Resolve (or re-resolve) the current location for the given patients.

    patient_ids=None resolves every patient in the database, in ascending
    patient_id order. If patient_ids is given, results are returned in that
    same, caller-specified order (not whatever order the database happens
    to return rows in); duplicate IDs in patient_ids are processed once
    each, in their first-occurrence position; an ID with no matching
    patient is reported as outcome NOT_FOUND rather than being silently
    dropped. Does not commit — the caller owns the outer transaction.
    """
    if patient_ids is None:
        patients = session.execute(
            select(Patient).order_by(Patient.patient_id)
        ).scalars().all()
        return ResolutionBatchResult([_resolve_patient(session, patient) for patient in patients])

    requested_ids = list(dict.fromkeys(patient_ids))
    patients_by_id = {
        patient.patient_id: patient
        for patient in session.execute(
            select(Patient).where(Patient.patient_id.in_(requested_ids))
        ).scalars()
    }

    outcomes = [
        _resolve_patient(session, patients_by_id[requested_id])
        if requested_id in patients_by_id
        else ResolutionOutcome(requested_id, ResolutionAction.NOT_FOUND)
        for requested_id in requested_ids
    ]
    return ResolutionBatchResult(outcomes)
