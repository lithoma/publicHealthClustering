# Project Log

## Current Status (as of 2026-09-15)

Milestones 1 through 7D are complete. The project has:

- A local PostgreSQL/PostGIS environment and the full database schema (`sql/schema.sql`).
- A Python package with configuration loading, DB engine/session factories, and a health check.
- SQLAlchemy ORM models, a complete bootstrap schema, and Alembic migrations.
- A CSV parsing/validation pipeline for communities, healthcare facilities, and patients, with a synthetic sample dataset (`data/sample/`).
- A transactional database importer that upserts validated CSV records by natural identifier.
- A patient location resolver that populates `patient_locations` by exact community match, then facility proxy, then unresolved.
- Reproducible PostGIS DBSCAN clustering over current resolved patient locations, partitioned by illness and recorded as immutable run snapshots.
- A local interactive map, managed/versioned data batches, and China city navigation.
- Deterministic per-cluster descriptive reports with charts and standalone HTML export.
- A versioned Chinese disease knowledge base with official source links.
- A model-driven Chinese prevention-strategy agent using de-identified aggregate context.

No geocoding or fuzzy matching of patient addresses exists yet — only exact
community-name matching and facility fallback. No real patient data has been
used anywhere — all sample and test data is synthetic.

## Next Milestone

Milestone 7E: expand the knowledge corpus and evaluate agent severity
calibration, citation support, consistency, safety, and expert usefulness.

---

## Log

### 2026-09-15 — Milestone 7A extension: Person–place–time reporting

**Implemented:** optional `sex` and `age_years` fields are accepted during CSV
import, normalized/validated, stored on patients and case events, exposed by the
canonical spatial view, and copied into immutable clustering result snapshots.
Reports now include age bands, median/range, sex distribution, demographic
completeness, peak date, and cases in the final three analysis days alongside
the existing spatial and location-quality measures. Dashboard case rows and
point popups expose available demographics. Clicking a cluster region opens a
direct link to its report. The Guangzhou demo populates both demographic fields
deterministically.

**Architecture:** Migration `20260915_02` adds demographics without making them
required, preserving old CSVs and legacy rows. Reports organize evidence by
person, place, and time and do not interpret case proportions as population
rates without denominators.

**Validation:** Optional-field normalization/range coverage was added to CSV
tests; reporting tests cover demographic distributions, completeness, and
person–place–time HTML sections. Full live-PostGIS suite: 90/90 passing, with
the same pre-existing Starlette `TestClient` deprecation warning. The local
database was migrated to `20260915_02`; the 66-case Guangzhou demo was restored
and all three reports were verified with complete age and sex data.

**Next:** Milestone 7B remains the versioned local prevention knowledge base.

### 2026-09-15 — Milestone 7A: Cluster descriptive reports

**Implemented:** `reporting.py` calculates one read-only report for each
non-noise cluster in a completed persisted run. Metrics include case/proxy
counts, proxy share, first/last diagnosis, active days, daily counts,
location-source distribution, projected centroid distances, WGS84 centroid,
buffer-union coverage area, density, and run-wide noise. The dashboard adds a
`Cluster 报告` view with compact charts and links to dependency-free standalone
HTML reports that can be viewed, downloaded, or printed. Reports never rerun
DBSCAN and never modify analytical rows.

**Validation:** 4 focused live-PostGIS reporting tests cover statistics and
noise, JSON and standalone HTML endpoints, empty completed runs, downloads,
missing runs/clusters, and non-completed runs. Full suite: 89/89 passing. The
only warning is the pre-existing Starlette `TestClient`/`httpx` deprecation.
The Guangzhou synthetic demo was restored afterward and verified with 66 cases,
3 clusters, and successful report generation.

**Limitations:** Reports contain only fields present in immutable clustering
snapshots. Coverage is an epsilon-buffer footprint, not a transmission or
administrative boundary. HTML is the only direct export format; browser print
can produce PDF.

**Next:** Milestone 7B, a versioned local prevention knowledge base with
source/page citations and disease/jurisdiction/effective-date filtering.

