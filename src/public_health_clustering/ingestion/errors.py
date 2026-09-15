"""Structured validation errors for CSV ingestion parsing/validation."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class ValidationErrorCode(enum.StrEnum):
    """Stable, machine-readable classification for a ValidationError."""

    MISSING_HEADER = "MISSING_HEADER"
    DUPLICATE_HEADER = "DUPLICATE_HEADER"
    MALFORMED_ROW = "MALFORMED_ROW"
    REQUIRED_FIELD = "REQUIRED_FIELD"
    DUPLICATE_ID = "DUPLICATE_ID"
    INVALID_DATE = "INVALID_DATE"
    INVALID_NUMBER = "INVALID_NUMBER"
    INVALID_VALUE = "INVALID_VALUE"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    FILE_ERROR = "FILE_ERROR"


@dataclass(frozen=True)
class ValidationError:
    """A single parsing or validation failure, tied to its source location.

    row_number is None for file-level errors (missing/unreadable file, decode
    errors, missing or duplicate header columns) that are not tied to one
    specific data row.
    """

    code: ValidationErrorCode
    source: str
    row_number: int | None
    field: str | None
    message: str

    def __str__(self) -> str:
        location = (
            f"{self.source}:{self.row_number}" if self.row_number is not None else self.source
        )
        field = f" [{self.field}]" if self.field else ""
        return f"{location}{field} ({self.code}): {self.message}"
