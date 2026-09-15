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


class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = (Index("patients_diagnosis_illness_idx", "diagnosis_date", "illness"),)

    patient_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    illness: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis_date: Mapped[date] = mapped_column(Date, nullable=False)
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
        CheckConstraint(
            "(location_source_snapshot = 'FACILITY_PROXY' AND is_proxy_snapshot = TRUE) "
            "OR (location_source_snapshot <> 'FACILITY_PROXY' AND is_proxy_snapshot = FALSE)",
            name="clustering_results_proxy_snapshot_check",
        ),
        UniqueConstraint(
            "run_id", "patient_id", name="clustering_results_run_patient_unique"
        ),
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
    cluster_id: Mapped[int | None] = mapped_column(Integer)
    illness_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis_date_snapshot: Mapped[date] = mapped_column(Date, nullable=False)
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
