"""SQLAlchemy ORM models mirroring sql/schema.sql.

This module is a read/write mapping of the existing schema only. It does not
define ingestion, location resolution, or clustering logic, and it does not
create or alter database objects — sql/schema.sql remains the single source
of truth for DDL.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKBElement
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Double,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class LocationSource(enum.StrEnum):
    """Matches the location_source / location_source_snapshot CHECK constraints."""

    COMMUNITY_REFERENCE = "COMMUNITY_REFERENCE"
    PATIENT_ADDRESS_GEOCODE = "PATIENT_ADDRESS_GEOCODE"
    FACILITY_PROXY = "FACILITY_PROXY"
    MANUAL = "MANUAL"
    OTHER = "OTHER"


class ClusteringRunStatus(enum.StrEnum):
    """Matches the clustering_runs.status CHECK constraint."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class KnowledgeDocumentStatus(enum.StrEnum):
    """Lifecycle state for curated disease-knowledge documents."""

    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"
    DRAFT = "DRAFT"


class StrEnumText(TypeDecorator):
    """Persists a Python StrEnum as TEXT, matching a TEXT + CHECK column in the schema.

    Unlike sqlalchemy.Enum(native_enum=False), this keeps the underlying column
    type exactly TEXT (no VARCHAR length, no type-driven CHECK) — the CHECK
    constraints are declared separately in each model's __table_args__ to match
    sql/schema.sql verbatim.
    """

    impl = Text
    cache_ok = True

    def __init__(self, enum_cls: type[enum.StrEnum], *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.enum_cls = enum_cls

    def process_bind_param(self, value: object, dialect: object) -> str | None:
        if value is None:
            return None
        return self.enum_cls(value).value

    def process_result_value(self, value: object, dialect: object) -> enum.StrEnum | None:
        if value is None:
            return None
        return self.enum_cls(value)


class HealthcareFacility(Base):
    __tablename__ = "healthcare_facilities"
    __table_args__ = (
        CheckConstraint("longitude BETWEEN -180 AND 180"),
        CheckConstraint("latitude BETWEEN -90 AND 90"),
        Index("healthcare_facilities_location_gix", "location", postgresql_using="gist"),
    )

    facility_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    facility_code: Mapped[str | None] = mapped_column(Text, unique=True)
    facility_name: Mapped[str] = mapped_column(Text, nullable=False)
    facility_type: Mapped[str | None] = mapped_column(Text)
    full_address: Mapped[str] = mapped_column(Text, nullable=False)
    longitude: Mapped[float] = mapped_column(Double, nullable=False)
    latitude: Mapped[float] = mapped_column(Double, nullable=False)
    location: Mapped[WKBElement | None] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        Computed("ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)", persisted=True),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    diagnosed_patients: Mapped[list["Patient"]] = relationship(
        back_populates="diagnosing_facility", passive_deletes=True
    )
    proxy_locations: Mapped[list["PatientLocation"]] = relationship(
        back_populates="facility", passive_deletes=True
    )


class Community(Base):
    __tablename__ = "communities"
    __table_args__ = (
        CheckConstraint("longitude BETWEEN -180 AND 180"),
        CheckConstraint("latitude BETWEEN -90 AND 90"),
        Index("communities_location_gix", "location", postgresql_using="gist"),
    )

    community_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    community_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    full_address: Mapped[str] = mapped_column(Text, nullable=False)
    longitude: Mapped[float] = mapped_column(Double, nullable=False)
    latitude: Mapped[float] = mapped_column(Double, nullable=False)
    location: Mapped[WKBElement | None] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        Computed("ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)", persisted=True),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    patient_locations: Mapped[list["PatientLocation"]] = relationship(
        back_populates="community", passive_deletes=True
    )


class DataBatch(Base):
    __tablename__ = "data_batches"

    batch_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    batch_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_name: Mapped[str | None] = mapped_column(Text)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                server_default=text("false"))
    is_included: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True,
                                               server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    patients: Mapped[list["Patient"]] = relationship(back_populates="batch")
    case_events: Mapped[list["CaseEvent"]] = relationship(back_populates="batch")


