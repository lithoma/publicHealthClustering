"""Integration tests for Milestone 4 patient location resolution.

Requires the local PostgreSQL/PostGIS container (docker compose up -d db).
Each test runs inside an outer DB transaction that is rolled back in the
fixture teardown, so nothing written by these tests is ever actually
committed to the shared dev database.
"""

from __future__ import annotations

import threading
from datetime import date

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from public_health_clustering.db import create_db_engine
from public_health_clustering.location_resolution import (
    ResolutionAction,
    resolve_patient_locations,
)
from public_health_clustering.models import (
    Community,
    HealthcareFacility,
    LocationSource,
    Patient,
    PatientLocation,
)


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


def _make_community(session: Session, name: str, **overrides: object) -> Community:
    community = Community(
        community_name=name,
        full_address=overrides.get("full_address", "1 Test Rd, Cedarbrook"),
        longitude=overrides.get("longitude", -98.5),
        latitude=overrides.get("latitude", 39.5),
    )
    session.add(community)
    session.flush()
    return community


def _make_facility(session: Session, code: str, **overrides: object) -> HealthcareFacility:
    facility = HealthcareFacility(
        facility_code=code,
        facility_name=overrides.get("facility_name", "Test Facility"),
        facility_type=overrides.get("facility_type", "CLINIC"),
        full_address=overrides.get("full_address", "1 Test Way, Cedarbrook"),
        longitude=overrides.get("longitude", -98.4),
        latitude=overrides.get("latitude", 39.4),
    )
    session.add(facility)
    session.flush()
    return facility


def _make_patient(session: Session, patient_id: str, **overrides: object) -> Patient:
    patient = Patient(
        patient_id=patient_id,
        name=overrides.get("name", "Test Patient"),
        illness=overrides.get("illness", "Measles"),
        diagnosis_date=overrides.get("diagnosis_date", date(2026, 9, 1)),
        address_text=overrides.get("address_text", "1 Test St, Cedarbrook"),
        community_name=overrides.get("community_name"),
        diagnosing_facility_id=overrides.get("diagnosing_facility_id"),
    )
    session.add(patient)
    session.flush()
    return patient


def _current_location(session: Session, patient_id: str) -> PatientLocation | None:
    return session.execute(
        select(PatientLocation).where(
            PatientLocation.patient_id == patient_id, PatientLocation.is_current.is_(True)
        )
    ).scalar_one_or_none()


def _all_locations(session: Session, patient_id: str) -> list[PatientLocation]:
    return list(
        session.execute(
            select(PatientLocation)
            .where(PatientLocation.patient_id == patient_id)
            .order_by(PatientLocation.patient_location_id)
        ).scalars()
    )


# --- exact community match ---------------------------------------------------------


def test_exact_community_match_resolves_to_community_reference(db_session: Session) -> None:
    community = _make_community(db_session, "Riverside Heights")
    _make_patient(db_session, "PAT-COMM-001", community_name="Riverside Heights")

    result = resolve_patient_locations(db_session, ["PAT-COMM-001"])

    assert result.resolved == 1
    assert result.outcomes[0].action == ResolutionAction.RESOLVED
    assert result.outcomes[0].location_source == LocationSource.COMMUNITY_REFERENCE

    row = _current_location(db_session, "PAT-COMM-001")
    assert row is not None
    assert row.location_source == LocationSource.COMMUNITY_REFERENCE
    assert row.is_proxy is False
    assert row.community_id == community.community_id
    assert row.facility_id is None

    # the location geometry was actually copied from the matched community
    patient_wkt = db_session.execute(
        select(func.ST_AsText(PatientLocation.location)).where(
            PatientLocation.patient_id == "PAT-COMM-001"
        )
    ).scalar_one()
    community_wkt = db_session.execute(
        select(func.ST_AsText(Community.location)).where(
            Community.community_id == community.community_id
        )
    ).scalar_one()
    assert patient_wkt == community_wkt


# --- facility fallback ---------------------------------------------------------------


