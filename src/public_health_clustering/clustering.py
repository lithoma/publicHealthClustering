"""Reproducible spatial clustering with PostGIS DBSCAN (Milestone 5).

Reads from `current_patient_spatial_points` (the canonical analytical view —
already-resolved, current patient locations only) and writes one
`clustering_runs` row plus one `clustering_results` row per analyzed patient.
Does not geocode, fuzzy-match, map, run SaTScan, or predict anything — it
only partitions already-resolved points by illness and clusters each
partition with PostGIS's `ST_ClusterDBSCAN`.

Temporal window
-----------------
The window is the explicit half-open interval [analysis_end - window_days,
analysis_end): a patient diagnosed exactly on `analysis_end - window_days`
is INCLUDED (inclusive lower bound); a patient diagnosed exactly on
`analysis_end` is EXCLUDED (exclusive upper bound). `analysis_end` and the
window boundaries are treated as calendar dates and compared directly
against `patients.diagnosis_date` (itself a DATE with no time component) —
deliberately avoiding any date-to-timestamp/timezone conversion for the
comparison itself. `clustering_runs.window_start`/`window_end`
(TIMESTAMPTZ columns) store these same two dates at UTC midnight, purely so
the schema's existing timestamp columns have an unambiguous value to hold —
that stored value is not what the patient filter compares against.

Distance, projection, and one-projection-per-run scope
----------------------------------------------------------
`patient_locations.location` (and therefore `current_patient_spatial_points
.location`) is `geometry(Point, 4326)` — geographic coordinates in degrees.
Running `ST_ClusterDBSCAN` directly on these would interpret `eps` in
degrees, which is not a meaningful distance unit. This module always
transforms to a caller-supplied projected coordinate system via
`ST_Transform(location, target_srid)` before clustering, so `eps_meters` is
interpreted in actual meters under that projection.

`target_srid` is a REQUIRED parameter with no default, and is persisted on
`clustering_runs.target_srid` — it materially changes what `eps_meters`
means, so a run without it recorded would not be reproducible. It is
validated (see below) but never guessed: which projected CRS is
appropriate is a property of the study region, not of this code. A single
run is scoped to a single, geographically coherent study area for which
one projected CRS is valid everywhere in that run's data — this module does
not attempt to detect or apply per-subregion projections within one run.
An analysis spanning multiple incompatible regions requires either a
projection valid across the whole area (e.g. a national equal-distance
projection) or separate runs, one per coherent region.

CRS validation
----------------
Before any clustering runs, `target_srid` is checked against PostGIS's own
`spatial_ref_sys` metadata (`srtext`, in WKT):

- unknown SRID -> rejected
- `srtext` starting with `GEOGCS` (a geographic/angular CRS, e.g. EPSG:4326)
  -> rejected, since eps_meters cannot mean anything in degrees
- a `PROJCS` whose outer (linear) `UNIT[...]` clause is not the metre
  (e.g. US survey foot state-plane systems) -> rejected

This is metadata-driven rather than a hardcoded allow-list of "known good"
SRIDs, so it generalizes to any projected, metre-based CRS already known to
PostGIS rather than only a small set this module happens to name.

Illness partitioning and run-local cluster identity
--------------------------------------------------------
`ST_ClusterDBSCAN(...) OVER (PARTITION BY illness ORDER BY patient_id)`
guarantees points with different illness values are never DBSCAN-neighbors
of each other, and that PostGIS's internal point-processing order is
deterministic (patient_id ascending) rather than incidental result-set
order — this matters because DBSCAN's handling of border points (points
reachable from more than one core point) can depend on visitation order,
and incidental ordering would make border-point cluster assignment,
raw cluster numbering, and this module's remapping (below) all
non-reproducible for identical inputs.

One further subtlety: PostGIS restarts cluster numbering at 0 *within each
partition*, so illness "Measles" cluster 0 and illness "Pertussis" cluster
0 are unrelated clusters that happen to share the same raw integer. This
module remaps (illness, raw_cluster_id) pairs to a single, run-local
integer (assigned in deterministic sorted order) before persisting.
cluster_id is therefore unique only *within a run* — its canonical
identity is the pair (run_id, cluster_id), not cluster_id alone. Comparing
cluster_id across two different runs is meaningless without also comparing
run_id.

Run lifecycle, session ownership, and reproducibility
-----------------------------------------------------------
`run_clustering` owns a dedicated SQLAlchemy session for its entire
lifecycle: it opens one internally (bound to `engine`, or to a freshly
created engine if none is given) and closes it when done. It never accepts
or commits an arbitrary caller-supplied session, so it can never commit
unrelated pending work the caller happens to have staged elsewhere, and a
caller's own session/transaction is never touched by this function at all.

Basic parameters (window_days, eps_meters, min_points) and target_srid are
validated *before* any clustering_runs row is created — a bad parameter is
a caller error, not a run failure, so it raises a plain ValueError with no
database row created at all.

Once a run row exists, its status is committed at each lifecycle
transition: PENDING (durable before any computation is attempted) ->
RUNNING -> COMPLETED or FAILED. If the RUNNING transition's own commit
fails, or the core computation raises, the session is rolled back (so no
partial clustering_results can survive) and the run is committed FAILED on
its own — then the original exception is re-raised, so callers get both a
durable audit record and a real error.

Every call to `run_clustering` creates a brand new `clustering_runs` row
and its own `clustering_results` rows, regardless of whether an identical
run already exists — this module does not deduplicate or overwrite prior
runs. "Idempotent" here means safe to call repeatedly without corrupting
anything, not "returns the same row twice." Each run is an independent,
reproducible snapshot: `clustering_results.patient_location_id`,
`location_snapshot`, `location_source_snapshot`, `is_proxy_snapshot`,
`illness_snapshot`, and `diagnosis_date_snapshot` are all copied at run
time and never updated afterward, so a run's recorded results stay
meaningful even if the referenced patient's current location, illness, or
diagnosis date later changes.
"""