### 2026-09-15 — Milestone 6 extensions: Dashboard and managed data

**Implemented:** standalone Folium visualization, a local FastAPI/Leaflet
dashboard, warm disease-level styling, translucent cluster regions, explicit
noise/proxy markers, city navigation, time/disease filtering, managed batches,
safe deletion, upload preview/confirmation, versioned case events, and Alembic.

**Validation:** The live-PostGIS suite reached 85 passing tests before
Milestone 7A. Dashboard behavior was exercised with a deterministic 66-case
Guangzhou synthetic demo.

### 2026-09-13 — Milestone 5: Spatial clustering

**Implemented:** `clustering.py` — `run_clustering()` reads the canonical `current_patient_spatial_points` view and runs PostGIS `ST_ClusterDBSCAN` independently per illness over the half-open diagnosis window `[analysis_end - window_days, analysis_end)`. Input order is deterministic (`patient_id`), and illness-scoped raw cluster numbers are remapped to run-local unique integers; cluster identity is always `(run_id, cluster_id)`, while noise remains `cluster_id = NULL`. Each run requires and persists a caller-selected projected, metre-based `target_srid`; unknown, geographic, and non-metre CRSs are rejected before a run row is created. One projection applies to the entire run.

**Reproducibility and lifecycle:** Results snapshot the patient location and its source/proxy status plus the analytical illness and diagnosis date. `run_clustering()` owns a private session, so lifecycle commits cannot commit unrelated caller work. Run states progress durably through `PENDING → RUNNING → COMPLETED/FAILED`; failures roll back partial results and preserve a FAILED audit row where the run was successfully created.

**Validation:** 17 Milestone 5 integration tests against the live PostGIS container — temporal boundaries, illness partitioning and deterministic remapping, proxy policy, cluster/noise behavior, empty cohorts, counts, parameter and CRS rejection before run creation, caller-session isolation, execution and RUNNING-transition failures, immutable snapshots, and independent repeated runs. Full suite: 76/76 passing.

### 2026-09-13 — Milestone 4: Patient location resolution

**Implemented:** `location_resolution.py` — `resolve_patient_locations()`. Resolution priority: exact `patients.community_name == communities.community_name` match, else the diagnosing facility as a proxy, else unresolved. Reconciliation (not blind re-insertion) drives every write: a patient becoming unresolved deactivates its stale current row instead of leaving it stranded; a reference whose own coordinates are corrected (same community/facility ID, different point) supersedes the current row with a new one rather than rewriting history in place. Per-patient reconciliation is serialized with a `SELECT ... FOR UPDATE` row lock (not a table lock) so concurrent runs for the same patient don't race into a spurious failure. Batch results are returned in deterministic order, report unknown requested patient IDs instead of dropping them, and de-duplicate repeated ID requests.

**Validation:** 18 integration tests against the live PostGIS container, including a real multi-threaded concurrency test — exact match, facility fallback, community-over-facility priority, unresolved (both "never resolved" and "became unresolved" cases), idempotent reruns, proxy-superseded-by-community, community/facility coordinate correction, at-most-one-current-row invariant, row-level transaction rollback, and batch ordering/dedup/unknown-ID handling. Full suite: 59/59 passing.

### 2026-09-11 — Milestone 3.2: Database import

**Implemented:** `ingestion/importer.py` — `import_communities`/`import_facilities`/`import_patients`/`import_dataset`. Upsert-in-place policy keyed by natural identifiers (`community_name`, `facility_code`, `patient_id`); per-row SAVEPOINT transaction isolation; `diagnosing_facility_code` resolved to `diagnosing_facility_id` at import time (an unresolvable code is a row-level failure, not silently set to NULL).

**Validation:** 11 new integration tests against the live PostGIS container — sample-data insert, repeat-import no-op, field-change update, fault-injected row-level rollback, FK resolution (happy and unhappy path), empty input, and post-failure consistency — each run inside a rolled-back outer transaction so nothing persists. Full suite: 41/41 passing.