class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = (
        CheckConstraint("sex IS NULL OR sex IN ('FEMALE', 'MALE', 'OTHER', 'UNKNOWN')"),
        CheckConstraint("age_years IS NULL OR age_years BETWEEN 0 AND 120"),
        Index("patients_diagnosis_illness_idx", "diagnosis_date", "illness"),
    )

    patient_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    illness: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis_date: Mapped[date] = mapped_column(Date, nullable=False)
    sex: Mapped[str | None] = mapped_column(Text)
    age_years: Mapped[int | None] = mapped_column(Integer)
    address_text: Mapped[str | None] = mapped_column(Text)
    community_name: Mapped[str | None] = mapped_column(Text)
    diagnosing_facility_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "healthcare_facilities.facility_id", onupdate="CASCADE", ondelete="SET NULL"
        ),
    )
    batch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_batches.batch_id", onupdate="CASCADE", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    diagnosing_facility: Mapped[HealthcareFacility | None] = relationship(
        back_populates="diagnosed_patients"
    )
    batch: Mapped[DataBatch | None] = relationship(back_populates="patients")
    locations: Mapped[list["PatientLocation"]] = relationship(
        back_populates="patient", passive_deletes=True
    )
    clustering_results: Mapped[list["ClusteringResult"]] = relationship(
        back_populates="patient", passive_deletes=True
    )
    case_events: Mapped[list["CaseEvent"]] = relationship(back_populates="patient")


class CaseEvent(Base):
    __tablename__ = "case_events"
    __table_args__ = (
        UniqueConstraint("batch_id", "source_case_id", name="case_events_batch_source_unique"),
        CheckConstraint("sex IS NULL OR sex IN ('FEMALE', 'MALE', 'OTHER', 'UNKNOWN')"),
        CheckConstraint("age_years IS NULL OR age_years BETWEEN 0 AND 120"),
        Index("case_events_batch_idx", "batch_id"),
        Index("case_events_patient_idx", "patient_id"),
    )

    case_event_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("data_batches.batch_id", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
    )
    patient_id: Mapped[str] = mapped_column(
        Text, ForeignKey("patients.patient_id", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
    )
    source_case_id: Mapped[str] = mapped_column(Text, nullable=False)
    illness: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis_date: Mapped[date] = mapped_column(Date, nullable=False)
    sex: Mapped[str | None] = mapped_column(Text)
    age_years: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    batch: Mapped[DataBatch] = relationship(back_populates="case_events")
    patient: Mapped[Patient] = relationship(back_populates="case_events")


class PatientLocation(Base):
    __tablename__ = "patient_locations"
    __table_args__ = (
        CheckConstraint(
            "location_source IN "
            "('COMMUNITY_REFERENCE', 'PATIENT_ADDRESS_GEOCODE', 'FACILITY_PROXY', "
            "'MANUAL', 'OTHER')"
        ),
        CheckConstraint(
            "location_source <> 'COMMUNITY_REFERENCE' "
            "OR (community_id IS NOT NULL AND is_proxy = FALSE)",
            name="patient_locations_community_source_check",
        ),
        CheckConstraint(
            "(location_source = 'FACILITY_PROXY' AND facility_id IS NOT NULL "
            "AND is_proxy = TRUE) "
            "OR (location_source <> 'FACILITY_PROXY' AND is_proxy = FALSE)",
            name="patient_locations_facility_proxy_check",
        ),
        UniqueConstraint(
            "patient_location_id", "patient_id", name="patient_locations_id_patient_unique"
        ),
        Index(
            "patient_locations_one_current_per_patient_idx",
            "patient_id",
            unique=True,
            postgresql_where=text("is_current = TRUE"),
        ),
        Index("patient_locations_location_gix", "location", postgresql_using="gist"),
        Index("patient_locations_patient_idx", "patient_id"),
    )

    patient_location_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )
    patient_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("patients.patient_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    location: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False
    )
    location_source: Mapped[LocationSource] = mapped_column(
        StrEnumText(LocationSource), nullable=False
    )
    community_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("communities.community_id", onupdate="CASCADE", ondelete="RESTRICT"),
    )
    facility_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "healthcare_facilities.facility_id", onupdate="CASCADE", ondelete="RESTRICT"
        ),
    )
    is_proxy: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    patient: Mapped[Patient] = relationship(back_populates="locations")
    community: Mapped[Community | None] = relationship(back_populates="patient_locations")
    facility: Mapped[HealthcareFacility | None] = relationship(
        back_populates="proxy_locations"
    )
    clustering_results: Mapped[list["ClusteringResult"]] = relationship(
        back_populates="patient_location", viewonly=True
    )


class ClusteringRun(Base):
    __tablename__ = "clustering_runs"
    __table_args__ = (
        CheckConstraint("window_days > 0"),
        CheckConstraint("eps_meters > 0"),
        CheckConstraint("min_points > 0"),
        CheckConstraint("target_srid > 0"),
        CheckConstraint("status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')"),
        CheckConstraint("case_count IS NULL OR case_count >= 0"),
        CheckConstraint("proxy_case_count IS NULL OR proxy_case_count >= 0"),
        CheckConstraint("cluster_count IS NULL OR cluster_count >= 0"),
        CheckConstraint(
            "window_end > window_start", name="clustering_runs_window_check"
        ),
        CheckConstraint(
            "proxy_case_count IS NULL OR case_count IS NULL "
            "OR proxy_case_count <= case_count",
            name="clustering_runs_counts_check",
        ),
    )

    run_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=7, server_default=text("7")
    )
    eps_meters: Mapped[float] = mapped_column(Double, nullable=False)
    min_points: Mapped[int] = mapped_column(Integer, nullable=False)
    target_srid: Mapped[int] = mapped_column(Integer, nullable=False)
    illness_filter: Mapped[str | None] = mapped_column(Text)
    include_proxy_locations: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    included_batch_ids: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ClusteringRunStatus] = mapped_column(
        StrEnumText(ClusteringRunStatus),
        nullable=False,
        default=ClusteringRunStatus.PENDING,
        server_default=text("'PENDING'"),
    )
    case_count: Mapped[int | None] = mapped_column(Integer)
    proxy_case_count: Mapped[int | None] = mapped_column(Integer)
    cluster_count: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    results: Mapped[list["ClusteringResult"]] = relationship(
        back_populates="run", passive_deletes=True
    )


