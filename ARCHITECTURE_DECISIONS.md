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
## Disease knowledge is local, Chinese, versioned, and source-traceable

Operational summaries are stored as Chinese document and section records rather
than embedded in prompts or UI code. Every document retains disease,
jurisdiction, issuing body, publication date, version, lifecycle status, and an
official source URL. Updates create or mark versions rather than silently
overwriting provenance. This makes later agent retrieval inspectable without a
separate agent-run history table.

## Chinese knowledge search uses deterministic substring matching initially

The first retrieval layer searches disease, title, issuer, section title,
content, and keywords with PostgreSQL case-insensitive substring matching.
Default PostgreSQL full-text tokenization is not used because it does not offer
reliable Chinese segmentation without an extension. The small curated corpus
does not yet justify an external search service or embedding index; either can
be added later behind the same service boundary.

## Knowledge content supports decisions but does not become clinical authority

Initial entries are concise Chinese summaries linked to current official source
documents. They are not copied wholesale and do not replace the official text,
clinical judgment, statutory reporting duties, or local response procedures.
The UI exposes the source link and status so users can verify currency.

## Strategy generation uses a deterministic evidence contract

The strategy workflow first constructs a typed context from one immutable
cluster report and current disease-matched knowledge sections. Its baseline
context is deterministic, while assessment, prioritized actions, limitations,
escalation criteria, and reasoning are produced by the required model call.
This stable contract makes model outputs testable and keeps factual calculation
outside the model; it is not a user-facing rules-based fallback.

## Strategy context excludes direct identifiers and exact coordinates

Only aggregate report measures and curated knowledge sections enter strategy
generation. Patient IDs, names, addresses, and exact case coordinates are not
included. No separate strategy-run table is created, and generating a brief
does not alter analytical or knowledge rows.

## Every strategy action must resolve to retrieved evidence

Actions carry one or more section-level evidence IDs. The response includes the
matching official document title, version, section, content summary, and source
URL. Missing matching knowledge is a clear failure rather than permission to
invent uncited disease guidance.
## The strategy endpoint requires a real model and has no mechanical fallback

Milestone 7D replaces baseline template generation with an OpenRouter model
call. If the key, privacy-compatible provider, structured response, or citations
are invalid, the request fails visibly. A generic template must not be presented
as agent analysis. The earlier deterministic evidence contract remains useful
as the input/output boundary, not as a user-facing strategy fallback.

## Agent context is rich at aggregate level and coarse at location level

The model receives cluster person–place–time statistics, season, run parameters,
data completeness, approximate configured city, current knowledge sections, and
a cautious prior-run comparison. Exact centroids and all patient-level records
are removed. Prior clusters are proximity hints only and are explicitly not
treated as proven continuation of a transmission chain.

## Free provider routing is constrained by privacy and schema requirements

The prototype uses `openrouter/free` with strict JSON Schema, required parameter
support, and denied provider data collection. Strict ZDR was tested but the free
pool had no compatible endpoint, so only de-identified aggregate context is sent
and provider retention remains a documented prototype limitation. Model identity is returned
to the UI because the free router may select different compatible models.
## Prediction is isolated and persisted because forecasts must be audited later

Prediction runs and grid results live in dedicated tables and never overwrite
cases, clustering runs, reports, knowledge, or strategies. Each result stores
its model/version, cutoff, horizon, feature parameters, feature snapshot,
uncertainty bounds, runtime, and validation status. This allows future observed
cases to be compared with exactly what was forecast at issuance time.

## The first forecasting model is shared, training-free, and unvalidated

All diseases use the same versioned calculation while remaining separate output
groups. The baseline combines recent 7-day incidence, the preceding week, older
14-day activity, bounded trend, and neighbouring-cell activity. Its weights are
explicit engineering assumptions. Synthetic scenarios test behavior only; they
must not be presented as evidence of real predictive accuracy.

## Learned models must implement the same forecast contract and beat baselines

Future negative-binomial and boosted-tree models consume the same feature
surface and emit the same persisted grid forecast shape. LightGBM/XGBoost are
challengers, not assumed public-health standards. Promotion requires rolling
time-based backtesting, uncertainty calibration, and performance reporting by
disease and geography; random train/test splits are not acceptable.

## Prediction input cohorts are immutable audit data

A forecast now snapshots every included batch ID and name plus its proxy-location
policy. Historical evaluation reuses that frozen cohort even if a batch is later
excluded, and batch deletion is blocked while prediction history references it.
This closes the gap between persisting model output and being able to explain
exactly which records were eligible when that output was produced.

## Forecast evaluation is rolling-origin, outcome-based, and immutable

The seven-day target is the half-open interval `[forecast_start, forecast_end)`.
An evaluation is allowed only once that interval has closed, attaches observed
counts to forecast cells, separately counts outcomes in cells the model did not
forecast, and cannot later be overwritten with a different data-completeness
date. Rolling historical cutoffs must use only information available before each
cutoff. Random train/test splits remain prohibited because they leak future time
structure into model development.

The initial score set includes point accuracy, interval quality, and spatial
prioritization: MAE, RMSE, mean interval score, interval coverage, top-k hotspot
recall, and observed cases outside forecast cells. No score changes the model's
`SIMULATION_UNVALIDATED` status; promotion requires representative real data and
explicit comparison against simple baselines.

## SaTScan will complement, not replace, clustering or prediction

DBSCAN remains a descriptive density partition, prediction estimates future
counts, and a future prospective SaTScan adapter will assess whether space-time
concentrations are statistically unusual. SaTScan results must use separate run
and alarm tables and appear as an optional map layer rather than rewriting
DBSCAN clusters or forecast cells.
