# Architecture Decisions

Long-lived design decisions for this project. Implementation details belong
in the code and `PROJECT_LOG.md`, not here — keep this file short, and only
add an entry when a decision will constrain how future milestones are built.

## Schema is the single source of truth

`sql/schema.sql` defines all DDL. ORM models mirror it exactly and never
generate or alter schema (no `create_all`, no Alembic yet). Any schema
change starts in `sql/schema.sql`.

## ORM types match the schema's actual column types

Enum-like TEXT+CHECK columns (`location_source`, `clustering_runs.status`)
are modeled as Python `StrEnum` via a custom TEXT-backed `TypeDecorator`,
not `sqlalchemy.Enum` — this avoids introducing a VARCHAR length or a native
Postgres ENUM type that the schema doesn't actually have.

## Deletion defers to PostgreSQL

ORM relationships use `passive_deletes=True` and no delete-orphan cascades.
`ON DELETE CASCADE`/`RESTRICT`/`SET NULL` in the schema is the single
authority on what happens when a row is deleted — the ORM does not
duplicate that logic.

## Configuration and database connections are lazily constructed

`config.py`/`db.py` expose factory functions, not import-time singletons, so
alternate settings can be injected (e.g. in tests) and configuration errors
surface as handled errors rather than import-time crashes.

## CSV ingestion is a strict pipeline: parse → validate → import

Each stage has one job and produces structured results (`ValidationError`,
`ImportOutcome`, each with a stable code) rather than raw strings or ad hoc
exceptions, for anything that is an expected data-quality outcome rather
than a bug.

## Spatial clustering uses one explicit projection per run

DBSCAN never operates directly on EPSG:4326 coordinates. Every run requires
a caller-selected projected CRS whose linear unit is metres, validates it
against PostGIS `spatial_ref_sys`, and persists `target_srid` with the run.
One run covers one geographically coherent area for projection; the system
does not choose or apply per-point projections within a run.

## Clustering results are immutable run snapshots

Each result snapshots the analytical illness and diagnosis date as well as
the resolved location, source, and proxy flag. DBSCAN runs independently per
illness; remapped cluster numbers are meaningful only inside their run, so
the durable identity of a cluster is `(run_id, cluster_id)`. Noise has a NULL
cluster ID.

## Clustering owns its transaction boundary

`run_clustering()` creates and closes its own session instead of accepting a
caller's session. Its lifecycle commits therefore cannot commit unrelated
pending caller work. Run status is durably recorded as
`PENDING → RUNNING → COMPLETED/FAILED`, and computation failures roll back
all partial results before FAILED is recorded.

## External identifiers, not CSV convenience labels, are the source of truth

The importer keys records on the schema's real natural keys
(`community_name`, `facility_code`, `patient_id`), not on staging-only
labels. `community_code` exists only in the sample CSV for human
readability and has no database column.

## Database import policy: idempotent upsert, row-level transaction isolation

Re-importing unchanged data is a no-op; a changed record updates in place
rather than being rejected as a conflict. Each row is written inside its
own SAVEPOINT so one row's failure cannot abort an entire batch, and the
caller (not the importer) controls the outer commit.

## Patient-level records are the analytical core

Every downstream feature (locations, clustering, mapping) is derived from
individual `patients` rows. Aggregation happens in views/queries built on
top of patient-level data, not by pre-aggregating away the per-patient
record.

## Location resolution priority: community match, then facility proxy, then unresolved

A patient's location is chosen by exact `communities.community_name` match
first; only if that fails does the diagnosing facility's location serve as
a proxy; if neither is available, the patient is unresolved. No geocoding
or fuzzy address matching exists yet — resolution only ever chooses between
two already-known reference points.

## patient_locations is an append-only history with exactly one current row

Resolving a patient never overwrites a `patient_locations` row in place.
A change of any kind — a better/different source, a corrected reference
coordinate, or the patient becoming unresolved — deactivates the existing
`is_current` row and, where a new location applies, inserts a new one. The
partial unique index (one `is_current=TRUE` row per patient) is the
enforced invariant; application code reconciles toward it rather than
replacing it.

## Reference coordinate corrections version the location, they don't mutate it

If a community's or facility's own coordinates are corrected after a
patient was already resolved against it, that patient's current location
is superseded by a new row reflecting the correction — the original row is
kept, non-current, as a historical record of what was true when it was
recorded.

## Per-patient reconciliation is serialized with a row lock, not a table lock

Concurrent resolution attempts for the same patient acquire a `SELECT ...
FOR UPDATE` lock on that patient's row before reading or writing anything
for them. This exists to prevent ordinary concurrent contention from
surfacing as a spurious failure; the database's partial unique index
remains the actual final invariant regardless of application-level locking.

## No real patient data anywhere in this repository

All sample and test data is synthetic, in every milestone — not only the
initial schema and seed data.

## Cluster reports are deterministic views over immutable results

Descriptive reports read only `clustering_runs` and `clustering_results` and
never rerun DBSCAN or modify analytical rows. Counts, dates, centroids,
distances, areas, densities, noise, and provenance distributions are calculated
by PostGIS and application code, not by a language model. This deterministic
report becomes the factual input to any later strategy agent.

## Report coverage area is an analytical footprint, not a risk boundary

A cluster's report area is the projected union of `eps_meters` buffers around
its member cases. It is useful for consistent comparison and visualization but
must not be described as an administrative boundary, transmission boundary, or
predictive risk surface.

## The future strategy agent has no mandatory approval gate or run-history table

The planned strategy agent may return its cited recommendation directly to the
user without a required human-approval workflow. It will not introduce a
separate database table for agent-run history. Its factual inputs remain
persisted clustering runs, deterministic reports, and versioned knowledge-base
documents; it must not automatically execute public-health interventions.

## Demographics belong to case events and immutable analytical snapshots

Age and sex are optional case-level attributes because one subject can appear in
multiple imported event batches. Clustering copies them into
`clustering_results` so historical reports remain reproducible after source
records change. Reports always expose demographic completeness and present
counts/distributions only; they do not label a group as higher risk without an
appropriate population denominator.

## Cluster reports prioritize person, place, and time

Report structure follows field-epidemiology practice: time is represented by
the diagnosis curve, peak, and recent activity; place by spatial extent,
density, centroid, and location precision; person by age and sex distributions.
Operationally useful missingness is shown alongside every demographic summary.
