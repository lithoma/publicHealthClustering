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
    age_known_count: int
    age_missing_count: int
    median_age_years: float | None
    minimum_age_years: int | None
    maximum_age_years: int | None
    age_group_counts: list[dict]
    sex_known_count: int
    sex_missing_count: int
    sex_counts: list[dict]
    peak_diagnosis_date: date
    peak_daily_cases: int
    recent_three_day_cases: int

    def to_dict(self) -> dict:
        value = asdict(self)
        value["first_diagnosis_date"] = self.first_diagnosis_date.isoformat()
        value["last_diagnosis_date"] = self.last_diagnosis_date.isoformat()
        value["peak_diagnosis_date"] = self.peak_diagnosis_date.isoformat()
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
            ClusteringResult.sex_snapshot,
            ClusteringResult.age_years_snapshot,
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
        ages = sorted(row.age_years_snapshot for row in cluster_rows if row.age_years_snapshot is not None)
        sexes = Counter(row.sex_snapshot for row in cluster_rows if row.sex_snapshot is not None)
        age_bands = [("0–4", 0, 4), ("5–14", 5, 14), ("15–24", 15, 24),
                     ("25–44", 25, 44), ("45–64", 45, 64), ("65+", 65, 120)]
        age_groups = [{"age_group": label, "case_count": sum(low <= age <= high for age in ages)}
                      for label, low, high in age_bands]
        age_groups = [item for item in age_groups if item["case_count"]]
        age_median = None
        if ages:
            age_middle = len(ages) // 2
            age_median = float(ages[age_middle] if len(ages) % 2 else
                               (ages[age_middle - 1] + ages[age_middle]) / 2)
        peak_day, peak_count = min(daily.items(), key=lambda item: (-item[1], item[0]))
        recent_cutoff = run.window_end.date().toordinal() - 3
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
            age_known_count=len(ages), age_missing_count=len(cluster_rows) - len(ages),
            median_age_years=age_median,
            minimum_age_years=min(ages) if ages else None,
            maximum_age_years=max(ages) if ages else None,
            age_group_counts=age_groups,
            sex_known_count=sum(sexes.values()), sex_missing_count=len(cluster_rows) - sum(sexes.values()),
            sex_counts=[{"sex": sex, "case_count": sexes[sex]} for sex in sorted(sexes)],
            peak_diagnosis_date=peak_day, peak_daily_cases=peak_count,
            recent_three_day_cases=sum(1 for value in dates if value.toordinal() >= recent_cutoff),
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
    median_age = "—" if report.median_age_years is None else f"{report.median_age_years:g}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(report.illness)} cluster {report.cluster_id} report</title>
<style>
body{{font:14px Inter,Arial,sans-serif;color:#242724;background:#f4f1e9;margin:0}}main{{max-width:960px;margin:32px auto;padding:28px;background:#fffdf7;border:1px solid #ded9cc;border-radius:14px}}h1{{margin:0;color:#8f321f}}h2{{font-size:15px;margin-top:28px}}.muted{{color:#6f746f}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}.card{{padding:14px;background:#f3eee4;border-radius:9px}}.card b{{display:block;font-size:21px;color:#a84328}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px;border-bottom:1px solid #e3dfd5;text-align:left}}.charts{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}.bar-row{{display:grid;grid-template-columns:115px 1fr 34px;gap:8px;align-items:center;margin:8px 0}}.bar-row i{{display:block;height:13px;background:#e4572e;border-radius:3px;min-width:2px}}.bar-row b{{text-align:right}}@media(max-width:700px){{.cards,.charts{{grid-template-columns:1fr 1fr}}}}@media print{{body{{background:white}}main{{border:0;margin:0;max-width:none}}}}
</style></head><body><main>
<p class="muted">Persisted DBSCAN run #{run.run_id}</p><h1>{escape(report.illness)} · Cluster {report.cluster_id}</h1>
<p>Diagnosis window {run.window_start.date().isoformat()} to {run.window_end.date().isoformat()} (end exclusive). Generated from immutable clustering snapshots.</p>
<section class="cards"><div class="card"><b>{report.case_count}</b>cases</div><div class="card"><b>{report.recent_three_day_cases}</b>cases in latest 3 days</div><div class="card"><b>{median_age}</b>median age</div><div class="card"><b>{report.proxy_share:.1%}</b>proxy locations</div></section>
<div class="charts"><section><h2>Time: cases by diagnosis date</h2>{_bars(report.daily_counts, 'date', 'case_count')}</section><section><h2>Person: age groups</h2>{_bars(report.age_group_counts, 'age_group', 'case_count')}</section><section><h2>Person: sex</h2>{_bars(report.sex_counts, 'sex', 'case_count')}</section><section><h2>Place: location sources</h2>{_bars(report.location_source_counts, 'source', 'case_count')}</section></div>
<h2>Spatial and temporal measures</h2><table>
<tr><th>Measure</th><th>Value</th></tr>
<tr><td>First diagnosis</td><td>{report.first_diagnosis_date.isoformat()}</td></tr><tr><td>Last diagnosis</td><td>{report.last_diagnosis_date.isoformat()}</td></tr>
<tr><td>Peak diagnosis date</td><td>{report.peak_diagnosis_date.isoformat()} ({report.peak_daily_cases} cases)</td></tr>
<tr><td>Age completeness</td><td>{report.age_known_count}/{report.case_count} known; {report.age_missing_count} missing</td></tr>
<tr><td>Age range</td><td>{'—' if report.minimum_age_years is None else f'{report.minimum_age_years}–{report.maximum_age_years} years'}</td></tr>
<tr><td>Sex completeness</td><td>{report.sex_known_count}/{report.case_count} known; {report.sex_missing_count} missing</td></tr>
<tr><td>Centroid (WGS84)</td><td>{report.centroid_longitude:.6f}, {report.centroid_latitude:.6f}</td></tr>
<tr><td>Descriptive coverage area</td><td>{report.coverage_area_km2:.2f} km²</td></tr>
<tr><td>Case density within coverage</td><td>{density} cases/km²</td></tr>
<tr><td>Mean distance to centroid</td><td>{report.mean_distance_to_centroid_m:.1f} m</td></tr><tr><td>Median distance to centroid</td><td>{report.median_distance_to_centroid_m:.1f} m</td></tr><tr><td>Maximum distance to centroid</td><td>{report.max_distance_to_centroid_m:.1f} m</td></tr>
<tr><td>Proxy locations</td><td>{report.proxy_case_count} ({report.proxy_share:.1%})</td></tr><tr><td>Noise in whole run</td><td>{noise['case_count']}</td></tr></table>
<h2>Analysis parameters</h2><table><tr><td>eps</td><td>{run.eps_meters:g} m</td></tr><tr><td>Minimum points</td><td>{run.min_points}</td></tr><tr><td>Target SRID</td><td>{run.target_srid}</td></tr><tr><td>Proxy locations included</td><td>{'Yes' if run.include_proxy_locations else 'No'}</td></tr></table>
<p class="muted">Coverage area is the union of {run.eps_meters:g}-metre buffers around cluster cases. It is descriptive, not an administrative boundary or transmission zone.</p>
<p class="muted">Age and sex values are case distributions. Without population denominators they are not attack rates and do not by themselves establish higher population risk.</p>
</main></body></html>"""
