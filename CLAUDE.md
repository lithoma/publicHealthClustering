# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

# Project

Public Health Spatial Disease Clustering Platform

A patient-level spatial disease clustering system for public health surveillance.

The project is being developed incrementally. Implement **one milestone at a time**.

Current status:

- ✅ Milestone 1 completed
- Local PostgreSQL/PostGIS development environment
- Initial database schema
- Python package scaffold
- ✅ Milestone 2.1–2.2 completed
- Application configuration (`config.py`)
- Database engine/session factories and health check (`db.py`)

Not yet implemented:

- Patient/community/facility ingestion
- Location resolution
- Geocoding
- Clustering execution
- Map generation
- APIs
- Real patient data support

Never assume later milestones already exist.

No real patient data should ever be stored inside this repository.

---

# Development Rules

- Implement only the requested milestone.
- Do not begin later milestones unless explicitly instructed.
- Do not redesign the architecture unless asked.
- Before making significant architectural or schema changes, explain the proposal first.
- Keep changes small and reviewable.
- Do not modify unrelated files.
- Preserve backwards compatibility whenever practical.
- Prefer explicit, readable code over clever implementations.
- When unsure, ask instead of guessing.

Always provide:

- summary of changed files
- explanation of important design decisions
- validation steps performed
- remaining work

---

# Environment

Create the virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Copy local configuration:

```bash
cp .env.example .env
```

Start PostgreSQL/PostGIS:

```bash
docker compose up -d db
docker compose ps
```

The database schema is initialized automatically for a fresh volume.

To safely reapply the schema:

```bash
docker compose exec -T db \
  psql \
  -v ON_ERROR_STOP=1 \
  -U public_health \
  -d public_health_clustering \
  -f /docker-entrypoint-initdb.d/001-schema.sql
```

Stop the database:

```bash
docker compose down
```

Do **not** use:

```bash
docker compose down --volumes
```

unless explicitly requested, because it destroys the database.

---

# Testing

Pytest is configured in `pyproject.toml`.

When tests are introduced:

- separate unit tests from integration tests
- PostGIS integration tests must run against Docker
- avoid requiring production datasets
- use synthetic data whenever possible

---

# Repository Layout

```
src/public_health_clustering/
    Python package

sql/schema.sql
    Source of truth for database schema

compose.yaml
    Local PostgreSQL/PostGIS development environment

data/raw/
data/processed/
    Local datasets (gitignored)

output/
    Generated outputs

notebooks/
    Research notebooks

tests/
    Unit and integration tests
```

---

# Architecture

The project follows a layered architecture.

```
Raw data
    ↓
Import
    ↓
Validation
    ↓
Location Resolution
    ↓
Current Patient Spatial View
    ↓
Spatial Clustering
    ↓
Visualization
```

Each layer should remain independent.

---

# Database Principles

## Raw data is immutable

Never overwrite original source fields.

For example:

- patient address
- reported community
- imported identifiers

must always remain unchanged.

Derived values belong in separate tables.

---

## Spatial provenance is first-class

Every patient location must record:

- where the coordinate came from
- whether it is a proxy
- how it was resolved
- when it became current

The analytical pipeline must always preserve provenance.

---

## Current analytical location

`current_patient_spatial_points`

is the canonical analytical view.

It represents the currently selected spatial point for each patient.

The selected point may come from:

- patient address
- community reference
- healthcare facility proxy
- manual correction

Downstream clustering should read from this view instead of directly accessing source tables.

---

# Schema Overview

## communities

Reference community dataset.

Stores:

- longitude
- latitude
- generated PostGIS geometry

Geometry is always generated from coordinates.

---

## healthcare_facilities

Reference healthcare facilities.

Stores:

- coordinates
- generated geometry

Used as a fallback spatial source.

---

## patients

Stores raw patient records only.

Does **not** contain geometry.

Original address fields must never be replaced.

---

## patient_locations

Stores resolved patient locations.

Each location contains:

- source
- geometry
- proxy status
- quality
- timestamps

Only one location may be current.

---

## clustering_runs

Stores:

- time window
- DBSCAN parameters
- execution status
- summary statistics

Each run must be reproducible.

---

## clustering_results

Stores immutable snapshots.

Each record preserves:

- geometry used
- location source
- proxy status

Historical runs must remain reproducible even if patient locations later change.

---

# Design Principles

Preserve these rules whenever modifying the schema.

- Raw source data is immutable.
- Derived geometry is stored separately.
- Geometry is generated from coordinates.
- Analytical views read normalized data.
- Historical clustering results must remain reproducible.
- CHECK constraints should enforce data consistency.
- Spatial provenance should never be inferred from free text.

---

# Coding Style

- Use type hints.
- Keep functions focused.
- Avoid duplication.
- Prefer composition over inheritance.
- Use SQLAlchemy ORM for application logic.
- Keep SQL in dedicated files when appropriate.
- Follow PEP 8.
- Add docstrings for public APIs.

---

# Milestone Roadmap

## ✅ Milestone 1

- Python package
- Docker
- PostgreSQL/PostGIS
- Initial schema

## Milestone 2

- ✅ 2.1 Configuration (`config.py`)
- ✅ 2.2 Database engine/session factories, health check (`db.py`)
- 2.3 SQLAlchemy models
- 2.4 Alembic

## Milestone 3

- Data ingestion

## Milestone 4

- Location resolution

## Milestone 5

- Spatial clustering

## Milestone 6

- Map generation

## Milestone 7

- Testing

## Milestone 8

- AI prediction module

Do not begin a milestone until explicitly requested.

---

# Privacy

Never commit:

- `.env`
- credentials
- API keys
- raw patient data
- personally identifiable information

Use synthetic datasets for development whenever possible.

---

# Before Finishing Any Task

Always provide:

1. Files changed
2. Important design decisions
3. Validation performed
4. Remaining work
5. Suggested next milestone

Do not silently make architectural changes.