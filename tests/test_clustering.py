"""Integration tests for Milestone 5 spatial clustering.

Requires the local PostgreSQL/PostGIS container (docker compose up -d db).

run_clustering() owns a separate session and commits each lifecycle
transition. Test setup is therefore committed before invoking it, and the
fixture truncates all application tables before and after each test.

EPSG:32614 (WGS84 / UTM zone 14N) is used throughout as target_srid because
it is a reasonable projection for this repo's synthetic "Cedarbrook" test
coordinates (~-98.5, 39.5) specifically — not because it is a sensible
default for any other region. See clustering.py's module docstring.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from public_health_clustering.clustering import current_patient_spatial_points, run_clustering
from public_health_clustering.db import create_db_engine
from public_health_clustering.models import (
    ClusteringResult,
    ClusteringRun,
    ClusteringRunStatus,
    Community,
    HealthcareFacility,
    LocationSource,
    Patient,
    PatientLocation,
)

TARGET_SRID = 32614  # UTM zone 14N — matches this test suite's synthetic coordinates only


@pytest.fixture()
def db_session():
    engine = create_db_engine()
    table_names = (
        "clustering_results, clustering_runs, patient_locations, patients, "
        "communities, healthcare_facilities"
    )
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
    session = Session(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with engine.begin() as connection:
            connection.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
        engine.dispose()


def _run(session: Session, **kwargs) -> ClusteringRun:
    """Commit visible test setup, then invoke the private-session API."""
    session.commit()
    run = run_clustering(engine=session.get_bind(), **kwargs)
    session.expire_all()
    return run


def _make_patient_with_location(
    session: Session,
    patient_id: str,
    *,
    illness: str,
    diagnosis_date: date,
    lon: float,
    lat: float,
    community: Community,
    is_proxy: bool = False,
    facility: HealthcareFacility | None = None,
) -> Patient:
    patient = Patient(
        patient_id=patient_id,
        name=f"Test {patient_id}",
        illness=illness,
        diagnosis_date=diagnosis_date,
        community_name=community.community_name if not is_proxy else None,
        diagnosing_facility_id=facility.facility_id if facility is not None else None,
    )
    session.add(patient)
    session.flush()

    location = PatientLocation(
        patient_id=patient_id,
        location=f"SRID=4326;POINT({lon} {lat})",
        location_source=LocationSource.FACILITY_PROXY if is_proxy else LocationSource.COMMUNITY_REFERENCE,
        community_id=None if is_proxy else community.community_id,
        facility_id=facility.facility_id if is_proxy else None,
        is_proxy=is_proxy,
        is_current=True,
    )
    session.add(location)
    session.flush()
    return patient


def _make_community(session: Session, name: str = "Cluster Test Community") -> Community:
    community = Community(
        community_name=name, full_address="1 Test Rd, Cedarbrook", longitude=-98.5, latitude=39.5
    )
    session.add(community)
    session.flush()
    return community


def _make_facility(session: Session, code: str = "FAC-CLUSTER-001") -> HealthcareFacility:
    facility = HealthcareFacility(
        facility_code=code,
        facility_name="Cluster Test Facility",
        facility_type="CLINIC",
        full_address="1 Test Way, Cedarbrook",
        longitude=-98.5,
        latitude=39.5,
    )
    session.add(facility)
    session.flush()
    return facility


def _results_by_patient(session: Session, run_id: int) -> dict[str, ClusteringResult]:
    rows = session.execute(
        select(ClusteringResult).where(ClusteringResult.run_id == run_id)
    ).scalars().all()
    return {r.patient_id: r for r in rows}


# --- exact 7-day boundary behavior --------------------------------------------------


def test_half_open_window_boundaries(db_session: Session) -> None:
    community = _make_community(db_session)
    analysis_end = date(2026, 9, 10)

    inside_lower = _make_patient_with_location(
        db_session, "PAT-WIN-LOWER", illness="Measles", diagnosis_date=date(2026, 9, 3),
        lon=-98.5, lat=39.5, community=community,
    )
    inside_upper = _make_patient_with_location(
        db_session, "PAT-WIN-LASTDAY", illness="Measles", diagnosis_date=date(2026, 9, 9),
        lon=-98.5, lat=39.5, community=community,
    )
    outside_lower = _make_patient_with_location(
        db_session, "PAT-WIN-TOOOLD", illness="Measles", diagnosis_date=date(2026, 9, 2),
        lon=-98.5, lat=39.5, community=community,
    )
    outside_upper = _make_patient_with_location(
        db_session, "PAT-WIN-ENDDAY", illness="Measles", diagnosis_date=date(2026, 9, 10),
        lon=-98.5, lat=39.5, community=community,
    )

    run = _run(
        db_session, analysis_end=analysis_end, target_srid=TARGET_SRID,
        eps_meters=100, min_points=1, window_days=7,
    )

    assert run.window_start.date() == date(2026, 9, 3)
    assert run.window_end.date() == date(2026, 9, 10)

    results = _results_by_patient(db_session, run.run_id)
    assert inside_lower.patient_id in results  # exactly on lower bound: included
    assert inside_upper.patient_id in results
    assert outside_lower.patient_id not in results  # one day before lower bound: excluded
    assert outside_upper.patient_id not in results  # exactly on analysis_end: excluded
    assert run.case_count == 2


# --- illness partitioning -----------------------------------------------------------


def test_different_illnesses_never_share_a_cluster(db_session: Session) -> None:
    community = _make_community(db_session)
    diagnosis_date = date(2026, 9, 5)

    # Two Measles patients and two Pertussis patients, all at the SAME point
    # (so they would all be one cluster if illness were ignored).
    for i, illness in enumerate(["Measles", "Measles", "Pertussis", "Pertussis"]):
        _make_patient_with_location(
            db_session, f"PAT-ILLNESS-{i}", illness=illness, diagnosis_date=diagnosis_date,
            lon=-98.5, lat=39.5, community=community,
        )

    run = _run(
        db_session, analysis_end=date(2026, 9, 10), target_srid=TARGET_SRID,
        eps_meters=100, min_points=2, window_days=7,
    )

    results = _results_by_patient(db_session, run.run_id)
    measles_clusters = {results[f"PAT-ILLNESS-{i}"].cluster_id for i in (0, 1)}
    pertussis_clusters = {results[f"PAT-ILLNESS-{i}"].cluster_id for i in (2, 3)}

    assert None not in measles_clusters  # both Measles points clustered
    assert None not in pertussis_clusters  # both Pertussis points clustered
    assert measles_clusters.isdisjoint(pertussis_clusters)  # never the same cluster_id
    assert run.cluster_count == 2


# --- proxy included vs excluded ------------------------------------------------------


def test_proxy_locations_excluded_when_policy_is_false(db_session: Session) -> None:
    community = _make_community(db_session)
    facility = _make_facility(db_session)
    diagnosis_date = date(2026, 9, 5)

    community_patient = _make_patient_with_location(
        db_session, "PAT-PROXYPOL-COMM", illness="Measles", diagnosis_date=diagnosis_date,
        lon=-98.5, lat=39.5, community=community,
    )
    proxy_patient = _make_patient_with_location(
        db_session, "PAT-PROXYPOL-PROXY", illness="Measles", diagnosis_date=diagnosis_date,
        lon=-98.5, lat=39.5, community=community, is_proxy=True, facility=facility,
    )

    excluded_run = _run(
        db_session, analysis_end=date(2026, 9, 10), target_srid=TARGET_SRID,
        eps_meters=100, min_points=1, window_days=7, include_proxy_locations=False,
    )
    excluded_results = _results_by_patient(db_session, excluded_run.run_id)
    assert community_patient.patient_id in excluded_results
    assert proxy_patient.patient_id not in excluded_results
    assert excluded_run.case_count == 1
    assert excluded_run.proxy_case_count == 0

    included_run = _run(
        db_session, analysis_end=date(2026, 9, 10), target_srid=TARGET_SRID,
        eps_meters=100, min_points=1, window_days=7, include_proxy_locations=True,
    )
    included_results = _results_by_patient(db_session, included_run.run_id)
    assert community_patient.patient_id in included_results
    assert proxy_patient.patient_id in included_results
    assert included_run.case_count == 2
    assert included_run.proxy_case_count == 1
    assert included_results[proxy_patient.patient_id].is_proxy_snapshot is True
    assert included_results[proxy_patient.patient_id].location_source_snapshot == (
        LocationSource.FACILITY_PROXY
    )


# --- DBSCAN cluster formation and noise, on deterministic coordinates ----------------


def test_dbscan_forms_clusters_and_marks_noise(db_session: Session) -> None:
    community = _make_community(db_session)
    diagnosis_date = date(2026, 9, 5)

    # ~55m apart (from empirical probing under EPSG:32614) — clusters at eps=100
    close_a = _make_patient_with_location(
        db_session, "PAT-DBSCAN-CLOSE-A", illness="Measles", diagnosis_date=diagnosis_date,
        lon=-98.500, lat=39.5000, community=community,
    )
    close_b = _make_patient_with_location(
        db_session, "PAT-DBSCAN-CLOSE-B", illness="Measles", diagnosis_date=diagnosis_date,
        lon=-98.500, lat=39.5005, community=community,
    )
    # ~1110m away — noise at eps=100
    far = _make_patient_with_location(
        db_session, "PAT-DBSCAN-FAR", illness="Measles", diagnosis_date=diagnosis_date,
        lon=-98.500, lat=39.510, community=community,
    )

    run = _run(
        db_session, analysis_end=date(2026, 9, 10), target_srid=TARGET_SRID,
        eps_meters=100, min_points=2, window_days=7,
    )

    results = _results_by_patient(db_session, run.run_id)
    assert results[close_a.patient_id].cluster_id is not None
    assert results[close_a.patient_id].cluster_id == results[close_b.patient_id].cluster_id
    assert results[far.patient_id].cluster_id is None  # DBSCAN noise
    assert run.cluster_count == 1
    assert run.case_count == 3


# --- empty eligible dataset ----------------------------------------------------------


def test_empty_eligible_dataset_completes_with_zero_counts(db_session: Session) -> None:
    run = _run(
        db_session, analysis_end=date(2026, 9, 10), target_srid=TARGET_SRID,
        eps_meters=100, min_points=2, window_days=7,
    )

    assert run.status == ClusteringRunStatus.COMPLETED
    assert run.case_count == 0
    assert run.proxy_case_count == 0
    assert run.cluster_count == 0
    assert _results_by_patient(db_session, run.run_id) == {}


# --- run status and summary counts ----------------------------------------------------


def test_run_status_and_summary_counts(db_session: Session) -> None:
    community = _make_community(db_session)
    diagnosis_date = date(2026, 9, 5)
    for i in range(3):
        _make_patient_with_location(
            db_session, f"PAT-SUMMARY-{i}", illness="Measles", diagnosis_date=diagnosis_date,
            lon=-98.5, lat=39.5, community=community,
        )

    run = _run(
        db_session, analysis_end=date(2026, 9, 10), target_srid=TARGET_SRID,
        eps_meters=100, min_points=2, window_days=7, illness_filter="Measles",
    )

    assert run.status == ClusteringRunStatus.COMPLETED
    assert run.started_at is not None
    assert run.completed_at is not None
    assert run.started_at <= run.completed_at
    assert run.case_count == 3
    assert run.proxy_case_count == 0
    assert run.cluster_count == 1
    assert run.illness_filter == "Measles"
    assert run.include_proxy_locations is True  # default
    assert run.target_srid == TARGET_SRID


def test_private_session_never_commits_caller_pending_state(
    db_session: Session,
) -> None:
    pending = Community(
        community_name="Caller Pending Community",
        full_address="99 Uncommitted Rd",
        longitude=-98.5,
        latitude=39.5,
    )
    db_session.add(pending)
    db_session.flush()

    run = run_clustering(
        engine=db_session.get_bind(), analysis_end=date(2026, 9, 10),
        target_srid=TARGET_SRID, eps_meters=100, min_points=2,
    )

    assert run.status == ClusteringRunStatus.COMPLETED
    assert db_session.in_transaction()
    with Session(bind=db_session.get_bind()) as independent_session:
        assert independent_session.get(Community, pending.community_id) is None


# --- validation happens before run creation; execution failures are durable -----------


@pytest.mark.parametrize("target_srid", [999999, 4326, 2277])
def test_invalid_or_non_metric_srid_creates_no_run(
    db_session: Session, target_srid: int
) -> None:
    before = db_session.execute(select(ClusteringRun)).scalars().all()
    with pytest.raises(ValueError):
        _run(
            db_session, analysis_end=date(2026, 9, 10), target_srid=target_srid,
            eps_meters=100, min_points=2, window_days=7,
        )
    assert db_session.execute(select(ClusteringRun)).scalars().all() == before


@pytest.mark.parametrize(
    ("parameter", "value"),
    [("window_days", 0), ("eps_meters", 0), ("min_points", 0)],
)
def test_invalid_basic_parameter_creates_no_run(
    db_session: Session, parameter: str, value: int
) -> None:
    kwargs = dict(
        analysis_end=date(2026, 9, 10), target_srid=TARGET_SRID,
        eps_meters=100, min_points=2, window_days=7,
    )
    kwargs[parameter] = value
    with pytest.raises(ValueError):
        _run(db_session, **kwargs)
    assert db_session.execute(select(ClusteringRun)).scalars().all() == []


def test_execution_failure_rolls_back_results_and_marks_run_failed(
    db_session: Session,
) -> None:
    engine = db_session.get_bind()
    db_session.commit()

    def fail_dbscan(conn, cursor, statement, parameters, context, executemany):
        if "ST_ClusterDBSCAN" in statement:
            raise RuntimeError("injected DBSCAN failure")

    event.listen(engine, "before_cursor_execute", fail_dbscan)
    try:
        with pytest.raises(RuntimeError, match="injected DBSCAN failure"):
            run_clustering(
                engine=engine, analysis_end=date(2026, 9, 10),
                target_srid=TARGET_SRID, eps_meters=100, min_points=2,
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_dbscan)

    db_session.expire_all()
    failed_run = db_session.execute(
        select(ClusteringRun).order_by(ClusteringRun.run_id.desc())
    ).scalars().first()
    assert failed_run.status == ClusteringRunStatus.FAILED
    assert failed_run.completed_at is not None
    assert db_session.execute(
        select(ClusteringResult).where(ClusteringResult.run_id == failed_run.run_id)
    ).scalars().all() == []


def test_running_transition_failure_marks_pending_run_failed(
    db_session: Session,
) -> None:
    engine = db_session.get_bind()
    failed_once = False

    def fail_running_update(conn, cursor, statement, parameters, context, executemany):
        nonlocal failed_once
        if not failed_once and statement.startswith("UPDATE clustering_runs"):
            failed_once = True
            raise RuntimeError("injected RUNNING transition failure")

    event.listen(engine, "before_cursor_execute", fail_running_update)
    try:
        with pytest.raises(RuntimeError, match="injected RUNNING transition failure"):
            run_clustering(
                engine=engine, analysis_end=date(2026, 9, 10),
                target_srid=TARGET_SRID, eps_meters=100, min_points=2,
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_running_update)

    db_session.expire_all()
    failed_run = db_session.execute(select(ClusteringRun)).scalar_one()
    assert failed_run.status == ClusteringRunStatus.FAILED
    assert failed_run.started_at is None  # RUNNING itself never became durable
    assert failed_run.completed_at is not None


# --- persisted snapshots remain unchanged after patient location changes -------------


def test_snapshot_is_unaffected_by_later_location_changes(db_session: Session) -> None:
    community = _make_community(db_session)
    other_community = _make_community(db_session, "Other Cluster Community")
    patient = _make_patient_with_location(
        db_session, "PAT-SNAPSHOT-001", illness="Measles", diagnosis_date=date(2026, 9, 5),
        lon=-98.5, lat=39.5, community=community,
    )

    run = _run(
        db_session, analysis_end=date(2026, 9, 10), target_srid=TARGET_SRID,
        eps_meters=100, min_points=1, window_days=7,
    )
    result = _results_by_patient(db_session, run.run_id)[patient.patient_id]
    original_location_id = result.patient_location_id
    assert result.illness_snapshot == "Measles"
    assert result.diagnosis_date_snapshot == date(2026, 9, 5)
    original_wkt = db_session.execute(
        select(ClusteringResult.location_snapshot).where(
            ClusteringResult.clustering_result_id == result.clustering_result_id
        )
    ).scalar_one()

    # the patient's current location changes to a different community/point
    old_current = db_session.execute(
        select(PatientLocation).where(
            PatientLocation.patient_id == patient.patient_id, PatientLocation.is_current.is_(True)
        )
    ).scalar_one()
    old_current.is_current = False
    db_session.add(
        PatientLocation(
            patient_id=patient.patient_id,
            location="SRID=4326;POINT(-90.0 30.0)",
            location_source=LocationSource.COMMUNITY_REFERENCE,
            community_id=other_community.community_id,
            is_proxy=False,
            is_current=True,
        )
    )
    db_session.flush()

    patient.illness = "Corrected illness"
    patient.diagnosis_date = date(2026, 9, 8)
    db_session.flush()

    # the historical result row is untouched
    db_session.refresh(result)
    assert result.patient_location_id == original_location_id
    unchanged_wkt = db_session.execute(
        select(ClusteringResult.location_snapshot).where(
            ClusteringResult.clustering_result_id == result.clustering_result_id
        )
    ).scalar_one()
    assert str(unchanged_wkt) == str(original_wkt)
    assert result.illness_snapshot == "Measles"
    assert result.diagnosis_date_snapshot == date(2026, 9, 5)


# --- multiple runs preserve independent historical results ---------------------------


def test_multiple_runs_are_independent_and_preserved(db_session: Session) -> None:
    community = _make_community(db_session)
    _make_patient_with_location(
        db_session, "PAT-MULTI-001", illness="Measles", diagnosis_date=date(2026, 9, 5),
        lon=-98.5, lat=39.5, community=community,
    )

    run_1 = _run(
        db_session, analysis_end=date(2026, 9, 10), target_srid=TARGET_SRID,
        eps_meters=100, min_points=1, window_days=7,
    )
    run_2 = _run(
        db_session, analysis_end=date(2026, 9, 10), target_srid=TARGET_SRID,
        eps_meters=100, min_points=1, window_days=7,
    )

    assert run_1.run_id != run_2.run_id

    all_runs = db_session.execute(
        select(ClusteringRun).where(ClusteringRun.run_id.in_([run_1.run_id, run_2.run_id]))
    ).scalars().all()
    assert len(all_runs) == 2

    results_1 = _results_by_patient(db_session, run_1.run_id)
    results_2 = _results_by_patient(db_session, run_2.run_id)
    assert set(results_1) == {"PAT-MULTI-001"}
    assert set(results_2) == {"PAT-MULTI-001"}
    assert results_1["PAT-MULTI-001"].cluster_id == results_2["PAT-MULTI-001"].cluster_id
    assert results_1["PAT-MULTI-001"].clustering_result_id != results_2["PAT-MULTI-001"].clustering_result_id
