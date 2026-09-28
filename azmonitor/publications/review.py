"""Cross-edition review of numbers extracted from publications.

An anchored pattern can still latch onto the wrong sentence in an edition whose wording differs from
the ones it was written against. A single reading cannot be checked in isolation, but a series of
readings can: a capital adequacy ratio that sits ten points away from every other edition is a
mis-read, not a collapse.

So after extraction each publication series is compared with the other editions of the same series.
A reading that deviates further than the indicator's tolerance is marked `needs_review` and is then
excluded from the fact pack, the dashboard and any claim, while staying in the database with the
sentence it came from so it can be checked and reinstated.
"""
from __future__ import annotations

import json
import statistics
from typing import Any

from ..storage.db import Database, utcnow
from ..util.log import get_logger
from .stability import INDICATORS

log = get_logger("pub_review")

REVIEWED_PREFIXES = ("cba.fsr.",)
MIN_EDITIONS = 3          # below this there is no basis for comparison, so nothing is flagged


def _tolerance(series_id: str) -> float:
    for ind in INDICATORS:
        if ind.series_id == series_id:
            return float(ind.max_deviation_pp)
    return 8.0


def review_publication_series(db: Database, write: bool = True) -> dict[str, Any]:
    """Flag readings that are inconsistent with the other editions of the same series."""
    flagged: list[dict[str, Any]] = []
    cleared: list[dict[str, Any]] = []
    checked = 0
    rows = db.conn.execute(
        "SELECT obs_id, series_id, dims, period_end, value, value_raw, cell_ref, publication_id, validation_status "
        "FROM observations WHERE status='current' AND value IS NOT NULL AND ("
        + " OR ".join("series_id LIKE ?" for _ in REVIEWED_PREFIXES) + ") ORDER BY series_id, dims, period_end",
        tuple(p + "%" for p in REVIEWED_PREFIXES)).fetchall()
    groups: dict[tuple[str, str], list[Any]] = {}
    for r in rows:
        if r["series_id"].startswith("cba.fsr.stress"):
            continue                       # a stress path is meant to move; it is judged by its scenario
        groups.setdefault((r["series_id"], r["dims"]), []).append(r)
    for (series_id, dims), group in groups.items():
        if len(group) < MIN_EDITIONS:
            continue
        values = [r["value"] for r in group]
        median = statistics.median(values)
        tol = _tolerance(series_id)
        for r in group:
            checked += 1
            deviation = abs(r["value"] - median)
            status = "needs_review" if deviation > tol else "verified"
            record = {"obs_id": r["obs_id"], "series_id": series_id, "dims": json.loads(dims or "{}"),
                      "period_end": r["period_end"], "value": r["value"], "median_of_editions": round(median, 3),
                      "deviation": round(deviation, 3), "tolerance": tol, "publication_id": r["publication_id"],
                      "citation": r["cell_ref"], "sentence": (r["value_raw"] or "")[:240]}
            if status != (r["validation_status"] or "verified"):
                if write:
                    db.conn.execute("UPDATE observations SET validation_status=? WHERE obs_id=?", (status, r["obs_id"]))
                (flagged if status == "needs_review" else cleared).append(record)
            elif status == "needs_review":
                flagged.append(record)
    if write:
        db.conn.commit()
    if flagged:
        log.warning("%d publication reading(s) held for review", len(flagged))
    return {"checked": checked, "flagged": flagged, "reinstated": cleared, "at": utcnow(),
            "rule": "a reading further than the indicator's tolerance from the median of the other editions of the "
                    "same series is held for review and is not used in any report"}
