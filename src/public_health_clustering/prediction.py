"""Auditable seven-day spatial forecasting and historical evaluation.

The version-1 forecast remains deliberately training-free. This module now
freezes its input batch cohort, supports outcome attachment after the forecast
window closes, and provides rolling-origin backtests. Evaluation does not imply
that the model is epidemiologically validated or promoted for operational use.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
import json
import math
import time

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.orm import Session

from .clustering import ClusteringParameterError, _validate_target_srid
from .models import (
    DataBatch,
    PredictionEvaluation,
    PredictionResult,
    PredictionRun,
    PredictionRunBatch,
)


MODEL_TYPE = "SHARED_SPATIOTEMPORAL_BASELINE"
MODEL_VERSION = "1"
HORIZON_DAYS = 7
HISTORY_DAYS = 28
PARAMETERS = {
    "recent_7_weight": 0.60,
    "previous_7_weight": 0.30,
    "older_14_weight": 0.10,
    "trend_strength": 0.25,
    "neighbor_weight": 0.15,
    "trend_min": 0.5,
    "trend_max": 2.0,
    "interval_method": "UNCALIBRATED_NORMAL_APPROXIMATION",
    "interval_alpha": 0.05,
}


class PredictionError(ValueError):
    """Raised for invalid prediction or evaluation requests."""


def _validated_batches(
    session: Session, batch_ids: Sequence[int] | None
) -> list[DataBatch]:
    if batch_ids is None:
        return list(
            session.scalars(
                select(DataBatch)
                .where(DataBatch.is_included.is_(True))
                .order_by(DataBatch.batch_id)
            )
        )

    requested = list(dict.fromkeys(batch_ids))
    if not requested:
        raise PredictionError("至少选择一个已纳入的数据批次")
    batches = list(
        session.scalars(
            select(DataBatch)
            .where(DataBatch.batch_id.in_(requested))
            .order_by(DataBatch.batch_id)
        )
    )
    found = {batch.batch_id for batch in batches}
    missing = sorted(set(requested) - found)
    excluded = sorted(batch.batch_id for batch in batches if not batch.is_included)
    if missing:
        raise PredictionError(f"数据批次不存在：{missing}")
    if excluded:
        raise PredictionError(f"数据批次当前已排除：{excluded}")
    return batches


def _validate_prediction_parameters(
    session: Session, target_srid: int, grid_size_meters: int
) -> None:
    if grid_size_meters <= 0:
        raise PredictionError("网格大小必须大于 0 米")
    try:
        _validate_target_srid(session, target_srid)
    except ClusteringParameterError as exc:
        raise PredictionError(str(exc)) from exc


def _count_rows(
    session: Session,
    *,
    start_date: date,
    end_date: date,
    target_srid: int,
    grid_size_meters: int,
    batch_ids: list[int],
    include_proxy_locations: bool,
    feature_cutoff: date | None = None,
    require_currently_included: bool = True,
) -> list[dict]:
    """Aggregate case counts into disease/grid cells for a half-open window."""
    if not batch_ids:
        return []
    proxy_filter = "" if include_proxy_locations else "AND is_proxy = FALSE"
    inclusion_filter = "AND batch_is_included=TRUE" if require_currently_included else ""
    if feature_cutoff is None:
        statement = text(
            f"""
            SELECT illness,
              floor(ST_X(ST_Transform(location,:srid))/:grid)::int AS cell_x,
              floor(ST_Y(ST_Transform(location,:srid))/:grid)::int AS cell_y,
              count(*)::int AS observed_cases
            FROM current_patient_spatial_points
            WHERE batch_id IN :batch_ids
              {inclusion_filter}
              AND diagnosis_date >= :start_date
              AND diagnosis_date < :end_date
              {proxy_filter}
            GROUP BY illness,cell_x,cell_y
            ORDER BY illness,cell_x,cell_y
            """
        ).bindparams(bindparam("batch_ids", expanding=True))
        return list(
            session.execute(
                statement,
                {
                    "srid": target_srid,
                    "grid": grid_size_meters,
                    "batch_ids": batch_ids,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            ).mappings()
        )

    statement = text(
        f"""
        WITH points AS (
          SELECT illness, diagnosis_date,
            floor(ST_X(ST_Transform(location,:srid))/:grid)::int AS cell_x,
            floor(ST_Y(ST_Transform(location,:srid))/:grid)::int AS cell_y
          FROM current_patient_spatial_points
          WHERE batch_id IN :batch_ids
            {inclusion_filter}
            AND diagnosis_date >= :start_date
            AND diagnosis_date < :end_date
            {proxy_filter}
        )
        SELECT illness,cell_x,cell_y,
          count(*) FILTER (WHERE diagnosis_date >= :d7)::int recent_7,
          count(*) FILTER (
            WHERE diagnosis_date >= :d14 AND diagnosis_date < :d7
          )::int previous_7,
          count(*) FILTER (WHERE diagnosis_date < :d14)::int older_14,
          count(*)::int total_28
        FROM points
        GROUP BY illness,cell_x,cell_y
        ORDER BY illness,cell_x,cell_y
        """
    ).bindparams(bindparam("batch_ids", expanding=True))
    return list(
        session.execute(
            statement,
            {
                "srid": target_srid,
                "grid": grid_size_meters,
                "batch_ids": batch_ids,
                "start_date": start_date,
                "end_date": end_date,
                "d7": feature_cutoff - timedelta(days=7),
                "d14": feature_cutoff - timedelta(days=14),
            },
        ).mappings()
    )


def create_prediction(
    session: Session,
    *,
    cutoff_date: date,
    target_srid: int,
    grid_size_meters: int = 1000,
    batch_ids: Sequence[int] | None = None,
    include_proxy_locations: bool = True,
) -> PredictionRun:
    """Persist a seven-day forecast and the immutable batch cohort behind it."""
    _validate_prediction_parameters(session, target_srid, grid_size_meters)
    batches = _validated_batches(session, batch_ids)
    selected_batch_ids = [batch.batch_id for batch in batches]
    started = time.monotonic()
    rows = _count_rows(
        session,
        start_date=cutoff_date - timedelta(days=HISTORY_DAYS),
        end_date=cutoff_date,
        target_srid=target_srid,
        grid_size_meters=grid_size_meters,
        batch_ids=selected_batch_ids,
        include_proxy_locations=include_proxy_locations,
        feature_cutoff=cutoff_date,
    )

    run = PredictionRun(
        cutoff_date=cutoff_date,
        forecast_start=cutoff_date,
        forecast_end=cutoff_date + timedelta(days=HORIZON_DAYS),
        horizon_days=HORIZON_DAYS,
        history_days=HISTORY_DAYS,
        grid_size_meters=grid_size_meters,
        target_srid=target_srid,
        model_type=MODEL_TYPE,
        model_version=MODEL_VERSION,
        validation_status="SIMULATION_UNVALIDATED",
        parameters_json=json.dumps(PARAMETERS, sort_keys=True),
        include_proxy_locations=include_proxy_locations,
        status="COMPLETED",
    )
    session.add(run)
    session.flush()
    for batch in batches:
        session.add(
            PredictionRunBatch(
                prediction_run_id=run.prediction_run_id,
                batch_id_snapshot=batch.batch_id,
                batch_name_snapshot=batch.batch_name,
            )
        )

    lookup = {
        (row["illness"], row["cell_x"], row["cell_y"]): row for row in rows
    }
    for row in rows:
        neighbors = [
            lookup.get((row["illness"], row["cell_x"] + dx, row["cell_y"] + dy))
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            if dx or dy
        ]
        neighbor_recent = sum(
            neighbor["recent_7"] for neighbor in neighbors if neighbor
        ) / 8
        level = (
            0.6 * row["recent_7"]
            + 0.3 * row["previous_7"]
            + 0.1 * (row["older_14"] / 2)
        )
        trend = max(
            0.5,
            min(2.0, (row["recent_7"] + 1) / (row["previous_7"] + 1)),
        )
        expected = max(0, level * (0.75 + 0.25 * trend) + 0.15 * neighbor_recent)
        spread = 1.96 * math.sqrt(expected + 1)
        polygon = session.scalar(
            select(
                func.ST_AsText(
                    func.ST_Transform(
                        func.ST_MakeEnvelope(
                            row["cell_x"] * grid_size_meters,
                            row["cell_y"] * grid_size_meters,
                            (row["cell_x"] + 1) * grid_size_meters,
                            (row["cell_y"] + 1) * grid_size_meters,
                            target_srid,
                        ),
                        4326,
                    )
                )
            )
        )
        features = {
            "recent_7": row["recent_7"],
            "previous_7": row["previous_7"],
            "older_14": row["older_14"],
            "neighbor_recent_mean": round(neighbor_recent, 3),
            "trend_ratio": round(trend, 3),
            "total_28": row["total_28"],
        }
        session.add(
            PredictionResult(
                prediction_run_id=run.prediction_run_id,
                disease=row["illness"],
                cell_x=row["cell_x"],
                cell_y=row["cell_y"],
                cell_geometry=f"SRID=4326;{polygon}",
                expected_cases=round(expected, 3),
                lower_bound=round(max(0, expected - spread), 3),
                upper_bound=round(expected + spread, 3),
                information_level="LOW" if row["total_28"] < 5 else "MODERATE",
                features_json=json.dumps(features, sort_keys=True),
            )
        )
    run.runtime_ms = round((time.monotonic() - started) * 1000)
    session.commit()
    return run


def _interval_score(
    lower: float, upper: float, observed: int, alpha: float = 0.05
) -> float:
    score = upper - lower
    if observed < lower:
        score += (2 / alpha) * (lower - observed)
    elif observed > upper:
        score += (2 / alpha) * (observed - upper)
    return score


def evaluate_prediction_run(
    session: Session, run_id: int, *, as_of_date: date
) -> PredictionEvaluation:
    """Attach observed outcomes and score one closed forecast window."""
    run = session.get(PredictionRun, run_id)
    if run is None:
        raise PredictionError("预测运行不存在")
    if run.status != "COMPLETED":
        raise PredictionError("只能评估已完成的预测运行")
    if as_of_date < run.forecast_end:
        raise PredictionError(
            f"预测窗口尚未结束；as_of_date 至少应为 {run.forecast_end.isoformat()}"
        )
    batch_snapshots = list(
        session.scalars(
            select(PredictionRunBatch)
            .where(PredictionRunBatch.prediction_run_id == run_id)
            .order_by(PredictionRunBatch.batch_id_snapshot)
        )
    )
    if not batch_snapshots:
        raise PredictionError("该旧预测没有批次快照，无法进行可复现评估")
    existing_evaluation = session.scalar(
        select(PredictionEvaluation).where(
            PredictionEvaluation.prediction_run_id == run_id
        )
    )
    if existing_evaluation is not None:
        if existing_evaluation.as_of_date != as_of_date:
            raise PredictionError("该预测已有不可变评估记录，不能使用新的 as_of_date 覆盖")
        return existing_evaluation

    actual_rows = _count_rows(
        session,
        start_date=run.forecast_start,
        end_date=run.forecast_end,
        target_srid=run.target_srid,
        grid_size_meters=run.grid_size_meters,
        batch_ids=[row.batch_id_snapshot for row in batch_snapshots],
        include_proxy_locations=run.include_proxy_locations,
        require_currently_included=False,
    )
    actual = {
        (row["illness"], row["cell_x"], row["cell_y"]): row["observed_cases"]
        for row in actual_rows
    }
    results = list(
        session.scalars(
            select(PredictionResult)
            .where(PredictionResult.prediction_run_id == run_id)
            .order_by(
                PredictionResult.disease,
                PredictionResult.cell_x,
                PredictionResult.cell_y,
            )
        )
    )
    forecast = {
        (result.disease, result.cell_x, result.cell_y): result for result in results
    }
    for key, result in forecast.items():
        result.observed_cases = actual.get(key, 0)

    absolute_errors = [
        abs(result.expected_cases - (result.observed_cases or 0)) for result in results
    ]
    squared_errors = [error**2 for error in absolute_errors]
    interval_scores = [
        _interval_score(
            result.lower_bound,
            result.upper_bound,
            result.observed_cases or 0,
        )
        for result in results
    ]
    covered = [
        result.lower_bound <= (result.observed_cases or 0) <= result.upper_bound
        for result in results
    ]
    outside_cases = sum(count for key, count in actual.items() if key not in forecast)
    observed_in_cells = sum(result.observed_cases or 0 for result in results)

    all_keys = set(forecast) | set(actual)
    positive_actual = [key for key in all_keys if actual.get(key, 0) > 0]
    hotspot_k = min(5, len(positive_actual)) or None
    hotspot_recall = None
    if hotspot_k:
        predicted_top = {
            key
            for key in sorted(
                forecast,
                key=lambda item: (-forecast[item].expected_cases, item),
            )[:hotspot_k]
        }
        observed_top = set(
            sorted(all_keys, key=lambda item: (-actual.get(item, 0), item))[:hotspot_k]
        )
        hotspot_recall = len(predicted_top & observed_top) / hotspot_k

    metrics = {
        "model_type": run.model_type,
        "model_version": run.model_version,
        "forecast_interval": "[forecast_start, forecast_end)",
        "forecast_total": round(sum(result.expected_cases for result in results), 3),
        "observed_total": observed_in_cells + outside_cases,
        "observed_in_forecast_cells": observed_in_cells,
        "observed_outside_forecast_cells": outside_cases,
        "interval_alpha": PARAMETERS["interval_alpha"],
    }
    evaluation = PredictionEvaluation(prediction_run_id=run_id)
    session.add(evaluation)
    evaluation.as_of_date = as_of_date
    evaluation.forecast_cell_count = len(results)
    evaluation.observed_cases_in_cells = observed_in_cells
    evaluation.observed_cases_outside_cells = outside_cases
    evaluation.mae = round(sum(absolute_errors) / len(results), 6) if results else None
    evaluation.rmse = (
        round(math.sqrt(sum(squared_errors) / len(results)), 6) if results else None
    )
    evaluation.mean_interval_score = (
        round(sum(interval_scores) / len(results), 6) if results else None
    )
    evaluation.interval_coverage = (
        round(sum(covered) / len(results), 6) if results else None
    )
    evaluation.hotspot_k = hotspot_k
    evaluation.hotspot_recall = (
        round(hotspot_recall, 6) if hotspot_recall is not None else None
    )
    evaluation.metrics_json = json.dumps(metrics, ensure_ascii=False, sort_keys=True)
    session.commit()
    return evaluation


def evaluation_json(session: Session, run_id: int) -> dict:
    run = session.get(PredictionRun, run_id)
    if run is None:
        raise PredictionError("预测运行不存在")
    evaluation = session.scalar(
        select(PredictionEvaluation).where(
            PredictionEvaluation.prediction_run_id == run_id
        )
    )
    if evaluation is None:
        raise PredictionError("该预测尚未附加观测结果")
    return {
        "prediction_run_id": run_id,
        "cutoff_date": run.cutoff_date.isoformat(),
        "forecast_start": run.forecast_start.isoformat(),
        "forecast_end": run.forecast_end.isoformat(),
        "as_of_date": evaluation.as_of_date.isoformat(),
        "model_type": run.model_type,
        "model_version": run.model_version,
        "forecast_cell_count": evaluation.forecast_cell_count,
        "observed_cases_in_cells": evaluation.observed_cases_in_cells,
        "observed_cases_outside_cells": evaluation.observed_cases_outside_cells,
        "mae": evaluation.mae,
        "rmse": evaluation.rmse,
        "mean_interval_score": evaluation.mean_interval_score,
        "interval_coverage": evaluation.interval_coverage,
        "hotspot_k": evaluation.hotspot_k,
        "hotspot_recall": evaluation.hotspot_recall,
        "metrics": json.loads(evaluation.metrics_json),
    }


def run_backtest(
    session: Session,
    *,
    start_cutoff: date,
    end_cutoff: date,
    as_of_date: date,
    target_srid: int,
    grid_size_meters: int = 1000,
    step_days: int = 7,
    batch_ids: Sequence[int] | None = None,
    include_proxy_locations: bool = True,
) -> list[dict]:
    """Run leakage-safe rolling historical forecasts and attach their outcomes."""
    if start_cutoff > end_cutoff:
        raise PredictionError("回测开始日期不能晚于结束日期")
    if step_days <= 0:
        raise PredictionError("回测步长必须大于 0 天")
    if end_cutoff + timedelta(days=HORIZON_DAYS) > as_of_date:
        raise PredictionError("最后一个预测窗口必须在 as_of_date 当日或之前结束")
    run_count = ((end_cutoff - start_cutoff).days // step_days) + 1
    if run_count > 52:
        raise PredictionError("单次回测最多允许 52 个历史切点")

    output = []
    cutoff = start_cutoff
    while cutoff <= end_cutoff:
        run = create_prediction(
            session,
            cutoff_date=cutoff,
            target_srid=target_srid,
            grid_size_meters=grid_size_meters,
            batch_ids=batch_ids,
            include_proxy_locations=include_proxy_locations,
        )
        evaluate_prediction_run(
            session, run.prediction_run_id, as_of_date=as_of_date
        )
        output.append(evaluation_json(session, run.prediction_run_id))
        cutoff += timedelta(days=step_days)
    return output


def prediction_json(session: Session, run_id: int) -> dict:
    run = session.get(PredictionRun, run_id)
    if not run:
        raise PredictionError("预测运行不存在")
    rows = session.execute(
        select(PredictionResult, func.ST_AsGeoJSON(PredictionResult.cell_geometry))
        .where(PredictionResult.prediction_run_id == run_id)
        .order_by(PredictionResult.expected_cases.desc())
    ).all()
    batches = list(
        session.scalars(
            select(PredictionRunBatch)
            .where(PredictionRunBatch.prediction_run_id == run_id)
            .order_by(PredictionRunBatch.batch_id_snapshot)
        )
    )
    return {
        "run": {
            "prediction_run_id": run.prediction_run_id,
            "cutoff_date": run.cutoff_date.isoformat(),
            "forecast_start": run.forecast_start.isoformat(),
            "forecast_end": run.forecast_end.isoformat(),
            "model_type": run.model_type,
            "model_version": run.model_version,
            "validation_status": run.validation_status,
            "grid_size_meters": run.grid_size_meters,
            "target_srid": run.target_srid,
            "include_proxy_locations": run.include_proxy_locations,
            "runtime_ms": run.runtime_ms,
            "batches": [
                {
                    "batch_id": batch.batch_id_snapshot,
                    "batch_name": batch.batch_name_snapshot,
                }
                for batch in batches
            ],
            "evaluated": run.evaluation is not None,
        },
        "cells": [
            {
                "disease": row.PredictionResult.disease,
                "expected_cases": row.PredictionResult.expected_cases,
                "lower_bound": row.PredictionResult.lower_bound,
                "upper_bound": row.PredictionResult.upper_bound,
                "observed_cases": row.PredictionResult.observed_cases,
                "information_level": row.PredictionResult.information_level,
                "features": json.loads(row.PredictionResult.features_json),
                "geometry": json.loads(row[1]),
            }
            for row in rows
        ],
    }
