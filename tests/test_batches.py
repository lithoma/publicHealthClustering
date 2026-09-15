"""Focused integration coverage for managed batches and case events."""

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from public_health_clustering.clustering import run_clustering
from public_health_clustering.dashboard import app
from public_health_clustering.db import create_db_engine
from public_health_clustering.ingestion import load_patients
from public_health_clustering.ingestion.importer import import_patients
from public_health_clustering.models import (
    CaseEvent, ClusteringResult, DataBatch, LocationSource, Patient, PatientLocation,
)


@pytest.fixture()
def db_session():
    engine = create_db_engine()
    tables = ("clustering_results, clustering_runs, case_events, patient_locations, "
              "patients, communities, healthcare_facilities, data_batches")
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


def test_same_patient_can_have_case_events_in_multiple_batches(db_session):
    records, errors = load_patients(Path("data/sample/patients.csv"))
    assert not errors
    first = records[:1]
    batches = [DataBatch(batch_name="Batch A"), DataBatch(batch_name="Batch B")]
    db_session.add_all(batches)
    db_session.flush()
    import_patients(db_session, first, batch=batches[0])
    import_patients(db_session, first, batch=batches[1])
    db_session.flush()

    assert db_session.scalar(select(func.count()).select_from(Patient)) == 1
    assert db_session.scalar(select(func.count()).select_from(CaseEvent)) == 2


def test_excluded_batch_is_not_clustered(db_session):
    batch = DataBatch(batch_name="Excluded", is_included=False)
    patient = Patient(patient_id="SUBJECT-1", name="匿名病例", illness="麻疹",
                      diagnosis_date=date(2026, 9, 5), batch=batch)
    db_session.add_all([batch, patient])
    db_session.flush()
    db_session.add_all([
        PatientLocation(patient_id=patient.patient_id,
                        location="SRID=4326;POINT(113.2644 23.1291)",
                        location_source=LocationSource.MANUAL, is_current=True, is_proxy=False),
        CaseEvent(batch=batch, patient=patient, source_case_id="CASE-1",
                  illness="麻疹", diagnosis_date=date(2026, 9, 5)),
    ])
    db_session.commit()

    run = run_clustering(analysis_end=date(2026, 9, 11), window_days=7,
                         eps_meters=900, min_points=1, target_srid=32649,
                         engine=db_session.get_bind())
    assert run.case_count == 0
    assert db_session.scalar(select(func.count()).select_from(ClusteringResult)) == 0


def test_patient_csv_preview_does_not_write_database(db_session):
    client = TestClient(app)
    content = Path("data/sample/patients.csv").read_bytes()
    response = client.post("/api/import/preview", files={"file": ("patients.csv", content, "text/csv")})
    assert response.status_code == 200
    assert response.json()["valid_count"] == 16
    assert db_session.scalar(select(func.count()).select_from(Patient)) == 0
