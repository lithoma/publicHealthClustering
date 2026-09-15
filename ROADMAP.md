# Roadmap

Milestone status as of 2026-09-15. A milestone is complete only after code,
tests, `PROJECT_LOG.md`, `ARCHITECTURE_DECISIONS.md`, this roadmap, and
`README.md` are updated and the result is committed to Git.

## Milestones 1–5 — Analytical foundation ✅

- Project, PostGIS, configuration, and ORM foundation
- Strict CSV ingestion and transactional import
- Patient location resolution with provenance and history
- Reproducible persisted PostGIS DBSCAN runs

## Milestone 6 — Dashboard, visualization, and managed data ✅

- Standalone Folium visualization of persisted runs
- Local FastAPI map dashboard and China city navigation
- Disease styling, cluster regions, noise, proxy markers, and data view
- Managed synthetic/real batches and safe include/exclude/delete behavior
- Alembic migrations and versioned case events

## Milestone 7A — Cluster descriptive reports ✅

- Deterministic per-cluster temporal, spatial, density, and provenance statistics
- Run-wide noise summary
- Dashboard report cards and compact charts
- Optional age/sex ingestion and immutable demographic snapshots
- Person–place–time summaries with completeness indicators
- Direct map-cluster links to the matching report
- Dependency-free standalone HTML report view/download
- Focused and full live-PostGIS test coverage

## Milestone 7B — Local prevention knowledge base ⬜

- Versioned local document ingestion and parsing
- Disease, jurisdiction, effective-date, and document-status metadata
- Search and retrieval with source/page citations
- No strategy generation in this milestone

## Milestone 7C — Prevention strategy agent ⬜

- Combine deterministic cluster reports with retrieved local guidance
- Produce structured, cited prevention recommendations automatically
- No mandatory human-approval gate
- No separate agent-run history table
- No automatic execution of external interventions

## Later work ⬜

- Building/street-level geocoding and coordinate import
- Disease-specific clustering parameter profiles
- Temporal cluster comparison and playback
- SaTScan and prediction only when explicitly scheduled

Do not begin a milestone until explicitly requested.
