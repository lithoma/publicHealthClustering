# Project Log

## Current Status (as of 2026-09-15)

Milestones 1 through 6 and Milestone 7A are complete. The project has:

- A local PostgreSQL/PostGIS environment and the full database schema (`sql/schema.sql`).
- A Python package with configuration loading, DB engine/session factories, and a health check.
- SQLAlchemy ORM models, a complete bootstrap schema, and Alembic migrations.
- A CSV parsing/validation pipeline for communities, healthcare facilities, and patients, with a synthetic sample dataset (`data/sample/`).
- A transactional database importer that upserts validated CSV records by natural identifier.
- A patient location resolver that populates `patient_locations` by exact community match, then facility proxy, then unresolved.
- Reproducible PostGIS DBSCAN clustering over current resolved patient locations, partitioned by illness and recorded as immutable run snapshots.
- A local interactive map, managed/versioned data batches, and China city navigation.
- Deterministic per-cluster descriptive reports with charts and standalone HTML export.

No geocoding or fuzzy matching of patient addresses exists yet — only exact
community-name matching and facility fallback. No real patient data has been
used anywhere — all sample and test data is synthetic.

## Next Milestone

Milestone 7B: versioned local prevention knowledge base.

---

## Log

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
