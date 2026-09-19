"""What counts as a change worth a new edition.

The old rule looked only at the banking reporting month, so a revised deposit series, a new policy
decision or a new stability report produced no new edition until the next month closed. That is the
wrong test: the question is whether anything the report *says* would change.

So an edition is identified by a fingerprint over its actual inputs:

* the anchor periods;
* every metric value the deck displays, at the precision the data are stored to;
* the publications visible to this edition and their release dates;
* the policy decisions in force, including rates, action and the announced next date;
* the narrative that will be written into it;
* the configuration that decides what is shown.

Two runs with identical inputs produce an identical fingerprint and the second returns `unchanged`.
Any material difference produces a new version, and the difference is named in the run summary, so
the reason an edition exists is always on the record.

Materiality is applied where the data are stored, not here: a value enters the dataset only when it
moves by more than the threshold configured for its series type (`settings.materiality`), so a
fingerprint change means a material change by construction.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .. import config
from ..storage.db import Database

VALUE_PRECISION = 6


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()


def config_hash() -> str:
    parts = {}
    for name in ("settings.yaml", "sources.yaml", "metrics.yaml", "reports.yaml", "theme.yaml", "glossary.yaml"):
        path = Path("config") / name
        parts[name] = hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else None
    return _sha(parts)[:16]


def narrative_hash(narrative_file: str | None, mode: str) -> str:
    if narrative_file and Path(narrative_file).exists():
        return f"file:{hashlib.sha256(Path(narrative_file).read_bytes()).hexdigest()[:16]}"
    return f"mode:{mode}"


def edition_inputs(fp: dict[str, Any], db: Database, narrative_file: str | None, mode: str) -> dict[str, Any]:
    metrics = {}
    for ref, entry in sorted((fp.get("metrics") or {}).items()):
        latest = entry.get("latest") or {}
        if latest.get("period") is None:
            continue
        value = latest.get("value")
        metrics[ref] = [latest["period"], round(value, VALUE_PRECISION) if isinstance(value, (int, float)) else value]
    publications = {p["publication_id"]: p.get("published_at") for p in (fp.get("publications") or {}).get("all", [])}
    decisions = {}
    for d in db.decisions():
        decisions[d["announcement_date"]] = [d["policy_rate"], d["corridor_floor"], d["corridor_ceiling"],
                                             d["action"], d["next_decision_date"]]
    return {
        "anchors": {k: (v or {}).get("period_end") for k, v in (fp.get("anchors") or {}).items()},
        "edition": fp.get("edition"),
        "metrics": metrics,
        "publications": publications,
        "decisions": decisions,
        "availability": sorted((fp.get("availability") or {}).get("missing") or []),
        "narrative": narrative_hash(narrative_file, mode),
        "config": config_hash(),
        "quality_blocking": sorted(c.get("id") or c.get("type", "") for c in (fp.get("quality") or {}).get("checks", [])
                                   if not c.get("ok") and c.get("severity") == "critical"),
    }


def fingerprint(fp: dict[str, Any], db: Database, narrative_file: str | None, mode: str) -> dict[str, Any]:
    inputs = edition_inputs(fp, db, narrative_file, mode)
    return {"fingerprint": _sha(inputs)[:24], "inputs": inputs}


def describe_change(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """Name what changed since the last edition, in the terms a reader would ask about."""
    if not previous:
        return {"trigger": "first edition", "details": []}
    prev, cur = previous.get("inputs") or {}, current.get("inputs") or {}
    details: list[str] = []
    if prev.get("anchors") != cur.get("anchors"):
        details.append(f"reporting anchors moved from {prev.get('anchors')} to {cur.get('anchors')}")
    new_pubs = sorted(set(cur.get("publications") or {}) - set(prev.get("publications") or {}))
    if new_pubs:
        details.append("new publication(s): " + ", ".join(new_pubs[:6]))
    changed_pub_dates = [k for k in (cur.get("publications") or {})
                         if k in (prev.get("publications") or {}) and cur["publications"][k] != prev["publications"][k]]
    if changed_pub_dates:
        details.append("publication date corrected for " + ", ".join(sorted(changed_pub_dates)[:4]))
    new_dec = sorted(set(cur.get("decisions") or {}) - set(prev.get("decisions") or {}))
    if new_dec:
        details.append("new policy decision(s): " + ", ".join(new_dec))
    changed_dec = [k for k in (cur.get("decisions") or {})
                   if k in (prev.get("decisions") or {}) and cur["decisions"][k] != prev["decisions"][k]]
    if changed_dec:
        details.append("policy decision detail changed for " + ", ".join(sorted(changed_dec)[:4]))
    revised, new_periods = [], []
    for ref, value in (cur.get("metrics") or {}).items():
        old = (prev.get("metrics") or {}).get(ref)
        if old is None:
            new_periods.append(ref)
        elif old[0] != value[0]:
            new_periods.append(ref)
        elif old[1] != value[1]:
            revised.append(ref)
    if new_periods:
        details.append(f"{len(new_periods)} metric(s) moved to a new period, including {', '.join(sorted(new_periods)[:4])}")
    if revised:
        details.append(f"{len(revised)} metric value(s) revised for an unchanged period, including {', '.join(sorted(revised)[:4])}")
    if prev.get("narrative") != cur.get("narrative"):
        # the component is "file:<hash>" for an analyst narrative and "mode:<mode>" otherwise, so
        # say which of the two moved rather than always reporting a changed file
        pn, cn = str(prev.get("narrative") or ""), str(cur.get("narrative") or "")
        if pn.split(":")[0] != cn.split(":")[0]:
            describe = lambda x: "an analyst narrative" if x.startswith("file:") else f"{x.split(':', 1)[-1]} text"
            details.append(f"the narrative source changed from {describe(pn)} to {describe(cn)}")
        elif cn.startswith("file:"):
            details.append("the supplied narrative changed")
        else:
            details.append(f"the narrative mode changed to {cn.split(':', 1)[-1]}")
    if prev.get("config") != cur.get("config"):
        details.append("configuration changed")
    if prev.get("availability") != cur.get("availability"):
        details.append("the set of unavailable inputs changed")
    trigger = details[0].split(":")[0] if details else "no material change"
    return {"trigger": trigger, "details": details}
