# Public Health Spatial Disease Clustering Platform

A local Python, FastAPI, PostgreSQL, and PostGIS application for importing
case batches, running reproducible disease-specific DBSCAN analyses, exploring
results on an interactive map, and generating deterministic cluster reports.

All repository samples and dashboard demos are synthetic. The application does
not yet geocode free-text addresses or generate AI prevention strategies.

## Current capabilities

- Strict CSV parsing, validation, preview, and transactional import
- Managed, independently included or excluded data batches
- Versioned case events separated from patient identity records
- Exact community matching and healthcare-facility proxy locations
- Persisted PostGIS `ST_ClusterDBSCAN` runs partitioned by disease
- China city navigation and appropriate projected SRID defaults
- Interactive cluster areas, case markers, noise, proxies, disease filters, and data table
- Per-cluster descriptive statistics, compact charts, and standalone HTML reports
- Alembic-managed schema evolution

## Prerequisites

- Python 3.12 or newer
- Docker with Docker Compose

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

## Start PostGIS and apply migrations

```bash
docker compose up -d db
docker compose ps
alembic upgrade head
python -m public_health_clustering.db
```

The health check prints the PostgreSQL and PostGIS versions. Alembic is the
normal upgrade path for an existing database. `sql/schema.sql` remains the
complete bootstrap schema used when a fresh Docker volume is first created.

## Start the dashboard

```bash
phc-dashboard
```

Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/). The dashboard provides:

- `聚集地图`: interactive map and disease filters
- `数据`: persisted case-level results for the selected run
- `Cluster 报告`: one descriptive report per cluster
- `导入数据`: CSV preview and confirmed batch import

If the database is empty, select Guangzhou and load its synthetic demo. The
demo creates 66 cases across three diseases and immediately runs clustering.

## Import data

The current patient CSV columns are:

```text
patient_id,name,illness,diagnosis_date,address_text,community_name,diagnosing_facility_code
```

Dates must use `YYYY-MM-DD`. The dashboard validates the complete file before
enabling confirmation. A confirmed file becomes a named batch that can be
included or excluded from later runs. A batch referenced by clustering results
cannot be deleted through the dashboard.

The current uploader does not accept coordinates and does not geocode addresses.
An imported patient becomes spatially eligible only when its community exactly
matches a known community or its diagnosing facility resolves to a known
facility proxy. Direct coordinate import and address geocoding are future work.

## Run clustering

Select the batches to analyze, then set:

- Analysis end date, which is excluded from the window
- Window length in days
- `eps_meters`, the DBSCAN neighbourhood distance
- `min_points`, the minimum DBSCAN neighbourhood size
- A projected, metre-based target SRID
- Whether facility proxy locations are eligible

Locations are stored as WGS84 longitude/latitude (`EPSG:4326`). DBSCAN transforms
them into the run's projected SRID so distances are measured in metres. The city
selector fills an appropriate UTM SRID, such as `32649` for Guangzhou. A run
persists its parameters, lifecycle status, cohort counts, batch selection, and
immutable result snapshots; viewing or reporting never reruns DBSCAN.

## Cluster reports

The `Cluster 报告` tab summarizes every non-noise cluster in a completed run:

- Case and proxy counts
- First/last diagnosis date and active-day span
- Daily case counts
- Location-source distribution
- WGS84 centroid
- Mean, median, and maximum distance to the centroid
- Coverage area and cases per square kilometre
- Run-wide noise count and the original analysis parameters

Coverage area is the union of `eps_meters` buffers around the cluster's cases.
It is a descriptive visualization footprint, not an administrative boundary or
a claimed transmission zone. Each full report is dependency-free HTML that can
be opened in a new tab, downloaded, or printed to PDF from the browser.

## Tests

Start the local PostGIS service, then run:

```bash
.venv/bin/pytest -q
```

The integration fixtures truncate the application's local tables before and
after tests. Do not point the test configuration at a database containing data
you need to retain. Reload the synthetic dashboard demo after a full test run.

## Project structure

```text
src/public_health_clustering/
  clustering.py          persisted PostGIS DBSCAN execution
  dashboard.py           FastAPI dashboard and local APIs
  ingestion/             CSV parsing, validation, and import
  location_resolution.py community/facility location resolution
  models.py              SQLAlchemy ORM models
  reporting.py           deterministic cluster statistics and HTML reports
  visualization.py       standalone Folium run maps
  web/dashboard.html     local dashboard interface
migrations/              Alembic migrations
sql/schema.sql            complete fresh-database bootstrap schema
tests/                    unit and live-PostGIS integration tests
```

## Current limitations and planned work

- No address geocoding or fuzzy location matching
- No direct longitude/latitude fields in the dashboard patient uploader
- No authentication or deployment hardening; the dashboard is local only
- No SaTScan, forecasting, prediction, or automatic intervention execution
- Local prevention knowledge base and evidence-grounded strategy agent are planned after reporting

See `ROADMAP.md`, `PROJECT_LOG.md`, and `ARCHITECTURE_DECISIONS.md` for milestone
status, validation history, and long-lived design decisions.
