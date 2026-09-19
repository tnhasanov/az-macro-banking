"""Observation access for calculations: current vintage or an information set as of a cutoff."""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from ..storage.db import Database


@dataclass
class SeriesInfo:
    series_id: str
    dims: dict[str, str]
    period_type: str | None
    unit: str | None
    population: str | None
    basis: str | None
    dataset_id: str | None
    source_id: str | None
    doc_ids: list[str] = field(default_factory=list)
    latest_period: dt.date | None = None
    latest_published_at: str | None = None
    label_original: str | None = None


class ObservationStore:
    """In-memory view of observations with helpers to fetch aligned monthly series."""

    def __init__(self, db: Database, as_of_cutoff_iso: str | None = None, historical_information_set: bool = False):
        rows = db.observations_as_of(as_of_cutoff_iso) if (historical_information_set and as_of_cutoff_iso) else db.current_observations()
        recs = [dict(r) for r in rows]
        self.df = pd.DataFrame(recs)
        if self.df.empty:
            self.df = pd.DataFrame(columns=["series_id", "dims", "period_end", "value"])
        else:
            self.df["period_end"] = pd.to_datetime(self.df["period_end"]).dt.date
        self.registry = {k: dict(v) for k, v in db.series_registry().items()}
        self.docs = {r["doc_id"]: dict(r) for r in db.all_documents()}
        self.mode = "historical_information_set" if (historical_information_set and as_of_cutoff_iso) else "reconstructed_current_vintage"
        self._cache: dict[tuple[str, str], pd.Series] = {}

    def series(self, series_id: str, dims: dict[str, str] | None = None, cutoff: dt.date | None = None) -> pd.Series:
        key = (series_id, json.dumps(dims or {}, ensure_ascii=False, sort_keys=True))
        if key not in self._cache:
            sub = self.df[(self.df["series_id"] == series_id) & (self.df["dims"] == key[1])]
            s = pd.Series(sub["value"].astype(float).values, index=pd.Index(sub["period_end"]), name=series_id).sort_index()
            s = s[~s.index.duplicated(keep="last")]
            self._cache[key] = s
        s = self._cache[key]
        if cutoff is not None:
            s = s[s.index <= cutoff]
        return s

    def info(self, series_id: str, dims: dict[str, str] | None = None) -> SeriesInfo | None:
        dj = json.dumps(dims or {}, ensure_ascii=False, sort_keys=True)
        sub = self.df[(self.df["series_id"] == series_id) & (self.df["dims"] == dj)]
        if sub.empty:
            return None
        last = sub.sort_values("period_end").iloc[-1]
        reg = self.registry.get(series_id, {})
        return SeriesInfo(
            series_id=series_id, dims=dims or {}, period_type=last.get("period_type") or reg.get("period_type"),
            unit=last.get("unit") or reg.get("unit"), population=last.get("population") or reg.get("population"), basis=last.get("basis"),
            dataset_id=last.get("dataset_id"), source_id=last.get("source_id"), doc_ids=sorted(set(sub["doc_id"].dropna())),
            latest_period=last["period_end"], latest_published_at=last.get("published_at"), label_original=last.get("label_original"),
        )

    def observation(self, series_id: str, period_end: dt.date, dims: dict[str, str] | None = None) -> dict[str, Any] | None:
        dj = json.dumps(dims or {}, ensure_ascii=False, sort_keys=True)
        sub = self.df[(self.df["series_id"] == series_id) & (self.df["dims"] == dj) & (self.df["period_end"] == period_end)]
        if sub.empty:
            return None
        return sub.iloc[-1].to_dict()

    def dims_for(self, series_id: str) -> list[dict[str, str]]:
        sub = self.df[self.df["series_id"] == series_id]
        return [json.loads(d) for d in sorted(set(sub["dims"]))]

    def has(self, series_id: str) -> bool:
        return bool((self.df["series_id"] == series_id).any())
