"""Integration tests for persisted-run Folium visualization (Milestone 6)."""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from public_health_clustering.db import create_db_engine
from public_health_clustering.models import (
    ClusteringResult, ClusteringRun, ClusteringRunStatus, Community,
    LocationSource, Patient, PatientLocation,
)
from public_health_clustering.visualization import (
    ClusteringRunNotCompletedError, ClusteringRunNotFoundError,
    cluster_color, generate_run_map,
)


@pytest.fixture()
def db_session():
    engine = create_db_engine()
    tables = "clustering_results, clustering_runs, patient_locations, patients, communities, healthcare_facilities"
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
        window_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 9, 8, tzinfo=timezone.utc), window_days=7,
        eps_meters=125, min_points=2, target_srid=32614,
        include_proxy_locations=True, status=status,
        case_count=0, proxy_case_count=0, cluster_count=0,
    )
    session.add(run)
    session.flush()
    return run


def _result(session: Session, run: ClusteringRun, patient_id: str, *, cluster_id, proxy=False):
    community = Community(community_name=f"Community {patient_id}", full_address="Test",
                          longitude=-98.5, latitude=39.5)
    session.add(community)
    session.flush()
    patient = Patient(patient_id=patient_id, name="Test", illness="Measles",
                      diagnosis_date=date(2026, 9, 5))
    session.add(patient)
    session.flush()
    location = PatientLocation(
        patient_id=patient_id, location="SRID=4326;POINT(-98.5 39.5)",
        location_source=LocationSource.FACILITY_PROXY if proxy else LocationSource.COMMUNITY_REFERENCE,
        community_id=None if proxy else community.community_id,
        facility_id=None, is_proxy=proxy, is_current=True,
    )
    # A proxy normally requires a facility. For this focused snapshot test,
    # insert the result after making a valid non-proxy source location.
    if proxy:
        location.location_source = LocationSource.OTHER
        location.is_proxy = False
    session.add(location)
    session.flush()
    session.add(ClusteringResult(
        run_id=run.run_id, patient_id=patient_id,
        patient_location_id=location.patient_location_id, cluster_id=cluster_id,
        illness_snapshot="Measles", diagnosis_date_snapshot=date(2026, 9, 5),
        location_snapshot="SRID=4326;POINT(-98.5 39.5)",
        location_source_snapshot=LocationSource.FACILITY_PROXY if proxy else LocationSource.COMMUNITY_REFERENCE,
        is_proxy_snapshot=proxy,
    ))
    run.case_count += 1
    run.proxy_case_count += int(proxy)
    run.cluster_count = 1


def test_clustered_noise_and_proxy_styles_and_content(db_session, tmp_path):
    run = _run(db_session)
    _result(db_session, run, "PAT-CLUSTER", cluster_id=4)
    _result(db_session, run, "PAT-NOISE", cluster_id=None)
    _result(db_session, run, "PAT-PROXY", cluster_id=4, proxy=True)
    db_session.commit()

    output = generate_run_map(run.run_id, tmp_path / "map.html", engine=db_session.get_bind())
    html = output.read_text()
    assert output.exists() and "PAT-CLUSTER" in html and "PAT-NOISE" in html
    assert "Noise" in html and cluster_color(4) in html and cluster_color(None) in html
    assert "proxy-marker" in html and "FACILITY_PROXY" in html and "Is proxy" in html
    assert "Analysis window" in html and "Window days" in html and "Target SRID" in html
    assert "fitBounds" in html


def test_cluster_colors_are_deterministic_and_distinct():
    assert cluster_color(1) == cluster_color(1)
    assert cluster_color(1) != cluster_color(2)
    assert cluster_color(None) not in {cluster_color(1), cluster_color(2)}


def test_empty_completed_run_writes_valid_html(db_session, tmp_path):
    run = _run(db_session)
    db_session.commit()
    output = generate_run_map(run.run_id, tmp_path / "empty.html", engine=db_session.get_bind())
    html = output.read_text()
    assert "<!DOCTYPE html>" in html and "Clustering run" in html and "Case count" in html


def test_missing_run_fails_clearly(db_session, tmp_path):
    with pytest.raises(ClusteringRunNotFoundError, match="999.*does not exist"):
        generate_run_map(999, tmp_path / "missing.html", engine=db_session.get_bind())


def test_non_completed_run_fails_clearly(db_session, tmp_path):
    run = _run(db_session, ClusteringRunStatus.RUNNING)
    db_session.commit()
    with pytest.raises(ClusteringRunNotCompletedError, match="RUNNING.*COMPLETED"):
        generate_run_map(run.run_id, tmp_path / "running.html", engine=db_session.get_bind())
    assert not (tmp_path / "running.html").exists()


def test_visualization_does_not_modify_analytical_rows(db_session, tmp_path):
    run = _run(db_session)
    _result(db_session, run, "PAT-READONLY", cluster_id=1)
    db_session.commit()
    before = db_session.execute(select(ClusteringResult).where(ClusteringResult.run_id == run.run_id)).scalars().all()
    generate_run_map(run.run_id, tmp_path / "readonly.html", engine=db_session.get_bind())
    db_session.expire_all()
    after = db_session.execute(select(ClusteringResult).where(ClusteringResult.run_id == run.run_id)).scalars().all()
    assert len(before) == len(after) == 1