def test_no_community_match_falls_back_to_facility_proxy(db_session: Session) -> None:
    facility = _make_facility(db_session, "FAC-FALLBACK-001")
    _make_patient(
        db_session,
        "PAT-FAC-001",
        community_name="Nonexistent Place",
        diagnosing_facility_id=facility.facility_id,
    )

    result = resolve_patient_locations(db_session, ["PAT-FAC-001"])

    assert result.resolved == 1
    assert result.outcomes[0].location_source == LocationSource.FACILITY_PROXY

    row = _current_location(db_session, "PAT-FAC-001")
    assert row is not None
    assert row.location_source == LocationSource.FACILITY_PROXY
    assert row.is_proxy is True
    assert row.facility_id == facility.facility_id
    assert row.community_id is None


# --- community preferred over facility ------------------------------------------------


def test_community_match_is_preferred_over_available_facility(db_session: Session) -> None:
    community = _make_community(db_session, "Harborview")
    facility = _make_facility(db_session, "FAC-PREFER-001")
    _make_patient(
        db_session,
        "PAT-PREFER-001",
        community_name="Harborview",
        diagnosing_facility_id=facility.facility_id,
    )

    result = resolve_patient_locations(db_session, ["PAT-PREFER-001"])

    assert result.outcomes[0].location_source == LocationSource.COMMUNITY_REFERENCE
    row = _current_location(db_session, "PAT-PREFER-001")
    assert row.community_id == community.community_id
    assert row.facility_id is None
    assert row.is_proxy is False


# --- unresolved patient ---------------------------------------------------------------


def test_patient_with_no_community_or_facility_is_unresolved(db_session: Session) -> None:
    _make_patient(db_session, "PAT-NONE-001", community_name=None, diagnosing_facility_id=None)

    result = resolve_patient_locations(db_session, ["PAT-NONE-001"])

    assert result.unresolved == 1
    assert result.outcomes[0].action == ResolutionAction.UNRESOLVED
    assert _current_location(db_session, "PAT-NONE-001") is None


def test_patient_with_nonmatching_community_and_no_facility_is_unresolved(
    db_session: Session,
) -> None:
    _make_patient(
        db_session, "PAT-NOMATCH-001", community_name="Ghost Town", diagnosing_facility_id=None
    )

    result = resolve_patient_locations(db_session, ["PAT-NOMATCH-001"])

    assert result.unresolved == 1
    assert _current_location(db_session, "PAT-NOMATCH-001") is None


# --- idempotent rerun ------------------------------------------------------------------


def test_rerun_with_unchanged_data_is_idempotent(db_session: Session) -> None:
    _make_community(db_session, "Maple Grove")
    _make_patient(db_session, "PAT-IDEMP-001", community_name="Maple Grove")

    first = resolve_patient_locations(db_session, ["PAT-IDEMP-001"])
    assert first.outcomes[0].action == ResolutionAction.RESOLVED
    first_row_id = _current_location(db_session, "PAT-IDEMP-001").patient_location_id

    second = resolve_patient_locations(db_session, ["PAT-IDEMP-001"])
    assert second.outcomes[0].action == ResolutionAction.UNCHANGED

    third = resolve_patient_locations(db_session, ["PAT-IDEMP-001"])
    assert third.outcomes[0].action == ResolutionAction.UNCHANGED

    rows = _all_locations(db_session, "PAT-IDEMP-001")
    assert len(rows) == 1
    assert rows[0].patient_location_id == first_row_id
    assert rows[0].is_current is True


def test_rerun_after_unresolved_stays_unresolved_with_no_rows(db_session: Session) -> None:
    _make_patient(db_session, "PAT-STILL-NONE", community_name=None, diagnosing_facility_id=None)

    resolve_patient_locations(db_session, ["PAT-STILL-NONE"])
    second = resolve_patient_locations(db_session, ["PAT-STILL-NONE"])

    assert second.outcomes[0].action == ResolutionAction.UNRESOLVED
    assert _all_locations(db_session, "PAT-STILL-NONE") == []


# --- proxy replaced by community --------------------------------------------------------


