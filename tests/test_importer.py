"""Integration tests for Milestone 3.2 database import.

Requires the local PostgreSQL/PostGIS container (docker compose up -d db).
Each test runs inside an outer DB transaction that is rolled back in the
fixture teardown, so nothing written by these tests is ever actually
committed to the shared dev database, regardless of what the code under
test does with session.begin_nested()/flush() internally.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from public_health_clustering.db import create_db_engine
from public_health_clustering.ingestion import (
    ImportAction,
    ValidatedCommunity,
    ValidatedFacility,
    ValidatedPatient,
    import_communities,
    import_dataset,
    import_facilities,
    import_patients,
    load_communities,
    load_facilities,
    load_patients,
)
from public_health_clustering.models import Community, HealthcareFacility, Patient

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "data" / "sample"


@pytest.fixture()
def db_session():
    engine = create_db_engine()
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _community(**overrides: object) -> ValidatedCommunity:
    defaults = dict(
        row_number=2,
        community_code="COMM-TEST",
        community_name="Test Community",
        full_address="1 Test Rd, Cedarbrook",
        longitude=-98.5,
        latitude=39.5,
    )
    return ValidatedCommunity(**{**defaults, **overrides})


def _facility(**overrides: object) -> ValidatedFacility:
    defaults = dict(
        row_number=2,
        facility_code="FAC-TEST",
        facility_name="Test Facility",
        facility_type="CLINIC",
        full_address="1 Test Way, Cedarbrook",
        longitude=-98.4,
        latitude=39.4,
    )
    return ValidatedFacility(**{**defaults, **overrides})


def _patient(**overrides: object) -> ValidatedPatient:
    defaults = dict(
        row_number=2,
        patient_id="PAT-TEST-001",
        name="Test Patient",
        illness="Measles",
        diagnosis_date=date(2026, 9, 1),
        address_text="1 Test St, Cedarbrook",
        community_name="",
        diagnosing_facility_code="",
    )
    return ValidatedPatient(**{**defaults, **overrides})


# --- insert valid sample records -------------------------------------------------


def test_import_sample_dataset_inserts_all_records(db_session: Session) -> None:
    communities, c_errors = load_communities(SAMPLE_DIR / "communities.csv")
    facilities, f_errors = load_facilities(SAMPLE_DIR / "healthcare_facilities.csv")
    patients, p_errors = load_patients(SAMPLE_DIR / "patients.csv")
    assert c_errors == f_errors == p_errors == []

    result = import_dataset(db_session, communities, facilities, patients)

    assert result.communities.inserted == 5
    assert result.facilities.inserted == 4
    assert result.patients.inserted == 16
    assert result.communities.failed == result.facilities.failed == result.patients.failed == 0

    assert db_session.execute(select(Community)).scalars().all().__len__() == 5
    assert db_session.execute(select(HealthcareFacility)).scalars().all().__len__() == 4
    assert db_session.execute(select(Patient)).scalars().all().__len__() == 16


# --- repeat import is idempotent --------------------------------------------------


def test_repeat_import_is_a_no_op(db_session: Session) -> None:
    record = _community()
    first = import_communities(db_session, [record])
    assert first.inserted == 1

    second = import_communities(db_session, [record])
    assert second.inserted == 0
    assert second.unchanged == 1

    rows = db_session.execute(
        select(Community).where(Community.community_name == "Test Community")
    ).scalars().all()
    assert len(rows) == 1


def test_repeat_import_of_full_dataset_produces_only_unchanged(db_session: Session) -> None:
    communities, _ = load_communities(SAMPLE_DIR / "communities.csv")
    facilities, _ = load_facilities(SAMPLE_DIR / "healthcare_facilities.csv")
    patients, _ = load_patients(SAMPLE_DIR / "patients.csv")

    import_dataset(db_session, communities, facilities, patients)
    second = import_dataset(db_session, communities, facilities, patients)

    assert second.communities.unchanged == 5
    assert second.facilities.unchanged == 4
    assert second.patients.unchanged == 16
    assert second.communities.inserted == second.facilities.inserted == second.patients.inserted == 0

    assert db_session.execute(select(Community)).scalars().all().__len__() == 5
    assert db_session.execute(select(Patient)).scalars().all().__len__() == 16


# --- changed-record update behavior -----------------------------------------------


def test_changed_community_field_triggers_update_in_place(db_session: Session) -> None:
    original = _community(full_address="1 Test Rd, Cedarbrook")
    import_communities(db_session, [original])

    changed = dataclasses.replace(original, full_address="2 Different Rd, Cedarbrook")
    result = import_communities(db_session, [changed])

    assert result.updated == 1
    assert result.inserted == 0

    row = db_session.execute(
        select(Community).where(Community.community_name == "Test Community")
    ).scalar_one()
    assert row.full_address == "2 Different Rd, Cedarbrook"

    # still exactly one row — an update, not a second insert
    count = len(
        db_session.execute(
            select(Community).where(Community.community_name == "Test Community")
        ).scalars().all()
    )
    assert count == 1


def test_changed_patient_field_triggers_update_in_place(db_session: Session) -> None:
    original = _patient(illness="Measles")
    import_patients(db_session, [original])

    changed = dataclasses.replace(original, illness="Pertussis")
    result = import_patients(db_session, [changed])

    assert result.updated == 1
    row = db_session.execute(
        select(Patient).where(Patient.patient_id == "PAT-TEST-001")
    ).scalar_one()
    assert row.illness == "Pertussis"


# --- transaction rollback on row-level failure -------------------------------------


def test_row_level_failure_does_not_abort_the_rest_of_the_batch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fault-inject the flush that would insert PAT-ROLLBACK-002, to simulate
    # a genuine database-level failure (e.g. an unexpected constraint
    # violation) without depending on timing-sensitive concurrent writes.
    # (session.execute(select(...)) also triggers autoflush internally, so
    # the fault is keyed on *which row is pending*, not a call count.)
    first = _patient(patient_id="PAT-ROLLBACK-001", name="First")
    boom = _patient(patient_id="PAT-ROLLBACK-002", name="Boom")
    third = _patient(patient_id="PAT-ROLLBACK-003", name="Third")

    real_flush = db_session.flush

    def flaky_flush(*args: object, **kwargs: object) -> None:
        if any(
            isinstance(obj, Patient) and obj.patient_id == "PAT-ROLLBACK-002"
            for obj in db_session.new
        ):
            raise SQLAlchemyError("simulated database failure")
        real_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", flaky_flush)

    result = import_patients(db_session, [first, boom, third])

    assert result.inserted == 2
    assert result.failed == 1
    outcomes_by_id = {o.external_id: o.action for o in result.outcomes}
    assert outcomes_by_id["PAT-ROLLBACK-001"] == ImportAction.INSERTED
    assert outcomes_by_id["PAT-ROLLBACK-002"] == ImportAction.FAILED
    assert outcomes_by_id["PAT-ROLLBACK-003"] == ImportAction.INSERTED

    # the failed row's work was rolled back to its savepoint — never persisted
    boom_row = db_session.execute(
        select(Patient).where(Patient.patient_id == "PAT-ROLLBACK-002")
    ).scalar_one_or_none()
    assert boom_row is None

    # rows before and after the failure both survived — no partial batch abort
    assert (
        db_session.execute(
            select(Patient).where(Patient.patient_id == "PAT-ROLLBACK-001")
        ).scalar_one().name
        == "First"
    )
    assert (
        db_session.execute(
            select(Patient).where(Patient.patient_id == "PAT-ROLLBACK-003")
        ).scalar_one().name
        == "Third"
    )


# --- foreign-key handling for diagnosing facility references ----------------------


def test_patient_with_valid_facility_code_resolves_the_foreign_key(db_session: Session) -> None:
    facility = _facility(facility_code="FAC-RESOLVE-001")
    import_facilities(db_session, [facility])

    patient = _patient(patient_id="PAT-FK-001", diagnosing_facility_code="FAC-RESOLVE-001")
    result = import_patients(db_session, [patient])

    assert result.inserted == 1
    row = db_session.execute(select(Patient).where(Patient.patient_id == "PAT-FK-001")).scalar_one()
    expected_facility_id = db_session.execute(
        select(HealthcareFacility.facility_id).where(
            HealthcareFacility.facility_code == "FAC-RESOLVE-001"
        )
    ).scalar_one()
    assert row.diagnosing_facility_id == expected_facility_id


def test_patient_with_unresolvable_facility_code_fails_and_is_not_inserted(
    db_session: Session,
) -> None:
    patient = _patient(patient_id="PAT-FK-BAD-001", diagnosing_facility_code="FAC-DOES-NOT-EXIST")
    result = import_patients(db_session, [patient])

    assert result.failed == 1
    assert result.inserted == 0
    assert result.outcomes[0].action == ImportAction.FAILED
    assert "FAC-DOES-NOT-EXIST" in result.outcomes[0].message

    row = db_session.execute(
        select(Patient).where(Patient.patient_id == "PAT-FK-BAD-001")
    ).scalar_one_or_none()
    assert row is None


def test_patient_with_blank_facility_code_is_valid(db_session: Session) -> None:
    patient = _patient(patient_id="PAT-NOFAC-001", diagnosing_facility_code="")
    result = import_patients(db_session, [patient])

    assert result.inserted == 1
    row = db_session.execute(
        select(Patient).where(Patient.patient_id == "PAT-NOFAC-001")
    ).scalar_one()
    assert row.diagnosing_facility_id is None


# --- empty input --------------------------------------------------------------------


def test_empty_input_is_a_no_op_for_all_three_entity_types(db_session: Session) -> None:
    assert import_communities(db_session, []).outcomes == []
    assert import_facilities(db_session, []).outcomes == []
    assert import_patients(db_session, []).outcomes == []

    assert db_session.execute(select(Community)).scalars().all() == []
    assert db_session.execute(select(HealthcareFacility)).scalars().all() == []
    assert db_session.execute(select(Patient)).scalars().all() == []


# --- database remains consistent after failures ------------------------------------


def test_database_is_consistent_after_a_mixed_success_failure_batch(db_session: Session) -> None:
    facility = _facility(facility_code="FAC-CONSIST-001")
    import_facilities(db_session, [facility])

    good = _patient(patient_id="PAT-CONSIST-GOOD", diagnosing_facility_code="FAC-CONSIST-001")
    import_patients(db_session, [good])  # already present, so re-importing it below is a no-op

    bad_fk = _patient(patient_id="PAT-CONSIST-BADFK", diagnosing_facility_code="NOPE")
    another_good = _patient(patient_id="PAT-CONSIST-GOOD2", diagnosing_facility_code="")

    # one unchanged re-import, one new insert, one failure — all in one batch
    result = import_patients(db_session, [good, bad_fk, another_good])

    assert result.unchanged == 1  # good
    assert result.inserted == 1  # another_good
    assert result.failed == 1  # bad_fk

    all_ids = {
        row.patient_id
        for row in db_session.execute(
            select(Patient).where(Patient.patient_id.like("PAT-CONSIST-%"))
        ).scalars()
    }
    assert all_ids == {"PAT-CONSIST-GOOD", "PAT-CONSIST-GOOD2"}
