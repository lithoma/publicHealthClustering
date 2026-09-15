"""Add optional case demographics and immutable clustering snapshots."""

from alembic import op

revision = "20260915_02"
down_revision = "20260915_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS current_patient_spatial_points")
    for table in ("patients", "case_events"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS sex TEXT")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS age_years INTEGER")
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_sex_check")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {table}_sex_check CHECK (sex IS NULL OR sex IN ('FEMALE','MALE','OTHER','UNKNOWN'))")
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_age_years_check")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {table}_age_years_check CHECK (age_years IS NULL OR age_years BETWEEN 0 AND 120)")
    op.execute("ALTER TABLE clustering_results ADD COLUMN IF NOT EXISTS sex_snapshot TEXT")
    op.execute("ALTER TABLE clustering_results ADD COLUMN IF NOT EXISTS age_years_snapshot INTEGER")
    op.execute("ALTER TABLE clustering_results DROP CONSTRAINT IF EXISTS clustering_results_sex_snapshot_check")
    op.execute("ALTER TABLE clustering_results DROP CONSTRAINT IF EXISTS clustering_results_age_years_snapshot_check")
    op.execute("ALTER TABLE clustering_results ADD CONSTRAINT clustering_results_sex_snapshot_check CHECK (sex_snapshot IS NULL OR sex_snapshot IN ('FEMALE','MALE','OTHER','UNKNOWN'))")
    op.execute("ALTER TABLE clustering_results ADD CONSTRAINT clustering_results_age_years_snapshot_check CHECK (age_years_snapshot IS NULL OR age_years_snapshot BETWEEN 0 AND 120)")
    op.execute("""
      CREATE VIEW current_patient_spatial_points AS
      SELECT p.patient_id,ce.illness,ce.diagnosis_date,ce.sex,ce.age_years,ce.case_event_id,
             pl.patient_location_id,pl.location,pl.location_source,pl.is_proxy,
             pl.community_id,pl.facility_id,pl.resolved_at,ce.batch_id,db.is_included AS batch_is_included
      FROM patients p JOIN case_events ce ON ce.patient_id=p.patient_id
      JOIN data_batches db ON db.batch_id=ce.batch_id
      JOIN patient_locations pl ON pl.patient_id=p.patient_id AND pl.is_current=TRUE
      UNION ALL
      SELECT p.patient_id,p.illness,p.diagnosis_date,p.sex,p.age_years,NULL::BIGINT,
             pl.patient_location_id,pl.location,pl.location_source,pl.is_proxy,
             pl.community_id,pl.facility_id,pl.resolved_at,p.batch_id,COALESCE(db.is_included,TRUE)
      FROM patients p LEFT JOIN data_batches db ON db.batch_id=p.batch_id
      JOIN patient_locations pl ON pl.patient_id=p.patient_id AND pl.is_current=TRUE
      WHERE NOT EXISTS (SELECT 1 FROM case_events ce WHERE ce.patient_id=p.patient_id)
    """)


def downgrade() -> None:
    raise RuntimeError("Demographic snapshot migration is intentionally irreversible; restore from backup.")
