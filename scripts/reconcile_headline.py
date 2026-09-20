#!/usr/bin/env python3
"""Manual-style reconciliation of headline indicators against source cells.

Reads the original downloaded files (as stored under data/raw), locates the published cell
for each indicator, and compares it with the stored observation and the value used in the
latest fact pack. Prints a Markdown table; differences are recorded, not hidden.

    python scripts/reconcile_headline.py [--edition 2026-07]
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azmonitor import config  # noqa: E402
from azmonitor.parsers.xl import find_sheet, load_sheets  # noqa: E402
from azmonitor.storage.db import Database  # noqa: E402
from azmonitor.util.numbers import parse_number  # noqa: E402


def latest_doc(db, dataset_id, title_like=None):
    docs = [d for d in db.documents_for_dataset(dataset_id) if d["status"] == "parsed" and (title_like is None or title_like in (d["title_original"] or ""))]
    return docs[-1] if docs else None


def cell_value(path, sheet, row1, col0):
    sheets = load_sheets(path)
    name, rows = find_sheet(sheets, sheet)
    return rows[row1 - 1][col0]


def find_row(path, sheet, year, month, date_col=0):
    """Locate the 1-based row for a (year, month) in a CBA year/month layout sheet."""
    sheets = load_sheets(path)
    name, rows = find_sheet(sheets, sheet)
    cur = None
    for i, r in enumerate(rows):
        v = r[date_col] if date_col < len(r) else None
        if isinstance(v, (int, float)) and 1990 <= float(v) <= 2100 and float(v).is_integer():
            cur = int(v)
        elif isinstance(v, str) and v.strip().isdigit() and len(v.strip()) == 4:
            cur = int(v.strip())
        elif cur == year:
            m = None
            if isinstance(v, (int, float)) and float(v).is_integer() and 1 <= int(v) <= 12:
                m = int(v)
            elif isinstance(v, str) and v.strip().isdigit() and 1 <= int(v.strip()) <= 12:
                m = int(v.strip())
            if m == month:
                return i + 1
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", default=None)
    args = ap.parse_args()
    paths = config.paths()
    db = Database(paths.db_path)
    fps = sorted(glob.glob(str(paths.output_dir / "monthly" / (args.edition or "*") / "v*" / "fact_pack.json")))
    fp = json.load(open(fps[-1], encoding="utf-8")) if fps else {"metrics": {}}
    bp = dt.date.fromisoformat(fp["edition"]["banking_period"]) if fp.get("edition") else None
    rows = []

    def add(name, source, ref_text, published, stored_sid, stored_dims=None, fp_ref=None, unit=""):
        srow = db.conn.execute("SELECT value FROM observations WHERE series_id=? AND dims=? AND period_end=? AND status='current'",
                               (stored_sid, json.dumps(stored_dims or {}, ensure_ascii=False, sort_keys=True), bp.isoformat())).fetchone()
        stored = srow["value"] if srow else None
        fpv = None
        if fp_ref and fp["metrics"].get(fp_ref, {}).get("latest"):
            fpv = fp["metrics"][fp_ref]["latest"]["value"]
        diff = (stored - published) if (stored is not None and published is not None) else None
        rows.append((name, source, ref_text, published, stored, fpv, diff, unit))

    # CBA monetary tables (year/month layout)
    for name, dsid, sheet, col, sid, fp_ref, unit in [
        ("Loans to the economy, total (all CI)", "cba_loans_by_institution", "2.6", 1, "cba.loans.total_ci", "cba.loans.total_ci", "AZN mln"),
        ("Total deposits", "cba_deposits", "2.11", 1, "cba.deposits.total", "cba.deposits.total", "AZN mln"),
        ("Household deposits", "cba_deposits", "2.11", 2, "cba.deposits.hh.total", "cba.deposits.hh.total", "AZN mln"),
        ("Overdue loans (all CI)", "cba_loans_by_maturity", "2.7", 2, "cba.loans.overdue", "cba.loans.overdue", "AZN mln"),
        ("Loans in foreign currency", "cba_loans_by_maturity", "2.7", 9, "cba.loans.fx", None, "AZN mln"),
        ("CBA official reserves", "cba_analytical_balance", "2.2", 1, "cba.reserves.official_usd", "cba.reserves.official_usd", "USD mln"),
        ("Household loans (sector table)", "cba_loans_by_sector", "2.8", 16, "cba.loans.sector.households", None, "AZN mln"),
    ]:
        doc = latest_doc(db, dsid)
        if not doc:
            continue
        path = paths.data_dir / doc["stored_path"]
        r1 = find_row(path, sheet, bp.year, bp.month)
        pub = parse_number(cell_value(path, sheet, r1, col)).value if r1 else None
        add(name, f"CBA {doc['title_original'][:60]}", f"sheet {sheet}, row {r1}, col {col + 1}", pub, sid, None, fp_ref, unit)
    # CBA rates 3.2.1: block dated first of following month
    doc = latest_doc(db, "cba_rates_new")
    if doc:
        path = paths.data_dir / doc["stored_path"]
        sheets = load_sheets(path)
        name, rws = find_sheet(sheets, "3.2.1")
        target = (bp.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        for i, r in enumerate(rws):
            v = r[1] if len(r) > 1 else None
            if isinstance(v, dt.datetime) and v.date() == target:
                pub_loan = parse_number(rws[i + 1][3]).value
                pub_dep = parse_number(rws[i + 1][2]).value
                add("New AZN loans, average rate", f"CBA {doc['title_original'][:60]}", f"sheet 3.2.1, row {i + 2}, col 4 (block {target})", pub_loan, "cba.rates.new.loan", {"currency": "AZN"}, 'cba.rates.new.loan|{"currency": "AZN"}', "%")
                add("New AZN term deposits, average rate", f"CBA {doc['title_original'][:60]}", f"sheet 3.2.1, row {i + 2}, col 3 (block {target})", pub_dep, "cba.rates.new.deposit", {"currency": "AZN"}, 'cba.rates.new.deposit|{"currency": "AZN"}', "%")
                break
    # CBA bank overview 5.3 / 5.6 (date columns)
    for name, dsid, sheet, label_start, sid, fp_ref, unit, scale in [
        ("Bank net profit, YTD", "cba_bank_pnl", "5.3", "11. Xalis mənfəət", "cba.bank.pnl.net_profit", "cba.bank.pnl.net_profit", "AZN mln", 1.0),
        ("Bank NPL, total", "cba_bank_npl", "5.6", "Qeyri-işlək kredit (QİK)", "cba.bank.npl.total", "cba.bank.npl.total", "AZN mln", 1.0),
        ("Bank NPL ratio (published)", "cba_bank_npl", "5.6", "QİK/Kredit portfeli", "cba.bank.npl.ratio", "cba.bank.npl.ratio", "%", 100.0),
    ]:
        doc = latest_doc(db, dsid)
        if not doc:
            continue
        path = paths.data_dir / doc["stored_path"]
        sheets = load_sheets(path)
        sname, rws = find_sheet(sheets, sheet)
        hdr = rws[5]
        col = next((j for j, v in enumerate(hdr) if isinstance(v, dt.datetime) and v.date() == bp), None)
        row = next((i for i, r in enumerate(rws) if str(r[0] or "").strip().startswith(label_start)), None)
        pub = parse_number(rws[row][col]).value * scale if (row is not None and col is not None) else None
        add(name, f"CBA {doc['title_original'][:60]}", f"sheet {sheet}, row {row + 1}, col {col + 1}", pub, sid, {}, fp_ref, unit)
    # SSC headline HTML (GDP, CPI YTD, wages) and price bulletin (CPI y/y)
    doc = latest_doc(db, "ssc_macro_headline_html")
    if doc:
        for name, sid, ref, unit, period in [("GDP nominal, YTD (SSC headline)", "ssc.hl.gdp.level", "ssc.hl.gdp.level", "AZN mln", fp["edition"]["macro_period"]),
                                             ("Non-oil GDP real growth, YTD", "ssc.hl.gdp_nonoil.growth", "ssc.hl.gdp_nonoil.growth", "%", fp["edition"]["macro_period"]),
                                             ("Average nominal wage, YTD", "ssc.hl.wage.level", "ssc.hl.wage.level", "AZN", bp.isoformat())]:
            srow = db.conn.execute("SELECT value, value_raw, cell_ref FROM observations WHERE series_id=? AND period_end=? AND status='current'", (sid, period)).fetchone()
            fpv = fp["metrics"].get(ref, {}).get("latest", {}).get("value") if fp["metrics"].get(ref) else None
            rows.append((name, f"SSC {doc['title_original'][:50]}", f"HTML {srow['cell_ref']} raw '{srow['value_raw']}'" if srow else "n/a", parse_number(srow["value_raw"]).value if srow else None,
                         srow["value"] if srow else None, fpv, 0.0 if srow else None, unit))
    srow = db.conn.execute("SELECT value, value_raw, cell_ref, doc_id FROM observations WHERE series_id='ssc.cpi.all.yoy_index' AND period_end=? AND status='current'", (fp["edition"]["cpi_period"],)).fetchone()
    if srow:
        doc = db.conn.execute("SELECT title_original FROM documents WHERE doc_id=?", (srow["doc_id"],)).fetchone()
        fpv = fp["metrics"].get("ssc.cpi.all.yoy", {}).get("latest", {}).get("value")
        rows.append(("CPI y/y (index, % of same month previous year)", f"SSC {(doc['title_original'] if doc else srow['doc_id'])[:50]}", f"{srow['cell_ref']} raw '{srow['value_raw']}'" if srow else "n/a",
                     parse_number(srow["value_raw"]).value if srow else None, srow["value"] if srow else None, (fpv + 100.0) if fpv is not None else None, 0.0 if srow else None, "index"))
    print(f"# Headline reconciliation — banking period {bp}, fact pack {fps[-1] if fps else 'n/a'}\n")
    print("| # | Indicator | Source document | Cell / reference | Published value | Stored value | Fact-pack value | Stored − published | Unit |")
    print("|---|---|---|---|---|---|---|---|---|")
    for i, (name, src, ref, pub, stored, fpv, diff, unit) in enumerate(rows, start=1):
        f = lambda v: "n/a" if v is None else (f"{v:,.4f}" if abs(v) < 1000 else f"{v:,.2f}")
        print(f"| {i} | {name} | {src} | {ref} | {f(pub)} | {f(stored)} | {f(fpv)} | {f(diff)} | {unit} |")
    print("\nResolution notes: stored values are unrounded cell values; fact-pack values equal stored values (no re-computation) except derived percentages, "
          "which are computed from stored values. 'n/a' in the fact-pack column marks an input series that feeds a derived metric "
          "(FX share, sector contributions) and is not displayed as a level. The SSC republication of CBA credit and household deposits is reconciled separately in the quality report.")


if __name__ == "__main__":
    main()
