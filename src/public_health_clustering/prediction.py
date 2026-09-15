"""Shared, training-free seven-day spatial forecasting baseline."""
from __future__ import annotations
from datetime import date, timedelta
import json, math, time
from sqlalchemy import text, select, func
from sqlalchemy.orm import Session
from .models import PredictionRun, PredictionResult

MODEL_TYPE = "SHARED_SPATIOTEMPORAL_BASELINE"
MODEL_VERSION = "1"
PARAMETERS = {"recent_7_weight": .60, "previous_7_weight": .30, "older_14_weight": .10, "trend_strength": .25, "neighbor_weight": .15, "trend_min": .5, "trend_max": 2.0}

def create_prediction(session: Session, *, cutoff_date: date, target_srid: int, grid_size_meters: int = 1000) -> PredictionRun:
    if grid_size_meters <= 0 or target_srid <= 0: raise ValueError("网格大小和目标 SRID 必须为正数")
    started = time.monotonic()
    rows = session.execute(text("""
      WITH points AS (
        SELECT illness, diagnosis_date,
          floor(ST_X(ST_Transform(location,:srid))/:grid)::int AS cell_x,
          floor(ST_Y(ST_Transform(location,:srid))/:grid)::int AS cell_y
        FROM current_patient_spatial_points
        WHERE batch_is_included=TRUE AND diagnosis_date >= :start AND diagnosis_date < :cutoff
      )
      SELECT illness,cell_x,cell_y,
        count(*) FILTER (WHERE diagnosis_date >= :d7)::int recent_7,
        count(*) FILTER (WHERE diagnosis_date >= :d14 AND diagnosis_date < :d7)::int previous_7,
        count(*) FILTER (WHERE diagnosis_date < :d14)::int older_14,
        count(*)::int total_28
      FROM points GROUP BY illness,cell_x,cell_y ORDER BY illness,cell_x,cell_y
    """), {"srid":target_srid,"grid":grid_size_meters,"start":cutoff_date-timedelta(days=28),"cutoff":cutoff_date,"d7":cutoff_date-timedelta(days=7),"d14":cutoff_date-timedelta(days=14)}).mappings().all()
    run = PredictionRun(cutoff_date=cutoff_date, forecast_start=cutoff_date, forecast_end=cutoff_date+timedelta(days=7), horizon_days=7, history_days=28, grid_size_meters=grid_size_meters, target_srid=target_srid, model_type=MODEL_TYPE, model_version=MODEL_VERSION, validation_status="SIMULATION_UNVALIDATED", parameters_json=json.dumps(PARAMETERS, sort_keys=True), status="COMPLETED")
    session.add(run); session.flush()
    lookup = {(r["illness"],r["cell_x"],r["cell_y"]):r for r in rows}
    for row in rows:
        neighbors=[lookup.get((row["illness"],row["cell_x"]+dx,row["cell_y"]+dy)) for dx in (-1,0,1) for dy in (-1,0,1) if dx or dy]
        neighbor_recent=sum(n["recent_7"] for n in neighbors if n)/8
        level=.6*row["recent_7"]+.3*row["previous_7"]+.1*(row["older_14"]/2)
        trend=max(.5,min(2.0,(row["recent_7"]+1)/(row["previous_7"]+1)))
        expected=max(0,level*(.75+.25*trend)+.15*neighbor_recent)
        spread=1.96*math.sqrt(expected+1)
        polygon=session.scalar(select(func.ST_AsText(func.ST_Transform(func.ST_MakeEnvelope(row["cell_x"]*grid_size_meters,row["cell_y"]*grid_size_meters,(row["cell_x"]+1)*grid_size_meters,(row["cell_y"]+1)*grid_size_meters,target_srid),4326))))
        features={"recent_7":row["recent_7"],"previous_7":row["previous_7"],"older_14":row["older_14"],"neighbor_recent_mean":round(neighbor_recent,3),"trend_ratio":round(trend,3),"total_28":row["total_28"]}
        session.add(PredictionResult(prediction_run_id=run.prediction_run_id,disease=row["illness"],cell_x=row["cell_x"],cell_y=row["cell_y"],cell_geometry=f"SRID=4326;{polygon}",expected_cases=round(expected,3),lower_bound=round(max(0,expected-spread),3),upper_bound=round(expected+spread,3),information_level="LOW" if row["total_28"]<5 else "MODERATE",features_json=json.dumps(features,sort_keys=True)))
    run.runtime_ms=round((time.monotonic()-started)*1000)
    session.commit(); return run

def prediction_json(session: Session, run_id: int) -> dict:
    run=session.get(PredictionRun,run_id)
    if not run: raise ValueError("预测运行不存在")
    rows=session.execute(select(PredictionResult,func.ST_AsGeoJSON(PredictionResult.cell_geometry)).where(PredictionResult.prediction_run_id==run_id).order_by(PredictionResult.expected_cases.desc())).all()
    return {"run":{"prediction_run_id":run.prediction_run_id,"cutoff_date":run.cutoff_date.isoformat(),"forecast_start":run.forecast_start.isoformat(),"forecast_end":run.forecast_end.isoformat(),"model_type":run.model_type,"model_version":run.model_version,"validation_status":run.validation_status,"grid_size_meters":run.grid_size_meters,"runtime_ms":run.runtime_ms},"cells":[{"disease":r.PredictionResult.disease,"expected_cases":r.PredictionResult.expected_cases,"lower_bound":r.PredictionResult.lower_bound,"upper_bound":r.PredictionResult.upper_bound,"information_level":r.PredictionResult.information_level,"features":json.loads(r.PredictionResult.features_json),"geometry":json.loads(r[1])} for r in rows]}