def test_facility_proxy_is_superseded_once_a_matching_community_appears(
    db_session: Session,
) -> None:
    facility = _make_facility(db_session, "FAC-SUPERSEDE-001")
    _make_patient(
        db_session,
        "PAT-SUPERSEDE-001",
        community_name="Later Community",
        diagnosing_facility_id=facility.facility_id,
    )

    first = resolve_patient_locations(db_session, ["PAT-SUPERSEDE-001"])
    assert first.outcomes[0].location_source == LocationSource.FACILITY_PROXY
    proxy_row = _current_location(db_session, "PAT-SUPERSEDE-001")
    proxy_row_id = proxy_row.patient_location_id

    # the community now becomes available (e.g. a later community import)
    community = _make_community(db_session, "Later Community")

    second = resolve_patient_locations(db_session, ["PAT-SUPERSEDE-001"])
    assert second.outcomes[0].action == ResolutionAction.UPDATED
    assert second.outcomes[0].location_source == LocationSource.COMMUNITY_REFERENCE

    current = _current_location(db_session, "PAT-SUPERSEDE-001")
    assert current.location_source == LocationSource.COMMUNITY_REFERENCE
    assert current.community_id == community.community_id
    assert current.patient_location_id != proxy_row_id

    # history preserved: the old proxy row still exists, just not current
    all_rows = _all_locations(db_session, "PAT-SUPERSEDE-001")
    assert len(all_rows) == 2
    old = next(r for r in all_rows if r.patient_location_id == proxy_row_id)
    assert old.is_current is False
    assert old.location_source == LocationSource.FACILITY_PROXY

    # re-running again now is idempotent against the new state
    third = resolve_patient_locations(db_session, ["PAT-SUPERSEDE-001"])
    assert third.outcomes[0].action == ResolutionAction.UNCHANGED
    assert len(_all_locations(db_session, "PAT-SUPERSEDE-001")) == 2


# --- one current location only ----------------------------------------------------------


def test_at_most_one_current_location_per_patient_throughout(db_session: Session) -> None:
    facility = _make_facility(db_session, "FAC-ONECURRENT-001")
    patient_id = "PAT-ONECURRENT-001"
    _make_patient(
        db_session, patient_id, community_name=None, diagnosing_facility_id=None
    )

    def current_count() -> int:
        return len(
            db_session.execute(
                select(PatientLocation).where(
                    PatientLocation.patient_id == patient_id,
                    PatientLocation.is_current.is_(True),
                )
            )
            .scalars()
            .all()
        )

    resolve_patient_locations(db_session, [patient_id])  # unresolved
    assert current_count() == 0

    db_session.execute(
        Patient.__table__.update()
        .where(Patient.patient_id == patient_id)
        .values(diagnosing_facility_id=facility.facility_id)
    )
    db_session.expire_all()
    resolve_patient_locations(db_session, [patient_id])  # now facility proxy
    assert current_count() == 1

    community = _make_community(db_session, "One Current Community")
    db_session.execute(
        Patient.__table__.update()
        .where(Patient.patient_id == patient_id)
        .values(community_name=community.community_name)
    )
    db_session.expire_all()
    resolve_patient_locations(db_session, [patient_id])  # now community reference
    assert current_count() == 1

    resolve_patient_locations(db_session, [patient_id])  # idempotent rerun
    assert current_count() == 1

    assert len(_all_locations(db_session, patient_id)) == 2


# --- transaction rollback ----------------------------------------------------------------