from __future__ import annotations

import re
import json
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, timezone

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Engine,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    select,
)
from sqlalchemy.orm import Session

from .db import create_db_engine, create_session_factory
from .models import ClusteringResult, ClusteringRun, ClusteringRunStatus, StrEnumText, LocationSource

# current_patient_spatial_points and spatial_ref_sys are plain SQL
# tables/views (the former from sql/schema.sql, the latter shipped by
# PostGIS), neither ORM-mapped — declared here as lightweight Core Tables
# purely for building queries against them.
_reference_metadata = MetaData()

current_patient_spatial_points = Table(
    "current_patient_spatial_points",
    _reference_metadata,
    Column("patient_id", Text),
    Column("illness", Text),
    Column("diagnosis_date", Date),
    Column("sex", Text),
    Column("age_years", Integer),
    Column("case_event_id", BigInteger),
    Column("patient_location_id", BigInteger),
    Column("location", Geometry(geometry_type="POINT", srid=4326, spatial_index=False)),
    Column("location_source", StrEnumText(LocationSource)),
    Column("is_proxy", Boolean),
    Column("community_id", BigInteger),
    Column("facility_id", BigInteger),
    Column("resolved_at", DateTime(timezone=True)),
    Column("batch_id", BigInteger),
    Column("batch_is_included", Boolean),
)

spatial_ref_sys = Table(
    "spatial_ref_sys",
    _reference_metadata,
    Column("srid", Integer, primary_key=True),
    Column("srtext", Text),
)

_LINEAR_UNIT_PATTERN = re.compile(r'UNIT\["([^"]+)"\s*,\s*([0-9.eE+-]+)')
_METRE_NAMES = {"metre", "meter", "m"}


class ClusteringParameterError(ValueError):
    """Raised for invalid caller-supplied clustering parameters, before any
    clustering_runs row is created."""