### 2026-09-10 — Milestone 3.1: CSV parsing and validation

**Implemented:** `ingestion/parsing.py` (structural parsing, preserves raw text, detects short/long rows and duplicate headers as errors instead of coercing them), `ingestion/validation.py` (field/range/date/duplicate checks, `ValidationErrorCode` enum), synthetic sample dataset (`data/sample/`). Identifier normalization (trim) applied consistently to `patient_id`/`facility_code`/`community_code`; `diagnosing_facility_code` validated for presence only, not cross-checked against facilities.

**Validation:** 30 unit tests covering valid sample data plus structural/content edge cases (malformed rows, duplicate headers, NaN/±Infinity coordinates, exact boundary coordinates, date-shape rejection, deterministic error ordering, no patient data echoed in error messages).

### 2026-09-10 — Milestone 2.3: SQLAlchemy ORM models

**Implemented:** `models.py` — typed declarative models for all six schema tables, including GeoAlchemy2 geometry columns. Deletion semantics deferred to PostgreSQL (`passive_deletes=True`, no ORM delete-orphan cascades); TEXT+CHECK enum columns modeled via a custom `StrEnumText` TypeDecorator instead of `sqlalchemy.Enum`; server defaults aligned with the schema's `CURRENT_TIMESTAMP`/literal defaults.

**Validation:** `configure_mappers()` clean with warnings promoted to errors; live read-only `SELECT` against all six tables; ORM metadata cross-checked column-by-column against `sql/schema.sql`.

### 2026-09-10 — Milestone 2.1–2.2: Configuration and database connection

**Implemented:** `config.py` (env/`.env`-based settings, lazily constructed, deterministic `.env` file resolution, password excluded from `repr()`), `db.py` (engine/session-factory functions, health check). `python -m public_health_clustering.db` prints the PostgreSQL/PostGIS versions and exits cleanly.

**Validation:** health check against the live container; invalid port and invalid `POSTGRES_PORT` both fail with a handled error, no traceback; credentials containing special characters round-trip correctly via `URL.create`.

### 2026-07-10 — Milestone 1: Project foundation

**Implemented:** Python package scaffold, Docker Compose PostgreSQL/PostGIS service, initial database schema (`sql/schema.sql` — six tables plus the `current_patient_spatial_points` view).

**Validation:** schema applied cleanly to a fresh container volume; `docker compose ps` reported healthy.
### 2026-09-15 — Milestone 7B: 中文疾病防控知识库

**Implemented:** All generated cluster-report headings, labels, demographic
categories, location-source categories, explanatory notes, and dashboard report
language are now Chinese. Added versioned `knowledge_documents` and
`knowledge_sections` tables, ORM models, migration `20260915_03`, an idempotent
curated seed, Chinese substring search, disease/status filtering, JSON APIs, and
a dashboard `防控知识库` view. Initial records cover 麻疹、百日咳、流感 and retain
the issuing body, publication date, version, status, and official source URL.

**Architecture:** Knowledge summaries are structured into searchable Chinese
sections and remain traceable to official documents. The database preserves
document versions and lifecycle status. PostgreSQL substring search is used
instead of built-in full-text search because its default tokenization does not
provide reliable Chinese word segmentation. This milestone retrieves evidence
only and does not generate strategies.

**Validation:** 3 focused knowledge-base tests cover idempotent seeding, Chinese
content, section search, disease filtering, API output, and missing documents;
updated reporting coverage verifies Chinese standalone HTML. Full live-PostGIS
suite: 93/93 passing, with one existing Starlette `TestClient` deprecation
warning. The database is at `20260915_03`; the 66-case Guangzhou demo, 3
clusters, and 3 knowledge documents were restored after testing.

**Limitations:** Initial summaries are curated starting content, not a complete
catalog of national or local guidance. Effective-date fields are available but
require source-specific population. Search is substring based and does not yet
rank results semantically or cite page numbers inside PDFs.

