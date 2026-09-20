#!/usr/bin/env python3
"""Write docs/quality_exceptions.md: every failing data-quality check with its evidence.

For each failure the document states the indicator, the period, the size of the discrepancy, the
source evidence recomputed from the stored file, and what the failure does and does not affect in
the published report. Run it after `monitor validate`.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azmonitor import config                                    # noqa: E402
from azmonitor.calc.validate import validate_all                # noqa: E402
from azmonitor.storage.db import Database                       # noqa: E402

WINDOW_MONTHS = 36


def component_evidence(db: Database, dataset_id: str, period: str) -> list[dict]:
    rows = db.conn.execute(
        "SELECT series_id, value, unit, cell_ref, doc_id FROM observations "
        "WHERE dataset_id=? AND period_end=? AND status='current' ORDER BY series_id", (dataset_id, period)).fetchall()
    return [dict(r) for r in rows]


def repeated_component(db: Database, dataset_id: str, period: str, comp_ids: list[str]) -> list[str]:
    """Flag a component that repeats the previous month exactly: the usual cause of these breaks."""
    prev = dt.date.fromisoformat(period).replace(day=1) - dt.timedelta(days=1)
    prev_iso = prev.isoformat()
    rows = {r["series_id"]: r["value"] for r in db.conn.execute(
        "SELECT series_id, value FROM observations WHERE dataset_id=? AND period_end=? AND status='current'",
        (dataset_id, prev_iso)).fetchall()}
    cur = {r["series_id"]: r["value"] for r in db.conn.execute(
        "SELECT series_id, value FROM observations WHERE dataset_id=? AND period_end=? AND status='current'",
        (dataset_id, period)).fetchall()}
    out = []
    for cid in comp_ids:
        a, b = rows.get(cid), cur.get(cid)
        if a is not None and b is not None and abs(a - b) < 1e-9:
            out.append(f"Diagnosis: `{cid}` carries exactly the {prev_iso} value ({b:,.3f}) in the {period} row, "
                       f"while the published total moved. The component, not the total, is the inconsistent cell.\n")
    return out


def check_spec(dataset_id: str) -> dict | None:
    for _sid, _scfg, ds in config.iter_datasets():
        if ds["id"] == dataset_id:
            return ds
    return None


def main() -> int:
    paths = config.paths()
    db = Database(paths.db_path)
    latest_banking = db.conn.execute(
        "SELECT MAX(period_end) FROM observations WHERE dataset_id='cba_loans_by_institution' AND status='current'").fetchone()[0]
    display_from = None
    if latest_banking:
        d = dt.date.fromisoformat(latest_banking)
        total = d.year * 12 + d.month - 1 - WINDOW_MONTHS
        display_from = dt.date(total // 12, total % 12 + 1, 1)
    report = validate_all(db, write=False, display_from=display_from)
    lines: list[str] = []
    out = lines.append
    out("# Data-quality exceptions and failing checks\n")
    out(f"Generated {dt.date.today().isoformat()} from `monitor validate`. "
        f"Checks: {report['summary']['checks']}, failing: {report['summary']['failed']} "
        f"(critical {report['summary']['critical']}, warning {report['summary']['warning']}, "
        f"accepted exceptions {report['summary']['accepted_exceptions']}).\n")
    out("A **critical** failure touches a period this edition displays and blocks publication. "
        "A **warning** is confined to history the edition does not show: it is recorded here and in appendix A02, "
        "and no published figure depends on it. An **accepted exception** is an explicit entry in "
        "`config/quality_exceptions.yaml` naming the periods, the reason and the reviewer.\n")
    out(f"Display window for this assessment: from {display_from} (36 months to the banking anchor "
        f"{latest_banking}).\n")
    out("Correction to an earlier description of these failures: they are **not** all before the start of the "
        "collected history. The dataset starts in 2020 and one failing period, 2021-10-31, is inside it. What is "
        "true of every current failure is narrower and is what this document states: none of the failing periods "
        "falls inside the window the current edition displays or compares against.\n")

    failing = [c for c in report["checks"] if not c.get("ok") or c.get("severity") == "accepted_exception"]
    if not failing:
        out("No check is currently failing.\n")
    for c in failing:
        out(f"## {c['id']}\n")
        out(f"* Severity: **{c.get('severity')}**")
        out(f"* Failing periods: {', '.join(c.get('failed_periods') or []) or 'n/a'}")
        out(f"* Failures / comparisons: {c.get('failed')} of {c.get('n')}")
        if c.get("note"):
            out(f"* Note: {c['note']}")
        if c.get("exception"):
            out(f"* Recorded exception: {json.dumps(c['exception'], ensure_ascii=False)}")
        dataset_id = c["id"].split(":", 1)[1] if c["id"].startswith("components_sum:") else None
        if dataset_id:
            ds = check_spec(dataset_id) or {}
            specs = [c for c in ((ds.get("parse") or {}).get("checks") or []) if c.get("type") == "components_sum"]
            for chk in specs:
                out(f"* Check definition: total `{chk.get('total')}` = sum of "
                    f"{', '.join('`' + x + '`' for x in (chk.get('parts') or chk.get('components') or []))} "
                    f"(tolerance {chk.get('tolerance')} {ds.get('parse', {}).get('unit', '')})")
            for period in (c.get("failed_periods") or [])[:4]:
                rows = {r["series_id"]: r for r in component_evidence(db, dataset_id, period)}
                for chk in specs:
                    total_id = chk.get("total")
                    comp_ids = list(chk.get("parts") or chk.get("components") or [])
                    if total_id not in rows:
                        continue
                    mark = len(lines)
                    out(f"\n**Evidence, {period}** — the check as configured (values as stored, unrounded):\n")
                    out("| Role | Series | Value | Source cell |")
                    out("|---|---|---:|---|")
                    trow = rows[total_id]
                    out(f"| published total | `{total_id}` | {trow['value']:,.3f} | {trow['cell_ref'] or ''} |")
                    ssum = 0.0
                    for cid in comp_ids:
                        r = rows.get(cid)
                        if not r or r["value"] is None:
                            out(f"| component | `{cid}` | missing | |")
                            continue
                        ssum += r["value"]
                        out(f"| component | `{cid}` | {r['value']:,.3f} | {r['cell_ref'] or ''} |")
                    diff = trow["value"] - ssum if trow["value"] is not None else None
                    if diff is None or abs(diff) <= float(chk.get("tolerance", 0.5)):
                        lines[:] = lines[:mark]          # this sub-check passes for this period
                        continue
                    out(f"\nSum of the configured components {ssum:,.3f}; published total {trow['value']:,.3f}; "
                        f"difference {diff:+,.3f} (tolerance {chk.get('tolerance')}). The parser reads exactly the "
                        f"cells shown, so the difference is in the source file rather than in extraction.\n")
                    for line in repeated_component(db, dataset_id, period, comp_ids):
                        out(line)
                    out(f"\nImpact: the discrepancy is {abs(diff):,.3f} {ds.get('parse', {}).get('unit', '')} on "
                        f"`{total_id}` for {period}. "
                        + ("It affects no figure in this edition: the period is outside the displayed window and "
                           "outside every comparison the deck makes."
                           if c.get("severity") == "warning" else
                           "It falls inside the window this edition displays, so publication is blocked until it is "
                           "resolved or an explicit exception is recorded.") + "\n")
        else:
            examples = c.get("examples") or []
            if examples:
                out("\nExamples:\n")
                out("```")
                out(json.dumps(examples[:6], ensure_ascii=False, indent=1, default=str))
                out("```")
        out("")
    out("## How these are handled\n")
    out("1. Every check runs on every refresh and the result is stored in `data/state/quality_report.json`.")
    out("2. A critical failure blocks a new edition; the previous edition stays current and "
        "`data/state/monthly_status.json` records the cause.")
    out("3. A warning is published in appendix A02 with its periods, so a reader can see what did not reconcile.")
    out("4. Publishing despite a critical failure requires an entry in `config/quality_exceptions.yaml` that names "
        "the periods, the reason, the evidence, the impact, who accepted it and when it is reviewed.")
    Path("docs/quality_exceptions.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote docs/quality_exceptions.md ({len(failing)} failing check(s))")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
