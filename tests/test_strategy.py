"""Focused live-PostGIS tests for evidence-grounded strategy briefs."""

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from public_health_clustering.dashboard import app
from public_health_clustering.db import create_db_engine
from public_health_clustering.knowledge_base import seed_chinese_knowledge_base
from public_health_clustering.models import ClusteringResult, ClusteringRun, ClusteringRunStatus, LocationSource, Patient, PatientLocation
from public_health_clustering.strategy import StrategyError, build_strategy_context, generate_strategy_brief


@pytest.fixture()
def db_session():
    engine = create_db_engine()
    tables = "knowledge_sections, knowledge_documents, clustering_results, clustering_runs, case_events, patient_locations, patients, communities, healthcare_facilities, data_batches"
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback(); session.close()
        with engine.begin() as connection:
            connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        engine.dispose()


def _case(session, run, index, lon):
    patient = Patient(patient_id=f"S-{index}", name="合成病例", illness="麻疹", diagnosis_date=date(2026, 9, 5), sex="FEMALE", age_years=20 + index)
    session.add(patient); session.flush()
    location = PatientLocation(patient_id=patient.patient_id, location=f"SRID=4326;POINT({lon} 23.1291)", location_source=LocationSource.MANUAL, is_proxy=False, is_current=True)
    session.add(location); session.flush()
    session.add(ClusteringResult(run_id=run.run_id, patient_id=patient.patient_id, patient_location_id=location.patient_location_id, cluster_id=1, illness_snapshot="麻疹", diagnosis_date_snapshot=date(2026, 9, 5), sex_snapshot="FEMALE", age_years_snapshot=20 + index, location_snapshot=f"SRID=4326;POINT({lon} 23.1291)", location_source_snapshot=LocationSource.MANUAL, is_proxy_snapshot=False))


def test_strategy_combines_aggregate_report_and_cited_knowledge(db_session):
    run = ClusteringRun(window_start=datetime(2026, 9, 4, tzinfo=timezone.utc), window_end=datetime(2026, 9, 11, tzinfo=timezone.utc), window_days=7, eps_meters=900, min_points=3, target_srid=32649, include_proxy_locations=True, status=ClusteringRunStatus.COMPLETED, case_count=3, proxy_case_count=0, cluster_count=1)
    db_session.add(run); db_session.flush()
    for index, lon in enumerate((113.2644, 113.2650, 113.2655), 1): _case(db_session, run, index, lon)
    seed_chinese_knowledge_base(db_session); db_session.flush()
    brief = generate_strategy_brief(build_strategy_context(db_session, run.run_id, 1))
    assert brief["title"] == "麻疹聚类 1 防控策略简报"
    assert brief["priorities"] and brief["evidence"]
    assert all(item["citations"] for item in brief["priorities"])
    assert all(item["source_url"].startswith("https://") for item in brief["evidence"])
    assert "患者" not in brief["assessment"]


def test_strategy_requires_matching_current_knowledge(db_session):
    run = ClusteringRun(window_start=datetime(2026, 9, 4, tzinfo=timezone.utc), window_end=datetime(2026, 9, 11, tzinfo=timezone.utc), window_days=7, eps_meters=900, min_points=1, target_srid=32649, include_proxy_locations=True, status=ClusteringRunStatus.COMPLETED, case_count=1, proxy_case_count=0, cluster_count=1)
    db_session.add(run); db_session.flush(); _case(db_session, run, 1, 113.2644); db_session.flush()
    with pytest.raises(StrategyError, match="没有疾病"): build_strategy_context(db_session, run.run_id, 1)


def test_strategy_api_returns_brief(db_session):
    run = ClusteringRun(window_start=datetime(2026, 9, 4, tzinfo=timezone.utc), window_end=datetime(2026, 9, 11, tzinfo=timezone.utc), window_days=7, eps_meters=900, min_points=1, target_srid=32649, include_proxy_locations=True, status=ClusteringRunStatus.COMPLETED, case_count=1, proxy_case_count=0, cluster_count=1)
    db_session.add(run); db_session.flush(); _case(db_session, run, 1, 113.2644); seed_chinese_knowledge_base(db_session); db_session.commit()
    response = TestClient(app).post(f"/api/strategies/clusters/{run.run_id}/1")
    assert response.status_code == 200
    assert response.json()["mode"] == "规则化证据简报"