**Next:** Milestone 7C can retrieve these versioned sections alongside a
deterministic cluster report to generate a cited Chinese prevention strategy.
### 2026-09-15 — Milestone 7C: Evidence-grounded prevention strategy workflow

**Implemented:** Added a read-only strategy service that combines one persisted
cluster report with matching current Chinese knowledge sections. The dashboard
now offers `生成防控策略` on every cluster and renders a Chinese assessment,
time-ordered priorities, data-quality cautions, escalation conditions, and
clickable evidence references. It sends no patient identifiers or exact
coordinates, writes no agent-run history, requires no approval gate, and never
executes an intervention.

**Architecture:** The first production-safe generator is deterministic and
works without a cloud API key. This separates evidence retrieval and safety
constraints from future model wording/refinement. Each recommendation carries
explicit knowledge-section IDs resolved to source title, version, and official
URL. Aggregate report facts and retrieved evidence are the only inputs.

**Validation:** 3 focused strategy tests cover aggregate-only context,
source-backed citations, missing-knowledge failure, and API output. Strategy,
reporting, and knowledge focused suite: 10/10 passing. Full live-PostGIS suite:
96/96 passing, with one existing Starlette `TestClient` deprecation warning.
The Guangzhou demo was restored with 66 cases and 3 clusters; all three cluster
strategies returned successfully with 3 evidence sections each.

**Limitations:** The current generator is rules-based rather than model-refined
because no OpenAI API key/model is configured locally. It does not infer causal
links, optimize resources, incorporate population denominators, or replace
official/local professional judgment. Model refinement can be added later
behind the same structured strategy contract using stateless API requests.

**Next:** Add optional model refinement and evaluation only after configuring a
provider credential/model, then expand the local corpus and page-level citation
precision.
### 2026-09-15 — Milestone 7D: Actual model strategy agent

**Implemented:** Replaced the deterministic strategy writer with a required
OpenRouter model call. The context builder now supplies aggregate cluster
statistics, analysis parameters, calendar month/season, approximate configured
city, demographic and location-quality completeness, current disease-matched
knowledge sections, and the nearest comparable same-disease cluster from a
prior non-overlapping run when available. Exact coordinates and all direct
patient identifiers are removed before transmission. The dashboard exposes
model assessment level, reasoning summary, prioritized actions and rationales,
missing information, data-quality warnings, escalation criteria, and citations.

**Architecture:** The initial provider is OpenRouter's `openrouter/free` router.
Requests demand strict JSON Schema, compatible-provider routing, and denied data
collection. Strict ZDR was removed after a live free-router request found no
compatible endpoint; the transmitted context is therefore limited to de-identified
aggregates. There is deliberately no rules-based
fallback: missing credentials, provider failure, malformed output, or invented
citation IDs fail clearly instead of displaying a mechanical strategy.

**Validation:** 4 focused strategy tests use a simulated provider and cover the
aggregate-only payload, Guangzhou/秋季 context, strict model response handling,
citation resolution, missing knowledge, API behavior, and missing-key refusal.
Combined focused suite: 11/11 passing. Full live-PostGIS suite: 97/97 passing,
with the existing Starlette `TestClient` warning. After a replacement key was
configured locally, a live Guangzhou 麻疹 request succeeded through
`dots-studio/dots-3-note-preview:free`, returning all required structured fields
and three valid evidence references. The live check also found and fixed a
non-ASCII HTTP-header issue and established that the free pool currently has no
endpoint satisfying both strict ZDR and structured output. Guangzhou demo and
all 3 knowledge documents were restored after the final test run.

**Limitations:** Free routing is intended for prototyping, has low rate limits,
variable capacity/model selection, and may have no endpoint satisfying strict
privacy plus structured-output requirements at a given moment. A replacement
key must be stored only in the ignored local `.env` file.

**Next:** Revoke the exposed key, configure a replacement, run one live strategy
evaluation, then strengthen knowledge coverage and formal output evaluations.
### 2026-09-15 — Milestone 8A: Shared seven-day spatial baseline

