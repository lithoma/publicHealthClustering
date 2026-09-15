"""Deterministic descriptive reports for persisted clustering results."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from html import escape
import math

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import ClusteringResult, ClusteringRun, ClusteringRunStatus


class ClusterReportError(ValueError):
    """Raised when a persisted run cannot produce a cluster report."""


@dataclass(frozen=True)
class ClusterReport:
    run_id: int
    cluster_id: int
    illness: str
    case_count: int
    proxy_case_count: int
    proxy_share: float
    first_diagnosis_date: date
    last_diagnosis_date: date
    active_days: int
    centroid_latitude: float
    centroid_longitude: float
    mean_distance_to_centroid_m: float
    median_distance_to_centroid_m: float
    max_distance_to_centroid_m: float
    coverage_area_km2: float
    cases_per_km2: float | None
    daily_counts: list[dict]
    location_source_counts: list[dict]

    def to_dict(self) -> dict:
        value = asdict(self)
        value["first_diagnosis_date"] = self.first_diagnosis_date.isoformat()
        value["last_diagnosis_date"] = self.last_diagnosis_date.isoformat()
        return value


def _completed_run(session: Session, run_id: int) -> ClusteringRun:
    run = session.get(ClusteringRun, run_id)
    if run is None:
        raise ClusterReportError(f"Clustering run {run_id} does not exist")
    if run.status != ClusteringRunStatus.COMPLETED:
        raise ClusterReportError(
            f"Clustering run {run_id} is {run.status.value}; reports require COMPLETED"
        )
    return run


def build_cluster_reports(session: Session, run_id: int) -> tuple[ClusteringRun, list[ClusterReport], dict]:
    """Calculate reports from immutable result snapshots without changing data."""
    run = _completed_run(session, run_id)
    projected = func.ST_Transform(ClusteringResult.location_snapshot, run.target_srid)
    rows = session.execute(
        select(
            ClusteringResult.cluster_id,
            ClusteringResult.illness_snapshot,
            ClusteringResult.diagnosis_date_snapshot,
            ClusteringResult.location_source_snapshot,
            ClusteringResult.is_proxy_snapshot,
            func.ST_X(projected).label("x"),
            func.ST_Y(projected).label("y"),
        )
        .where(ClusteringResult.run_id == run_id)
        .order_by(ClusteringResult.cluster_id, ClusteringResult.patient_id)
    ).all()

    grouped: dict[int, list] = defaultdict(list)
    noise_by_illness: Counter[str] = Counter()
    noise_proxy_count = 0
    for row in rows:
        if row.cluster_id is None:
            noise_by_illness[row.illness_snapshot] += 1
            noise_proxy_count += int(row.is_proxy_snapshot)
        else:
            grouped[int(row.cluster_id)].append(row)

    reports: list[ClusterReport] = []
    for cluster_id, cluster_rows in sorted(grouped.items()):
        illnesses = {row.illness_snapshot for row in cluster_rows}
        if len(illnesses) != 1:
            raise ClusterReportError(
                f"Cluster {cluster_id} contains multiple diseases: {sorted(illnesses)}"
            )
        xs = [float(row.x) for row in cluster_rows]
        ys = [float(row.y) for row in cluster_rows]
        center_x, center_y = sum(xs) / len(xs), sum(ys) / len(ys)
        distances = sorted(math.hypot(x - center_x, y - center_y) for x, y in zip(xs, ys))
        middle = len(distances) // 2
        median = (
            distances[middle]
            if len(distances) % 2
            else (distances[middle - 1] + distances[middle]) / 2
        )
        centroid = session.execute(
            select(
                func.ST_X(func.ST_Transform(func.ST_SetSRID(func.ST_Point(center_x, center_y), run.target_srid), 4326)),
                func.ST_Y(func.ST_Transform(func.ST_SetSRID(func.ST_Point(center_x, center_y), run.target_srid), 4326)),
            )
        ).one()
        coverage_area_m2 = float(session.scalar(
            select(func.ST_Area(func.ST_UnaryUnion(func.ST_Collect(func.ST_Buffer(projected, run.eps_meters)))))
            .where(
                ClusteringResult.run_id == run_id,
                ClusteringResult.cluster_id == cluster_id,
            )
        ) or 0)
        dates = [row.diagnosis_date_snapshot for row in cluster_rows]
        daily = Counter(dates)
        sources = Counter(row.location_source_snapshot.value for row in cluster_rows)
        proxy_count = sum(int(row.is_proxy_snapshot) for row in cluster_rows)
        area_km2 = coverage_area_m2 / 1_000_000
        reports.append(ClusterReport(
            run_id=run_id,
            cluster_id=cluster_id,
            illness=next(iter(illnesses)),
            case_count=len(cluster_rows),
            proxy_case_count=proxy_count,
            proxy_share=proxy_count / len(cluster_rows),
            first_diagnosis_date=min(dates),
            last_diagnosis_date=max(dates),
            active_days=(max(dates) - min(dates)).days + 1,
            centroid_latitude=float(centroid[1]),
            centroid_longitude=float(centroid[0]),
            mean_distance_to_centroid_m=sum(distances) / len(distances),
            median_distance_to_centroid_m=median,
            max_distance_to_centroid_m=max(distances),
            coverage_area_km2=area_km2,
            cases_per_km2=len(cluster_rows) / area_km2 if area_km2 else None,
            daily_counts=[{"date": day.isoformat(), "case_count": daily[day]} for day in sorted(daily)],
            location_source_counts=[{"source": source, "case_count": sources[source]} for source in sorted(sources)],
        ))
    noise = {
        "case_count": sum(noise_by_illness.values()),
        "proxy_case_count": noise_proxy_count,
        "by_illness": [
            {"illness": illness, "case_count": noise_by_illness[illness]}
            for illness in sorted(noise_by_illness)
        ],
    }
    return run, reports, noise


def _bars(items: list[dict], label_key: str, value_key: str) -> str:
    maximum = max((item[value_key] for item in items), default=1)
    return "".join(
        f'<div class="bar-row"><span>{escape(str(item[label_key]))}</span>'
        f'<i style="width:{100 * item[value_key] / maximum:.1f}%"></i>'
        f'<b>{item[value_key]}</b></div>'
        for item in items
    ) or '<p class="muted">No data</p>'


def render_cluster_report_html(run: ClusteringRun, report: ClusterReport, noise: dict) -> str:
    """Return a dependency-free standalone HTML report suitable for saving or printing."""
    density = "—" if report.cases_per_km2 is None else f"{report.cases_per_km2:.1f}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(report.illness)} cluster {report.cluster_id} report</title>
<style>
body{{font:14px Inter,Arial,sans-serif;color:#242724;background:#f4f1e9;margin:0}}main{{max-width:960px;margin:32px auto;padding:28px;background:#fffdf7;border:1px solid #ded9cc;border-radius:14px}}h1{{margin:0;color:#8f321f}}h2{{font-size:15px;margin-top:28px}}.muted{{color:#6f746f}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}.card{{padding:14px;background:#f3eee4;border-radius:9px}}.card b{{display:block;font-size:21px;color:#a84328}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px;border-bottom:1px solid #e3dfd5;text-align:left}}.charts{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}.bar-row{{display:grid;grid-template-columns:115px 1fr 34px;gap:8px;align-items:center;margin:8px 0}}.bar-row i{{display:block;height:13px;background:#e4572e;border-radius:3px;min-width:2px}}.bar-row b{{text-align:right}}@media(max-width:700px){{.cards,.charts{{grid-template-columns:1fr 1fr}}}}@media print{{body{{background:white}}main{{border:0;margin:0;max-width:none}}}}
</style></head><body><main>
<p class="muted">Persisted DBSCAN run #{run.run_id}</p><h1>{escape(report.illness)} · Cluster {report.cluster_id}</h1>
<p>Diagnosis window {run.window_start.date().isoformat()} to {run.window_end.date().isoformat()} (end exclusive). Generated from immutable clustering snapshots.</p>
<section class="cards"><div class="card"><b>{report.case_count}</b>cases</div><div class="card"><b>{report.active_days}</b>active days</div><div class="card"><b>{report.coverage_area_km2:.2f}</b>coverage km²</div><div class="card"><b>{density}</b>cases/km²</div></section>
<div class="charts"><section><h2>Cases by diagnosis date</h2>{_bars(report.daily_counts, 'date', 'case_count')}</section><section><h2>Location sources</h2>{_bars(report.location_source_counts, 'source', 'case_count')}</section></div>
<h2>Spatial and temporal measures</h2><table>
<tr><th>Measure</th><th>Value</th></tr>
<tr><td>First diagnosis</td><td>{report.first_diagnosis_date.isoformat()}</td></tr><tr><td>Last diagnosis</td><td>{report.last_diagnosis_date.isoformat()}</td></tr>
<tr><td>Centroid (WGS84)</td><td>{report.centroid_longitude:.6f}, {report.centroid_latitude:.6f}</td></tr>
<tr><td>Mean distance to centroid</td><td>{report.mean_distance_to_centroid_m:.1f} m</td></tr><tr><td>Median distance to centroid</td><td>{report.median_distance_to_centroid_m:.1f} m</td></tr><tr><td>Maximum distance to centroid</td><td>{report.max_distance_to_centroid_m:.1f} m</td></tr>
<tr><td>Proxy locations</td><td>{report.proxy_case_count} ({report.proxy_share:.1%})</td></tr><tr><td>Noise in whole run</td><td>{noise['case_count']}</td></tr></table>
<h2>Analysis parameters</h2><table><tr><td>eps</td><td>{run.eps_meters:g} m</td></tr><tr><td>Minimum points</td><td>{run.min_points}</td></tr><tr><td>Target SRID</td><td>{run.target_srid}</td></tr><tr><td>Proxy locations included</td><td>{'Yes' if run.include_proxy_locations else 'No'}</td></tr></table>
<p class="muted">Coverage area is the union of {run.eps_meters:g}-metre buffers around cluster cases. It is descriptive, not an administrative boundary or transmission zone.</p>
</main></body></html>"""
