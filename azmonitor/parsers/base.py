"""Common parser types."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Observation:
    series_id: str
    period_end: dt.date
    value: float | None
    value_raw: str = ""
    period_start: dt.date | None = None
    freq: str = "M"
    period_type: str = "month_end_stock"
    dims: dict[str, str] = field(default_factory=dict)
    missing_reason: str | None = None
    unit: str | None = None
    scale_note: str | None = None
    currency: str | None = None
    population: str | None = None
    basis: str | None = None
    source_id: str | None = None
    sheet: str | None = None
    cell_ref: str | None = None
    label_original: str | None = None
    extraction_method: str = "xlsx_cell"
    preliminary: bool = False
    method_break: bool = False
    flags: list[str] = field(default_factory=list)


@dataclass
class ParseResult:
    observations: list[Observation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.observations)


class ParserError(Exception):
    pass