class ClusteringResult(Base):
    __tablename__ = "clustering_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["patient_location_id", "patient_id"],
            ["patient_locations.patient_location_id", "patient_locations.patient_id"],
            name="clustering_results_patient_location_fk",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "location_source_snapshot IN "
            "('COMMUNITY_REFERENCE', 'PATIENT_ADDRESS_GEOCODE', 'FACILITY_PROXY', "
            "'MANUAL', 'OTHER')"
        ),
        CheckConstraint("sex_snapshot IS NULL OR sex_snapshot IN ('FEMALE', 'MALE', 'OTHER', 'UNKNOWN')"),
        CheckConstraint("age_years_snapshot IS NULL OR age_years_snapshot BETWEEN 0 AND 120"),
        CheckConstraint(
            "(location_source_snapshot = 'FACILITY_PROXY' AND is_proxy_snapshot = TRUE) "
            "OR (location_source_snapshot <> 'FACILITY_PROXY' AND is_proxy_snapshot = FALSE)",
            name="clustering_results_proxy_snapshot_check",
        ),
        UniqueConstraint("run_id", "case_event_id", name="clustering_results_run_case_event_unique"),
        Index(
            "clustering_results_location_snapshot_gix",
            "location_snapshot",
            postgresql_using="gist",
        ),
        Index("clustering_results_run_idx", "run_id"),
    )

    clustering_result_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("clustering_runs.run_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    patient_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("patients.patient_id", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=False,
    )
    patient_location_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    case_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("case_events.case_event_id", onupdate="CASCADE", ondelete="RESTRICT")
    )
    cluster_id: Mapped[int | None] = mapped_column(Integer)
    illness_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis_date_snapshot: Mapped[date] = mapped_column(Date, nullable=False)
    sex_snapshot: Mapped[str | None] = mapped_column(Text)
    age_years_snapshot: Mapped[int | None] = mapped_column(Integer)
    location_snapshot: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False
    )
    location_source_snapshot: Mapped[LocationSource] = mapped_column(
        StrEnumText(LocationSource), nullable=False
    )
    is_proxy_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )

    run: Mapped[ClusteringRun] = relationship(back_populates="results")
    patient: Mapped[Patient] = relationship(back_populates="clustering_results")
    patient_location: Mapped[PatientLocation] = relationship(
        back_populates="clustering_results", viewonly=True
    )


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint("status IN ('CURRENT', 'SUPERSEDED', 'DRAFT')"),
        UniqueConstraint("source_url", "version_label", name="knowledge_documents_source_version_unique"),
        Index("knowledge_documents_disease_status_idx", "disease", "status"),
    )

    document_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    disease: Mapped[str] = mapped_column(Text, nullable=False)
    title_zh: Mapped[str] = mapped_column(Text, nullable=False)
    issuing_body: Mapped[str] = mapped_column(Text, nullable=False)
    jurisdiction: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'中国'"))
    publication_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)
    version_label: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[KnowledgeDocumentStatus] = mapped_column(
        StrEnumText(KnowledgeDocumentStatus), nullable=False,
        default=KnowledgeDocumentStatus.CURRENT, server_default=text("'CURRENT'")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    sections: Mapped[list["KnowledgeSection"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True,
        order_by="KnowledgeSection.sort_order"
    )


class KnowledgeSection(Base):
    __tablename__ = "knowledge_sections"
    __table_args__ = (
        CheckConstraint("sort_order >= 0"),
        UniqueConstraint("document_id", "sort_order", name="knowledge_sections_document_order_unique"),
        Index("knowledge_sections_document_idx", "document_id"),
    )

    section_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_documents.document_id", onupdate="CASCADE", ondelete="CASCADE"), nullable=False
    )
    section_title_zh: Mapped[str] = mapped_column(Text, nullable=False)
    content_zh: Mapped[str] = mapped_column(Text, nullable=False)
    keywords_zh: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    document: Mapped[KnowledgeDocument] = relationship(back_populates="sections")


class PredictionRun(Base):
    __tablename__ = "prediction_runs"
    __table_args__ = (CheckConstraint("horizon_days > 0"), CheckConstraint("history_days > 0"), CheckConstraint("grid_size_meters > 0"), CheckConstraint("target_srid > 0"), CheckConstraint("status IN ('COMPLETED', 'FAILED')"))
    prediction_run_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    cutoff_date: Mapped[date] = mapped_column(Date, nullable=False)
    forecast_start: Mapped[date] = mapped_column(Date, nullable=False)
    forecast_end: Mapped[date] = mapped_column(Date, nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7, server_default=text("7"))
    history_days: Mapped[int] = mapped_column(Integer, nullable=False, default=28, server_default=text("28"))
    grid_size_meters: Mapped[int] = mapped_column(Integer, nullable=False)
    target_srid: Mapped[int] = mapped_column(Integer, nullable=False)
    model_type: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    validation_status: Mapped[str] = mapped_column(Text, nullable=False)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'COMPLETED'"))
    include_proxy_locations: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    runtime_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    results: Mapped[list["PredictionResult"]] = relationship(back_populates="run", passive_deletes=True)
    batch_snapshots: Mapped[list["PredictionRunBatch"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )
    evaluation: Mapped["PredictionEvaluation | None"] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True,
        uselist=False,
    )


class PredictionResult(Base):
    __tablename__ = "prediction_results"
    __table_args__ = (UniqueConstraint("prediction_run_id", "disease", "cell_x", "cell_y", name="prediction_results_run_disease_cell_unique"), CheckConstraint("expected_cases >= 0 AND lower_bound >= 0 AND upper_bound >= lower_bound"), CheckConstraint("observed_cases IS NULL OR observed_cases >= 0"), Index("prediction_results_run_idx", "prediction_run_id"), Index("prediction_results_cell_gix", "cell_geometry", postgresql_using="gist"))
    prediction_result_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    prediction_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("prediction_runs.prediction_run_id", onupdate="CASCADE", ondelete="CASCADE"), nullable=False)
    disease: Mapped[str] = mapped_column(Text, nullable=False)
    cell_x: Mapped[int] = mapped_column(Integer, nullable=False)
    cell_y: Mapped[int] = mapped_column(Integer, nullable=False)
    cell_geometry: Mapped[WKBElement] = mapped_column(Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False), nullable=False)
    expected_cases: Mapped[float] = mapped_column(Double, nullable=False)
    lower_bound: Mapped[float] = mapped_column(Double, nullable=False)
    upper_bound: Mapped[float] = mapped_column(Double, nullable=False)
    information_level: Mapped[str] = mapped_column(Text, nullable=False)
    features_json: Mapped[str] = mapped_column(Text, nullable=False)
    observed_cases: Mapped[int | None] = mapped_column(Integer)
    run: Mapped[PredictionRun] = relationship(back_populates="results")


