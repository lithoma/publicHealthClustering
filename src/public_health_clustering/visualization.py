"""Read-only Folium visualization of persisted clustering runs."""

from __future__ import annotations

import colorsys
import hashlib
from html import escape
from pathlib import Path

import folium
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from .db import create_db_engine
from .models import ClusteringResult, ClusteringRun, ClusteringRunStatus


class ClusteringRunNotFoundError(LookupError):
    """Raised when a requested persisted clustering run does not exist."""


class ClusteringRunNotCompletedError(ValueError):
    """Raised when a run is not ready for visualization."""


def cluster_color(cluster_id: int | None) -> str:
    """Return a stable color for a run-local cluster; noise is dark gray."""
    if cluster_id is None:
        return "#4b5563"
    digest = hashlib.sha256(str(cluster_id).encode("ascii")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65536
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.72, 0.78)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def _display(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return "—" if value is None else str(value)


def _metadata_html(run: ClusteringRun) -> str:
    fields = (
        ("Analysis window", f"{run.window_start.isoformat()} – {run.window_end.isoformat()}"),
        ("Window days", run.window_days),
        ("Epsilon (meters)", run.eps_meters),
        ("Minimum points", run.min_points),
        ("Target SRID", run.target_srid),
        ("Include proxy locations", run.include_proxy_locations),
        ("Case count", run.case_count),
        ("Proxy case count", run.proxy_case_count),
        ("Cluster count", run.cluster_count),
        ("Status", run.status.value),
    )
    rows = "".join(
        f"<tr><th style='text-align:left;padding-right:8px'>{escape(label)}</th>"
        f"<td>{escape(_display(value))}</td></tr>" for label, value in fields
    )
    return (
        "<div id='run-metadata' style='position:fixed;top:10px;right:10px;z-index:9999;"
        "background:white;padding:10px;border:1px solid #999;border-radius:4px;"
        "font:12px sans-serif;max-width:390px'><strong>Clustering run "
        f"{run.run_id}</strong><table>{rows}</table></div>"
    )


def _point_html(result: ClusteringResult) -> str:
    cluster = "Noise" if result.cluster_id is None else str(result.cluster_id)
    fields = (
        ("Patient", result.patient_id),
        ("Illness", result.illness_snapshot),
        ("Diagnosis date", result.diagnosis_date_snapshot),
        ("Cluster", cluster),
        ("Location source", result.location_source_snapshot.value),
        ("Is proxy", result.is_proxy_snapshot),
    )
    return "<br>".join(
        f"<strong>{escape(label)}:</strong> {escape(_display(value))}" for label, value in fields
    )


def generate_run_map(
    run_id: int,
    output_path: str | Path,
    *,
    engine: Engine | None = None,
) -> Path:
    """Render one completed persisted run to a standalone HTML file.

    This function only selects snapshot rows; it neither invokes clustering nor
    changes analytical database state.
    """
    owns_engine = engine is None
    engine = engine or create_db_engine()
    destination = Path(output_path)
    try:
        with Session(engine) as session:
            run = session.get(ClusteringRun, run_id)
            if run is None:
                raise ClusteringRunNotFoundError(f"Clustering run {run_id} does not exist")
            if run.status != ClusteringRunStatus.COMPLETED:
                raise ClusteringRunNotCompletedError(
                    f"Clustering run {run_id} has status {run.status.value}; expected COMPLETED"
                )

            rows = session.execute(
                select(
                    ClusteringResult,
                    func.ST_Y(ClusteringResult.location_snapshot).label("latitude"),
                    func.ST_X(ClusteringResult.location_snapshot).label("longitude"),
                )
                .where(ClusteringResult.run_id == run_id)
                .order_by(ClusteringResult.patient_id)
            ).all()

            center = [0.0, 0.0]
            if rows:
                center = [
                    sum(float(row.latitude) for row in rows) / len(rows),
                    sum(float(row.longitude) for row in rows) / len(rows),
                ]
            map_ = folium.Map(location=center, zoom_start=2, control_scale=True)
            map_.get_root().html.add_child(folium.Element(_metadata_html(run)))

            bounds: list[list[float]] = []
            for row in rows:
                result = row.ClusteringResult
                location = [float(row.latitude), float(row.longitude)]
                bounds.append(location)
                content = _point_html(result)
                color = cluster_color(result.cluster_id)
                tooltip = f"{result.patient_id} — " + (
                    "Noise" if result.cluster_id is None else f"Cluster {result.cluster_id}"
                )
                if result.is_proxy_snapshot:
                    icon = folium.DivIcon(
                        class_name="proxy-marker",
                        html=(f"<div title='FACILITY_PROXY' style='color:{color};font-size:24px;"
                              "line-height:20px;text-shadow:0 0 2px white'>▲</div>"),
                    )
                    folium.Marker(location, icon=icon, tooltip=tooltip,
                                  popup=folium.Popup(content, max_width=350)).add_to(map_)
                else:
                    folium.CircleMarker(
                        location, radius=7, color=color, fill=True, fill_color=color,
                        fill_opacity=0.82, weight=2, tooltip=tooltip,
                        popup=folium.Popup(content, max_width=350),
                    ).add_to(map_)

            if bounds:
                map_.fit_bounds(bounds, padding=(24, 24))

            destination.parent.mkdir(parents=True, exist_ok=True)
            map_.save(str(destination))
            return destination
    finally:
        if owns_engine:
            engine.dispose()
