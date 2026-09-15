"""Focused live-PostGIS tests for evidence-grounded strategy briefs."""

from datetime import date, datetime, timezone
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from public_health_clustering.dashboard import app
from public_health_clustering.db import create_db_engine
from public_health_clustering.knowledge_base import seed_chinese_knowledge_base
from public_health_clustering.models import ClusteringResult, ClusteringRun, ClusteringRunStatus, LocationSource, Patient, PatientLocation
from public_health_clustering.strategy import StrategyError, build_strategy_context, generate_strategy_brief


class FakeResponse:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps(self.payload, ensure_ascii=False).encode()


def fake_opener(request, timeout):
    payload = json.loads(request.data)
    context = json.loads(payload["messages"][1]["content"])
    assert "centroid_latitude" not in context["cluster_report"]
    evidence_id = context["evidence"][0]["evidence_id"]
    content = {"title": "麻疹聚类防控策略", "assessment": "病例在秋季形成空间聚集，需结合接触史进一步研判。",
               "situation_level": "加强监测", "reasoning_summary": ["病例时间集中", "年龄资料完整"],
               "priorities": [{"priority": "立即", "action": "开展流行病学调查", "rationale": "核实聚集范围", "citations": [evidence_id]}],
               "additional_information_needed": ["免疫史", "接触史"], "data_quality": ["缺少人口分母"],
               "escalation": "出现重症或病例持续增加时升级响应", "disclaimer": "辅助研判，不替代属地专业判断。"}
    return FakeResponse({"model": "free-test-model", "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})


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


def test_strategy_combines_aggregate_report_and_cited_knowledge(db_session, monkeypatch):
    run = ClusteringRun(window_start=datetime(2026, 9, 4, tzinfo=timezone.utc), window_end=datetime(2026, 9, 11, tzinfo=timezone.utc), window_days=7, eps_meters=900, min_points=3, target_srid=32649, include_proxy_locations=True, status=ClusteringRunStatus.COMPLETED, case_count=3, proxy_case_count=0, cluster_count=1)
    db_session.add(run); db_session.flush()
    for index, lon in enumerate((113.2644, 113.2650, 113.2655), 1): _case(db_session, run, index, lon)
    seed_chinese_knowledge_base(db_session); db_session.flush()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    context = build_strategy_context(db_session, run.run_id, 1)
    assert context.calendar["season"] == "秋季"
    assert context.location["nearest_configured_city"] == "广州"
    brief = generate_strategy_brief(context, opener=fake_opener)
    assert brief["title"] == "麻疹聚类防控策略"
    assert brief["priorities"] and brief["evidence"]
    assert all(item["citations"] for item in brief["priorities"])
    assert all(item["source_url"].startswith("https://") for item in brief["evidence"])
    assert brief["mode"] == "模型智能体 · free-test-model"


def test_strategy_requires_matching_current_knowledge(db_session):
    run = ClusteringRun(window_start=datetime(2026, 9, 4, tzinfo=timezone.utc), window_end=datetime(2026, 9, 11, tzinfo=timezone.utc), window_days=7, eps_meters=900, min_points=1, target_srid=32649, include_proxy_locations=True, status=ClusteringRunStatus.COMPLETED, case_count=1, proxy_case_count=0, cluster_count=1)
    db_session.add(run); db_session.flush(); _case(db_session, run, 1, 113.2644); db_session.flush()
    with pytest.raises(StrategyError, match="没有疾病"): build_strategy_context(db_session, run.run_id, 1)


def test_strategy_api_returns_brief(db_session, monkeypatch):
    run = ClusteringRun(window_start=datetime(2026, 9, 4, tzinfo=timezone.utc), window_end=datetime(2026, 9, 11, tzinfo=timezone.utc), window_days=7, eps_meters=900, min_points=1, target_srid=32649, include_proxy_locations=True, status=ClusteringRunStatus.COMPLETED, case_count=1, proxy_case_count=0, cluster_count=1)
    db_session.add(run); db_session.flush(); _case(db_session, run, 1, 113.2644); seed_chinese_knowledge_base(db_session); db_session.commit()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("public_health_clustering.strategy.urlopen", fake_opener)
    response = TestClient(app).post(f"/api/strategies/clusters/{run.run_id}/1")
    assert response.status_code == 200
    assert response.json()["mode"] == "模型智能体 · free-test-model"


def test_strategy_has_no_fallback_without_api_key(db_session, monkeypatch):
    run = ClusteringRun(window_start=datetime(2026, 9, 4, tzinfo=timezone.utc), window_end=datetime(2026, 9, 11, tzinfo=timezone.utc), window_days=7, eps_meters=900, min_points=1, target_srid=32649, include_proxy_locations=True, status=ClusteringRunStatus.COMPLETED, case_count=1, proxy_case_count=0, cluster_count=1)
    db_session.add(run); db_session.flush(); _case(db_session, run, 1, 113.2644); seed_chinese_knowledge_base(db_session); db_session.flush()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(StrategyError, match="OPENROUTER_API_KEY"):
        generate_strategy_brief(build_strategy_context(db_session, run.run_id, 1))
