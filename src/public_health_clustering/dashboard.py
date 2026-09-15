"""Local web dashboard for running and exploring spatial clustering."""

from __future__ import annotations

from datetime import date
import json
import math
from pathlib import Path
import random
import tempfile

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .clustering import ClusteringParameterError, run_clustering
from .db import create_db_engine
from .ingestion import load_patients
from .ingestion.importer import import_patients
from .models import (
    ClusteringResult,
    ClusteringRun,
    CaseEvent,
    DataBatch,
    LocationSource,
    Patient,
    PatientLocation,
)

PACKAGE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Public Health Clustering Explorer")

DISEASE_COLORS = {
    "Measles": "#e4572e",
    "Pertussis": "#f29e4c",
    "Influenza": "#c44536",
    "麻疹": "#e4572e",
    "百日咳": "#f29e4c",
    "流感": "#c44536",
}

CITY_CONFIG = {
    "guangzhou": {"name": "广州", "latitude": 23.1291, "longitude": 113.2644,
                  "zoom": 12, "target_srid": 32649},
    "shenzhen": {"name": "深圳", "latitude": 22.5431, "longitude": 114.0579,
                 "zoom": 12, "target_srid": 32650},
    "shanghai": {"name": "上海", "latitude": 31.2304, "longitude": 121.4737,
                 "zoom": 11, "target_srid": 32651},
    "beijing": {"name": "北京", "latitude": 39.9042, "longitude": 116.4074,
                "zoom": 11, "target_srid": 32650},
    "chengdu": {"name": "成都", "latitude": 30.5728, "longitude": 104.0668,
                "zoom": 11, "target_srid": 32648},
    "wuhan": {"name": "武汉", "latitude": 30.5928, "longitude": 114.3055,
              "zoom": 11, "target_srid": 32650},
}


def disease_color(illness: str) -> str:
    """Return a stable, disease-level color shared by points and regions."""
    if illness in DISEASE_COLORS:
        return DISEASE_COLORS[illness]
    import hashlib
    import colorsys

    hue = int.from_bytes(hashlib.sha256(illness.encode()).digest()[:2], "big") / 65536
    # Keep generated colors in the warm red-orange half of the wheel while
    # retaining deterministic separation for diseases not in the named palette.
    warm_hue = (0.97 + hue * 0.16) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(warm_hue, 0.68, 0.82)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


class RunRequest(BaseModel):
    analysis_end: date
    window_days: int = Field(7, gt=0)
    eps_meters: float = Field(500, gt=0)
    min_points: int = Field(2, gt=0)
    target_srid: int = Field(32614, gt=0)
    include_proxy_locations: bool = True
    illness_filter: str | None = None
    batch_ids: list[int] | None = None


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


@app.get("/api/cities")
def cities() -> list[dict]:
    return [{"id": city_id, **config} for city_id, config in CITY_CONFIG.items()]


async def _validated_patient_upload(file: UploadFile):
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "CSV 文件不能超过 10 MB")
    with tempfile.NamedTemporaryFile(suffix=".csv") as temporary:
        temporary.write(content)
        temporary.flush()
        return load_patients(Path(temporary.name))


@app.post("/api/import/preview")
async def preview_patient_import(file: UploadFile = File(...)) -> dict:
    records, errors = await _validated_patient_upload(file)
    return {"valid_count": len(records), "error_count": len(errors),
            "errors": [{"row": e.row_number, "field": e.field,
                        "code": e.code.value, "message": e.message} for e in errors[:100]]}


