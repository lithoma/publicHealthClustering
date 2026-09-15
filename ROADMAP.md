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

## Milestone 7B — Local prevention knowledge base ✅

- Versioned Chinese documents and section-level structured content
- Disease, jurisdiction, effective-date, issuing-body, and status metadata
- Chinese keyword search, disease filtering, API retrieval, and official source links
- Dashboard knowledge view for 麻疹、百日咳、流感
- Chinese standalone cluster reports and dashboard report language
- No strategy generation in this milestone

## Milestone 7C — Prevention strategy agent ✅

- Combine deterministic cluster reports with retrieved local guidance
- Produce structured, cited prevention recommendations automatically
- Chinese assessment, prioritized actions, limitations, and escalation criteria
- Aggregate-only context without patient identifiers or exact coordinates
- No mandatory human-approval gate
- No separate agent-run history table
- No automatic execution of external interventions

## Milestone 7D — Model-driven strategy agent ✅

- Required OpenRouter model call with no mechanical fallback
- Aggregate cluster, season, approximate city, prior-run, and knowledge context
- Strict structured Chinese output and citation-ID validation
- Denied-data-collection routing with de-identified aggregate payloads
- Local credential configuration required before live generation

## Milestone 7E — Knowledge expansion and agent evaluation ⬜

- Broader national, provincial, and municipal guidance corpus
- Page/paragraph-level citations and supersession workflows
- Unsupported-claim, consistency, safety, and expert-quality evaluations
- Severity calibration tests, including clustered-case escalation scenarios
- Cross-model comparison and minimum acceptable response-quality thresholds
- Resource capacity, vaccination, severity, and population-denominator inputs

## Later work ⬜

- Building/street-level geocoding and coordinate import
- Disease-specific clustering parameter profiles
- Temporal cluster comparison and playback
- SaTScan and prediction only when explicitly scheduled

Do not begin a milestone until explicitly requested.