class PredictionRunBatch(Base):
    """Immutable batch identity used to construct one prediction run."""

    __tablename__ = "prediction_run_batches"
    __table_args__ = (Index("prediction_run_batches_batch_idx", "batch_id_snapshot"),)

    prediction_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("prediction_runs.prediction_run_id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    batch_id_snapshot: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_name_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    run: Mapped[PredictionRun] = relationship(back_populates="batch_snapshots")


class PredictionEvaluation(Base):
    """Observed-outcome scores for one completed historical prediction."""

    __tablename__ = "prediction_evaluations"
    __table_args__ = (
        CheckConstraint("forecast_cell_count >= 0"),
        CheckConstraint("observed_cases_in_cells >= 0"),
        CheckConstraint("observed_cases_outside_cells >= 0"),
        CheckConstraint("interval_coverage IS NULL OR interval_coverage BETWEEN 0 AND 1"),
        CheckConstraint("hotspot_k IS NULL OR hotspot_k > 0"),
        CheckConstraint("hotspot_recall IS NULL OR hotspot_recall BETWEEN 0 AND 1"),
        Index("prediction_evaluations_run_idx", "prediction_run_id"),
    )

    prediction_evaluation_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )
    prediction_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("prediction_runs.prediction_run_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    forecast_cell_count: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_cases_in_cells: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_cases_outside_cells: Mapped[int] = mapped_column(Integer, nullable=False)
    mae: Mapped[float | None] = mapped_column(Double)
    rmse: Mapped[float | None] = mapped_column(Double)
    mean_interval_score: Mapped[float | None] = mapped_column(Double)
    interval_coverage: Mapped[float | None] = mapped_column(Double)
    hotspot_k: Mapped[int | None] = mapped_column(Integer)
    hotspot_recall: Mapped[float | None] = mapped_column(Double)
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    run: Mapped[PredictionRun] = relationship(back_populates="evaluation")