@app.post("/api/import/patients")
async def confirm_patient_import(
    batch_name: str = Form(...),
    source_name: str = Form(""),
    file: UploadFile = File(...),
) -> dict:
    records, errors = await _validated_patient_upload(file)
    if errors:
        raise HTTPException(400, f"CSV 仍有 {len(errors)} 个验证错误，未写入数据库")
    engine = create_db_engine()
    try:
        with Session(engine) as session:
            if session.scalar(select(DataBatch).where(DataBatch.batch_name == batch_name)):
                raise HTTPException(409, "批次名称已存在")
            batch = DataBatch(batch_name=batch_name.strip(), source_name=source_name.strip() or None,
                              is_synthetic=False, is_included=True)
            session.add(batch)
            session.flush()
            outcome = import_patients(session, records, batch=batch)
            if outcome.failed:
                session.rollback()
                raise HTTPException(400, f"有 {outcome.failed} 条记录无法导入，整个批次未写入")
            batch_id, saved_name = batch.batch_id, batch.batch_name
            session.commit()
            return {"batch_id": batch_id, "batch_name": saved_name,
                    "record_count": len(records)}
    finally:
        engine.dispose()


@app.get("/api/batches")
def batches() -> list[dict]:
    engine = create_db_engine()
    try:
        with Session(engine) as session:
            rows = session.execute(select(DataBatch).order_by(DataBatch.created_at.desc())).scalars()
            return [{"batch_id": b.batch_id, "batch_name": b.batch_name,
                     "source_name": b.source_name, "is_synthetic": b.is_synthetic,
                     "is_included": b.is_included,
                     "patient_count": session.scalar(select(func.count()).select_from(CaseEvent).where(CaseEvent.batch_id == b.batch_id)),
                     "created_at": b.created_at.isoformat()} for b in rows]
    finally:
        engine.dispose()


@app.patch("/api/batches/{batch_id}")
def set_batch_included(batch_id: int, included: bool) -> dict:
    engine = create_db_engine()
    try:
        with Session(engine) as session:
            batch = session.get(DataBatch, batch_id)
            if batch is None:
                raise HTTPException(404, "Batch does not exist")
            batch.is_included = included
            session.commit()
            return {"batch_id": batch_id, "is_included": included}
    finally:
        engine.dispose()


@app.delete("/api/batches/{batch_id}")
def delete_batch(batch_id: int) -> dict:
    engine = create_db_engine()
    try:
        with Session(engine) as session:
            batch = session.get(DataBatch, batch_id)
            if batch is None:
                raise HTTPException(404, "Batch does not exist")
            event_ids = select(CaseEvent.case_event_id).where(CaseEvent.batch_id == batch_id)
            dependent = session.scalar(select(func.count()).select_from(ClusteringResult).where(
                ClusteringResult.case_event_id.in_(event_ids)
            ))
            if dependent:
                raise HTTPException(409, "该批次已被分析运行引用；请保留审计历史或先删除相关运行。")
            count = session.scalar(select(func.count()).select_from(CaseEvent).where(CaseEvent.batch_id == batch_id))
            patient_ids = list(session.scalars(select(CaseEvent.patient_id).where(CaseEvent.batch_id == batch_id)))
            session.query(CaseEvent).filter(CaseEvent.batch_id == batch_id).delete(synchronize_session=False)
            for patient_id in patient_ids:
                has_other_events = session.scalar(select(func.count()).select_from(CaseEvent).where(CaseEvent.patient_id == patient_id))
                if not has_other_events:
                    patient = session.get(Patient, patient_id)
                    if patient is not None:
                        session.delete(patient)
            session.delete(batch)
            session.commit()
            return {"deleted": True, "patient_count": count}
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
                "color": disease_color(row.ClusteringResult.illness_snapshot),
                "location_source": row.ClusteringResult.location_source_snapshot.value,
                "is_proxy": row.ClusteringResult.is_proxy_snapshot,
                "latitude": float(row.latitude), "longitude": float(row.longitude),
            } for row in rows]

            projected = func.ST_Transform(
                ClusteringResult.location_snapshot, run.target_srid
            )
            buffered = func.ST_Buffer(projected, run.eps_meters)
            area_query = select(
                ClusteringResult.illness_snapshot,
                ClusteringResult.cluster_id,
                func.count().label("case_count"),
                func.ST_AsGeoJSON(
                    func.ST_Transform(
                        func.ST_UnaryUnion(func.ST_Collect(buffered)), 4326
                    )
                ).label("geojson"),
            ).where(
                ClusteringResult.run_id == run_id,
                ClusteringResult.cluster_id.is_not(None),
            )
            if start:
                area_query = area_query.where(
                    ClusteringResult.diagnosis_date_snapshot >= start
                )
            if end:
                area_query = area_query.where(
                    ClusteringResult.diagnosis_date_snapshot <= end
                )
            area_query = area_query.group_by(
                ClusteringResult.illness_snapshot, ClusteringResult.cluster_id
            ).order_by(ClusteringResult.illness_snapshot, ClusteringResult.cluster_id)
            areas = [{
                "illness": row.illness_snapshot,
                "cluster_id": row.cluster_id,
                "case_count": row.case_count,
                "color": disease_color(row.illness_snapshot),
                "geometry": json.loads(row.geojson),
            } for row in session.execute(area_query)]
            diseases = sorted({point["illness"] for point in points})
            return {
                "run": _run_json(run), "points": points, "areas": areas,
                "diseases": [{"name": disease, "color": disease_color(disease)}
                             for disease in diseases],
            }
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
            batch_ids=request.batch_ids,
        )
    except ClusteringParameterError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _run_json(run)