def test_row_level_failure_rolls_back_both_the_supersede_and_the_insert(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    facility = _make_facility(db_session, "FAC-ROLLBACK-001")
    _make_patient(
        db_session,
        "PAT-ROLLBACK-A",
        community_name="Rollback Community",
        diagnosing_facility_id=facility.facility_id,
    )
    _make_patient(db_session, "PAT-ROLLBACK-B", community_name=None, diagnosing_facility_id=None)

    # establish an existing current FACILITY_PROXY row for A, since only
    # the facility exists yet (the community is added afterward below)
    resolve_patient_locations(db_session, ["PAT-ROLLBACK-A"])
    proxy_row_id = _current_location(db_session, "PAT-ROLLBACK-A").patient_location_id

    community = _make_community(db_session, "Rollback Community")
    facility_2 = _make_facility(db_session, "FAC-ROLLBACK-002")
    _make_patient(
        db_session,
        "PAT-ROLLBACK-C",
        community_name=None,
        diagnosing_facility_id=facility_2.facility_id,
    )

    real_flush = db_session.flush

    def flaky_flush(*args: object, **kwargs: object) -> None:
        for obj in db_session.new:
            if (
                isinstance(obj, PatientLocation)
                and obj.patient_id == "PAT-ROLLBACK-A"
                and obj.location_source == LocationSource.COMMUNITY_REFERENCE
            ):
                raise SQLAlchemyError("simulated database failure")
        real_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", flaky_flush)

    result = resolve_patient_locations(
        db_session, ["PAT-ROLLBACK-A", "PAT-ROLLBACK-B", "PAT-ROLLBACK-C"]
    )

    outcomes_by_id = {o.patient_id: o for o in result.outcomes}
    assert outcomes_by_id["PAT-ROLLBACK-A"].action == ResolutionAction.FAILED
    assert outcomes_by_id["PAT-ROLLBACK-B"].action == ResolutionAction.UNRESOLVED
    assert outcomes_by_id["PAT-ROLLBACK-C"].action == ResolutionAction.RESOLVED

    monkeypatch.undo()  # restore real flush before further queries/assertions

    # A: the supersede was rolled back along with the failed insert — the
    # original proxy row is still current, not orphaned as non-current.
    a_rows = _all_locations(db_session, "PAT-ROLLBACK-A")
    assert len(a_rows) == 1
    assert a_rows[0].patient_location_id == proxy_row_id
    assert a_rows[0].is_current is True
    assert a_rows[0].location_source == LocationSource.FACILITY_PROXY

    # B: genuinely unresolved, no row
    assert _all_locations(db_session, "PAT-ROLLBACK-B") == []

    # C: unaffected by A's failure — session was not poisoned
    c_row = _current_location(db_session, "PAT-ROLLBACK-C")
    assert c_row is not None
    assert c_row.location_source == LocationSource.FACILITY_PROXY


# --- stale current location cleared on becoming unresolved (review fix 1) -------------


def test_current_facility_proxy_is_cleared_when_patient_becomes_unresolved(
    db_session: Session,
) -> None:
    facility = _make_facility(db_session, "FAC-CLEAR-001")
    _make_patient(
        db_session, "PAT-CLEAR-PROXY", community_name=None, diagnosing_facility_id=facility.facility_id
    )
    resolve_patient_locations(db_session, ["PAT-CLEAR-PROXY"])
    stale_id = _current_location(db_session, "PAT-CLEAR-PROXY").patient_location_id

    # the facility reference goes away (e.g. corrected on re-import)
    db_session.execute(
        Patient.__table__.update()
        .where(Patient.patient_id == "PAT-CLEAR-PROXY")
        .values(diagnosing_facility_id=None)
    )
    db_session.expire_all()

    result = resolve_patient_locations(db_session, ["PAT-CLEAR-PROXY"])

    assert result.outcomes[0].action == ResolutionAction.UNRESOLVED
    assert _current_location(db_session, "PAT-CLEAR-PROXY") is None

    # history preserved: the old row still exists, just not current
    rows = _all_locations(db_session, "PAT-CLEAR-PROXY")
    assert len(rows) == 1
    assert rows[0].patient_location_id == stale_id
    assert rows[0].is_current is False

    # current_patient_spatial_points must no longer expose this patient
    assert _view_row_count(db_session, "PAT-CLEAR-PROXY") == 0


def test_current_community_reference_is_cleared_when_patient_becomes_unresolved(
    db_session: Session,
) -> None:
    _make_community(db_session, "Clear Me Community")
    _make_patient(db_session, "PAT-CLEAR-COMM", community_name="Clear Me Community")
    resolve_patient_locations(db_session, ["PAT-CLEAR-COMM"])
    stale_id = _current_location(db_session, "PAT-CLEAR-COMM").patient_location_id

    # the community reference goes away (e.g. patient record corrected)
    db_session.execute(
        Patient.__table__.update()
        .where(Patient.patient_id == "PAT-CLEAR-COMM")
        .values(community_name=None)
    )
    db_session.expire_all()

    result = resolve_patient_locations(db_session, ["PAT-CLEAR-COMM"])

    assert result.outcomes[0].action == ResolutionAction.UNRESOLVED
    assert _current_location(db_session, "PAT-CLEAR-COMM") is None

    rows = _all_locations(db_session, "PAT-CLEAR-COMM")
    assert len(rows) == 1
    assert rows[0].patient_location_id == stale_id
    assert rows[0].is_current is False
    assert rows[0].location_source == LocationSource.COMMUNITY_REFERENCE

    assert _view_row_count(db_session, "PAT-CLEAR-COMM") == 0


def _view_row_count(session: Session, patient_id: str) -> int:
    # current_patient_spatial_points is a plain SQL view (not ORM-mapped);
    # query it directly via raw text.
    return session.execute(
        text(
            "SELECT count(*) FROM current_patient_spatial_points "
            "WHERE patient_id = :patient_id"
        ),
        {"patient_id": patient_id},
    ).scalar_one()


# --- reference coordinate correction (review fix 2) -----------------------------------


def test_community_coordinate_correction_creates_new_current_row(db_session: Session) -> None:
    community = _make_community(db_session, "Correction Community", longitude=-98.5, latitude=39.5)
    _make_patient(db_session, "PAT-COORD-COMM", community_name="Correction Community")

    first = resolve_patient_locations(db_session, ["PAT-COORD-COMM"])
    assert first.outcomes[0].action == ResolutionAction.RESOLVED
    old_row = _current_location(db_session, "PAT-COORD-COMM")
    old_row_id = old_row.patient_location_id

    # re-running with nothing changed is still a no-op
    assert resolve_patient_locations(db_session, ["PAT-COORD-COMM"]).outcomes[0].action == (
        ResolutionAction.UNCHANGED
    )

    community.longitude = -97.0
    community.latitude = 40.0
    db_session.flush()
    db_session.expire_all()

    second = resolve_patient_locations(db_session, ["PAT-COORD-COMM"])
    assert second.outcomes[0].action == ResolutionAction.UPDATED
    assert second.outcomes[0].location_source == LocationSource.COMMUNITY_REFERENCE

    new_row = _current_location(db_session, "PAT-COORD-COMM")
    assert new_row.patient_location_id != old_row_id
    assert new_row.community_id == community.community_id

    rows = _all_locations(db_session, "PAT-COORD-COMM")
    assert len(rows) == 2
    old = next(r for r in rows if r.patient_location_id == old_row_id)
    assert old.is_current is False

    old_wkt, new_wkt = (
        db_session.execute(
            select(func.ST_AsText(PatientLocation.location)).where(
                PatientLocation.patient_location_id == row_id
            )
        ).scalar_one()
        for row_id in (old_row_id, new_row.patient_location_id)
    )
    assert old_wkt != new_wkt
    assert old_wkt == "POINT(-98.5 39.5)"
    assert new_wkt == "POINT(-97 40)"

    # re-running again is idempotent against the corrected coordinates
    third = resolve_patient_locations(db_session, ["PAT-COORD-COMM"])
    assert third.outcomes[0].action == ResolutionAction.UNCHANGED
    assert len(_all_locations(db_session, "PAT-COORD-COMM")) == 2


def test_facility_coordinate_correction_creates_new_current_row(db_session: Session) -> None:
    facility = _make_facility(db_session, "FAC-COORD-001", longitude=-98.4, latitude=39.4)
    _make_patient(
        db_session,
        "PAT-COORD-FAC",
        community_name=None,
        diagnosing_facility_id=facility.facility_id,
    )

    first = resolve_patient_locations(db_session, ["PAT-COORD-FAC"])
    assert first.outcomes[0].action == ResolutionAction.RESOLVED
    old_row_id = _current_location(db_session, "PAT-COORD-FAC").patient_location_id

    facility.longitude = -96.0
    facility.latitude = 41.0
    db_session.flush()
    db_session.expire_all()

    second = resolve_patient_locations(db_session, ["PAT-COORD-FAC"])
    assert second.outcomes[0].action == ResolutionAction.UPDATED
    assert second.outcomes[0].location_source == LocationSource.FACILITY_PROXY

    new_row = _current_location(db_session, "PAT-COORD-FAC")
    assert new_row.patient_location_id != old_row_id
    assert new_row.facility_id == facility.facility_id

    rows = _all_locations(db_session, "PAT-COORD-FAC")
    assert len(rows) == 2
    old = next(r for r in rows if r.patient_location_id == old_row_id)
    assert old.is_current is False
    assert old.location_source == LocationSource.FACILITY_PROXY

    third = resolve_patient_locations(db_session, ["PAT-COORD-FAC"])
    assert third.outcomes[0].action == ResolutionAction.UNCHANGED
    assert len(_all_locations(db_session, "PAT-COORD-FAC")) == 2


# --- deterministic batch semantics (review fix 4) --------------------------------------


def test_unknown_requested_patient_id_is_reported_not_silently_dropped(
    db_session: Session,
) -> None:
    _make_patient(db_session, "PAT-KNOWN-001", community_name=None, diagnosing_facility_id=None)

    result = resolve_patient_locations(
        db_session, ["PAT-KNOWN-001", "PAT-DOES-NOT-EXIST"]
    )

    assert len(result.outcomes) == 2
    assert result.outcomes[0].patient_id == "PAT-KNOWN-001"
    assert result.outcomes[0].action == ResolutionAction.UNRESOLVED
    assert result.outcomes[1].patient_id == "PAT-DOES-NOT-EXIST"
    assert result.outcomes[1].action == ResolutionAction.NOT_FOUND
    assert result.not_found == 1


def test_duplicate_requested_patient_ids_are_processed_once_in_first_occurrence_order(
    db_session: Session,
) -> None:
    _make_community(db_session, "Dedup Community")
    _make_patient(db_session, "PAT-DEDUP-001", community_name="Dedup Community")

    result = resolve_patient_locations(
        db_session, ["PAT-DEDUP-001", "PAT-DEDUP-001", "PAT-DEDUP-001"]
    )

    assert len(result.outcomes) == 1
    assert result.outcomes[0].patient_id == "PAT-DEDUP-001"
    assert result.outcomes[0].action == ResolutionAction.RESOLVED
    assert len(_all_locations(db_session, "PAT-DEDUP-001")) == 1


def test_requested_order_is_preserved_regardless_of_database_row_order(
    db_session: Session,
) -> None:
    for patient_id in ("PAT-ORDER-C", "PAT-ORDER-A", "PAT-ORDER-B"):
        _make_patient(db_session, patient_id, community_name=None, diagnosing_facility_id=None)

    requested = ["PAT-ORDER-B", "PAT-ORDER-C", "PAT-ORDER-A"]
    result = resolve_patient_locations(db_session, requested)

    assert [o.patient_id for o in result.outcomes] == requested


# --- concurrent resolution for the same patient (review fix 3/6) ----------------------


def test_concurrent_resolution_of_the_same_patient_does_not_fail_or_duplicate() -> None:
    engine = create_db_engine()
    setup_session = Session(bind=engine)
    patient_id = "PAT-CONCURRENT-001"
    community_name = "Concurrency Test Community"
    try:
        setup_session.add(
            Community(
                community_name=community_name,
                full_address="1 Concurrency Rd, Cedarbrook",
                longitude=-98.5,
                latitude=39.5,
            )
        )
        setup_session.add(
            Patient(
                patient_id=patient_id,
                name="Concurrency Test Patient",
                illness="Measles",
                diagnosis_date=date(2026, 9, 1),
                community_name=community_name,
            )
        )
        setup_session.commit()

        results: list[object] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            thread_session = Session(bind=engine)
            try:
                barrier.wait(timeout=5)
                result = resolve_patient_locations(thread_session, [patient_id])
                thread_session.commit()
                results.append(result)
            except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
                thread_session.rollback()
                errors.append(exc)
            finally:
                thread_session.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"worker threads raised: {errors}"
        actions = sorted(r.outcomes[0].action for r in results)
        assert ResolutionAction.FAILED not in actions
        assert actions == sorted([ResolutionAction.RESOLVED, ResolutionAction.UNCHANGED])

        with Session(bind=engine) as verify_session:
            current_rows = verify_session.execute(
                select(PatientLocation).where(
                    PatientLocation.patient_id == patient_id,
                    PatientLocation.is_current.is_(True),
                )
            ).scalars().all()
            assert len(current_rows) == 1
    finally:
        setup_session.rollback()
        cleanup_session = Session(bind=engine)
        cleanup_session.execute(delete(PatientLocation).where(PatientLocation.patient_id == patient_id))
        cleanup_session.execute(delete(Patient).where(Patient.patient_id == patient_id))
        cleanup_session.execute(delete(Community).where(Community.community_name == community_name))
        cleanup_session.commit()
        cleanup_session.close()
        setup_session.close()
        engine.dispose()
