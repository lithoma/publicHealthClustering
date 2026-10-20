"""Focused live-PostGIS tests for the uncalibrated seven-day baseline."""
from datetime import date
import json
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from public_health_clustering.db import create_db_engine
from public_health_clustering.models import DataBatch, CaseEvent, Patient, PatientLocation, LocationSource
from public_health_clustering.prediction import (
    MODEL_TYPE,
    PredictionError,
    create_prediction,
    evaluate_prediction_run,
    evaluation_json,
    prediction_json,
    run_backtest,
)

@pytest.fixture()
def session():
    engine=create_db_engine(); tables="prediction_evaluations,prediction_run_batches,prediction_results,prediction_runs,clustering_results,clustering_runs,case_events,patient_locations,patients,communities,healthcare_facilities,data_batches"
    with engine.begin() as c: c.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    value=Session(engine,expire_on_commit=False)
    try: yield value
    finally:
        value.rollback(); value.close()
        with engine.begin() as c: c.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        engine.dispose()

def add_case(session,index,day,lon=113.2644,illness="麻疹",batch=None):
    batch=batch or session.query(DataBatch).first()
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
    assert output["run"]["batches"][0]["batch_name"]=="预测合成批次"
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


def test_prediction_rejects_geographic_srid(session):
    with pytest.raises(PredictionError, match="geographic coordinate system"):
        create_prediction(session,cutoff_date=date(2026,9,11),target_srid=4326)


def test_explicit_batch_cohort_is_snapshotted_and_excluded_batches_fail(session):
    included=DataBatch(batch_name="纳入批次",is_synthetic=True,is_included=True)
    excluded=DataBatch(batch_name="排除批次",is_synthetic=True,is_included=False)
    session.add_all([included,excluded]); session.flush()
    add_case(session,1,date(2026,9,10),batch=included)
    add_case(session,2,date(2026,9,10),batch=excluded)
    run=create_prediction(session,cutoff_date=date(2026,9,11),target_srid=32649,batch_ids=[included.batch_id])
    output=prediction_json(session,run.prediction_run_id)
    assert output["run"]["batches"]==[{"batch_id":included.batch_id,"batch_name":"纳入批次"}]
    with pytest.raises(PredictionError,match="当前已排除"):
        create_prediction(session,cutoff_date=date(2026,9,11),target_srid=32649,batch_ids=[excluded.batch_id])


def test_evaluation_attaches_outcomes_and_counts_new_cells(session):
    add_case(session,1,date(2026,9,10))
    run=create_prediction(session,cutoff_date=date(2026,9,11),target_srid=32649)
    batch=session.query(DataBatch).first()
    add_case(session,2,date(2026,9,12),batch=batch)
    add_case(session,3,date(2026,9,13),lon=113.55,batch=batch)
    batch.is_included=False
    session.flush()

    evaluate_prediction_run(session,run.prediction_run_id,as_of_date=date(2026,9,18))
    output=evaluation_json(session,run.prediction_run_id)
    prediction=prediction_json(session,run.prediction_run_id)
    assert output["observed_cases_in_cells"]==1
    assert output["observed_cases_outside_cells"]==1
    assert output["mae"] is not None
    assert output["interval_coverage"] in {0.0,1.0}
    assert prediction["cells"][0]["observed_cases"]==1
    assert prediction["run"]["evaluated"] is True


def test_evaluation_requires_closed_window_and_is_immutable(session):
    add_case(session,1,date(2026,9,10))
    run=create_prediction(session,cutoff_date=date(2026,9,11),target_srid=32649)
    with pytest.raises(PredictionError,match="尚未结束"):
        evaluate_prediction_run(session,run.prediction_run_id,as_of_date=date(2026,9,17))
    evaluate_prediction_run(session,run.prediction_run_id,as_of_date=date(2026,9,18))
    with pytest.raises(PredictionError,match="不可变评估"):
        evaluate_prediction_run(session,run.prediction_run_id,as_of_date=date(2026,9,19))


def test_single_cutoff_rolling_backtest(session):
    add_case(session,1,date(2026,9,3))
    add_case(session,2,date(2026,9,5))
    rows=run_backtest(session,start_cutoff=date(2026,9,4),end_cutoff=date(2026,9,4),as_of_date=date(2026,9,11),target_srid=32649)
    assert len(rows)==1
    assert rows[0]["cutoff_date"]=="2026-09-04"
    assert rows[0]["metrics"]["observed_total"]==1
