"""Focused live-PostGIS tests for Milestone 7A cluster reports."""

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from public_health_clustering.dashboard import app
from public_health_clustering.db import create_db_engine
from public_health_clustering.models import (
    ClusteringResult,
    ClusteringRun,
    ClusteringRunStatus,
    HealthcareFacility,
    LocationSource,
    Patient,
    PatientLocation,
)
from public_health_clustering.reporting import ClusterReportError, build_cluster_reports


@pytest.fixture()
def db_session():
    engine = create_db_engine()
    tables = (
        "clustering_results, clustering_runs, case_events, patient_locations, "
        "patients, communities, healthcare_facilities, data_batches"
    )
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with engine.begin() as connection:
            connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        engine.dispose()


def _run(session: Session, status=ClusteringRunStatus.COMPLETED) -> ClusteringRun:
    run = ClusteringRun(
        window_start=datetime(2026, 9, 4, tzinfo=timezone.utc),
        window_end=datetime(2026, 9, 11, tzinfo=timezone.utc),
        window_days=7,
        eps_meters=900,
        min_points=3,
        target_srid=32649,
        include_proxy_locations=True,
        status=status,
        case_count=4 if status == ClusteringRunStatus.COMPLETED else None,
        proxy_case_count=1 if status == ClusteringRunStatus.COMPLETED else None,
        cluster_count=1 if status == ClusteringRunStatus.COMPLETED else None,
    )
    session.add(run)
    session.flush()
    return run


def _result(session: Session, run: ClusteringRun, suffix: str, lon: float, lat: float,
            *, cluster_id: int | None, day: int, proxy: bool = False, illness: str = "麻疹"):
    facility = None
    if proxy:
        facility = HealthcareFacility(
            facility_code=f"REPORT-FAC-{suffix}",
            facility_name=f"Synthetic facility {suffix}",
            full_address="Synthetic test address",
            longitude=lon,
            latitude=lat,
        )
        session.add(facility)
        session.flush()
    patient = Patient(
        patient_id=f"REPORT-{suffix}", name="合成病例", illness=illness,
        diagnosis_date=date(2026, 9, day), diagnosing_facility=facility,
    )
    session.add(patient)
    session.flush()
    location = PatientLocation(
        patient_id=patient.patient_id,
        location=f"SRID=4326;POINT({lon} {lat})",
        location_source=LocationSource.FACILITY_PROXY if proxy else LocationSource.MANUAL,
        facility_id=facility.facility_id if facility else None,
        is_proxy=proxy,
        is_current=True,
    )
    session.add(location)
    session.flush()
    session.add(ClusteringResult(
        run_id=run.run_id,
        patient_id=patient.patient_id,
        patient_location_id=location.patient_location_id,
        cluster_id=cluster_id,
        illness_snapshot=illness,
        diagnosis_date_snapshot=date(2026, 9, day),
        location_snapshot=f"SRID=4326;POINT({lon} {lat})",
        location_source_snapshot=LocationSource.FACILITY_PROXY if proxy else LocationSource.MANUAL,
        is_proxy_snapshot=proxy,
    ))


def test_cluster_report_calculates_statistics_and_noise_from_snapshots(db_session):
    run = _run(db_session)
    _result(db_session, run, "A", 113.2644, 23.1291, cluster_id=7, day=4)
    _result(db_session, run, "B", 113.2650, 23.1291, cluster_id=7, day=4, proxy=True)
    _result(db_session, run, "C", 113.2647, 23.1297, cluster_id=7, day=6)
    _result(db_session, run, "NOISE", 113.31, 23.18, cluster_id=None, day=8)
    db_session.flush()

    saved_run, reports, noise = build_cluster_reports(db_session, run.run_id)

    assert saved_run.run_id == run.run_id
    assert len(reports) == 1
    report = reports[0]
    assert report.cluster_id == 7
    assert report.illness == "麻疹"
    assert report.case_count == 3
    assert report.proxy_case_count == 1
    assert report.proxy_share == pytest.approx(1 / 3)
    assert report.first_diagnosis_date == date(2026, 9, 4)
    assert report.last_diagnosis_date == date(2026, 9, 6)
    assert report.active_days == 3
    assert report.coverage_area_km2 > 0
    assert report.max_distance_to_centroid_m > report.mean_distance_to_centroid_m > 0
    assert report.daily_counts == [
        {"date": "2026-09-04", "case_count": 2},
        {"date": "2026-09-06", "case_count": 1},
    ]
    assert noise == {
        "case_count": 1,
        "proxy_case_count": 0,
        "by_illness": [{"illness": "麻疹", "case_count": 1}],
    }


def test_report_api_and_standalone_html_export(db_session):
    run = _run(db_session)
    for suffix, lon in (("A", 113.2644), ("B", 113.2650), ("C", 113.2655)):
        _result(db_session, run, suffix, lon, 23.1291, cluster_id=2, day=5)
    db_session.commit()
    client = TestClient(app)

    response = client.get(f"/api/reports/clusters?run_id={run.run_id}")
    assert response.status_code == 200
    assert response.json()["clusters"][0]["case_count"] == 3

    response = client.get(f"/reports/clusters/{run.run_id}/2.html?download=true")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "<!doctype html>" in response.text
    assert "Cases by diagnosis date" in response.text
    assert "Coverage area is the union" in response.text
    assert client.get(f"/reports/clusters/{run.run_id}/999.html").status_code == 404


def test_empty_completed_run_has_empty_report_collection(db_session):
    run = _run(db_session)
    run.case_count = run.proxy_case_count = run.cluster_count = 0
    db_session.flush()

    _, reports, noise = build_cluster_reports(db_session, run.run_id)
    assert reports == []
    assert noise["case_count"] == 0


def test_missing_and_noncompleted_runs_fail_clearly(db_session):
    with pytest.raises(ClusterReportError, match="does not exist"):
        build_cluster_reports(db_session, 999999)

    run = _run(db_session, ClusteringRunStatus.RUNNING)
    db_session.flush()
    with pytest.raises(ClusterReportError, match="reports require COMPLETED"):
        build_cluster_reports(db_session, run.run_id)