def _validate_run_parameters(window_days: int, eps_meters: float, min_points: int) -> None:
    if window_days <= 0:
        raise ClusteringParameterError(f"window_days must be > 0, got {window_days}")
    if eps_meters <= 0:
        raise ClusteringParameterError(f"eps_meters must be > 0, got {eps_meters}")
    if min_points <= 0:
        raise ClusteringParameterError(f"min_points must be > 0, got {min_points}")


def _validate_target_srid(session: Session, target_srid: int) -> None:
    """Reject target_srid unless spatial_ref_sys metadata shows it is a
    projected (not geographic) CRS whose linear unit is the metre."""
    srtext = session.execute(
        select(spatial_ref_sys.c.srtext).where(spatial_ref_sys.c.srid == target_srid)
    ).scalar_one_or_none()

    if srtext is None:
        raise ClusteringParameterError(
            f"target_srid {target_srid} was not found in spatial_ref_sys — "
            "it must be a known, valid coordinate system SRID."
        )

    if srtext.startswith("GEOGCS"):
        raise ClusteringParameterError(
            f"target_srid {target_srid} is a geographic coordinate system (degrees), "
            "not a projected one. eps_meters cannot be interpreted in degrees — choose "
            "a projected, metre-based CRS appropriate for the study region."
        )

    if not srtext.startswith("PROJCS"):
        raise ClusteringParameterError(
            f"target_srid {target_srid} is neither a projected (PROJCS) nor geographic "
            "(GEOGCS) coordinate system per spatial_ref_sys — it cannot be validated as "
            "a metre-based projected CRS."
        )

    unit_matches = _LINEAR_UNIT_PATTERN.findall(srtext)
    if not unit_matches:
        raise ClusteringParameterError(
            f"target_srid {target_srid}: could not determine its linear unit from "
            "spatial_ref_sys metadata."
        )

    # The outer (linear) UNIT clause is the last one in a PROJCS's WKT — the
    # first is always the nested GEOGCS's angular unit (degrees).
    unit_name, unit_factor = unit_matches[-1]
    is_metric = unit_name.strip().lower() in _METRE_NAMES and abs(float(unit_factor) - 1.0) < 1e-9
    if not is_metric:
        raise ClusteringParameterError(
            f"target_srid {target_srid} uses linear unit {unit_name!r} "
            f"(factor to metres: {unit_factor}), not metres — eps_meters requires a "
            "metre-based projected CRS."
        )


def _mark_failed(session: Session, run: ClusteringRun) -> None:
    """Best-effort: leave the run in a truthful FAILED state. Call only
    after the session's current transaction has already been rolled back."""
    run.status = ClusteringRunStatus.FAILED
    run.completed_at = datetime.now(timezone.utc)
    session.commit()