**Implemented:** Added an isolated prediction subsystem that forecasts the next
7 days by disease and configurable projected grid cell. It uses one shared,
training-free model contract for every disease with fixed versioned features:
case counts in days 1–7, 8–14, and 15–28; bounded week-over-week trend;
neighbouring-cell recent activity; and conservative uncertainty bounds.
Prediction runs, feature snapshots, parameters, grid polygons, expected counts,
intervals, runtime, and validation status are persisted separately from
clustering. The dashboard adds an optional `七日预测` tab and choropleth map.

**Architecture:** `SHARED_SPATIOTEMPORAL_BASELINE` version 1 is deliberately
uncalibrated and requires no training. Disease is a grouping dimension, not a
separate fitted model. The prediction storage/API contract is model-neutral so
negative-binomial, LightGBM, or XGBoost challengers can be added without changing
the interface. Predictions never modify source cases, clusters, reports,
knowledge, or agent output.

**Validation:** 3 focused live-PostGIS tests cover reproducible persisted grid
forecasts, temporal features and bounds, shared cross-disease behavior, GeoJSON,
and empty history. Full suite: 100/100 passing, with the existing Starlette
`TestClient` warning. Database migrated to `20260915_04`. Guangzhou demo and 3
knowledge documents were restored; prediction run #1 contains 12 disease-grid
cells and is labelled `SIMULATION_UNVALIDATED`.

**Limitations:** The initial weights and uncertainty interval are engineering
assumptions, not calibrated epidemiological estimates. Only cells with cases in
the preceding 28 days are forecast. Synthetic data validates software behavior,
not real-world accuracy. Population denominators, reporting delays, vaccination,
weather, mobility, severity, and interventions are not yet modeled.

**Next:** With real history, use rolling-origin backtests to calibrate the
baseline and compare it with negative-binomial regression and boosted-tree
challengers. Promote no model until it beats simple baselines by disease,
geography, horizon, and uncertainty calibration.

### 2026-10-20 — Milestone 8B-1: Reproducible forecast evaluation

**Implemented:** Prediction runs now snapshot their exact included batches and
proxy-location policy, use the clustering subsystem's projected metre-based SRID
validation, and protect referenced batches from deletion. Added immutable
observed-outcome attachment for closed seven-day windows and leakage-safe rolling
historical backtests. Persisted evaluations report MAE, RMSE, mean interval score,
95% interval coverage, top-k hotspot recall, observed cases inside forecast cells,
and cases appearing in cells the baseline did not forecast. The prediction screen
now exposes a Chinese historical-evaluation panel without changing clustering,
reports, knowledge, or strategy behavior.

**Architecture:** Forecast and outcome intervals are explicitly half-open.
Changing current batch inclusion does not change the cohort used for a saved
evaluation. Evaluation records are immutable and do not promote the existing
`SIMULATION_UNVALIDATED` baseline. SaTScan remains a future complementary alarm
layer rather than a replacement for either DBSCAN or forecasting.

**Validation:** A fresh, isolated PostGIS 17 / PostGIS 3.5 Docker volume was used,
so the user's normal database was untouched. The migration from `20260915_04` to
`20261020_05` was applied and its new table/column verified. Focused prediction
suite: 8/8 passed. Full suite: 105/105 passed with the existing Starlette
`TestClient` deprecation warning. Live API and browser checks generated a forecast,
ran a two-cutoff backtest, rendered the evaluation UI, and demonstrated detection
of actual cases outside the forecasted grid cells.

**Limitations:** The model weights and interval formula remain uncalibrated. A
batch cohort is frozen, but later corrections to a case's current location before
first evaluation can still affect its attached outcome cell. The baseline still
forecasts only cells active during its 28-day history and does not yet correct
reporting delay or use population denominators.

**Next:** Add direct longitude/latitude import for real-data readiness, then use
representative real longitudinal data to compare naive baselines with a shared
negative-binomial count model. Add SaTScan after the evaluation/data contract is
stable; defer boosted trees until data volume and rolling backtests justify them.
