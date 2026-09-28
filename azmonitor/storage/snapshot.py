"""Immutable analytical snapshots (Parquet) referenced by report manifests."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .db import Database


def export_snapshot(db: Database, snapshots_dir: Path, analytics_dir: Path, metrics_table: pd.DataFrame | None = None) -> dict[str, Any]:
    rows = [dict(r) for r in db.current_observations()]
    df = pd.DataFrame(rows)
    content_hash = hashlib.sha256(json.dumps([(r["series_id"], r["dims"], r["period_end"], r["value"]) for r in rows], sort_keys=True, default=str).encode()).hexdigest()[:16]
    sid = f"{dt.datetime.utcnow():%Y%m%dT%H%M%S}-{content_hash}"
    d = snapshots_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(d / "observations.parquet", index=False)
    if metrics_table is not None and not metrics_table.empty:
        metrics_table.to_parquet(d / "metrics.parquet", index=False)
    docs = pd.DataFrame([dict(r) for r in db.all_documents()])
    docs.to_parquet(d / "documents.parquet", index=False)
    # rolling analytical table for ad-hoc use
    analytics_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(analytics_dir / "observations_current.parquet", index=False)
    if metrics_table is not None and not metrics_table.empty:
        metrics_table.to_parquet(analytics_dir / "metrics_current.parquet", index=False)
    meta = {"snapshot_id": sid, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "n_observations": len(df),
            "content_hash": content_hash, "path": str(d)}
    (d / "snapshot.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta
