"""Focused live-PostGIS tests for the uncalibrated seven-day baseline."""
from datetime import date
import json
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from public_health_clustering.db import create_db_engine
from public_health_clustering.models import DataBatch, CaseEvent, Patient, PatientLocation, LocationSource
from public_health_clustering.prediction import create_prediction, prediction_json, MODEL_TYPE

@pytest.fixture()
def session():
    engine=create_db_engine(); tables="prediction_results,prediction_runs,clustering_results,clustering_runs,case_events,patient_locations,patients,communities,healthcare_facilities,data_batches"
    with engine.begin() as c: c.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    value=Session(engine,expire_on_commit=False)
    try: yield value
    finally:
        value.rollback(); value.close()
        with engine.begin() as c: c.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        engine.dispose()

def add_case(session,index,day,lon=113.2644,illness="麻疹"):
    batch=session.query(DataBatch).first()
    if not batch: batch=DataBatch(batch_name="预测合成批次",is_synthetic=True,is_included=True); session.add(batch); session.flush()
    p=Patient(patient_id=f"PRED-{index}",name="合成",illness=illness,diagnosis_date=day,batch_id=batch.batch_id)
    session.add(p); session.flush(); session.add(CaseEvent(batch_id=batch.batch_id,patient_id=p.patient_id,source_case_id=p.patient_id,illness=illness,diagnosis_date=day)); session.add(PatientLocation(patient_id=p.patient_id,location=f"SRID=4326;POINT({lon} 23.1291)",location_source=LocationSource.MANUAL,is_proxy=False,is_current=True)); session.flush()

def test_shared_baseline_persists_reproducible_grid_forecast(session):
    for i,day in enumerate([date(2026,9,10),date(2026,9,9),date(2026,9,3),date(2026,8,22)],1): add_case(session,i,day)
    run=create_prediction(session,cutoff_date=date(2026,9,11),target_srid=32649,grid_size_meters=1000)
    output=prediction_json(session,run.prediction_run_id)
    assert output["run"]["model_type"]==MODEL_TYPE
    assert output["run"]["validation_status"]=="SIMULATION_UNVALIDATED"
    assert output["run"]["forecast_end"]=="2026-09-18"
    assert len(output["cells"])==1
    cell=output["cells"][0]
    assert cell["features"]["recent_7"]==2 and cell["features"]["previous_7"]==1
    assert cell["expected_cases"]>0 and cell["upper_bound"]>=cell["expected_cases"]
    assert cell["geometry"]["type"]=="Polygon"

def test_diseases_share_model_but_keep_separate_outputs(session):
    add_case(session,1,date(2026,9,10),illness="麻疹"); add_case(session,2,date(2026,9,10),illness="流感")
    output=prediction_json(session,create_prediction(session,cutoff_date=date(2026,9,11),target_srid=32649).prediction_run_id)
    assert {cell["disease"] for cell in output["cells"]}=={"麻疹","流感"}

def test_empty_history_creates_valid_empty_run(session):
    output=prediction_json(session,create_prediction(session,cutoff_date=date(2026,9,11),target_srid=32649).prediction_run_id)
    assert output["cells"]==[]
