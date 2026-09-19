"""Weekly release digest: 4–6 slides built only from evidence published since the last digest."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .. import config
from ..facts import FactPackBuilder
from ..narrative.fmt import plabel
from ..storage.db import Database, utcnow
from ..util.log import get_logger
from ..util.periods import parse_as_of
from .builder import Deck, align_series, month_labels
from .monthly import _chg, _fmt, _kpi, _per
from .pdf import convert_to_pdf, previews, soffice_available

log = get_logger("weekly")

MACRO_REFS = [("ssc.hl.gdp.growth", "GDP real growth, YTD"), ("ssc.hl.gdp_nonoil.growth", "Non-oil GDP, YTD"), ("ssc.cpi.all.yoy", "CPI y/y"), ("ssc.hl.wage.growth", "Nominal wage, YTD y/y"),
              ("ssc.hl.exports.level", "Exports, YTD"), ("ssc.hl.imports.level", "Imports, YTD"), ("ssc.hl.retail.growth", "Retail turnover, YTD y/y"), ("ssc.hl.investment.growth", "Investment, YTD y/y")]
BANK_REFS = [("cba.loans.total_ci", "Loans to the economy"), ("cba.loans.total_ci.yoy", "Loans y/y"), ("cba.deposits.total", "Total deposits"), ("cba.deposits.total.yoy", "Deposits y/y"),
             ("cba.deposits.fx_share", "Deposit FX share"), ("cba.bank.npl.ratio", "NPL ratio (banks)"), ("cba.bank.pnl.net_profit", "Net profit, YTD"), ("cba.bank.equity_to_assets", "Capital / assets")]


def publications_slide(d, C, fp: dict[str, Any], db: Database, since_iso: str, src_line: str, lang: str) -> None:
    """Newly released policy and stability publications, and what each one is about.

    A publication counts as new when it was first seen in this window. A second language edition of
    a publication already reported, or a re-download of an unchanged file, is not a new release and
    does not appear here twice.
    """
    from ..publications.factpack import PublicationFacts

    pf = PublicationFacts(db, dt.date.fromisoformat(fp["as_of"]), lang)
    fresh = pf.new_since(since_iso + "T00:00:00+00:00")
    s = d.new_slide()
    d.add_title(s, f"Policy and stability publications released since {since_iso}",
                "New publications",
                "Reporting period and release date are shown separately; a translation is not a second release")
    if fresh:
        rows = [[p["type"], p.get("edition") or "", p.get("reporting_period_end") or "not stated",
                 p.get("published_at") or "unknown", ", ".join(p.get("languages") or []),
                 str(len(p.get("documents") or []))] for p in fresh[:10]]
        d.add_table(s, 0.45, 1.45, 12.35, 2.6,
                    ["Publication", "Edition", "Reporting period end", "Published", "Languages", "Files"],
                    rows, col_widths=[2.8, 2.2, 2.4, 2.0, 1.6, 1.35], font_size=8.5)
    else:
        d.add_text(s, 0.45, 1.5, 12.35, 0.6,
                   "No policy review, policy statement, stability report or rate decision was released in this window. "
                   "The previous editions remain current and are listed in the monthly appendix.",
                   size=10.5, color=C["text"])
    pol = pf.policy_block()
    decision = pol.get("decision") or {}
    rows2 = [["Latest rate decision", decision.get("announcement_date") or "n/a",
              f"{decision.get('policy_rate')}% (corridor {decision.get('corridor_floor')}%–{decision.get('corridor_ceiling')}%)",
              pol["stance"].get("label") or ""],
             ["Next rate decision", (pol.get("next_decision") or {}).get("date") or "not announced", "",
              (pol.get("next_decision") or {}).get("basis") or ""]]
    stab_report = (pf.stability_block().get("report") or {})
    rows2.append(["Latest stability report", stab_report.get("published_at") or "n/a",
                  f"reporting period {stab_report.get('reporting_period_end') or 'n/a'}", stab_report.get("edition") or ""])
    d.add_text(s, 0.45, 4.3, 12.35, 0.25, "Standing position after this window", size=9, bold=True, color=C["muted"])
    d.add_table(s, 0.45, 4.57, 12.35, 1.5, ["Item", "Date", "Detail", "Note"], rows2,
                col_widths=[2.8, 2.0, 4.2, 3.35], font_size=8.5)
    d.add_footer(s, src_line, d.page)
    d.add_notes(s, "A brief is produced separately for each new policy review, stability report and rate decision "
                   "(`monitor report --type mpr-brief|fsr-brief|decision-update`). Publications first seen during a "
                   "historical backfill are not treated as new releases for briefing.")


def generate_weekly(as_of: str | None, since: str | None, lang: str, force: bool = False, db: Database | None = None) -> dict[str, Any]:
    paths = config.paths()
    paths.ensure()
    own = db is None
    db = db or Database(paths.db_path)
    as_of_d = parse_as_of(as_of)
    if since:
        since_iso = dt.date.fromisoformat(since).isoformat()
    else:
        eds = [e for e in db.editions("weekly") if e["status"] == "generated"]
        since_iso = eds[-1]["generated_at"][:10] if eds else (as_of_d - dt.timedelta(days=7)).isoformat()
    vint = [dict(v) for v in db.vintages_since(since_iso)]
    new_docs = [dict(d) for d in db.all_documents() if (d["first_seen_at"] or "") >= since_iso and d["status"] in ("parsed", "stored")]
    if not vint and not force:
        status = {"status": "no_update", "since": since_iso, "as_of": as_of_d.isoformat(), "at": utcnow(), "note": "no new observations or revisions since the last digest; previous deck remains current"}
        (paths.state_dir / "weekly_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        if own:
            db.close()
        return status
    b = FactPackBuilder(db, as_of_d, lang=lang)
    fp = b.build()
    fp["report_type"] = "weekly"
    changed_datasets = sorted({v["dataset_id"] for v in vint})
    theme = dict(config.theme())
    theme["_root"] = str(config.ROOT)
    d = Deck(theme, lang)
    C = theme["colors"]
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    edition = as_of_d.isoformat()
    version = len([e for e in db.editions("weekly") if e["edition_period"] == edition]) + 1
    out = paths.output_dir / "weekly" / edition / f"v{version}_{ts}"
    out.mkdir(parents=True, exist_ok=True)
    src_line = f"Source: CBA and SSC documents first seen after {since_iso}; retrieved {fp['generated_at'][:10]}; cutoff {fp['as_of']} (Asia/Baku)."

    # W01 cover + new releases
    s = d.new_slide()
    d.add_title(s, f"Weekly release digest: {len(new_docs)} new official documents and {sum(v['n_new_periods'] for v in vint)} new observations since {since_iso}",
                config.term("weekly_digest", lang), f"As of {as_of_d} · {config.term('draft_label', lang)} · datasets changed: {', '.join(changed_datasets)[:110]}")
    rows = []
    for doc in sorted(new_docs, key=lambda x: x["first_seen_at"])[-14:]:
        vs = [v for v in vint if v["doc_id"] == doc["doc_id"]]
        rows.append([doc["dataset_id"], (doc["title_original"] or "")[:60], doc["source_id"], doc.get("published_at") or "n/a", doc["retrieved_at"][:10],
                     str(sum(v["n_new_periods"] for v in vs)), str(sum(v["n_revisions"] for v in vs))])
    d.add_table(s, 0.45, 1.45, 12.35, 4.0, ["Dataset", "Document", "Source", "Published", "Retrieved", "New obs", "Revised"], rows, col_widths=[2.6, 4.8, 1.2, 1.1, 1.1, 0.8, 0.8], font_size=8, align=["l", "l", "l", "l", "l", "r", "r"])
    kp = []
    for ref, lbl in MACRO_REFS[:2] + BANK_REFS[:2]:
        m = fp["metrics"].get(ref)
        if m and m.get("latest"):
            kp.append(_kpi(m, lbl))
    for i, (val, lbl, sub) in enumerate(kp[:4]):
        d.add_kpi(s, 0.45 + i * 3.12, 5.6, 3.0, 1.05, val, lbl, sub)
    d.add_footer(s, src_line, d.page)
    d.add_notes(s, "New releases are documents whose content hash was first seen after the digest start date. A re-downloaded unchanged file is not a new release.\n" + "\n".join(f"- {x['doc_id']}: {x['document_url']}" for x in new_docs[:40]))

    def evidence_slide(title: str, section: str, refs: list[tuple[str, str]], chart_ids: list[tuple[str, dict | None, str]]):
        s = d.new_slide()
        d.add_title(s, title, section, f"Values with their own reference periods · comparison basis shown per tile · cutoff {fp['as_of']}")
        tiles = []
        for ref, lbl in refs:
            m = fp["metrics"].get(ref)
            if m and m.get("latest"):
                tiles.append(_kpi(m, lbl))
        for i, (val, lbl, sub) in enumerate(tiles[:8]):
            col, row = i % 4, i // 4
            d.add_kpi(s, 0.45 + col * 3.12, 1.45 + row * 1.15, 3.0, 1.05, val, lbl, sub)
        y = 1.45 + 2 * 1.15 + 0.15
        cw = (12.35 - 0.2) / max(1, len(chart_ids))
        for j, (sid, dims, lbl) in enumerate(chart_ids):
            cs = b.chart_series(sid, dims, window=24, label=lbl)
            if not cs["points"]:
                continue
            cats = month_labels([p[0] for p in cs["points"]], lang)
            d.add_text(s, 0.45 + j * (cw + 0.2), y, cw, 0.22, f"{lbl} ({cs.get('unit') or ''})", size=9, bold=True, color=C["muted"])
            d.add_line_chart(s, 0.45 + j * (cw + 0.2), y + 0.22, cw, 6.7 - y - 0.22, cats, [{"name": lbl, "values": [p[1] for p in cs["points"]], "color": C["primary"]}], number_format="0.0", legend=False)
        d.add_footer(s, src_line, d.page)
        d.add_notes(s, "Metrics shown are the standard monitor metrics (see monthly appendix A01) restricted to series updated in this window where possible.")

    publications_slide(d, C, fp, db, since_iso, src_line, lang)
    evidence_slide("New macroeconomic evidence", "Macro", MACRO_REFS, [("ssc.hl.gdp_nonoil.growth", None, "Non-oil GDP, real YTD y/y"), ("ssc.cpi.all.yoy", None, "CPI y/y")])
    evidence_slide("New banking and funding evidence", "Banking", BANK_REFS, [("cba.loans.total_ci.yoy", None, "Loans y/y"), ("cba.deposits.total.yoy", None, "Deposits y/y")])
    # W04 material development from monitoring flags
    trig = [f for f in fp.get("flags", []) if f.get("triggered")]
    if trig:
        s = d.new_slide()
        f0 = trig[0]
        d.add_title(s, f"Monitoring flag: {f0['metric']} moved {f0['move']:+.1f} pp over {f0['window']} observations (threshold {f0['threshold_pp']} pp)", "Material development",
                    "Configured flag = prompt for a source check, not a risk score or forecast")
        cs = b.chart_series(f0["metric"], None, window=36)
        cats = month_labels([p[0] for p in cs["points"]], lang)
        d.add_line_chart(s, 0.45, 1.5, 8.0, 5.1, cats, [{"name": cs["label"], "values": [p[1] for p in cs["points"]], "color": C["primary"]}], number_format="0.0", legend=False)
        rows = [[f["id"], f["metric"], f"{f['current']:.2f}", f"{f['comparison']:.2f}", f"{f['move']:+.2f}", str(f["threshold_pp"]), f["direction"], "yes" if f["triggered"] else "no"] for f in fp["flags"] if f.get("metric")]
        d.add_table(s, 8.7, 1.5, 4.2, 2.6, ["Flag", "Metric", "Now", "Then", "Move", "Thr.", "Dir.", "Hit"], rows, col_widths=[1.3, 2.2, 0.7, 0.7, 0.7, 0.5, 0.5, 0.4], font_size=7)
        d.add_so_what(s, 8.7, 4.4, 4.2, 2.2, "Check the source table for revisions or reclassification before interpreting; compare with internal segment data; consider seasonality and structural breaks in the comparison window.", heading="NEXT STEP")
        d.add_footer(s, src_line, d.page)
    # W05 questions + upcoming
    s = d.new_slide()
    d.add_title(s, "Questions for management and upcoming publications", "Questions", "Expected dates from the official schedule (within 30 days after the reporting period); not confirmed publications")
    nxt = fp["slides"]["M18"]["next_releases"][:8]
    rows = [[r["title"][:70], r["source"], plabel(r["next_period"], "month_end_stock"), r["expected_by"]] for r in nxt]
    d.add_table(s, 0.45, 1.45, 8.0, 3.2, ["Dataset", "Source", "Next period", "Expected by"], rows, col_widths=[4.6, 1.0, 1.2, 1.2], font_size=8.5, align=["l", "l", "l", "l"])
    qs = ["Do the newly published banking aggregates change the picture used in the last monthly monitor (loans, deposits, FX share)?",
          "Which of the changed datasets carry revisions to earlier months, and do they affect previously discussed findings?",
          "Are there sector or pricing moves that justify an interim review before the next monthly edition?"]
    d.add_text(s, 8.7, 1.45, 4.2, 3.2, [[{"text": q, "size": 10}] for q in qs], bullets=True, space_after=6)
    d.add_footer(s, src_line, d.page)
    if len(new_docs) > 14:
        s = d.new_slide()
        d.add_title(s, "Sources and caveats", "Sources", "All documents first seen in the window")
        rows = [[x["dataset_id"], (x["title_original"] or "")[:70], x["document_url"][:70], x.get("published_at") or "n/a"] for x in new_docs[:30]]
        d.add_table(s, 0.45, 1.45, 12.35, 5.2, ["Dataset", "Title", "URL", "Published"], rows, col_widths=[2.4, 4.5, 4.5, 1.0], font_size=7, align=["l", "l", "l", "l"])
        d.add_footer(s, src_line, d.page)
    stem = f"AZ_Weekly_Digest_{edition}_v{version}_{ts}"
    pptx_path = d.save(out / f"{stem}.pptx")
    pdf_info: dict[str, Any] = {"status": "skipped"}
    if soffice_available():
        try:
            pdf = convert_to_pdf(pptx_path, out)
            pdf_info = {"status": "ok", "path": str(pdf)}
            previews(pdf, out / "previews")
        except Exception as exc:
            pdf_info = {"status": "failed", "reason": str(exc)}
    manifest = {"report_type": "weekly", "edition": edition, "version": version, "since": since_iso, "generated_at": utcnow(), "as_of": as_of_d.isoformat(), "new_documents": [x["doc_id"] for x in new_docs],
                "changed_datasets": changed_datasets, "files": {"pptx": str(pptx_path), "pdf": pdf_info}, "fact_pack_hash": fp.get("fact_pack_hash"), "n_slides": d.page}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (out / "fact_pack.json").write_text(json.dumps(fp, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    db.add_edition({"edition_id": f"weekly:{edition}:v{version}", "report_type": "weekly", "edition_period": edition, "version": version, "generated_at": manifest["generated_at"], "as_of": as_of_d.isoformat(),
                    "snapshot_id": None, "status": "generated", "path": str(out), "manifest_path": str(out / "manifest.json"), "anchors": json.dumps({"since": since_iso})})
    (paths.state_dir / "weekly_status.json").write_text(json.dumps({"status": "generated", "path": str(out), "at": manifest["generated_at"], "since": since_iso}, indent=2), encoding="utf-8")
    if own:
        db.close()
    return {"status": "generated", "path": str(out), "pptx": str(pptx_path), "pdf": pdf_info, "n_slides": d.page, "since": since_iso, "new_documents": len(new_docs), "changed_datasets": changed_datasets}