def run_clustering(
    *,
    analysis_end: date,
    target_srid: int,
    eps_meters: float,
    min_points: int,
    window_days: int = 7,
    illness_filter: str | None = None,
    include_proxy_locations: bool = True,
    batch_ids: Sequence[int] | None = None,
    engine: Engine | None = None,
) -> ClusteringRun:
    """Run one reproducible DBSCAN clustering pass and record it.

    Owns its own session for the entire run (see module docstring) — pass
    `engine` to reuse an existing Engine/connection pool; otherwise one is
    created and disposed of internally. Always creates a new clustering_runs
    row (see module docstring on idempotency/history).
    """
    _validate_run_parameters(window_days, eps_meters, min_points)

    window_start_date = analysis_end - timedelta(days=window_days)
    window_end_date = analysis_end
    window_start_dt = datetime.combine(window_start_date, time.min, tzinfo=timezone.utc)
    window_end_dt = datetime.combine(window_end_date, time.min, tzinfo=timezone.utc)

    owns_engine = engine is None
    engine = engine or create_db_engine()
    session_factory = create_session_factory(engine)
    # The returned run is detached after this private session closes. Keep its
    # committed attributes readable by callers.
    session = session_factory(expire_on_commit=False)

    try:
        _validate_target_srid(session, target_srid)

        run = ClusteringRun(
            window_start=window_start_dt,
            window_end=window_end_dt,
            window_days=window_days,
            eps_meters=eps_meters,
            min_points=min_points,
            target_srid=target_srid,
            illness_filter=illness_filter,
            include_proxy_locations=include_proxy_locations,
            included_batch_ids=json.dumps(list(batch_ids)) if batch_ids is not None else None,
            status=ClusteringRunStatus.PENDING,
        )
        session.add(run)
        session.commit()  # PENDING is durable before any computation is attempted

        run.status = ClusteringRunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc)
        try:
            session.commit()
        except Exception as original_error:
            session.rollback()
            try:
                _mark_failed(session, run)
            except Exception as status_error:
                session.rollback()
                original_error.add_note(
                    f"Additionally failed to persist FAILED status: {status_error!r}"
                )
            raise

        try:
            cpsp = current_patient_spatial_points
            filters = [
                cpsp.c.diagnosis_date >= window_start_date,
                cpsp.c.diagnosis_date < window_end_date,
                cpsp.c.batch_is_included.is_(True),
            ]
            if batch_ids is not None:
                filters.append(cpsp.c.batch_id.in_(list(batch_ids)))
            if illness_filter is not None:
                filters.append(cpsp.c.illness == illness_filter)
            if not include_proxy_locations:
                filters.append(cpsp.c.is_proxy.is_(False))

            transformed_location = func.ST_Transform(cpsp.c.location, target_srid)
            raw_cluster_id = func.ST_ClusterDBSCAN(
                transformed_location, eps_meters, min_points
            ).over(
                partition_by=cpsp.c.illness,
                order_by=(cpsp.c.patient_id, cpsp.c.case_event_id),
            )

            query = select(
                cpsp.c.patient_id,
                cpsp.c.illness,
                cpsp.c.diagnosis_date,
                cpsp.c.sex,
                cpsp.c.age_years,
                cpsp.c.case_event_id,
                cpsp.c.patient_location_id,
                cpsp.c.location,
                cpsp.c.location_source,
                cpsp.c.is_proxy,
                raw_cluster_id.label("raw_cluster_id"),
            ).where(*filters)

            rows = session.execute(query).all()

            # Remap (illness, raw_cluster_id) -> a single run-local integer,
            # in a deterministic order, so cluster_id never collides across
            # illnesses despite PostGIS restarting numbering per partition.
            distinct_pairs = sorted(
                {
                    (row.illness, row.raw_cluster_id)
                    for row in rows
                    if row.raw_cluster_id is not None
                }
            )
            global_cluster_id_by_pair = {
                pair: idx for idx, pair in enumerate(distinct_pairs, start=1)
            }

            for row in rows:
                cluster_id = (
                    global_cluster_id_by_pair[(row.illness, row.raw_cluster_id)]
                    if row.raw_cluster_id is not None
                    else None
                )
                session.add(
                    ClusteringResult(
                        run_id=run.run_id,
                        patient_id=row.patient_id,
                        patient_location_id=row.patient_location_id,
                        case_event_id=row.case_event_id,
                        cluster_id=cluster_id,
                        illness_snapshot=row.illness,
                        diagnosis_date_snapshot=row.diagnosis_date,
                        sex_snapshot=row.sex,
                        age_years_snapshot=row.age_years,
                        location_snapshot=row.location,
                        location_source_snapshot=row.location_source,
                        is_proxy_snapshot=row.is_proxy,
                    )
                )

            run.case_count = len(rows)
            run.proxy_case_count = sum(1 for row in rows if row.is_proxy)
            run.cluster_count = len(distinct_pairs)
            run.status = ClusteringRunStatus.COMPLETED
            run.completed_at = datetime.now(timezone.utc)
            session.commit()
        except Exception as original_error:
            session.rollback()
            try:
                _mark_failed(session, run)
            except Exception as status_error:
                session.rollback()
                original_error.add_note(
                    f"Additionally failed to persist FAILED status: {status_error!r}"
                )
            raise

        return run
    finally:
        session.close()
        if owns_engine:
            engine.dispose()
