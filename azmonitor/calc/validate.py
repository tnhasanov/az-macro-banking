"""Data-quality checks on the current dataset: reconciliation, decomposition, labels, freshness."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .. import config
from ..pipeline import run_dataset_checks
from ..parsers.base import Observation
from ..storage.db import Database
from ..util.periods import today_baku
from .data import ObservationStore
from .metrics import MetricEngine


def _obs_from_rows(rows) -> list[Observation]:
    out = []
    for r in rows:
        out.append(Observation(series_id=r["series_id"], period_end=dt.date.fromisoformat(r["period_end"]), value=r["value"],
                               dims=json.loads(r["dims"] or "{}"), period_type=r["period_type"]))
    return out


def validate_all(db: Database | None = None, write: bool = True) -> dict[str, Any]:
    paths = config.paths()
    own = db is None
    db = db or Database(paths.db_path)
    checks: list[dict[str, Any]] = []
    # 1. source totals vs components (per dataset config)
    for sid, scfg, ds in config.iter_datasets():
        rows = db.current_observations(dataset_id=ds["id"])
        if not rows:
            continue
        res = run_dataset_checks(ds, _obs_from_rows(rows))
        failed = [c for c in res if not c["ok"]]
        checks.append({"check": f"components_sum:{ds['id']}", "n": len(res), "failed": len(failed), "ok": not failed,
                       "examples": failed[:3], "history_start": config.settings().get("history_start")})
    store = ObservationStore(db)
    eng = MetricEngine(store)
    eng.compute_all()
    # 2. SSC republication of CBA credit and household deposits reconciles with CBA tables
    for ssc_id, cba_id, tol, note in [("ssc.hl.credit.level", "cba.loans.total_ci", 0.15, "SSC 'İqtisadiyyata kredit qoyuluşları' vs CBA table 2.6 total"),
                                       ("ssc.hl.hh_deposits.level", "cba.hh_savings.total", 0.15, "SSC 'Fiziki şəxslərin əmanətləri' vs CBA table 2.13 total")]:
        a, b = store.series(ssc_id), store.series(cba_id)
        common = a.index.intersection(b.index)
        diffs = [(d.isoformat(), float(a[d]), float(b[d]), float(a[d] - b[d])) for d in common if abs(a[d] - b[d]) > tol]
        checks.append({"check": f"reconcile:{ssc_id}~{cba_id}", "n": len(common), "failed": len(diffs), "ok": not diffs, "examples": diffs[:5], "note": note,
                       "tolerance_azn_mln": tol})
    # 3. growth contributions reconcile to total growth
    tot = eng.get("cba.loans.sector.real_total.yoy")
    parts = [eng.get(f"cba.loans.sector.{k}.contrib") for k in ["households", "trade_services", "agriculture", "construction", "industry", "transport_comm", "mining_energy", "other_all", "overdue_unclassified"]]
    if tot is not None and all(p is not None for p in parts):
        s = pd.concat([p.series for p in parts], axis=1).sum(axis=1, min_count=len(parts))
        common = s.index.intersection(tot.series.index)
        bad = [(d.isoformat(), float(s[d]), float(tot.series[d])) for d in common if abs(s[d] - tot.series[d]) > 0.05]
        checks.append({"check": "contributions_reconcile:cba.loans.sector", "n": len(common), "failed": len(bad), "ok": not bad, "examples": bad[:3]})
    dep = eng.get("cba.deposits.total.yoy")
    dparts = [eng.get(f"cba.deposits.{k}.contrib") for k in ["hh", "nfc", "fin"]]
    if dep is not None and all(p is not None for p in dparts):
        s = pd.concat([p.series for p in dparts], axis=1).sum(axis=1, min_count=3)
        common = s.index.intersection(dep.series.index)
        bad = [(d.isoformat(), float(s[d]), float(dep.series[d])) for d in common if abs(s[d] - dep.series[d]) > 0.05]
        checks.append({"check": "contributions_reconcile:cba.deposits", "n": len(common), "failed": len(bad), "ok": not bad, "examples": bad[:3]})
    # 4. NPL ratio decomposition reconciles and calculated ratio matches published ratio
    dec = eng.get("cba.bank.npl.decomp_yoy")
    if dec is not None and not dec.series.empty:
        resid = (dec.extra["numerator_effect"] + dec.extra["denominator_effect"] - dec.series).abs().dropna()
        checks.append({"check": "npl_decomposition_reconciles", "n": int(len(resid)), "failed": int((resid > 1e-6).sum()), "ok": bool((resid <= 1e-6).all())})
    pub, calc = eng.get("cba.bank.npl.ratio"), eng.get("cba.bank.npl.ratio_calc")
    pub_s = store.series("cba.bank.npl.ratio")
    if calc is not None and not pub_s.empty:
        common = pub_s.index.intersection(calc.series.index)
        bad = [(d.isoformat(), float(pub_s[d]), float(calc.series[d])) for d in common if abs(pub_s[d] - calc.series[d]) > 0.05]
        checks.append({"check": "npl_ratio_published_vs_calculated", "n": len(common), "failed": len(bad), "ok": not bad, "examples": bad[:3],
                       "note": "published QİK/Kredit portfeli vs NPL total / loan portfolio total (5.4)"})
    # 5. overdue vs NPL labels stay distinct (different series ids, units and populations)
    reg = store.registry
    ov, npl = reg.get("cba.loans.overdue"), reg.get("cba.bank.npl.total")
    checks.append({"check": "overdue_vs_npl_distinct", "ok": bool(ov and npl and ov["population"] != npl["population"] and "overdue" in (ov["label_en"] or "").lower()
                                                                and "non-performing" in (npl["label_en"] or "").lower()), "n": 2, "failed": 0})
    # 6. balance sheet identity
    a = store.series("cba.bank.assets.total", {"currency": "all"})
    l = store.series("cba.bank.liab.total", {"currency": "all"})
    k = store.series("cba.bank.capital.total", {"currency": "all"})
    common = a.index.intersection(l.index).intersection(k.index)
    bad = [(d.isoformat(), float(a[d] - l[d] - k[d])) for d in common if abs(a[d] - l[d] - k[d]) > 2.0]
    checks.append({"check": "balance_sheet_identity", "n": len(common), "failed": len(bad), "ok": not bad, "examples": bad[:3]})
    # 7. metric configuration errors
    checks.append({"check": "metric_config", "n": len(eng.cfg), "failed": len(eng.errors), "ok": not eng.errors, "examples": list(eng.errors.items())[:5]})
    # 8. freshness by dataset
    states = db.dataset_states()
    fresh = []
    today = today_baku()
    for sid, scfg, ds in config.iter_datasets():
        st = states.get(ds["id"])
        latest = st["latest_period_end"] if st else None
        lag = ds.get("expected_lag_days")
        age = (today - dt.date.fromisoformat(latest)).days if latest else None
        fresh.append({"dataset_id": ds["id"], "status": st["status"] if st else "never_checked", "latest_period_end": latest, "age_days": age,
                      "expected_lag_days": lag, "stale": bool(lag and age and age > lag + 45)})
    checks.append({"check": "freshness", "n": len(fresh), "failed": sum(1 for f in fresh if f["stale"]), "ok": True, "datasets": fresh})
    summary = {"checks": len(checks), "failed": sum(1 for c in checks if not c["ok"]), "as_of": today.isoformat()}
    report = {"summary": summary, "checks": checks}
    path = paths.state_dir / "quality_report.json"
    if write:
        paths.ensure()
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if own:
        db.close()
    return {"summary": summary, "checks": checks, "path": str(path)}
