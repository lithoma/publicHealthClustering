"""Local web dashboard for running and exploring spatial clustering."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .clustering import ClusteringParameterError, run_clustering
from .db import create_db_engine
from .ingestion import load_communities, load_facilities, load_patients
from .ingestion.importer import import_dataset
from .location_resolution import resolve_patient_locations
from .models import ClusteringResult, ClusteringRun, ClusteringRunStatus
from .visualization import cluster_color

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parents[1]
app = FastAPI(title="Public Health Clustering Explorer")


class RunRequest(BaseModel):
    analysis_end: date
    window_days: int = Field(7, gt=0)
    eps_meters: float = Field(500, gt=0)
    min_points: int = Field(2, gt=0)
    target_srid: int = Field(32614, gt=0)
    include_proxy_locations: bool = True
    illness_filter: str | None = None


def _run_json(run: ClusteringRun) -> dict:
    return {
        "run_id": run.run_id, "window_start": run.window_start.isoformat(),
        "window_end": run.window_end.isoformat(), "window_days": run.window_days,
        "eps_meters": run.eps_meters, "min_points": run.min_points,
        "target_srid": run.target_srid, "illness_filter": run.illness_filter,
        "include_proxy_locations": run.include_proxy_locations,
        "status": run.status.value, "case_count": run.case_count,
        "proxy_case_count": run.proxy_case_count, "cluster_count": run.cluster_count,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(PACKAGE_DIR / "web" / "dashboard.html")


@app.get("/api/runs")
def list_runs() -> list[dict]:
    engine = create_db_engine()
    try:
        with Session(engine) as session:
            runs = session.execute(
                select(ClusteringRun).order_by(ClusteringRun.run_id.desc())
            ).scalars().all()
            return [_run_json(run) for run in runs]
    finally:
        engine.dispose()


@app.get("/api/results")
def results(
    run_id: int,
    start: date | None = Query(None),
    end: date | None = Query(None),
) -> dict:
    engine = create_db_engine()
    try:
        with Session(engine) as session:
            run = session.get(ClusteringRun, run_id)
            if run is None:
                raise HTTPException(404, f"Clustering run {run_id} does not exist")
            query = select(
                ClusteringResult,
                func.ST_Y(ClusteringResult.location_snapshot).label("latitude"),
                func.ST_X(ClusteringResult.location_snapshot).label("longitude"),
            ).where(ClusteringResult.run_id == run_id)
            if start:
                query = query.where(ClusteringResult.diagnosis_date_snapshot >= start)
            if end:
                query = query.where(ClusteringResult.diagnosis_date_snapshot <= end)
            rows = session.execute(query.order_by(ClusteringResult.patient_id)).all()
            points = [{
                "patient_id": row.ClusteringResult.patient_id,
                "illness": row.ClusteringResult.illness_snapshot,
                "diagnosis_date": row.ClusteringResult.diagnosis_date_snapshot.isoformat(),
                "cluster_id": row.ClusteringResult.cluster_id,
                "cluster_label": "Noise" if row.ClusteringResult.cluster_id is None else f"Cluster {row.ClusteringResult.cluster_id}",
                "color": cluster_color(row.ClusteringResult.cluster_id),
                "location_source": row.ClusteringResult.location_source_snapshot.value,
                "is_proxy": row.ClusteringResult.is_proxy_snapshot,
                "latitude": float(row.latitude), "longitude": float(row.longitude),
            } for row in rows]
            return {"run": _run_json(run), "points": points}
    finally:
        engine.dispose()


@app.post("/api/runs")
def create_run(request: RunRequest) -> dict:
    try:
        run = run_clustering(
            analysis_end=request.analysis_end, window_days=request.window_days,
            eps_meters=request.eps_meters, min_points=request.min_points,
            target_srid=request.target_srid,
            include_proxy_locations=request.include_proxy_locations,
            illness_filter=request.illness_filter or None,
        )
    except ClusteringParameterError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _run_json(run)


@app.post("/api/demo")
def load_demo() -> dict:
    """Idempotently load synthetic sample rows, resolve locations, and cluster."""
    sample = PROJECT_DIR / "data" / "sample"
    communities, community_errors = load_communities(sample / "communities.csv")
    facilities, facility_errors = load_facilities(sample / "healthcare_facilities.csv")
    patients, patient_errors = load_patients(sample / "patients.csv")
    errors = community_errors + facility_errors + patient_errors
    if errors:
        raise HTTPException(500, f"Synthetic sample validation failed ({len(errors)} errors)")
    engine = create_db_engine()
    try:
        with Session(engine) as session:
            import_dataset(session, communities, facilities, patients)
            resolve_patient_locations(session)
            session.commit()
        run = run_clustering(
            analysis_end=date(2026, 9, 11), window_days=7, eps_meters=500,
            min_points=2, target_srid=32614, include_proxy_locations=True,
            engine=engine,
        )
        return _run_json(run)
    finally:
        engine.dispose()


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