@app.post("/api/demo")
def load_demo(city: str = "guangzhou") -> dict:
    """Idempotently load deterministic synthetic cases around a Chinese city."""
    if city not in CITY_CONFIG:
        raise HTTPException(400, f"Unsupported city: {city}")
    config = CITY_CONFIG[city]
    engine = create_db_engine()
    try:
        with Session(engine) as session:
            rng = random.Random(f"public-health-demo-{city}")
            batch_name = f"{config['name']}合成演示-2026-09"
            batch = session.scalar(select(DataBatch).where(DataBatch.batch_name == batch_name))
            if batch is None:
                batch = DataBatch(batch_name=batch_name, source_name="系统生成的固定随机演示数据",
                                  is_synthetic=True, is_included=True)
                session.add(batch)
                session.flush()
            dense_groups = (
                ("麻疹", -0.018, 0.010, 24),
                ("百日咳", 0.012, 0.016, 22),
                ("流感", 0.027, -0.010, 20),
            )
            for group_index, (illness, lon_offset, lat_offset, count) in enumerate(dense_groups):
                center_lon = config["longitude"] + lon_offset
                center_lat = config["latitude"] + lat_offset
                for index in range(count):
                    patient_id = f"CN-{city.upper()}-{group_index + 1}-{index + 1:03d}"
                    if session.get(Patient, patient_id) is not None:
                        continue
                    angle = rng.random() * 6.283185307
                    radius = (rng.random() ** 0.6) * 0.006
                    lon = center_lon + radius * math.cos(angle)
                    lat = center_lat + radius * math.sin(angle)
                    patient = Patient(
                        patient_id=patient_id,
                        name=f"合成{illness}病例{index + 1}",
                        illness=illness,
                        diagnosis_date=date(2026, 9, 4 + index % 7),
                        batch_id=batch.batch_id,
                    )
                    session.add(patient)
                    session.flush()
                    session.add(PatientLocation(
                        patient_id=patient_id,
                        location=f"SRID=4326;POINT({lon} {lat})",
                        location_source=LocationSource.MANUAL,
                        is_proxy=False,
                        is_current=True,
                    ))
                    session.add(CaseEvent(
                        batch_id=batch.batch_id, patient_id=patient_id,
                        source_case_id=patient_id, illness=illness,
                        diagnosis_date=patient.diagnosis_date,
                    ))
            batch_id = batch.batch_id
            session.commit()
        run = run_clustering(
            analysis_end=date(2026, 9, 11), window_days=7, eps_meters=900,
            min_points=3, target_srid=config["target_srid"], include_proxy_locations=True,
            batch_ids=[batch_id],
            engine=engine,
        )
        return _run_json(run)
    finally:
        engine.dispose()


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
